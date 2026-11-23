# -*- coding: utf-8 -*-
"""
قلب النظام: Customer Message -> Sentiment + RAG -> Confidence -> Decision.

المسارات:
  غاضب                          => priority  (تذكرة أولوية)
  ثقة منخفضة جداً (< 0.25)      => escalated (موظف بشري، بدون إجابة توليدية)
  ثقة متوسطة (0.25 - 0.35)      => resolved  (إجابة متحفظة تُوضّح الشروط بدقة)
  ثقة عالية (>= 0.35)           => resolved  (إجابة كاملة)
"""
import os
import re
import sys
import threading
import time
from datetime import date, datetime, timedelta

from engine import actions, ai, audit, cust_memory, guardrails, llm, policy_extractor, premath, providers, rag, reranker, rule_evaluator, sentiment, session, store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_tenant_id():
    return os.environ.get('TENANT_ID', 'default').strip() or 'default'


def get_kb_dir(tenant=None):
    tenant = tenant or get_tenant_id()
    tenant_dir = os.path.join(BASE_DIR, 'data', 'tenants', tenant)
    new_kb_dir = os.path.join(tenant_dir, 'knowledge_base')
    os.makedirs(new_kb_dir, exist_ok=True)
    old_kb_dir = os.path.join(BASE_DIR, 'data', 'knowledge_base')
    if tenant == 'default' and os.path.exists(old_kb_dir) and os.path.isdir(old_kb_dir) and old_kb_dir != new_kb_dir:
        for item in os.listdir(old_kb_dir):
            s = os.path.join(old_kb_dir, item)
            d = os.path.join(new_kb_dir, item)
            if not os.path.exists(d):
                try:
                    os.replace(s, d)
                except OSError:
                    pass
        try:
            if not os.listdir(old_kb_dir):
                os.rmdir(old_kb_dir)
            print('📦 [Migration] تم نقل ملفات المعرفة من %s إلى %s' % (old_kb_dir, new_kb_dir))
        except OSError:
            pass
    return new_kb_dir


# عتبات قابلة للضبط
ANGER_THRESHOLD = 0.50
CONF_ESCALATE = 0.35
CONF_MIN_CUTOFF = 0.25
ENABLE_LLM = os.environ.get('ENABLE_LLM', 'true').lower() in ('true', '1', 'yes')

_INDEX_BY_TENANT = {}

# ---------------------------------------------------------------- كاش الردود
# أسئلة متطابقة (بلا سياق محادثة سابق) لا تُعالج ولا تُولَّد مرتين —
# يقلص زمن الاستجابة لأجزاء الثانية ويخفف الحمل على GPU تحت الضغط.
_ANSWER_CACHE = {}
_ANSWER_CACHE_LOCK = threading.Lock()
_ANSWER_CACHE_MAX = 500
_ANSWER_CACHE_TTL = 600  # ثانية


def _cache_key(tenant, message):
    norm = rag.normalize(message)[:200]
    norm = re.sub(r'\s+', ' ', norm).strip()
    return '%s|%s' % (tenant, norm)


def _cache_get(key):
    with _ANSWER_CACHE_LOCK:
        item = _ANSWER_CACHE.get(key)
        if not item:
            return None
        answer, model_used, audit_notes, ts = item
        if time.time() - ts > _ANSWER_CACHE_TTL:
            _ANSWER_CACHE.pop(key, None)
            return None
        return answer, model_used, list(audit_notes)


def _cache_put(key, answer, model_used, audit_notes):
    with _ANSWER_CACHE_LOCK:
        if len(_ANSWER_CACHE) >= _ANSWER_CACHE_MAX:
            oldest = min(_ANSWER_CACHE, key=lambda k: _ANSWER_CACHE[k][3])
            _ANSWER_CACHE.pop(oldest, None)
        _ANSWER_CACHE[key] = (answer, model_used, list(audit_notes or []), time.time())


def clear_answer_cache():
    """تفريغ كاش الردود (يُستدعى عند أي تغيير في قاعدة المعرفة)."""
    with _ANSWER_CACHE_LOCK:
        _ANSWER_CACHE.clear()


def get_index():
    tenant = get_tenant_id()
    if tenant not in _INDEX_BY_TENANT:
        kb_dir = get_kb_dir(tenant)
        _INDEX_BY_TENANT[tenant] = rag.KnowledgeIndex(kb_dir).build()
        policy_extractor.get_tenant_policies(tenant, kb_dir)
    return _INDEX_BY_TENANT[tenant]


def reload_index():
    tenant = get_tenant_id()
    kb_dir = get_kb_dir(tenant)
    _INDEX_BY_TENANT[tenant] = rag.KnowledgeIndex(kb_dir).build()
    policy_extractor.get_tenant_policies(tenant, kb_dir, force_reload=True)
    clear_answer_cache()  # المعرفة تغيرت: كل الردود المخزنة قد تصبح قديمة
    providers.invalidate_context_cache(tenant)  # إبطال Context Cache السحابي
    return _INDEX_BY_TENANT[tenant]


def _confidence(query, matches):
    """الثقة = 0.45 * (تشابه الكوني المعايَر) + 0.55 * (تغطية كلمات السؤال)."""
    if not matches:
        return 0.0
    top_chunk, top_score = matches[0]
    nc = min(1.0, top_score / 0.35)
    orig, expanded = rag.content_tokens(query)
    if not orig:
        return 0.0
    
    primary_tokens = set()
    for m_chunk, _ in matches[:3]:
        primary_tokens.update(rag.tokenize(m_chunk.get('source', '') + ' ' + m_chunk.get('heading', '') + ' ' + m_chunk.get('text', '')))
    primary_tokens = set(t for t in primary_tokens if t not in rag.STOPWORDS)
    
    matched_orig = sum(1 for t in orig if any(syn in primary_tokens for syn in rag.SYNONYMS.get(t, {t})))
    coverage = float(matched_orig) / len(orig)
    coverage = min(1.0, coverage)

    # إذا كانت معظم الكلمات الجوهرية في السؤال غير موجودة في المستندات المسترجعة (سؤال خارج النطاق)
    if coverage < 0.50:
        return round((coverage / 0.50) * (0.45 * nc * coverage + 0.55 * coverage), 3)

    return round(0.45 * nc + 0.55 * coverage, 3)


