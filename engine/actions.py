# -*- coding: utf-8 -*-
"""
engine/actions.py — المحطة الخامسة: العقود البرمجية والاستمارات الإجبارية (Structured JSON Outputs & Webhook).

يستخرج نوايا الإجراءات الحقيقية فقط (حجز، طلب شراء، تسجيل عميل محتمل، طلب مكالمة، شكوى رسمية)
في قالب JSON صارم ومُتحقَّق منه برمجياً (Schema-Validated) لتمريره إلى قاعدة البيانات أو
Webhook خارجي (n8n / CRM / ERP) بدون أخطاء وبدون اختراع أي بيانات تتبع أو شحن وهمية.

ملاحظة تصميمية:
- لا يعمل على أسئلة المعلومات العادية (FAQ / الأسعار) للحفاظ على سرعة 0ms.
- يُستدعى فقط عند رصد مؤشر إجراء صريح، وبشكل خلفي غير حاجب عند إرسال الـ Webhook.
"""
import json
import os
import re
import threading
import urllib.request

from engine import audit, store

VALID_ACTIONS = (
    'booking',
    'order_request',
    'lead_capture',
    'callback_request',
    'complaint_escalation',
    'none',
)

_ACTION_TRIGGERS = {
    'booking': re.compile(r'(?:احجز|أحجز|عايز\s+احجز|حجز\s+(?:موعد|كشف|جلسة|غرفة|سيارة)|book\s+(?:an?\s+)?(?:appointment|slot|room)|reserve)', re.IGNORECASE),
    'order_request': re.compile(r'(?:عايز\s+اطلب|عاوز\s+اطلب|اعمل\s+اوردر|أعمل\s+أوردر|اطلب\s+الان|اطلب\s+الآن|أطلب\s+منكم|سجل\s+طلب|place\s+an?\s+order|i\s+want\s+to\s+order)', re.IGNORECASE),
    'callback_request': re.compile(r'(?:كلموني|اتصلوا\s+بيا|اتصل\s+بي|تواصلوا\s+معي|عايز\s+حد\s+يكلمني|call\s+me\s+back|contact\s+me)', re.IGNORECASE),
    'complaint_escalation': re.compile(r'(?:عايز\s+اقدم\s+شكوى|شكوى\s+رسمية|رفع\s+شكوى|تقديم\s+شكوى|file\s+a\s+complaint|formal\s+complaint)', re.IGNORECASE),
    'lead_capture': re.compile(r'(?:عرض\s+سعر\s+لشركت|سجل\s+بياناتي|اسمي\s+.*\s+ورقمي|مهتم\s+بالاشتراك|request\s+a\s+quote)', re.IGNORECASE),
}

_PHONE_RE = re.compile(r'(?:\+?\d{1,3}[\s\-]?)?(01[0125]\d{8}|05\d{8})')
_QTY_RE = re.compile(r'(?:عدد|كمية|كميه|qty|quantity)\s*[:=]?\s*(\d{1,6})|(\d{1,5})\s*(?:قطعة|قطعه|نسخة|نسخه|علبة|علبه|كارت|كرتونة|بوكس|متر|أيام|ايام)', re.IGNORECASE)


def is_enabled():
    return os.environ.get('ENABLE_ACTION_EXTRACTOR', 'true').strip().lower() in ('true', '1', 'yes')


def detect_action_intent(query):
    """فحص حتمي سريع (0ms): هل رسالة العميل تطلب إجراءً عملياً يستحق استمارة JSON؟"""
    if not query:
        return 'none'
    for act, pat in _ACTION_TRIGGERS.items():
        if pat.search(query):
            return act
    return 'none'


def validate_action_payload(raw):
    """عقد برمجي صارم (Schema Validation): يضمن أنواع الحقول ويمنع أي حقل عشوائي."""
    if not isinstance(raw, dict):
        return None
    action = str(raw.get('action') or 'none').strip().lower()
    if action not in VALID_ACTIONS:
        action = 'none'

    def _clean_str(val, max_len=120):
        if val is None:
            return None
        s = str(val).strip()
        if not s or s.lower() in ('null', 'none', 'n/a', 'غير محدد'):
            return None
        return s[:max_len]

    qty = raw.get('quantity')
    try:
        qty = int(qty) if qty is not None and str(qty).strip() != '' else None
        if qty is not None and qty <= 0:
            qty = None
    except (TypeError, ValueError):
        qty = None

    try:
        conf = round(max(0.0, min(1.0, float(raw.get('confidence', 0.0)))), 2)
    except (TypeError, ValueError):
        conf = 0.0

    return {
        'action': action,
        'customer_name': _clean_str(raw.get('customer_name'), 60),
        'phone': _clean_str(raw.get('phone'), 25),
        'item_or_service': _clean_str(raw.get('item_or_service'), 100),
        'quantity': qty,
        'preferred_time': _clean_str(raw.get('preferred_time'), 60),
        'notes': _clean_str(raw.get('notes'), 200),
        'confidence': conf,
    }


