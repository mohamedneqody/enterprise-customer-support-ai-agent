# -*- coding: utf-8 -*-
"""
سيرفر الدعم الذكي - Python خالص بدون أي مكتبات خارجية.

التشغيل:   python app.py            (البورت الافتراضي 9090)
           python app.py 8000       (بورت مخصص)

الواجهات:
  GET  /            واجهة الشات مع العميل
  GET  /dashboard   لوحة التحكم (Resolved/Escalated/Response Time/Satisfaction)
  POST /api/chat    معالجة رسالة عميل  {message, session_id}
  POST /api/feedback تقييم الإجابة     {ticket_id, rating: 1..5}
  GET  /api/stats   إحصائيات الداشبورد
  GET  /api/tickets آخر التذاكر        (?limit=50)
  GET  /api/kb      ملفات قاعدة المعرفة
  POST /api/kb/reload  إعادة فهرسة المعرفة بعد إضافة/تعديل ملفات
"""
import email
import email.policy
import json
import os
import re
import secrets
import sys
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import train
from engine import pipeline, store, telegram_bot

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, 'static')

# تحميل ملف .env تلقائياً إذا كان موجوداً
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

ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN')
IS_GENERATED_TOKEN = False
if not ADMIN_TOKEN:
    ADMIN_TOKEN = secrets.token_urlsafe(32)
    IS_GENERATED_TOKEN = True

TENANT_ID = os.environ.get('TENANT_ID', 'default').strip() or 'default'