def extract_comparison_targets(query):
    """استخراج طرفي المقارنة بشكل عام ومجرد من أي سؤال مقارنة باللغة العربية."""
    context_match = re.search(r'\[السياق من سؤالك السابق:\s*(.+?)\]', query)
    target_text = context_match.group(1) if context_match else query
    
    clean = re.sub(r'[\?؟\.\!]', '', target_text).strip()
    
    # 1. نمط: بين X و Y
    if 'بين' in clean:
        after_bayn = clean.split('بين', 1)[1].strip()
        m = re.search(r'\s+(?:و(?=[\u0621-\u064A])|وبين\s+|أم\s+|أو\s+|ولا\s+)(.+)', after_bayn)
        if m:
            t1 = after_bayn[:m.start()].strip()
            t2 = m.group(1).strip()
            t2 = re.sub(r'\s+(?:من ناحية.*|في السعر.*|في التكلفة.*|في الضمان.*)$', '', t2).strip()
            if len(t1) >= 2 and len(t2) >= 2:
                return t1, t2

    # 2. نمط: فرق X عن Y
    if 'فرق' in clean and 'عن' in clean:
        m = re.search(r'فرق\s+(.+?)\s+عن\s+(.+)', clean)
        if m:
            t1 = m.group(1).strip()
            t2 = m.group(2).strip()
            t2 = re.sub(r'\s+(?:من ناحية.*|في السعر.*|في التكلفة.*|في الضمان.*)$', '', t2).strip()
            if len(t1) >= 2 and len(t2) >= 2:
                return t1, t2

    # 3. نمط: X ولا Y
    m2 = re.search(r'(.+?)\s+(?:ولا|أم)\s+(.+?)(?:\s+أحسن|\s+أفضل|\s+أرخص|\s+أغلى|\s+الفرق|$)', clean)
    if m2:
        return m2.group(1).strip(), m2.group(2).strip()
        
    return None


# ---------------------------------------------------------------- كشف حقن التعليمات
# ادعاء صلاحية أو أمر بتجاهل القواعد أو استخراج البرومبت (عربي + إنجليزي) — يُرفض برسالة مميزة
_INJECTION_PATTERNS = [
    # أنماط عربية (انتحال صلاحية / تجاهل قواعد / سرقة تعليمات النظام)
    r'بصفتك\s+(?:مدير|مسؤول|مالك|موظف|أدمن|ادمن|admin)',
    r'(?:وافق(?:\s*لي)?|وافقي|أكد لي|أكدلي)\s+(?:على\s+)?(?:خصم|تخفيض|إلغاء|استرداد)',
    r'تجاهل\s+(?:كل\s+)?(?:التعليمات|القواعد|البرومبت|كل ما سبق|الاوامر)',
    r'أنت الآن\s+', r'انت الان\s+',
    r'من\s+(?:الأعلى|الإدارة|الإداره)\s+(?:أطلب|انا)',
    r'(?:اعرض|اظهر|اطبع|قولي|هات)\s+(?:لي\s+)?(?:تعليمات النظام|البرومبت|التعليمات السريه|التعليمات السرية|system prompt)',
    r'تجاوز\s+(?:القيود|القواعد|الحماية|النظام)',
    # أنماط إنجليزية (Prompt Injection / System Prompt Extraction / Jailbreak)
    r'ignore\s+(?:all\s+)?(?:previous|prior|above)\s+(?:instructions|rules|prompts)',
    r'disregard\s+(?:all\s+)?(?:previous|prior|system)\s+(?:instructions|rules)',
    r'(?:reveal|show|print|dump|repeat)\s+(?:your\s+|the\s+)?(?:system\s+prompt|hidden\s+instructions|initial\s+instructions)',
    r'(?:act\s+as|you\s+are\s+now|pretend\s+to\s+be)\s+(?:an?\s+)?(?:admin|administrator|developer|root|owner|dan|jailbreak)',
    r'\b(?:jailbreak|developer\s+mode|override\s+rules|bypass\s+policy)\b',
]

_INJECTION_REFUSAL = ('عذراً، لا أملك صلاحية الموافقة على خصومات أو استثناءات خارج العروض المنشورة '
                      'رسمياً في قاعدة معرفتي. طلباتك داخل هذه العروض أنفذها فوراً، وأي شيء خارجها '
                      'يحتاج موافقة الإدارة — حوّلت استفسارك لموظف بشري لمتابعته.')


def _detect_injection(message):
    """هل الرسالة تحاول انتحال صلاحية أو تجاوز القواعد أو كشف تعليمات النظام؟"""
    n = rag.normalize(message)
    return any(re.search(pat, n, re.IGNORECASE) for pat in _INJECTION_PATTERNS)


def _has_arabic_any(text, words):
    t = rag.normalize(text)
    return any(w in t for w in words)


def _is_refund_like(text):
    """أسئلة الإلغاء/العربون/الاسترداد تكمل المسار الكامل (كاشف التعارض
    بين العرض والسياسة قد يلزم) ولا تأخذ المسار السريع المبكر."""
    n = rag.normalize(text)
    return any(w in n for w in ('عربون', 'الغيت', 'الغاء', 'استرداد', 'لغيت', 'هرجع', 'بيرجع'))


def _sanitize_entry(msg):
    """المحطة 1 — تنقية المدخل قبل أي طبقة: تعطيل رموز كسر صندوق البرومبت
    ([ ] { } ` < >) التي قد ينتحل بها المخترق شكل قوالب النظام أو وسوم XML،
    مع الحفاظ على النص العربي والمعنى كما هو."""
    return (msg.replace('[', '(').replace(']', ')')
               .replace('{', '(').replace('}', ')')
               .replace('<', '(').replace('>', ')')
               .replace('`', "'"))


# ---------------------------------------------------------------- كشف تعارض المصادر
# جمل حظر استرداد مقابل جمل إجازة استرداد على نفس الموضوع من ملفين مختلفين →
# عرض الاثنين بمصادرهما + تصعيد. بند "لا يسري" الخاص بتطبيق الخصم ليس حظر استرداد
# ولا يجوز أن يثير تعارضاً (Q7 درس موثق).
_BAN_CLAUSE_RE = re.compile(r'[^.\n]*(?:غير قابل للاسترداد|غير قابله للاسترداد|لا يُسترد|لا يسترد|لا يرد|ميرجعش|غير مسترد|مش بترجع|مش بيتسترد)[^.\n]*')
_ALLOW_CLAUSE_RE = re.compile(r'[^.\n]*(?:استرداد العربون بالكامل|بيرجع بالكامل|يُسترد بالكامل|استرداد بالكامل|يُرد كاملاً|يُرد كاملا|يرد كاملا|يرد كامل)(?<!غير )[^.\n]*')


