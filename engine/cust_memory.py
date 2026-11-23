# -*- coding: utf-8 -*-
"""
engine/cust_memory.py — المحطة 2: هندسة الذاكرة طويلة المدى للعميل (Memory Engineering).

جدول SQLite في نفس قاعدة بيانات المستأجر (tickets.db) — مفتاحه معرّف العميل
(جلسة الويب أو رقم التليجرام/واتساب) وقيمته ملف JSON يتضمن:
1. history: آخر 5 تبادلات مختصرة.
2. facts: حقائق وتفضيلات دائمة مهيكلة للعميل (Domain-Agnostic: الاسم، الهاتف، الفرع المفضل،
   رقم الطلب، الباقة، الحساسيات/الأمراض المزمنة في العيادات/الصيدليات، التفضيلات).
   - استخراج فوري حتمي (0ms) للحقائق الصريحة.
   - استخراج خلفي اختياري عبر LLM (Async) عند وجود مؤشرات شخصية دون أي زيادة في زمن الرد.

الخصوصية: البيانات داخل مستأجر واحد فقط، ودالة purge() تحذف ملف عميل
عند طلبه (حق النسيان — مربوطة بـ DELETE /api/memory/<customer_key>).
"""
import json
import os
import re
import sqlite3
import threading
from datetime import datetime

from engine import store

_LOCK = threading.Lock()
_MAX_FACTS = 15

_PERSONAL_CUES_RE = re.compile(
    r'(?:اسمي|أنا\s+اسمي|انا\s+اسمي|رقمي|تليفوني|موبايلي|عنواني|شركتي|فرعي|'
    r'بفضل|مفضل|عندي\s+(?:حساسية|حساسيه|سكر|سكري|ضغط|ربو|مرض|شركة|مطعم|عيادة|صيدلية)|'
    r'حساسية\s+من|حساسيه\s+من|مصاب\s+ب|allergy|allergic|my name|prefer|phone)',
    re.IGNORECASE,
)


