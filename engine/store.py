# -*- coding: utf-8 -*-
"""تخزين التذاكر في JSON (بدون قاعدة بيانات) مع قفل للخيوط المتعددة."""
import json
import os
import threading

_LOCK = threading.Lock()
BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_tenant_id():
    return os.environ.get('TENANT_ID', 'default').strip() or 'default'


def get_tickets_file():
    tenant = get_tenant_id()
    tenant_dir = os.path.join(BASE_DIR, 'data', 'tenants', tenant)
    os.makedirs(tenant_dir, exist_ok=True)
    new_path = os.path.join(tenant_dir, 'tickets.json')
    old_path = os.path.join(BASE_DIR, 'data', 'tickets.json')
    if tenant == 'default' and os.path.exists(old_path) and not os.path.exists(new_path):
        try:
            os.replace(old_path, new_path)
            print('📦 [Migration] تم نقل ملف التذاكر من %s إلى %s' % (old_path, new_path))
        except OSError:
            pass
    return new_path


def _read():
    tickets_file = get_tickets_file()
    if not os.path.exists(tickets_file):
        return []
    try:
        with open(tickets_file, encoding='utf-8') as f:
            data = json.load(f)
        return data.get('tickets', [])
    except (ValueError, OSError):
        return []


def _write(tickets):
    tickets_file = get_tickets_file()
    os.makedirs(os.path.dirname(tickets_file), exist_ok=True)
    tmp = tickets_file + '.tmp'
    with open(tmp, 'w', encoding='utf-8') as f:
        json.dump({'tickets': tickets}, f, ensure_ascii=False, indent=2)
    for _ in range(5):
        try:
            os.replace(tmp, tickets_file)
            return
        except OSError:
            import time
            time.sleep(0.05)
    try:
        with open(tickets_file, 'w', encoding='utf-8') as f:
            json.dump({'tickets': tickets}, f, ensure_ascii=False, indent=2)
        if os.path.exists(tmp):
            os.remove(tmp)
    except Exception:
        pass


def load_tickets():
    with _LOCK:
        return _read()


def next_id():
    with _LOCK:
        tickets = _read()
        biggest = 0
        for t in tickets:
            try:
                biggest = max(biggest, int(str(t.get('id', 'T-0')).split('-')[-1]))
            except ValueError:
                pass
        return 'T-%04d' % (biggest + 1)


def add_ticket(ticket):
    with _LOCK:
        tickets = _read()
        tickets.append(ticket)
        _write(tickets)


def update_ticket(ticket_id, **fields):
    with _LOCK:
        tickets = _read()
        for t in tickets:
            if t.get('id') == ticket_id:
                t.update(fields)
                _write(tickets)
                return t
        return None


def set_rating(ticket_id, rating):
    """تقييم العميل؛ لو التقييم سيء على إجابة محلولة بتتحول تلقائياً لموظف بشري."""
    ticket = update_ticket(ticket_id, rating=int(rating))
    if ticket and ticket.get('status') == 'resolved' and int(rating) <= 2:
        update_ticket(ticket_id, status='escalated',
                      escalated_reason='negative_feedback')
    return ticket