def extract_action_deterministic(query, customer_facts=None):
    """استخراج استمارة الإجراء حتمياً بدون استدعاء LLM."""
    act = detect_action_intent(query)
    if act == 'none':
        return None
    facts = customer_facts or {}
    m_phone = _PHONE_RE.search(query or '')
    m_qty = _QTY_RE.search(query or '')
    qty = None
    if m_qty:
        qty = m_qty.group(1) or m_qty.group(2)

    return validate_action_payload({
        'action': act,
        'customer_name': facts.get('name'),
        'phone': m_phone.group(0).strip() if m_phone else facts.get('phone'),
        'item_or_service': None,
        'quantity': qty,
        'preferred_time': None,
        'notes': (query or '').strip()[:200],
        'confidence': 0.85,
    })


def extract_action_structured(query, *, customer_facts=None, use_llm=False):
    """استخراج استمارة JSON صارمة (حتمي أولاً + إثراء اختياري بـ JSON Mode للـ LLM)."""
    if not is_enabled():
        return None
    base = extract_action_deterministic(query, customer_facts=customer_facts)
    if not base:
        return None
    if not use_llm:
        return base

    try:
        from engine import providers
        prompt = (
            'حوّل رسالة العميل التالية إلى استمارة JSON صارمة لهذا العقد البرمجي فقط بدون أي نص إضافي:\n'
            '{"action": "booking|order_request|lead_capture|callback_request|complaint_escalation|none", '
            '"customer_name": null, "phone": null, "item_or_service": null, "quantity": null, '
            '"preferred_time": null, "notes": null, "confidence": 0.9}\n'
            'استخرج فقط ما ذُكر صراحة في الرسالة ولا تخترع أي حقل.\n'
            f'<user_message>{query[:500]}</user_message>'
        )
        res = providers.complete(prompt, tier='fast', temperature=0.0, max_tokens=220, json_mode=True, timeout=15.0)
        raw = res.get('text', '')
        m = re.search(r'\{.*\}', raw, re.DOTALL)
        if m:
            parsed = validate_action_payload(json.loads(m.group(0)))
            if parsed and parsed['action'] != 'none':
                # الاحتفاظ بالقيم الحتمية المؤكدة إن أهملها النموذج
                for k in ('customer_name', 'phone', 'quantity'):
                    if not parsed.get(k) and base.get(k):
                        parsed[k] = base[k]
                return parsed
    except Exception:
        pass
    return base


def dispatch_webhook(ticket_id, session_id, payload):
    """إرسال استمارة الإجراء إلى ACTION_WEBHOOK_URL (مثلاً n8n / CRM) بأمان."""
    url = os.environ.get('ACTION_WEBHOOK_URL', '').strip()
    if not url or not payload or payload.get('action') == 'none':
        return False
    body = {
        'event': 'customer_action_extracted',
        'tenant_id': os.environ.get('TENANT_ID', 'default').strip() or 'default',
        'ticket_id': ticket_id,
        'session_id': session_id,
        'action': payload,
    }
    headers = {'Content-Type': 'application/json'}
    secret = os.environ.get('ACTION_WEBHOOK_SECRET', '').strip()
    if secret:
        headers['X-Action-Secret'] = secret
    try:
        req = urllib.request.Request(url, data=json.dumps(body, ensure_ascii=False).encode('utf-8'), headers=headers)
        with urllib.request.urlopen(req, timeout=10.0) as resp:
            ok = 200 <= resp.status < 300
            audit.log_event({'type': 'action_webhook', 'ticket_id': ticket_id, 'action': payload['action'], 'ok': ok})
            return ok
    except Exception as e:
        audit.log_event({'type': 'action_webhook_error', 'ticket_id': ticket_id, 'error': str(e)[:200]})
        return False


def process_async(ticket_id, session_id, query, *, use_llm=False):
    """تشغيل استخراج الإجراء + تحديث التذكرة + إرسال Webhook في خيط خلفي بلا أي تأخير على العميل."""
    if not is_enabled() or detect_action_intent(query) == 'none':
        return

    def _worker():
        try:
            from engine import cust_memory
            facts = (cust_memory.get_profile(session_id) or {}).get('facts') or {}
            payload = extract_action_structured(query, customer_facts=facts, use_llm=use_llm)
            if not payload or payload.get('action') == 'none':
                return
            store.update_ticket(ticket_id, action_payload=payload)
            audit.log_event({'type': 'action_extracted', 'ticket_id': ticket_id, 'payload': payload})
            dispatch_webhook(ticket_id, session_id, payload)
        except Exception:
            pass

    threading.Thread(target=_worker, daemon=True).start()