def _connect():
    """نفس قاعدة بيانات التذاكر للمستأجر — جدول منفصل customer_memory."""
    conn = sqlite3.connect(store.get_tickets_file(), timeout=15)
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS customer_memory (
        customer_key TEXT PRIMARY KEY,
        profile      TEXT NOT NULL)''')


def extract_facts_deterministic(text):
    """استخراج حتمي فوري (0ms) للحقائق الصريحة من رسالة العميل (عام لأي نشاط تجاري)."""
    if not text:
        return {}
    facts = {}
    t = str(text).strip()
    _CLAUSE_STOP = r'(?=$|[،,\.\n؟\?]|\s+و(?:رقمي|تليفوني|موبايلي|عندي|بروح|طلب|عايز|عاوز|انا|أنا|عنواني|فرع|حساس|بفضل|مفضل|رقمه|اسمي)\b|\s+و\s+)'

    # 1. الاسم الصريح (يتوقف قبل أدوات العطف مثل "ورقمي" / "وعندي")
    m_name = re.search(
        r'(?:اسمي|أنا\s+اسمي|انا\s+اسمي|my name is)\s+'
        r'([\u0621-\u064Aa-zA-Z]{2,20}(?:\s+(?!و(?:رقمي|تليفوني|موبايلي|عندي|بروح|طلب|عايز|عاوز|انا|أنا|عنواني|فرع|حساس))[\u0621-\u064Aa-zA-Z]{2,20})?)',
        t,
        re.IGNORECASE,
    )
    if m_name:
        facts['name'] = m_name.group(1).strip()[:50]

    # 2. رقم الهاتف (مصري/دولي)
    m_phone = re.search(r'(?:\+?\d{1,3}[\s\-]?)?(01[0125]\d{8}|05\d{8})', t)
    if m_phone:
        facts['phone'] = m_phone.group(0).strip()[:25]

    # 3. رقم الطلب / الفاتورة
    m_order = re.search(r'(?:طلب|اوردر|أوردر|فاتورة|order)\s*(?:رقم|#)?\s*#?(\d{3,10})', t, re.IGNORECASE)
    if m_order:
        facts['last_order_id'] = m_order.group(1)

    # 4. الفرع المفضل
    m_branch = re.search(
        r'(?:فرع|بروح\s+فرع|قريب\s+من\s+فرع)\s+'
        r'([\u0621-\u064A]{3,20}(?:\s+(?!و(?:رقمي|تليفوني|موبايلي|عندي|بروح|طلب|عايز|عاوز|انا|أنا|عنواني))[\u0621-\u064A]{3,15})?)',
        t,
    )
    if m_branch:
        b = m_branch.group(1).strip()
        if b not in ('الفرع', 'الشركة', 'المتجر', 'ايه', 'فين', 'كام'):
            facts['preferred_branch'] = b[:40]

    # 5. الحساسية / الحالات الصحية (لقطاع العيادات والصيدليات — عامة بلا hardcoding لمجال)
    m_allergy = re.search(
        r'(?:حساسية|حساسيه|allergic\s+to)\s+(?:من\s+)?([\u0621-\u064Aa-zA-Z0-9\s\-]{2,40}?)' + _CLAUSE_STOP,
        t,
        re.IGNORECASE,
    )
    if m_allergy:
        facts['allergy'] = m_allergy.group(1).strip()[:60]

    chronic = re.findall(r'\b(السكري|سكري|سكر|الضغط|ضغط\s*الدم|الربو|ربو|القلب|الكلى|حامل|حمل)\b', t)
    if chronic:
        facts['health_notes'] = '، '.join(dict.fromkeys(chronic))[:80]

    # 6. التفضيلات العامة ("بفضل ..." / "دايماً بطلب ...")
    m_pref = re.search(
        r'(?:بفضل|أفضل|دايماً\s+بطلب|دائماً\s+أطلب)\s+([\u0621-\u064Aa-zA-Z0-9\s\-]{3,45}?)' + _CLAUSE_STOP,
        t,
    )
    if m_pref:
        facts['preference'] = m_pref.group(1).strip()[:60]

    return facts


def _merge_facts(existing, new_facts):
    merged = dict(existing or {})
    for k, v in (new_facts or {}).items():
        ks = str(k).strip()[:32]
        vs = str(v).strip()[:100]
        if ks and vs and vs.lower() not in ('null', 'none', 'غير محدد', 'لا يوجد'):
            merged[ks] = vs
    if len(merged) > _MAX_FACTS:
        for k in list(merged.keys())[:-_MAX_FACTS]:
            merged.pop(k, None)
    return merged


def _save_facts(customer_key, new_facts):
    if not customer_key or not new_facts:
        return
    key = str(customer_key)[:120]
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                row = conn.execute('SELECT profile FROM customer_memory WHERE customer_key = ?', (key,)).fetchone()
                prof = json.loads(row[0]) if row else {}
                prof['facts'] = _merge_facts(prof.get('facts'), new_facts)
                conn.execute('INSERT OR REPLACE INTO customer_memory (customer_key, profile) VALUES (?, ?)',
                             (key, json.dumps(prof, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass


def _extract_facts_llm_async(customer_key, query):
    """استخراج خلفي بالـ LLM للحقائق الشخصية الدائمة (JSON صارم) دون تعطيل الرد."""
    if os.environ.get('ENABLE_MEMORY_FACTS', 'true').strip().lower() not in ('true', '1', 'yes'):
        return
    if not query or len(query) < 20 or not _PERSONAL_CUES_RE.search(query):
        return

    def _worker():
        try:
            from engine import providers
            prompt = (
                'استخرج فقط الحقائق الدائمة المذكورة صراحة عن العميل في الرسالة التالية '
                '(مثل: الاسم، الهاتف، المدينة، الفرع المفضل، التفضيل، الحساسية أو الحالة الصحية). '
                'لا تخترع أي معلومة. أعد JSON فقط بالشكل {"facts": {"key": "value"}} أو {"facts": {}}:\n'
                f'<user_message>{query[:400]}</user_message>'
            )
            res = providers.complete(prompt, tier='fast', temperature=0.0, max_tokens=160, json_mode=True, timeout=20.0)
            raw = res.get('text', '')
            m = re.search(r'\{.*\}', raw, re.DOTALL)
            if not m:
                return
            data = json.loads(m.group(0))
            facts = data.get('facts') if isinstance(data, dict) else None
            if isinstance(facts, dict) and facts:
                _save_facts(customer_key, facts)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()


def remember(customer_key, query, answer, enable_llm_extract=False):
    """تحديث ملف العميل (السجل + الحقائق المهيكلة) بعد كل إجابة — لا يكسر المسار الرئيسي أبداً."""
    if not customer_key:
        return
    key = str(customer_key)[:120]
    det_facts = extract_facts_deterministic(query)
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                row = conn.execute('SELECT profile FROM customer_memory WHERE customer_key = ?',
                                   (key,)).fetchone()
                prof = json.loads(row[0]) if row else {}
                hist = prof.get('history', [])
                hist.append({'q': (query or '')[:160], 'a': (answer or '')[:160]})
                prof['history'] = hist[-5:]
                prof['facts'] = _merge_facts(prof.get('facts'), det_facts)
                prof['total_interactions'] = prof.get('total_interactions', 0) + 1
                prof['last_seen'] = datetime.now().isoformat(timespec='seconds')
                prof.setdefault('first_seen', prof['last_seen'])
                conn.execute('INSERT OR REPLACE INTO customer_memory (customer_key, profile) VALUES (?, ?)',
                             (key, json.dumps(prof, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
        if enable_llm_extract:
            _extract_facts_llm_async(key, query)
    except Exception:
        pass  # الذاكرة تحسين — فشلها لا يمس الرد


def get_profile(customer_key):
    """إرجاع ملف العميل الكامل (لواجهة الـ API أو الداشبورد)."""
    if not customer_key:
        return {}
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                row = conn.execute('SELECT profile FROM customer_memory WHERE customer_key = ?',
                                   (str(customer_key)[:120],)).fetchone()
            finally:
                conn.close()
        return json.loads(row[0]) if row else {}
    except Exception:
        return {}


def recall(customer_key, limit=2):
    """سطر سياق مختصر يدمج الحقائق الدائمة + آخر تعاملات العميل — فارغ إن لم يوجد."""
    prof = get_profile(customer_key)
    if not prof:
        return ''
    segments = []
    facts = prof.get('facts') or {}
    if facts:
        f_str = '، '.join(f'{k}: {v}' for k, v in list(facts.items())[:8])
        segments.append('حقائق محفوظة عن العميل: ' + f_str)
    hist = prof.get('history', [])[-limit:]
    if hist:
        parts = ['(%d) سأل: %s' % (i, h.get('q', '')[:90]) for i, h in enumerate(hist, 1)]
        segments.append('آخر استفساراته: ' + ' | '.join(parts))
    return '\n'.join(segments)


def purge(customer_key):
    """حق النسيان: حذف ملف عميل بالكامل."""
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                conn.execute('DELETE FROM customer_memory WHERE customer_key = ?',
                             (str(customer_key)[:120],))
                conn.commit()
            finally:
                conn.close()
        return True
    except Exception:
        return False
