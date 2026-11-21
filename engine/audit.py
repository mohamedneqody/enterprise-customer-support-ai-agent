# -*- coding: utf-8 -*-
"""
engine/audit.py — سجل تدقيق دائم (JSONL) لأحداث النظام القابلة للمساءلة.
يُسجل لكل مستأجر في data/tenants/{tenant}/audit_log.jsonl:
- تدخلات الـ Guardrail (شطب مهلة مخترعة، تصحيح موافقة مخالفة للسياسة...).
- إنشاء التذاكر والحالات الحرجة (تصعيد/أولوية).
الملف بصيغة سطور JSON (JSONL) سهل الأرشفة والاستعلام.
"""
import json
import os
import threading
from datetime import datetime

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
_LOCK = threading.Lock()


def _log_path(tenant=None):
    tenant = tenant or os.environ.get('TENANT_ID', 'default').strip() or 'default'
    d = os.path.join(BASE_DIR, 'data', 'tenants', tenant)
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, 'audit_log.jsonl')


def log_event(event, tenant=None):
    """إلحاق حدث واحد بالسجل؛ لا يفشل العمل أبداً لو تعذر الكتابة."""
    try:
        entry = {'ts': datetime.now().isoformat(timespec='seconds')}
        entry.update(event)
        line = json.dumps(entry, ensure_ascii=False)
        with _LOCK:
            with open(_log_path(tenant), 'a', encoding='utf-8') as f:
                f.write(line + '\n')
    except Exception:
        pass


def log_guardrail(ticket_id, query, notes, tenant=None):
    if not notes:
        return
    log_event({
        'type': 'guardrail_intervention',
        'ticket_id': ticket_id,
        'query': (query or '')[:200],
        'notes': notes,
    }, tenant=tenant)


def log_ticket(ticket):
    log_event({
        'type': 'ticket_created',
        'ticket_id': ticket.get('id'),
        'status': ticket.get('status'),
        'confidence': ticket.get('confidence'),
        'escalated_reason': ticket.get('escalated_reason'),
        'model_used': ticket.get('model_used'),
        'channel': ticket.get('channel'),
    }, tenant=ticket.get('tenant'))