def _detect_policy_conflict(matches, query):
    """يعيد (جملة الحظر، مصدرها، جملة الإجازة، مصدرها) لو التعارض قائم
    ومرتبط بموضوع سؤال العميل فعلاً (تقاطع كلمتين دالتين مع جمل الحظر/الإجازة)."""
    from engine.premath import _words_seq as _pw, _pair_match
    qw = set()
    for w in re.findall(r'[\u0621-\u064a]{3,}', rag.normalize(query)):
        qw.add(re.sub(r'^(?:و|ف|ب|ك)?(?:ال|لل)', '', w).lstrip('و'))
    ban = allow = None
    from engine.premath import _norm as _pn, _words_seq as _pws
    for chunk, _ in matches:
        text = _pn(chunk.get('text', ''))
        if ban is None:
            m = _BAN_CLAUSE_RE.search(text)
            if m:
                # السطر كاملاً (لا الذيل): كلمات الموضوع (عرض الموسم) قبل المثير
                # ضرورية لبابة الصلة — الذيل وحده لا يحوي موضوع السؤال
                ls = text.rfind('\n', 0, m.start()) + 1
                le = text.find('\n', m.end())
                le = len(text) if le < 0 else le
                ban = (text[ls:le].strip(), chunk.get('source', ''), set(_pws(text[ls:le])))
        if allow is None:
            m = _ALLOW_CLAUSE_RE.search(text)
            if m:
                ls = text.rfind('\n', 0, m.start()) + 1
                le = text.find('\n', m.end())
                le = len(text) if le < 0 else le
                allow = (text[ls:le].strip(), chunk.get('source', ''), set(_pws(text[ls:le])))
    if ban and allow and ban[1] != allow[1]:
        # بوابة الصلة الجذرية: موضوع التعارض = **عنوان الـchunk الحامل للجملة**
        # (اسم العرض/باب السياسة) — السؤال يجب أن يذكره صراحة أو يصادقه
        # صرفياً (عرض/موسم). كلمات متناثرة من النص لا تفتح التعارض.
        from engine.premath import _words as _pw2
        by_src = {c.get('source', ''): c for c, _ in matches}
        ban_head = by_src.get(ban[1], {}).get('heading', '')
        allow_head = by_src.get(allow[1], {}).get('heading', '')
        hit_ban = _pair_match(qw, _pw2(ban_head))
        hit_allow = _pair_match(qw, _pw2(allow_head))
        # شرط الإسناد: بند العرض المالي يسري فقط على من حجز **هذا العرض** —
        # العميل لازم يكون ذكر العرض صراحة (مثل «من عرض الموسم») — وإلا
        # يبقى جواب النافذة الحتمية (قبل/بعد المهلة) هو الصحيح بلا تعارض
        mentions_offer = 'عرض' in qw or 'عرض' in ' '.join(qw)
        if mentions_offer and (hit_ban >= 2 or hit_allow >= 2):
            return ban[:2], allow[:2]
    return None


_TOP_CTX = 3  # مقاطع المعرفة المرسلة للمُحكّم الآلي

def _nearest_kb_hedge(matches):
    """حل «أقرب حاجة + إعادة صياغة»: يُلحق بالمصعَّد أقرب معلومة عضوية من
    قاعدة المعرفة بتحذير صريح أنها قد لا تكون المقصودة — بلا أي رقم مخترع.
    chunks الحقن (0.9) مستبعدة: هي لفحص التعارض."""
    organic = [m for m in (matches or []) if 0.30 <= m[1] < 0.89]
    if not organic:
        return ''
    try:
        from engine.ai import _friendly_chunk
        info = _friendly_chunk(organic[0][0])
    except Exception:
        return ''
    if not info or any(w in info for w in ('service_id', 'branch_id', 'tags')):
        return ''
    return ('\n\nأقرب معلومة عندنا في قاعدة المعرفة (ممكن تكون دي قصدك أو مش متعلقة بالظبط):\n'
            + info + '\nلو مش دي قصدك، جرب تعيد صياغة سؤالك — ولو محتاج تأكيد رسمي حوّلناك لموظف.')


ESCALATION_UNIFIED = ('المعلومة دي مش موجودة في المستندات المتاحة عندي حالياً، '
                      'فمش هقدر أحدثك برقم أو مبلغ من عندي — حوّلت سؤالك لموظف بشري '
                      'وهيتواصل معاك ويأكدلك التفاصيل رسمياً.')


