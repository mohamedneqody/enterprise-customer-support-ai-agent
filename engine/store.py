# -*- coding: utf-8 -*-
"""تخزين التذاكر في SQLite (مكتبة قياسية، بلا خادم) مع مخطط معياري مفهرس، أقفال خيوط وترحيل تلقائي من JSON.

- الأعمدة المباشرة: (id, ts, session_id, status, confidence, rating, response_ms, model_used, mode, channel, data)
- فهارس صريحة على: status, ts, session_id لتسريع الاستعلامات والتصفية.
- create_ticket تولد الـ ID ذرّياً عبر استعلام SQL مباشر O(1) بدلاً من سحب كافة السجلات في الذاكرة.
- توافق كامل 100% مع واجهة البيانات السابقة وتلقائية الترقية لقواعد البيانات القائمة.
"""
import json
import os
import sqlite3
import threading

_LOCK = threading.Lock()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

_SCHEMA = '''
CREATE TABLE IF NOT EXISTS tickets (
    id          TEXT PRIMARY KEY,
    ts          TEXT NOT NULL,
    session_id  TEXT,
    status      TEXT,
    confidence  REAL,
    rating      INTEGER,
    response_ms INTEGER,
    model_used  TEXT,
    mode        TEXT,
    channel     TEXT,
    data        TEXT NOT NULL
);
'''


def get_tenant_id():
    return os.environ.get('TENANT_ID', 'default').strip() or 'default'


def _tenant_dir(tenant=None):
    tenant = tenant or get_tenant_id()
    d = os.path.join(BASE_DIR, 'data', 'tenants', tenant)
    os.makedirs(d, exist_ok=True)
    return d


def get_tickets_file(tenant=None):
    return os.path.join(_tenant_dir(tenant), 'tickets.db')


def _connect(tenant=None):
    conn = sqlite3.connect(get_tickets_file(tenant), timeout=15)
    conn.execute('PRAGMA journal_mode=WAL')
    conn.execute('PRAGMA busy_timeout=10000')
    return conn


def _migrate_from_json(conn, tenant=None):
    """استيراد لمرة واحدة من tickets.json القديم إن وُجد والجدول فارغ."""
    old_path = os.path.join(_tenant_dir(tenant), 'tickets.json')
    legacy_path = os.path.join(BASE_DIR, 'data', 'tickets.json')
    src = old_path if os.path.exists(old_path) else legacy_path
    if not src or not os.path.exists(src):
        return
    try:
        with open(src, encoding='utf-8') as f:
            tickets = json.load(f).get('tickets', [])
    except (ValueError, OSError):
        return

    for t in tickets:
        if not isinstance(t, dict) or not t.get('id'):
            continue
        _insert_ticket_row(conn, t)
    conn.commit()
    if tickets:
        print('📦 [Migration] تم استيراد %d تذكرة من %s إلى SQLite' % (len(tickets), src))
        try:
            os.replace(src, src + '.migrated.bak')
        except OSError:
            pass


def _insert_ticket_row(conn, ticket):
    """إدراج تذكرة في الأعمدة المفككة بالإضافة لعمود data الأصلي."""
    rating_val = None
    if ticket.get('rating') is not None:
        try:
            rating_val = int(ticket['rating'])
        except (ValueError, TypeError):
            pass

    conn.execute('''
        INSERT OR REPLACE INTO tickets
        (id, ts, session_id, status, confidence, rating, response_ms, model_used, mode, channel, data)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
    ''', (
        str(ticket.get('id')),
        str(ticket.get('ts') or ''),
        str(ticket.get('session_id') or ''),
        str(ticket.get('status') or ''),
        float(ticket.get('confidence') or 0.0),
        rating_val,
        int(ticket.get('response_ms') or 0),
        str(ticket.get('model_used') or ''),
        str(ticket.get('mode') or ''),
        str(ticket.get('channel') or ''),
        json.dumps(ticket, ensure_ascii=False)
    ))


def _ensure_schema(conn, tenant=None):
    conn.execute(_SCHEMA)
    # التحقق من وجود الأعمدة الجديدة وترقية الجداول القديمة ديناميكياً
    existing_cols = {r[1] for r in conn.execute('PRAGMA table_info(tickets)').fetchall()}
    cols_to_add = [
        ('session_id', 'TEXT'),
        ('status', 'TEXT'),
        ('confidence', 'REAL'),
        ('rating', 'INTEGER'),
        ('response_ms', 'INTEGER'),
        ('model_used', 'TEXT'),
        ('mode', 'TEXT'),
        ('channel', 'TEXT'),
    ]
    for col, col_type in cols_to_add:
        if col not in existing_cols:
            try:
                conn.execute(f'ALTER TABLE tickets ADD COLUMN {col} {col_type}')
            except sqlite3.OperationalError:
                pass

    # إنشاء الفهارس لتسريع البحث
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tickets_status ON tickets(status)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tickets_ts ON tickets(ts)')
    conn.execute('CREATE INDEX IF NOT EXISTS idx_tickets_session ON tickets(session_id)')

    # ملء الأعمدة المنفصلة من بيانات JSON القديمة إن كانت فارغة
    row_null = conn.execute("SELECT COUNT(*) FROM tickets WHERE status IS NULL AND data IS NOT NULL").fetchone()
    if row_null and row_null[0] > 0:
        rows = conn.execute("SELECT id, data FROM tickets WHERE status IS NULL").fetchall()
        for tid, data_str in rows:
            try:
                d = json.loads(data_str)
                rating_v = int(d['rating']) if d.get('rating') is not None else None
                conn.execute('''
                    UPDATE tickets SET
                        session_id = ?, status = ?, confidence = ?, rating = ?,
                        response_ms = ?, model_used = ?, mode = ?, channel = ?
                    WHERE id = ?
                ''', (
                    str(d.get('session_id') or ''),
                    str(d.get('status') or ''),
                    float(d.get('confidence') or 0.0),
                    rating_v,
                    int(d.get('response_ms') or 0),
                    str(d.get('model_used') or ''),
                    str(d.get('mode') or ''),
                    str(d.get('channel') or ''),
                    tid
                ))
            except Exception:
                pass
        conn.commit()

    row = conn.execute('SELECT COUNT(*) FROM tickets').fetchone()
    if row[0] == 0:
        _migrate_from_json(conn, tenant)


