# -*- coding: utf-8 -*-
"""
engine/whatsapp_bot.py — قناة WhatsApp Business عبر WhatsApp Cloud API (Meta).
يستخدم حصرياً مكتبة Python القياسية (urllib + json) بنفس نمط telegram_bot.

الإعداد في .env:
    WHATSAPP_TOKEN=<Access Token من Meta Developer Console>
    WHATSAPP_PHONE_NUMBER_ID=<Phone Number ID>
    WHATSAPP_VERIFY_TOKEN=<أي نص سرّي تكتبه أيضاً في إعدادات Webhook عند Meta>

المسارات في app.py:
    GET  /api/webhook/whatsapp  ← مصادقة الـ Webhook (hub.challenge)
    POST /api/webhook/whatsapp  ← استقبال رسائل العملاء
"""
import json
import os
import sys
import urllib.request

from engine import pipeline

GRAPH_API = 'https://graph.facebook.com/v18.0'


def get_token():
    return os.environ.get('WHATSAPP_TOKEN', '').strip()


def get_phone_number_id():
    return os.environ.get('WHATSAPP_PHONE_NUMBER_ID', '').strip()


def get_verify_token():
    return os.environ.get('WHATSAPP_VERIFY_TOKEN', '').strip()


def is_configured():
    return bool(get_token() and get_phone_number_id())


def verify_webhook(query_params):
    """مصادقة تسجيل الـ Webhook عند Meta: يجب إرجاع hub.challenge حرفياً."""
    if not get_verify_token():
        return None
    if query_params.get('hub.mode') == 'subscribe' and \
            query_params.get('hub.verify_token') == get_verify_token():
        return query_params.get('hub.challenge')
    return None


def send_message(to, text):
    """إرسال رسالة نصية للعميل عبر Cloud API — النصوص الطويلة تُقسّم لرسائل
    متتالية (حد واتساب ~4096 حرفاً) بدل قصّها بصمت وضياع باقي الإجابة."""
    token, phone_id = get_token(), get_phone_number_id()
    if not (token and phone_id):
        return False, 'whatsapp_not_configured'
    if not to or not text:
        return False, 'to_and_text_required'

    chunks = [text[i:i + 4000] for i in range(0, len(text), 4000)] or [text]
    url = '%s/%s/messages' % (GRAPH_API, phone_id)
    last_result, last_info = False, 'no_chunks'
    for idx, chunk in enumerate(chunks, 1):
        body = chunk if len(chunks) == 1 else '%s\n(%d/%d)' % (chunk, idx, len(chunks))
        payload = {
            'messaging_product': 'whatsapp',
            'to': str(to),
            'type': 'text',
            'text': {'body': body},
        }
        try:
            data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
            req = urllib.request.Request(url, data=data, headers={
                'Content-Type': 'application/json',
                'Authorization': 'Bearer %s' % token,
            })
            with urllib.request.urlopen(req, timeout=15) as resp:
                body_json = json.loads(resp.read().decode('utf-8'))
                last_result, last_info = bool(body_json.get('messages')), body_json
        except Exception as e:
            print('[WhatsApp Error] send to %s: %s' % (to, e), file=sys.stderr)
            return False, str(e)
    return last_result, last_info


def verify_signature(payload_bytes, signature_header):
    """تحقق اختياري من توقيع X-Hub-Signature-256 (طبقة ضد انتحال الطلبات).
    يُفعَّل تلقائياً عند ضبط WHATSAPP_APP_SECRET في .env."""
    import hashlib
    import hmac as hmac_mod
    app_secret = os.environ.get('WHATSAPP_APP_SECRET', '').strip()
    if not app_secret:
        return True  # غير مفعل — لا نرفض الطلبات
    if not signature_header or not signature_header.startswith('sha256='):
        return False
    expected = hmac_mod.new(app_secret.encode('utf-8'), payload_bytes or b'',
                            hashlib.sha256).hexdigest()
    return hmac_mod.compare_digest(signature_header[7:], expected)


def extract_update_details(payload):
    """استخراج (wa_id، النص، الاسم) من كائن Webhook Payload الخاص بـ Meta."""
    msg = _first_message(payload)
    if not msg:
        return None, None, None
    wa_id = msg.get('from')
    text = (msg.get('text') or {}).get('body', '').strip() if msg.get('type') == 'text' else ''
    return wa_id, text, _contact_name(payload)


def _first_message(payload):
    """أول رسالة فعلية من العميل (تجاهل تحديثات الحالة delivery/read)."""
    if not isinstance(payload, dict):
        return None
    try:
        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                value = change.get('value', {})
                messages = value.get('messages') or []
                if messages:
                    return messages[0]
    except (AttributeError, TypeError, IndexError):
        pass
    return None


def _contact_name(payload):
    try:
        for entry in payload.get('entry', []):
            for change in entry.get('changes', []):
                contacts = change.get('value', {}).get('contacts') or []
                if contacts:
                    return (contacts[0].get('profile') or {}).get('name', '')
    except (AttributeError, TypeError, IndexError):
        pass
    return ''


def handle_update(payload):
    """معالجة رسالة واتساب وتمريرها لنفس محرك الدعم وإرسال الرد."""
    wa_id, text, name = extract_update_details(payload)
    if not wa_id:
        return {'ok': True, 'action': 'ignored_no_message'}

    msg = _first_message(payload)
    # رسائل الوسائط (صور/صوت/موقع): لا تُتجاهل بصمت — نطلب النص بلطف
    if msg and msg.get('type') not in (None, 'text'):
        store_name = os.environ.get('STORE_NAME', '').strip() or 'الدعم'
        send_message(wa_id, 'أهلاً%s في %s 👋\nمعرفش أتعامل مع المرفقات ده حالياً — من فضلك اكتب استفسارك نصياً وهساعدك فوراً.' %
                     ((' يا ' + name) if name else '', store_name))
        return {'ok': True, 'action': 'replied_media_not_supported', 'type': msg.get('type')}

    if not text:
        store_name = os.environ.get('STORE_NAME', '').strip() or 'الدعم'
        send_message(wa_id, 'أهلاً%s في %s! من فضلك اكتب استفسارك نصياً لنتمكن من مساعدتك.' %
                     ((' يا ' + name) if name else '', store_name))
        return {'ok': True, 'action': 'replied_empty_text'}

    # نفس الـ pipeline الحقيقي — جلسة معزولة لكل رقم واتساب
    session_id = 'wa_%s' % wa_id
    ticket = pipeline.handle_message(text, session_id=session_id)
    answer = ticket.get('answer', '')

    if answer:
        send_success, send_info = send_message(wa_id, answer)
    else:
        send_success, send_info = False, 'empty_answer'

    return {
        'ok': True,
        'wa_id': wa_id,
        'ticket_id': ticket.get('id'),
        'status': ticket.get('status'),
        'sent': send_success,
        'send_info': send_info,
    }