class Handler(BaseHTTPRequestHandler):
    server_version = 'SupportAgent/1.0'

    # ---------------- helpers ----------------

    def _check_auth(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return False
        token = auth[7:].strip()
        return bool(token and token == ADMIN_TOKEN)

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _file(self, name, ctype):
        full = os.path.join(STATIC_DIR, name)
        if not os.path.isfile(full):
            self._json({'error': 'not_found'}, 404)
            return
        with open(full, 'rb') as f:
            body = f.read()
        self.send_response(200)
        self.send_header('Content-Type', ctype)
        self.send_header('Content-Length', str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self):
        try:
            length = int(self.headers.get('Content-Length') or 0)
            raw = self.rfile.read(length) if length else b'{}'
            return json.loads(raw.decode('utf-8')), None
        except (ValueError, UnicodeDecodeError):
            return None, 'invalid_json'

    def log_message(self, fmt, *args):
        pass  # سكوت في الكونسول

    # ---------------- routes ----------------

    def do_GET(self):
        try:
            self._do_GET_impl()
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # Client disconnected
        except Exception as e:
            print(f"[Error in GET {self.path}]: {e}", file=sys.stderr)
            try: self._json({"error": "server_error"}, 500)
            except: pass

    def do_POST(self):
        try:
            self._do_POST_impl()
        except (ConnectionAbortedError, BrokenPipeError):
            pass  # Client disconnected
        except Exception as e:
            print(f"[Error in POST {self.path}]: {e}", file=sys.stderr)
            try: self._json({"error": "server_error"}, 500)
            except: pass

    def _do_GET_impl(self):
        path = urlsplit(self.path).path
        qs = parse_qs(urlsplit(self.path).query)
        if path in ('/', '/chat', '/index.html'):
            self._file('chat.html', 'text/html; charset=utf-8')
        elif path in ('/dashboard', '/dashboard.html'):
            self._file('dashboard.html', 'text/html; charset=utf-8')
        elif path == '/api/stats':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            self._json(pipeline.compute_stats())
        elif path == '/api/tickets':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            try:
                limit = int(qs.get('limit', ['50'])[0])
            except ValueError:
                limit = 50
            tickets = list(reversed(store.load_tickets()))
            self._json({'tickets': tickets[:limit]})
        elif path == '/api/kb':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            idx = pipeline.get_index()
            self._json({'sources': idx.sources(), 'total_chunks': len(idx.chunks)})
        else:
            self._json({'error': 'not_found'}, 404)

    def _do_POST_impl(self):
        parsed_url = urlsplit(self.path)
        path = parsed_url.path
        qs = parse_qs(parsed_url.query)
        if path == '/api/kb/upload':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            upload_mode = qs.get('mode', ['append'])[0].lower()
            content_type = self.headers.get('Content-Type', '')
            if 'multipart/form-data' not in content_type:
                self._json({'error': 'multipart_form_data_required'}, 400)
                return
            length = int(self.headers.get('Content-Length', 0))
            if length <= 0 or length > 50 * 1024 * 1024:
                self._json({'error': 'invalid_file_size'}, 400)
                return
            raw_body = self.rfile.read(length)
            msg = email.message_from_bytes(
                b'Content-Type: ' + content_type.encode('utf-8') + b'\r\n\r\n' + raw_body,
                policy=email.policy.default
            )
            attachments = []
            for part in msg.walk():
                fn = part.get_filename()
                if fn:
                    ext = os.path.splitext(fn)[1].lower()
                    if ext not in train.SUPPORTED:
                        self._json({
                            'error': 'unsupported_file_format',
                            'file': fn,
                            'message': 'صيغة الملف غير مدعومة: %s. تم إلغاء رفع الحزمة بالكامل.' % (ext or 'بدون امتداد')
                        }, 400)
                        return
                    attachments.append((fn, part.get_payload(decode=True)))

            if not attachments:
                self._json({'error': 'no_valid_file_found'}, 400)
                return

            saved_files = []
            tmp_dir = os.path.join(BASE_DIR, 'data', 'temp_uploads')
            os.makedirs(tmp_dir, exist_ok=True)
            
            kb_dir = pipeline.get_kb_dir()
            staging_dir = os.path.join(BASE_DIR, 'data', 'knowledge_base_staging')
            
            if upload_mode == 'replace':
                import shutil
                if os.path.exists(staging_dir):
                    shutil.rmtree(staging_dir, ignore_errors=True)
                os.makedirs(staging_dir, exist_ok=True)
                target_out_dir = staging_dir
            else:
                target_out_dir = kb_dir
            
            created_md_files = []

            try:
                for fn, payload in attachments:
                    tmp_path = os.path.join(tmp_dir, fn)
                    with open(tmp_path, 'wb') as f:
                        f.write(payload)
                    try:
                        out_path, count = train.process_file(tmp_path, out_dir=target_out_dir)
                        created_md_files.append(out_path)
                        saved_files.append({'file': fn, 'sections': count})
                    finally:
                        if os.path.exists(tmp_path):
                            try:
                                os.remove(tmp_path)
                            except OSError:
                                pass
            except Exception as e:
                import shutil
                if upload_mode == 'replace':
                    if os.path.exists(staging_dir):
                        shutil.rmtree(staging_dir, ignore_errors=True)
                else:
                    for md_file in created_md_files:
                        if os.path.exists(md_file):
                            try:
                                os.remove(md_file)
                            except OSError:
                                pass
                self._json({'error': 'processing_failed', 'detail': str(e)}, 500)
                return

            if upload_mode == 'replace':
                import shutil
                for old_f in os.listdir(kb_dir):
                    old_path = os.path.join(kb_dir, old_f)
                    if os.path.isfile(old_path):
                        try:
                            os.remove(old_path)
                        except OSError:
                            pass
                
                for st_f in os.listdir(staging_dir):
                    st_path = os.path.join(staging_dir, st_f)
                    dest_path = os.path.join(kb_dir, st_f)
                    shutil.move(st_path, dest_path)
                
                shutil.rmtree(staging_dir, ignore_errors=True)

            idx = pipeline.reload_index()
            self._json({
                'ok': True,
                'uploaded': saved_files,
                'total_chunks': len(idx.chunks),
                'sources': idx.sources()
            }, 200)
            return

        data, err = self._body_json()
        if err:
            self._json({'error': err}, 400)
            return
        if path == '/api/chat':
            message = str(data.get('message') or '')
            session_id = str(data.get('session_id') or '')[:64]
            enable_llm = data.get('enable_llm')
            if enable_llm is None and 'mode' in data:
                enable_llm = (data.get('mode') == 'llm')
            result = pipeline.handle_message(message, session_id=session_id, enable_llm=enable_llm)
            self._json(result, 200 if 'error' not in result else 400)
        elif path == '/api/feedback':
            ticket_id = str(data.get('ticket_id') or '')
            try:
                rating = int(data.get('rating'))
            except (TypeError, ValueError):
                rating = 0
            if not ticket_id or not 1 <= rating <= 5:
                self._json({'error': 'ticket_id_and_rating_1_5_required'}, 400)
                return
            ticket = store.set_rating(ticket_id, rating)
            self._json({'ok': bool(ticket)}, 200 if ticket else 404)
        elif path == '/api/tickets/reply':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            ticket_id = str(data.get('ticket_id') or '')
            reply_text = str(data.get('reply') or '').strip()
            if not ticket_id or not reply_text:
                self._json({'error': 'ticket_id_and_reply_required'}, 400)
                return
            t = store.update_ticket(ticket_id, human_reply=reply_text, status='resolved')
            self._json({'ok': bool(t), 'ticket': t}, 200 if t else 404)
        elif path == '/api/tickets/copilot':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            ticket_id = str(data.get('ticket_id') or '')
            if not ticket_id:
                self._json({'error': 'ticket_id_required'}, 400)
                return
            tickets = store.load_tickets()
            ticket = next((t for t in tickets if t['id'] == ticket_id), None)
            if not ticket:
                self._json({'error': 'ticket_not_found'}, 404)
                return
            history = session.get_history(ticket.get('session_id')) if ticket.get('session_id') else []
            if not history and ticket.get('message'):
                history = [{'query': ticket['message'], 'answer': ticket.get('answer', '')}]
            
            from engine import llm
            draft_res = llm.generate_copilot_draft(history)
            self._json({'ok': True, 'summary': draft_res['summary'], 'draft': draft_res['draft']}, 200)
        elif path == '/api/kb/text':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            title = str(data.get('title') or '').strip()
            content = str(data.get('content') or '').strip()
            if not title or not content:
                self._json({'error': 'title_and_content_required'}, 400)
                return
            safe_fn = re.sub(r'[^a-zA-Z0-9_\u0600-\u06FF\-]+', '_', title).strip('_')[:50] + '.md'
            kb_dir = pipeline.get_kb_dir()
            target_path = os.path.join(kb_dir, safe_fn)
            with open(target_path, 'w', encoding='utf-8') as f:
                f.write(f'# {title}\n\n{content}\n')
            idx = pipeline.reload_index()
            self._json({'ok': True, 'file': safe_fn, 'total_chunks': len(idx.chunks), 'sources': idx.sources()}, 200)
        elif path == '/api/kb/reload':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            idx = pipeline.reload_index()
            self._json({'ok': True, 'sources': idx.sources(), 'total_chunks': len(idx.chunks)})
        elif path == '/api/webhook/telegram':
            if not telegram_bot.is_configured():
                self._json({'error': 'telegram_bot_not_configured'}, 503)
                return
            secret = os.environ.get('TELEGRAM_WEBHOOK_SECRET', '').strip()
            if secret:
                req_secret = self.headers.get('X-Telegram-Bot-Api-Secret-Token', '').strip()
                if req_secret != secret:
                    self._json({'error': 'invalid_secret_token'}, 403)
                    return
            try:
                result = telegram_bot.handle_update(data)
            except Exception as e:
                print('[Telegram Webhook Error] %s' % e, file=sys.stderr)
                try:
                    chat_id, _, _ = telegram_bot.extract_update_details(data)
                    if chat_id:
                        fallback_msg = 'عذراً، حصل خطأ تقني مؤقت. جاري تحويلك لموظف الدعم، برجاء المحاولة تاني بعد شوية.'
                        telegram_bot.send_message(chat_id, fallback_msg)
                except Exception as send_err:
                    print('[Telegram Fallback Send Error] %s' % send_err, file=sys.stderr)
                result = {'ok': True, 'error': str(e)}
            # دائماً نرجع 200 OK لتليجرام لتأكيد الاستلام ومنع الـ retries
            self._json({'ok': True, 'result': result}, 200)
        else:
            self._json({'error': 'not_found'}, 404)


import threading
import time
import webbrowser

def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass
    port = 9090
    if len(sys.argv) > 1:
        try:
            port = int(sys.argv[1])
        except ValueError:
            print('البورت "%s" غير صالح، هنستخدم %d' % (sys.argv[1], port))

    idx = pipeline.get_index()  # تسخين الفهرس + التأكد إن قاعدة المعرفة موجودة

    server = None
    for candidate in [port] + list(range(port + 1, port + 11)):
        try:
            server = ThreadingHTTPServer(('', candidate), Handler)
            port = candidate
            break
        except OSError:
            if candidate == port:
                print('  ! البورت %d مشغول بسيرفر تاني (غالباً مشروع قديم لسه شغال).' % port)
                print('    هنجرب بورتات تانية تلقائياً...')

    if server is None:
        print('  X مش لاقيين بورت فاضي من %d ل %d.' % (port, port + 10))
        print('  اقفل السيرفر القديم من CMD بالأوامر دي:')
        print('     netstat -ano | findstr :%d' % port)
        print('     taskkill /PID <الرقم_اللي_ظهر_في_العمود_الأخير> /F')
        print('=' * 58)
        return

    # تشغيل بوت تيليجرام التفاعلي تلقائياً في الخلفية إذا كان التوكن مضبوطاً
    if telegram_bot.is_configured():
        def start_tg_bot():
            try:
                import run_telegram_bot
                run_telegram_bot.main()
            except Exception as e:
                print('  ⚠️ خطأ في تشغيل بوت تيليجرام: %s' % e, file=sys.stderr)
        
        tg_t = threading.Thread(target=start_tg_bot, daemon=True)
        tg_t.start()

    # فتح المتصفح تلقائياً للشات ولوحة التحكم
    def auto_open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open('http://localhost:%d/' % port)
        except Exception:
            pass

    threading.Thread(target=auto_open_browser, daemon=True).start()

    store_name = os.environ.get('STORE_NAME', 'متجر النور')
    print('=================================================================')
    print(f'  🚀 نظام الدعم الذكي الشامل — {store_name} (All-In-One AI Suite)')
    print('  المستأجر (Tenant): %s' % os.environ.get('TENANT_ID', 'default'))
    print('  ملفات المعرفة: %d | أقسام مفهرسة: %d' % (len(idx.sources()), len(idx.chunks)))
    print('=' * 65)
    print('  🌐 واجهة الشات للعميل :  http://localhost:%d/' % port)
    print('  📊 لوحة التحكم للإدارة :  http://localhost:%d/dashboard' % port)
    print('  🔑 رمز الدخول للإدارة :  %s' % ADMIN_TOKEN)
    if telegram_bot.is_configured():
        print('  🤖 بوت تيليجرام       :  يعمل الآن ويستقبل الرسائل مباشرة في الوقت الفعلي!')
    else:
        print('  🤖 بوت تيليجرام       :  معطل (لم يتم تعيين TELEGRAM_BOT_TOKEN في .env)')
    print('=' * 65)
    print('  🟢 كل الخدمات تعمل بنجاح! (اضغط Ctrl+C للإيقاف في أي وقت)\n')

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nتم إيقاف السيرفر. سلام!')


if __name__ == '__main__':
    main()
