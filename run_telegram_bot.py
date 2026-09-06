# -*- coding: utf-8 -*-
"""
run_telegram_bot.py - تشغيل بوت تيليجرام بنظام الـ Long Polling للاختبار الفوري والتصوير
يعمل محلياً بدون الحاجة لـ ngrok أو دومين خارجي!

التشغيل:
    python run_telegram_bot.py
"""
import os
import sys
import time
import json
import urllib.request
import urllib.error

# تحميل .env تلقائياً
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
env_path = os.path.join(BASE_DIR, '.env')
if os.path.isfile(env_path):
    try:
        with open(env_path, 'r', encoding='utf-8') as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith('#') and '=' in line:
                    k, v = line.split('=', 1)
                    k = k.strip()
                    v = v.strip().strip('\'"')
                    if k and k not in os.environ:
                        os.environ[k] = v
    except Exception:
        pass

from engine import telegram_bot

try:
    sys.stdout.reconfigure(encoding='utf-8')
except Exception:
    pass

def main():
    token = telegram_bot.get_token()
    if not token:
        print('[خطأ] لم يتم العثور على TELEGRAM_BOT_TOKEN في ملف .env أو متغيرات البيئة!')
        sys.exit(1)

    print('=' * 65)
    print('  🤖 جارِ الاتصال وتفعيل بوت تيليجرام عبر Long Polling...')
    print('=' * 65)

    # فحص بيانات البوت
    try:
        me_url = f'https://api.telegram.org/bot{token}/getMe'
        with urllib.request.urlopen(me_url, timeout=10) as resp:
            me_data = json.loads(resp.read().decode('utf-8'))
            if me_data.get('ok'):
                bot_info = me_data['result']
                print(f"  ✅ اسم البوت: {bot_info.get('first_name')}")
                print(f"  ✅ يوزر البوت: @{bot_info.get('username')}")
                print(f"  👉 افتح تيليجرام وابحث عن @{bot_info.get('username')} وابدأ الدردشة فوراً!")
            else:
                print(f"  ⚠️ تحذير: استجابة غير متوقعة من تيليجرام: {me_data}")
    except Exception as e:
        print(f"  [خطأ أثناء فحص البوت]: {e}")

    # مسح أي webhook قديم حتى يعمل الـ polling بنجاح
    try:
        del_webhook_url = f'https://api.telegram.org/bot{token}/deleteWebhook?drop_pending_updates=False'
        with urllib.request.urlopen(del_webhook_url, timeout=10) as resp:
            pass
    except Exception:
        pass

    print('\n  🟢 البوت يعمل الآن ويستمع لرسائلك في الوقت الفعلي...')
    print('  (اضغط Ctrl+C لإيقاف البوت في أي وقت)\n' + '-' * 65)

    offset = 0
    while True:
        try:
            poll_url = f'https://api.telegram.org/bot{token}/getUpdates?offset={offset}&timeout=20'
            req = urllib.request.Request(poll_url)
            with urllib.request.urlopen(req, timeout=30) as resp:
                data = json.loads(resp.read().decode('utf-8'))
                if not data.get('ok'):
                    time.sleep(2)
                    continue

                for update in data.get('result', []):
                    offset = max(offset, update['update_id'] + 1)
                    chat_id, text, from_u = telegram_bot.extract_update_details(update)
                    user_name = from_u.get('first_name') or from_u.get('username') or 'مستخدم'
                    
                    if text:
                        print(f"📩 رسالة جديدة من [{user_name} - ID:{chat_id}]: {text}")
                        # معالجة الرسالة وإرسال الرد
                        res = telegram_bot.handle_update(update)
                        print(f"  ↳ ✅ تم الرد بنجاح!")
                    
        except KeyboardInterrupt:
            print('\n🛑 تم إيقاف البوت بنجاح.')
            break
        except Exception as e:
            time.sleep(2)

if __name__ == '__main__':
    main()