def handle_message(message, session_id='', now=None, enable_llm=None):
    """معالجة رسالة عميل كاملة: تحليل + بحث متعدد المراحل + Re-ranking + قرار + تذكرة.
    الحمايات قبل كل شيء: حقن التعليمات/انتحال الصلاحية → رفض صريح فوري."""
    started = time.perf_counter()
    message = (message or '').strip()[:2000]
    if not message:
        return {'error': 'empty_message'}
    # المحطة 1: تنقية رموز كسر الصندوق قبل أي طبقة (استرجاع/برومبت/تخزين)
    message = _sanitize_entry(message)
    now = time.time() if now is None else now

    # المحطة 2 (النصف القرائي): سحب ملف العميل من الذاكرة طويلة المدى
    cust_note = cust_memory.recall(session_id) if session_id else ''

    # 0. حقن التعليمات / انتحال صلاحية: رفض قصير ثم إجابة **الجزء المشروع فقط**
    # من السؤال من القواعد الرسمية (عيب 8) — لا خروج مبكر يُسقط السؤال كله
    injection_prefix = ''
    if _detect_injection(message):
        injection_prefix = _INJECTION_REFUSAL + '\n\n'

    # 1. التخصيب التتابعي وحل الضمائر السياقية (Coreference Resolution)
    history = session.get_history(session_id) if session_id else []
    enriched_query = session.enrich_followup_query(session_id, message)
    search_query = enriched_query if enriched_query != message else message
    if search_query != message:
        print('🔗 [Session] %r -> %r' % (message, search_query), file=sys.stderr)

    # 1.5 المسار السريع الحتمي (عيب 13): حساب premath على كامل الفهرس **قبل**
    # الاسترجاع والتوليد — الأسئلة المحسومة (سعر/حسبة/نطاق/نافذة/مقارنة) تُرد
    # في ثوانٍ من غير LLM ولا استرجاع، وهذا لا يمس أسئلة الفهم الحر أصلاً
    index = get_index()
    # 1.4 مطابقة السيناريوهات والأسئلة المركبة الموثقة (FAQ / Enterprise Scenarios):
    # إذا كان السؤال يطابق عنوان سيناريو مركب أو FAQ بنسبة عالية (>= 65%)، نُرجع الإجابة
    # المهيكلة الكاملة فوراً مع توثيق المصدر دون اختزالها في سطر واحد.
    q_norm_tokens = set(t for t in rag.tokenize(search_query) if t not in rag.STOPWORDS and len(t) >= 3)
    if len(q_norm_tokens) >= 4:
        best_sc_chunk = None
        best_sc_score = 0.0
        for ch in index.chunks:
            txt = ch.get('text', '')
            if not re.search(r'(?:^|\n)\s*id\s*:\s*[SF]\d+', txt):
                continue
            h_tokens = set(t for t in rag.tokenize(ch.get('heading', '')) if t not in rag.STOPWORDS and len(t) >= 3)
            if not h_tokens:
                continue
            ov = len(q_norm_tokens & h_tokens) / float(min(len(q_norm_tokens), len(h_tokens)))
            if ov > best_sc_score:
                best_sc_score = ov
                best_sc_chunk = ch
        if best_sc_chunk is not None and best_sc_score >= 0.65:
            ans_body = ai._friendly_chunk(best_sc_chunk)
            src_label = f"{best_sc_chunk.get('source', '')} :: {best_sc_chunk.get('heading', '')[:60]}"
            response_ms = int((time.perf_counter() - started) * 1000)
            if session_id:
                session.add_exchange(session_id, message, ans_body, matches=[(best_sc_chunk, 0.96)])
            ticket = {
                'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'session_id': session_id, 'channel': 'web', 'query': message,
                'answer': ans_body, 'status': 'resolved', 'confidence': 0.96,
                'anger': 0.0, 'sources': [src_label],
                'escalated_reason': None, 'response_ms': response_ms,
                'retrieval_ms': 1, 'rerank_ms': 0, 'llm_ms': 0,
                'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'model_used': 'enterprise_scenario_engine',
                'mode': 'deterministic'
            }
            ticket = store.create_ticket(ticket)
            audit.log_ticket(ticket)
            return ticket

    _use_llm_local = bool(enable_llm) if enable_llm is not None else ENABLE_LLM
    if _use_llm_local:
        try:
            pm_fast = premath.handle(search_query, index.chunks)
        except Exception:
            pm_fast = {'forced_answer': None, 'parts': [], 'resolved_all': False}
        if injection_prefix and not pm_fast.get('forced_answer'):
            pm_fast = {'forced_answer': None, 'parts': [], 'resolved_all': False}
        # مسارات الإلغاء/العربون تكمل المسار الكامل: كاشف التعارض (عيب 6) قد يلزم
        # عيب 9: مطالبة بتعويض/غرامة/كاش عن شيء غير موثق في القاعدة → تصعيد
        # موحد بلا أي مبلغ مخترع (ولو فيه جواب حتمي آخر في نفس السؤال المركب)
        comp_claim = (re.search(r'تعويض|تعوض|غرم', message)
                      or (re.search(r'كاش', message) and re.search(r'اتأخر|تأخير|تاخير', message)))
        if comp_claim and not _is_refund_like(search_query):
            answer = (injection_prefix + ESCALATION_UNIFIED).strip()
            response_ms = int((time.perf_counter() - started) * 1000)
            if session_id:
                session.add_exchange(session_id, message, answer, matches=[])
            ticket = {
                'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'session_id': session_id, 'channel': 'web', 'query': message,
                'answer': answer, 'status': 'escalated', 'confidence': 0.0,
                'anger': 0.0, 'sources': [], 'escalated_reason': 'out_of_kb',
                'response_ms': response_ms,
                'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'resolved_at': None, 'model_used': 'fabrication_guard', 'mode': 'extractive'
            }
            ticket = store.create_ticket(ticket)
            audit.log_ticket(ticket)
            return ticket
        if pm_fast.get('forced_answer') and pm_fast.get('resolved_all') and not _is_refund_like(search_query):
            answer = (injection_prefix + guardrails.sanitize_text(pm_fast['forced_answer'])).strip()
            response_ms = int((time.perf_counter() - started) * 1000)
            if session_id:
                session.add_exchange(session_id, message, answer, matches=[])
            ticket = {
                'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'session_id': session_id,
                'channel': 'web',
                'query': message,
                'answer': answer,
                'status': 'resolved',
                'confidence': 0.9,
                'anger': 0.0,
                'sources': [],
                'escalated_reason': 'unauthorized_request' if injection_prefix else None,
                'response_ms': response_ms,
                'retrieval_ms': 0,
                'rerank_ms': 0,
                'llm_ms': 0,
                'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'model_used': 'premath_fast',
                'mode': 'deterministic'
            }
            ticket = store.create_ticket(ticket)
            audit.log_ticket(ticket)
            return ticket

    # 2. البحث متعدد المسارات (Multi-Hop / Multi-Target Search)
    t_retrieval = time.perf_counter()
    comp_targets = extract_comparison_targets(search_query)
    candidates_dict = {}
    entity_chunks = []
    anchor_ban = anchor_allow = None
    grounding_retried = False

    def add_candidate(chunk, score):
        k = (chunk.get('source', ''), chunk.get('heading', ''))
        if k not in candidates_dict or score > candidates_dict[k][1]:
            candidates_dict[k] = (chunk, float(score))

    index = get_index()

    if comp_targets:
        t1, t2 = comp_targets
        # عيب 6: تمثيلا الحظر/الإجازة يُلتقطان في مسار المقارنة أيضاً
        if _has_arabic_any(search_query, ('عربون', 'الغيت', 'إلغاء', 'الغاء', 'استرداد')):
            from engine.premath import _norm as _pn2
            for chunk in index.chunks:
                t = _pn2(chunk.get('text', ''))
                if 'عربون' not in t and 'استرداد' not in t:
                    continue
                if anchor_ban is None and _BAN_CLAUSE_RE.search(t):
                    anchor_ban = chunk
                if anchor_allow is None and _ALLOW_CLAUSE_RE.search(t):
                    anchor_allow = chunk
        m1 = index.search(t1, top_k=3)
        m2 = index.search(t2, top_k=3)
        m_all = index.search(search_query, top_k=4)
        for pair in zip(m1[:2], m2[:2]):
            for c, s in pair:
                add_candidate(c, s)
        for m_list in [m1, m2, m_all]:
            for c, s in m_list:
                add_candidate(c, s)
    else:
        # بحث مباشر
        base_matches = index.search(search_query, top_k=8)
        for c, s in base_matches:
            add_candidate(c, s)

        # حقن عام لموضوع "العربون/الإلغاء": سياسات الاسترداد وحالات الحظر من مصادر
        # مختلفة يجب أن تُجمع معاً ليظهر التعارض إن وُجد (لا يعتمد على أسماء ملفات)
        anchor_ban = anchor_allow = None
        if _has_arabic_any(search_query, ('عربون', 'الغيت', 'إلغاء', 'الغاء', 'استرداد')):
            from engine.premath import _norm as _pn
            for chunk in index.chunks:
                t = _pn(chunk.get('text', ''))
                if 'عربون' not in t and 'استرداد' not in t:
                    continue
                is_ban = _BAN_CLAUSE_RE.search(t) is not None
                is_allow = _ALLOW_CLAUSE_RE.search(t) is not None
                if is_ban or is_allow:
                    add_candidate(chunk, 0.9)
                    # عيب 6: احتفظ بتمثيلي الحظر/الإجازة لضمان دخولهما matches
                    if is_ban and anchor_ban is None:
                        anchor_ban = chunk
                    if is_allow and anchor_allow is None:
                        anchor_allow = chunk

        # Multi-Hop Entity & Category Anchor Injection:
        query_norm = rag.normalize(search_query)
        q_tokens = [t for t in rag.tokenize(search_query) if t not in rag.STOPWORDS]
        entity_chunks = []
        for chunk in index.chunks:
            h_norm = rag.normalize(chunk.get('heading', ''))
            h_words = [w for w in h_norm.split() if len(w) >= 3 and w not in rag.STOPWORDS]
            
            # مطابقة اسم الكيان الصريح
            if len(h_words) >= 2 and all(w in query_norm for w in h_words[:2]):
                entity_chunks.append((chunk, 0.95))
                add_candidate(chunk, 0.95)
            # مطابقة الفئة أو الوصف الدلالي بالمرادفات (مثل: رخيصة/اقتصادية، فاخرة، ميني باص)
            elif q_tokens:
                chunk_all_tokens = set(rag.tokenize(chunk.get('heading', '') + ' ' + chunk.get('text', '')))
                for qt in q_tokens:
                    syns = rag.SYNONYMS.get(qt, {qt})
                    if any(s in chunk_all_tokens for s in syns) and any(kw in chunk.get('text', '') for kw in ('الفئة', 'سعر', 'rate', 'مواصفات', 'باقة')):
                        add_candidate(chunk, 0.85)

    candidates = list(candidates_dict.values())
    retrieval_ms = int((time.perf_counter() - t_retrieval) * 1000)

    # 3. إعادة الترتيب الدلالي المتقدم باستخدام BERT / Sentence-Transformers (Re-Ranking)
    t_rerank = time.perf_counter()
    matches = reranker.rerank_matches(search_query, candidates, top_k=5)

    # ضمان وجود بطاقة الكيان المعني في السياق للعمليات الحسابية
    if entity_chunks:
        seen_heads = set(c.get('heading') for c, _ in matches)
        for ec, sc in entity_chunks:
            if ec.get('heading') not in seen_heads:
                matches.insert(0, (ec, sc))
    # عيب 6: تمثيلا الحظر/الإجازة المحقونان يدائماً في matches — بيهما
    # يضيع بند العرض المالي ويحسم النموذج طرفاً واحداً بثقة
    if _is_refund_like(search_query):
        seen_heads = set(c.get('heading') for c, _ in matches)
        for ac in (anchor_ban, anchor_allow):
            if ac is not None and ac.get('heading') not in seen_heads:
                matches.append((ac, 0.88))
    matches = matches[:7]
    rerank_ms = int((time.perf_counter() - t_rerank) * 1000)

    confidence = _confidence(search_query, matches)

    # حماية المتابعات من بوابة الثقة المنخفضة: سؤال أُعيدت صياغته بنجاح من ذاكرة
    # الجلسة له سياق — لا يجوز تصعيده فوراً لو انخفضت الثقة (بوابة الـ ~200ms)
    is_followup = (search_query != message) and bool(history)
    if is_followup and confidence < CONF_ESCALATE:
        confidence = max(confidence, 0.32)

    if enable_llm is not None:
        use_llm = bool(enable_llm)
    else:
        use_llm = ENABLE_LLM or os.environ.get('ENABLE_LLM', 'true').lower() in ('true', '1', 'yes')

    # Sentiment Analysis (LLM-based)
    sentiment_tag = 'neutral'
    if use_llm:
        sentiment_tag = llm.analyze_sentiment(message)
    else:
        if sentiment.anger_score(message) >= ANGER_THRESHOLD:
            sentiment_tag = 'angry'

    anger = 1.0 if sentiment_tag == 'angry' else 0.0

    if sentiment_tag == 'angry':
        status, reason = 'priority', 'angry_customer'
    elif confidence < CONF_MIN_CUTOFF:
        status, reason = 'escalated', 'very_low_confidence'
    else:
        # 0.25-0.35: إجابة متحفظة (is_medium_conf) / 0.35+: إجابة كاملة
        status, reason = 'resolved', None

    answerable = status == 'resolved' or confidence >= CONF_MIN_CUTOFF

    # 4. فحص الأسئلة الحوارية والترحيب
    chitchat = ai.check_chitchat(message)
    if chitchat:
        confidence = 1.0
        status, reason = 'resolved', None
        answerable = True
        sources = [chitchat['source']]
        if use_llm:
            mock_chunk = {'source': 'about_and_identity.md', 'heading': 'من نحن وما هوية المساعد', 'text': chitchat['answer']}
            answer, model_used, audit_notes = llm.generate_answer(message, [(mock_chunk, 1.0)], status, answerable, history=history)
        else:
            answer = chitchat['answer']
            model_used = 'extractive_chitchat'
            audit_notes = []
        response_ms = int((time.perf_counter() - started) * 1000)
        if session_id:
            session.add_exchange(session_id, message, answer, matches=[(mock_chunk, 1.0)] if use_llm else None)
        mode = 'llm' if (use_llm and model_used not in ('extractive_rag', 'extractive_chitchat')) else 'extractive'
        ticket = {
            'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
            'session_id': session_id,
            'channel': 'web',
            'query': message,
            'answer': answer,
            'status': status,
            'confidence': confidence,
            'anger': anger,
            'sources': sources,
            'escalated_reason': reason,
            'response_ms': response_ms,
            'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
            'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds') if status == 'resolved' else None,
            'model_used': model_used,
            'mode': mode
        }
        ticket = store.create_ticket(ticket)
        audit.log_guardrail(ticket['id'], message, audit_notes)
        audit.log_ticket(ticket)
        return ticket

    # 5. توليد الإجابة
    sources = []
    seen_src = set()
    for chunk, _ in matches[:4]:
        heading = chunk.get('heading', '').strip()
        source = chunk.get('source', '').strip()
        label = f"{source} :: {heading}" if heading else source
        if label not in seen_src:
            seen_src.add(label)
            sources.append(label)

    # 4.5 كشف تعارض المصادر: حظر من ملف وإجازة من ملف آخر على نفس الموضوع →
    # يُعرض الاثنان بمصادرهما ويُصعَّد — لكن لا يُثار عند وجود حاسم حتمي
    # (نافذة إلغاء بقاعدتين "قبل N/خلال N" نافذة واحدة تُحسم رقمياً، لا تعارضاً)
    pm_early = premath.handle(search_query, index.chunks) if use_llm else {'forced_answer': None}
    has_deterministic = bool(pm_early.get('forced_answer'))
    # عيب 6: أسئلة الإلغاء/العربون تُفحص للتعارض دائماً حتى لو في جواب حتمي —
    # لأن جواب النافذة من السياسة قد يتعارض مع بند عرض مالي (عرض الموسم)
    refund_like = _is_refund_like(search_query)
    conflict = _detect_policy_conflict(matches, message) if (refund_like or not has_deterministic) else None
    if conflict:
        (ban_txt, ban_src), (allow_txt, allow_src) = conflict
        # ترجيح عام للمصدر الأدق: النص الأكثر تحديداً للموضوع (طوله + ذكر الاسترداد
        # صراحة بشروط) يُرجَّح، مع بقاء عرض النصين وإبقاء الحسم النهائي للبشري
        more_specific = ban_txt if len(ban_txt) >= len(allow_txt) else allow_txt
        more_src = ban_src if more_specific is ban_txt else allow_src
        answer = ('⚠️ في تعارض بين وثيقتين في قاعدة المعرفة بخصوص استفسارك:\n\n'
                  '• النص 1 (من %s): "%s"\n\n'
                  '• النص 2 (من %s): "%s"\n\n'
                  'الأرجح حسبنا: %s (المصدر: %s) — لأنه نص مخصص صريح للموضوع بشروطه، '
                  'بينما النص الآخر إشارة أعم. ومع ذلك هذا حكم آلي قد يخطئ في الحالات الخاصة، '
                  'لذا حوّلت سؤالك لموظف بشري يأكد لك القاعدة المطبقة نهائياً.'
                  % (ban_src, ban_txt, allow_src, allow_txt, more_specific, more_src))
        response_ms = int((time.perf_counter() - started) * 1000)
        if session_id:
            session.add_exchange(session_id, message, answer, matches=matches)
        ticket = {
            'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
            'session_id': session_id,
            'channel': 'web',
            'query': message,
            'answer': answer,
            'status': 'escalated',
            'confidence': confidence,
            'anger': anger,
            'sources': sources,
            'escalated_reason': 'policy_conflict',
            'response_ms': response_ms,
            'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
            'resolved_at': None,
            'model_used': 'conflict_detector',
            'mode': 'extractive'
        }
        ticket = store.create_ticket(ticket)
        audit.log_ticket(ticket)
        return ticket

    if use_llm:
        # الحقائق القاطعة الحتمية: premath على كل الفهرس (أسعار/استعجال/خصم/عربون/فرع/
        # مقارنة/عملة + تقسيم المركبات) — وعند غيابها قواعد rule_evaluator (طوارئ/نسب)
        pm = pm_early
        forced_answer = pm.get('forced_answer')
        if not forced_answer:
            forced_answer = _compute_forced_answer(search_query)
        # المسار السريع القاطع: جواب حتمي كامل لكل أجزاء السؤال = رد نهائي مباشر
        # بلا استدعاء LLM (زمن 1-3 ث بدل 30-60) — لا مجال للهلوسة أصلاً هنا.
        if forced_answer and pm.get('resolved_all') and status != 'priority' and answerable:
            clean_forced = guardrails.sanitize_text(forced_answer)
            answer = (injection_prefix + clean_forced).strip() if injection_prefix else clean_forced
            model_used = 'premath_direct'
            audit_notes = []
            response_ms = int((time.perf_counter() - started) * 1000)
            if session_id:
                session.add_exchange(session_id, message, answer, matches=matches)
            ticket = {
                'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'session_id': session_id,
                'channel': 'web',
                'query': message,
                'answer': answer,
                'status': 'resolved',
                'confidence': max(confidence, 0.85),
                'anger': anger,
                'sources': sources,
                'escalated_reason': None,
                'response_ms': response_ms,
                'retrieval_ms': retrieval_ms,
                'rerank_ms': rerank_ms,
                'llm_ms': 0,
                'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
                'model_used': model_used,
                'mode': 'deterministic'
            }
            ticket = store.create_ticket(ticket)
            audit.log_ticket(ticket)
            return ticket
        # الكاش: فقط للأسئلة المستقلة (بلا تاريخ محادثة) عالية الثقة المحلولة
        cacheable = (not history) and status == 'resolved' and confidence >= 0.5
        ckey = _cache_key(get_tenant_id(), search_query) if cacheable else None
        cached = _cache_get(ckey) if ckey else None
        if cached:
            answer, model_used, audit_notes = cached
            model_used = model_used + '_cached'
            llm_ms = 0
        else:
            t_llm = time.perf_counter()
            cache_ctx = None
            if providers.get_provider() == 'gemini' and providers.is_context_cache_enabled():
                cache_ctx = '\n\n'.join(
                    f"## {c.get('heading', '')}\n{c.get('text', '')}"
                    for c in index.chunks[:40]
                )
            answer, model_used, audit_notes = llm.generate_answer(
                search_query,
                matches,
                status,
                answerable,
                history=history,
                is_medium_conf=(CONF_MIN_CUTOFF <= confidence < CONF_ESCALATE),
                forced_answer=forced_answer,
                cust_note=cust_note,
                confidence=confidence,
                tenant_id=get_tenant_id(),
                cacheable_context=cache_ctx,
            )
            llm_ms = int((time.perf_counter() - t_llm) * 1000)
            if ckey and audit_notes == []:
                _cache_put(ckey, answer, model_used, audit_notes)
            # عيب 11: إجابة مغطية جزءاً من أجزاء السؤال فقط = ثقة أقل
            if forced_answer and not pm.get('resolved_all'):
                confidence = min(confidence, 0.55)
    else:
        forced_answer = None
        answer = ai.build_answer(matches, status, answerable)
        model_used = 'extractive_rag'
        audit_notes = []
        llm_ms = 0

    response_ms = int((time.perf_counter() - started) * 1000)

    # فحص ما بعد التوليد (Post-generation Escalation Detection)
    # إجابة "المعلومة غير متوفرة" لا يجوز أن تُصنف محلولة بثقة عالية أبداً (سياسة موحدة)
    escalation_indicators = [
        'تحويلك لموظف', 'تحويلك إلى موظف', 'تحويلك لوكيل', 'تحويلك إلى وكيل',
        'موظف الدعم البشري', 'سأقوم بتحويلك', 'حوّلتك لموظف', 'سأحولها لموظف',
        'لا أملك معلومات', 'لا أملك أي معلومات', 'ليس لدي معلومات',
        'لا تتوفر لدي معلومات', 'لم أتمكن من صياغة إجابة', 'خارج قاعدة معرفتي',
    ]
    out_of_kb_indicators = [
        'ليس لدي معلومات', 'لا أملك معلومات', 'لا أملك أي معلومات',
        'لا تتوفر لدي معلومات', 'غير موضحة في المستندات', 'خارج قاعدة معرفتي',
        'غير متوفر في المستندات',
    ]
    # عيب 11 (التنفيذ النهائي): إجابة توليدية بثقة تحت عتبة الحسم (0.35)
    # تتحول لموظف بشري بدل ما تتعرض — المسار الحتمي (premath) محصّن منه
    # لأن ثقته 0.9 مبنية على حساب موثق
    if (status == 'resolved' and model_used not in
            ('extractive_rag', 'extractive_fallback', 'extractive_think_fallback',
             'premath_direct', 'premath_fast', 'conflict_detector', 'fabrication_guard')
            and confidence < CONF_ESCALATE):
        answer = ESCALATION_UNIFIED + _nearest_kb_hedge(matches)
        status = 'escalated'
        reason = 'low_confidence'

    # عيب 9: سؤال يطلب مبلغاً أو رقماً + فشل التوليد/الاستخراج في توثيقه
    # → تصعيد موحد بنفس الرسالة، بلا أي رقم مخترع
    money_ask = re.search(r'(?:كام|كام؟|رقم|كم)', message)
    failed_generation = use_llm and model_used in ('extractive_think_fallback', 'extractive_fallback', 'extractive_rag')
    if (failed_generation and money_ask and not forced_answer
            and status == 'resolved' and not answer.strip().startswith('هيُرد')):
        answer = ESCALATION_UNIFIED + _nearest_kb_hedge(matches)
        status = 'escalated'
        reason = 'out_of_kb'

    if any(ind in answer for ind in escalation_indicators):
        if status != 'priority':
            status = 'escalated'
            # سبب التصعيد بنوعه: خارج المعرفة ≠ عدم يقين النموذج
            if any(ind in answer for ind in out_of_kb_indicators):
                reason = 'out_of_kb'
                answer = ESCALATION_UNIFIED + _nearest_kb_hedge(matches)
            else:
                reason = reason or 'bot_uncertain'

    # محطة 6ب (Reflexion): فشل التحقق الرقمي لا يتصعد فوراً — يُعاد للنموذج
    # مرة واحدة مع سبب الرفض ليصحح نفسه من نفس المقاطع، ويُتحقق من إعادة
    # المحاولة بنفس الحارس؛ فشلها مجدداً = تصعيد نهائي (بلا حلقات هلوسة)
    grounding_fail = [n for n in (audit_notes or [])
                      if 'غير موجود في السياق' in n]
    if (grounding_fail and status == 'resolved'
            and confidence < 0.8 and use_llm and model_used not in
            ('premath_direct', 'premath_fast', 'extractive_think_fallback')
            and not grounding_retried):
        feedback = '؛ '.join(grounding_fail)[:350]
        t_llm = time.perf_counter()
        answer, model_used, audit_notes = llm.generate_answer(
            search_query,
            matches,
            status,
            answerable,
            history=history,
            is_medium_conf=(CONF_MIN_CUTOFF <= confidence < CONF_ESCALATE),
            forced_answer=forced_answer,
            cust_note=cust_note,
            retry_feedback=feedback,
            confidence=confidence,
            tenant_id=get_tenant_id(),
        )
        llm_ms = llm_ms + int((time.perf_counter() - t_llm) * 1000)
        model_used = model_used + '_reflexion'
        grounding_retried = True
        grounding_fail2 = [n for n in (audit_notes or [])
                           if 'غير موجود في السياق' in n]
        if grounding_fail2:
            status = 'escalated'
            reason = 'verification_failed'

    # تحقق ما بعد التوليد: أرقام مالية مشبوهة + إجابة محلولة → تصعيد لا إرسال أعمى
    if (audit_notes and status == 'resolved'
            and any('غير موجود في السياق' in n for n in audit_notes)
            and confidence < 0.8):
        status = 'escalated'
        reason = 'verification_failed'

    if injection_prefix and 'unauthorized' not in str(reason):
        answer = (injection_prefix + answer).strip()
        if status == 'resolved':
            reason = reason or 'unauthorized_request'

    if session_id:
        session.add_exchange(session_id, message, answer, matches=matches)

    mode = 'llm' if (use_llm and model_used not in ('extractive_rag', 'extractive_chitchat')) else 'extractive'
    ticket = {
        'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'session_id': session_id,
        'channel': 'web',
        'query': message,
        'answer': answer,
        'status': status,
        'confidence': confidence,
        'anger': anger,
        'sources': sources,
        'escalated_reason': reason,
        'response_ms': response_ms,
        'retrieval_ms': retrieval_ms,
        'rerank_ms': rerank_ms,
        'llm_ms': llm_ms,
        'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds') if status == 'resolved' else None,
        'model_used': model_used,
        'mode': mode
    }
    ticket = store.create_ticket(ticket)
    audit.log_guardrail(ticket['id'], message, audit_notes)
    audit.log_ticket(ticket)
    # المحطة الخامسة: استخراج استمارة JSON للإجراءات الحقيقية (حجز/طلب/شكوى/Lead) في خيط خلفي
    try:
        actions.process_async(ticket['id'], session_id, message, use_llm=use_llm)
    except Exception:
        pass
    # المحطة الثامنة: مُحكّم آلي في خيط خلفي للإجابات التوليدية المحلولة فقط
    # (المسارات الحتمية موثقة بالبناء، والمصعّدة راجعة لبشري أصلاً) —
    # الحكم غير المدعوم يُعلَّم على التذكرة ويُسجل في audit_log للمساءلة
    if (status == 'resolved' and use_llm and answerable
            and model_used not in ('extractive_rag', 'extractive_fallback',
                                   'extractive_think_fallback', 'premath_direct',
                                   'premath_fast', 'conflict_detector',
                                   'fabrication_guard', 'injection_guard')
            and not model_used.endswith('_cached')):
        try:
            from engine import judge
            judge.grade_async(ticket['id'], message, answer,
                              [m[0].get('text', '') for m in matches[:3]])
        except Exception:
            pass
    return ticket