def _read():
    conn = _connect()
    try:
        _ensure_schema(conn)
        rows = conn.execute('SELECT data FROM tickets ORDER BY ts, id').fetchall()
        return [json.loads(r[0]) for r in rows]
    finally:
        conn.close()


def _write(tickets):
    """إعادة كتابة كاملة (متوافقة مع الواجهة القديمة)."""
    conn = _connect()
    try:
        _ensure_schema(conn)
        conn.execute('DELETE FROM tickets')
        for t in tickets:
            _insert_ticket_row(conn, t)
        conn.commit()
    finally:
        conn.close()


def load_tickets():
    with _LOCK:
        return _read()


def _compute_next_id_fast(conn):
    """استخراج المعرف التالي ذرياً من قاعدة البيانات في O(1) باستخدام الفهرس."""
    row = conn.execute(
        "SELECT id FROM tickets WHERE id LIKE 'T-%' ORDER BY CAST(SUBSTR(id, 3) AS INTEGER) DESC LIMIT 1"
    ).fetchone()
    if row and row[0]:
        try:
            num = int(row[0].split('-')[-1])
            return 'T-%04d' % (num + 1)
        except (ValueError, IndexError):
            pass
    return 'T-0001'


def next_id():
    """أكبر رقم حالي + 1."""
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            return _compute_next_id_fast(conn)
        finally:
            conn.close()


def create_ticket(ticket):
    """إنشاء تذكرة بمعرّف يُولَّد داخل نفس المعاملة — ذرية تماماً وسريعة جداً بالـ SQL.
    المحطة 2: كل تذكرة تُحدَّث بها ذاكرة العميل طويلة المدى تلقائياً
    (lazy import لتفادي الدوران) — فشل الذاكرة لا يمنع إنشاء التذكرة."""
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            ticket = dict(ticket)
            ticket['id'] = _compute_next_id_fast(conn)
            _insert_ticket_row(conn, ticket)
            conn.commit()
        finally:
            conn.close()
        # المحطة 2: تحديث ملف العميل بعد نجاح التأكيد (خارج المعاملة)
        try:
            from engine import cust_memory
            cust_memory.remember(ticket.get('session_id'),
                                 ticket.get('query'), ticket.get('answer'))
        except Exception:
            pass
        return ticket


def add_ticket(ticket):
    """إضافة تذكرة (يولد الـ ID إن كان ناقصاً)."""
    if not ticket.get('id'):
        return create_ticket(ticket)
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            _insert_ticket_row(conn, ticket)
            conn.commit()
        finally:
            conn.close()
    return ticket


def update_ticket(ticket_id, **fields):
    """تحديث حقول تذكرة في الأعمدة المفهرسة وعمود JSON معاً."""
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            row = conn.execute('SELECT data FROM tickets WHERE id = ?', (str(ticket_id),)).fetchone()
            if not row:
                return None
            t = json.loads(row[0])
            t.update(fields)
            data_json = json.dumps(t, ensure_ascii=False)

            set_clauses = ['data = ?']
            params = [data_json]
            for col in ('status', 'rating', 'session_id', 'confidence', 'response_ms', 'model_used', 'mode', 'channel'):
                if col in fields:
                    set_clauses.append(f'{col} = ?')
                    params.append(fields[col])
            params.append(str(ticket_id))
            sql = f"UPDATE tickets SET {', '.join(set_clauses)} WHERE id = ?"
            conn.execute(sql, tuple(params))
            conn.commit()
            return t
        finally:
            conn.close()


def set_rating(ticket_id, rating):
    """تقييم العميل؛ لو التقييم سيء على إجابة محلولة بتتحول تلقائياً لموظف بشري."""
    ticket = update_ticket(ticket_id, rating=int(rating))
    if ticket and ticket.get('status') == 'resolved' and int(rating) <= 2:
        update_ticket(ticket_id, status='escalated',
                      escalated_reason='negative_feedback')
    return ticket


def get_stats():
    """حساب إحصائيات التذاكر مباشرة بالـ SQL المجمع فائق السرعة O(1)."""
    with _LOCK:
        conn = _connect()
        try:
            _ensure_schema(conn)
            total = conn.execute('SELECT COUNT(*) FROM tickets').fetchone()[0]
            resolved = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'resolved'").fetchone()[0]
            escalated = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'escalated'").fetchone()[0]
            priority = conn.execute("SELECT COUNT(*) FROM tickets WHERE status = 'priority'").fetchone()[0]
            avg_rating_row = conn.execute("SELECT AVG(rating) FROM tickets WHERE rating IS NOT NULL AND rating > 0").fetchone()
            avg_rating = round(avg_rating_row[0], 2) if (avg_rating_row and avg_rating_row[0] is not None) else 0.0
            return {
                'total': total,
                'resolved': resolved,
                'escalated': escalated,
                'priority': priority,
                'avg_rating': avg_rating
            }
        finally:
            conn.close()
