# -*- coding: utf-8 -*-
"""
engine/cust_memory.py — المحطة 2: الذاكرة طويلة المدى للعميل (Customer Memory).

جدول SQLite في نفس قاعدة بيانات المستأجر (tickets.db) — مفتاحه معرّف العميل
(جلسة الويب أو رقم التليجرام/واتساب) وقيمته ملف JSON يتضمن عدّاد التفاعلات
وآخر التبادلات المختصرة. يُقرأ عند بدء المعالجة ليُحقن في برومبت النموذج
كسطر سياق («هذا العميل تفاعل معك سابقاً عن كذا»)، ويُحدَّث تلقائياً بعد
كل إجابة — فلا يبدأ العميل من الصفر في كل رسالة.

الخصوصية: البيانات داخل مستأجر واحد فقط، ودالة purge() تحذف ملف عميل
عند طلبه (حق النسيان).
"""
import json
import sqlite3
import threading
from datetime import datetime

from engine import store

_LOCK = threading.Lock()


def _connect():
    """نفس قاعدة بيانات التذاكر للمستأجر — جدول منفصل customer_memory."""
    conn = sqlite3.connect(store.get_tickets_file(), timeout=15)
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _ensure(conn):
    conn.execute('''CREATE TABLE IF NOT EXISTS customer_memory (
        customer_key TEXT PRIMARY KEY,
        profile      TEXT NOT NULL)''')


def remember(customer_key, query, answer):
    """تحديث ملف العميل بعد كل إجابة — لا يكسر المسار الرئيسي أبداً."""
    if not customer_key:
        return
    key = str(customer_key)[:120]
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
                prof['total_interactions'] = len(hist)
                prof['last_seen'] = datetime.now().isoformat(timespec='seconds')
                prof.setdefault('first_seen', prof['last_seen'])
                conn.execute('INSERT OR REPLACE INTO customer_memory (customer_key, profile) VALUES (?, ?)',
                             (key, json.dumps(prof, ensure_ascii=False)))
                conn.commit()
            finally:
                conn.close()
    except Exception:
        pass  # الذاكرة تحسين — فشلها لا يمس الرد


def recall(customer_key, limit=2):
    """سطر سياق مختصر من آخر تعاملات العميل — فارغ إن لم يوجد."""
    if not customer_key:
        return ''
    try:
        with _LOCK:
            conn = _connect()
            try:
                _ensure(conn)
                row = conn.execute('SELECT profile FROM customer_memory WHERE customer_key = ?',
                                   (str(customer_key)[:120],)).fetchone()
            finally:
                conn.close()
        if not row:
            return ''
        prof = json.loads(row[0])
        hist = prof.get('history', [])[-limit:]
        if not hist:
            return ''
        parts = []
        for i, h in enumerate(hist, 1):
            parts.append('(%d) سأل: %s' % (i, h.get('q', '')[:90]))
        return ' | '.join(parts)
    except Exception:
        return ''


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