def _compute_forced_answer(query):
    """
    حساب الإجابة القاطعة الحتمية على الكتالوج الكامل من الفهرس (كل بطاقات
    price_egp الخاصة بالمستأجر) — لا يستدعى إلا عند وجود حساب أسعار/مقارنة/طوارئ،
    ويعيد نص الإجابة النهائي الجاهز أو None. الحساب على بيانات المستأجر نفسه
    فهو grounding كامل وليس اختراعاً.
    """
    try:
        idx = get_index()
        kb_text = '\n\n'.join(
            (c.get('heading', '') + '\n' + c.get('text', '')).strip()
            for c in idx.chunks if 'price_egp:' in c.get('text', '')
        )
        if not kb_text:
            return None
        # فقط القاعدتان السعرية الحتمية — باقي القواعد تُقيَّم على السياق المسترجع داخل llm
        res = rule_evaluator.evaluate_comparison_price_diff(query, kb_text)
        if res and res.get('final_answer'):
            return res['final_answer']
        res = rule_evaluator.evaluate_emergency_surcharge(query, kb_text)
        if res and res.get('final_answer'):
            return res['final_answer']
    except Exception as e:
        print('[ForcedAnswer Error] %s' % e, file=sys.stderr)
    return None


def compute_stats():
    """إحصائيات الداشبورد الكاملة: عدادات الحالة + الرضا + آخر التذاكر + رسم آخر 7 أيام."""
    from collections import defaultdict
    tickets = store.load_tickets()
    total = len(tickets)
    resolved = sum(1 for t in tickets if t.get('status') == 'resolved')
    escalated = sum(1 for t in tickets if t.get('status') == 'escalated')
    priority = sum(1 for t in tickets if t.get('status') == 'priority')
    avg_conf = (sum(t.get('confidence', 0) for t in tickets) / total) if total else 0.0
    avg_resp = (sum(t.get('response_ms', 0) for t in tickets) / total) if total else 0.0

    rated = [t for t in tickets if t.get('rating')]
    satisfaction = None
    avg_rating = None
    if rated:
        avg_rating = sum(t.get('rating', 0) for t in rated) / len(rated)
        satisfaction = round(100.0 * sum(1 for t in rated if t.get('rating', 0) >= 4) / len(rated))

    judge_flagged_count = sum(1 for t in tickets if t.get('judge_flagged'))

    # آخر 12 تذكرة بصيغة حقول الداشبورد (message بدلاً من query)
    recent = []
    for t in list(reversed(tickets))[:12]:
        recent.append({
            'id': t.get('id'),
            'ts': t.get('ts'),
            'message': t.get('query', ''),
            'status': t.get('status'),
            'confidence': t.get('confidence', 0),
            'response_ms': t.get('response_ms', 0),
            'rating': t.get('rating'),
            'human_reply': t.get('human_reply'),
            'model_used': t.get('model_used'),
            'judge_supported': t.get('judge_supported'),
            'judge_flagged': bool(t.get('judge_flagged')),
            'judge_problems': t.get('judge_problems', ''),
            'action_payload': t.get('action_payload'),
        })

    # تجميع آخر 7 أيام لرسم الأعمدة
    by_day = defaultdict(lambda: {'resolved': 0, 'escalated': 0, 'priority': 0, 'total': 0})
    for t in tickets:
        day = (t.get('created_at') or t.get('ts') or '')[:10]
        if not day:
            continue
        st = t.get('status')
        bucket = by_day[day]
        bucket['total'] += 1
        if st in ('resolved', 'escalated', 'priority'):
            bucket[st] += 1
    days = [{'date': d, **by_day[d]} for d in sorted(by_day)[-7:]]

    return {
        'total': total,
        'total_tickets': total,
        'resolved': resolved,
        'escalated': escalated,
        'priority': priority,
        'unresolved': total - resolved - escalated - priority,
        'judge_flagged_count': judge_flagged_count,
        'avg_confidence': round(avg_conf, 2),
        'avg_response_ms': round(avg_resp),
        'resolution_rate': round(resolved / total * 100, 1) if total else 0.0,
        'satisfaction': satisfaction,
        'avg_rating': round(avg_rating, 2) if avg_rating else None,
        'rated_count': len(rated),
        'recent': recent,
        'days': days,
    }

