# -*- coding: utf-8 -*-
"""
engine/telegram_bot.py — استقبال وإرسال رسائل Telegram Bot عبر Webhook و Telegram Bot API.
يستخدم حصرياً مكتبة Python القياسية (urllib + json).
"""
import json
import os
import sys
import urllib.error
import urllib.request

from engine import pipeline

TELEGRAM_BOT_TOKEN = os.environ.get('TELEGRAM_BOT_TOKEN', '').strip()
TELEGRAM_WEBHOOK_SECRET = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '').strip()


def get_token():
    return os.environ.get('TELEGRAM_BOT_TOKEN', TELEGRAM_BOT_TOKEN).strip()


def is_configured():
    return bool(get_token())


def send_message(chat_id, text, token=None):
    """إرسال رسالة رد للعميل على Telegram عبر sendMessage API."""
    bot_token = token or get_token()
    if not bot_token:
        return False, 'token_missing'
    if not chat_id or not text:
        return False, 'chat_id_and_text_required'

    url = 'https://api.telegram.org/bot%s/sendMessage' % bot_token
    payload = {
        'chat_id': chat_id,
        'text': text,
    }

    try:
        data = json.dumps(payload).encode('utf-8')
        req = urllib.request.Request(
            url,
            data=data,
            headers={'Content-Type': 'application/json'}
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            body = resp.read().decode('utf-8')
            res = json.loads(body)
            return bool(res.get('ok')), res
    except Exception as e:
        print('[Telegram Error] Failed to send message to chat %s: %s' % (chat_id, e), file=sys.stderr)
        return False, str(e)


def extract_update_details(update_json):
    """استخراج chat_id والنص ومعلومات المستخدم من كائن Telegram Update."""
    if not isinstance(update_json, dict):
        return None, None, None

    msg = (
        update_json.get('message')
        or update_json.get('edited_message')
        or update_json.get('channel_post')
        or {}
    )

    chat = msg.get('chat', {})
    chat_id = chat.get('id')
    text = (msg.get('text') or msg.get('caption') or '').strip()
    from_user = msg.get('from', {})

    return chat_id, text, from_user


def handle_update(update_json):
    """معالجة التحديث القادم من Telegram وتمريره لمحرك الدعم وإرسال الرد."""
    chat_id, text, from_user = extract_update_details(update_json)
    if not chat_id:
        return {'ok': True, 'action': 'ignored_no_chat_id'}

    # لو رسالة بدون نص (ستيكر أو صورة من غير كابشن)
    if not text:
        default_reply = 'أهلاً بك في متجر النور! يرجى كتابة استفسارك نصياً لنتمكن من مساعدتك.'
        send_message(chat_id, default_reply)
        return {'ok': True, 'action': 'replied_empty_text'}

    # تحويل chat_id لـ session_id مع prefix tg_
    session_id = 'tg_%s' % chat_id

    # تمرير الرسالة لنفس الـ pipeline الحقيقي
    ticket = pipeline.handle_message(text, session_id=session_id)
    answer = ticket.get('answer', '')

    if answer:
        send_success, send_info = send_message(chat_id, answer)
    else:
        send_success, send_info = False, 'empty_answer'

    return {
        'ok': True,
        'chat_id': chat_id,
        'ticket_id': ticket.get('id'),
        'status': ticket.get('status'),
        'sent': send_success,
        'send_info': send_info
    }
