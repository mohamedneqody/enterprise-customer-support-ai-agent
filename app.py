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
import hmac
import json
import os
import re
import secrets
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

try:
    sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    sys.stderr.reconfigure(encoding='utf-8', errors='replace')
except Exception:
    pass

import train
from engine import analytics, pipeline, session, store, telegram_bot, whatsapp_bot

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

def _persist_env(key, value):
    """تحديث مفتاح في .env مع الحفاظ على باقي الأسطر."""
    path = os.path.join(BASE_DIR, '.env')
    lines = []
    if os.path.isfile(path):
        with open(path, encoding='utf-8') as f:
            lines = f.readlines()
    replaced = False
    for i, line in enumerate(lines):
        if line.strip().startswith(key + '='):
            lines[i] = '%s=%s\n' % (key, value)
            replaced = True
            break
    if not replaced:
        if lines and not lines[-1].endswith('\n'):
            lines[-1] += '\n'
        lines.append('%s=%s\n' % (key, value))
    with open(path, 'w', encoding='utf-8') as f:
        f.writelines(lines)


ADMIN_TOKEN = os.environ.get('ADMIN_TOKEN', '').strip()
_WEAK_TOKENS = {'admin123', 'admin', '123456', 'password', 'test', 'token'}
if not ADMIN_TOKEN or ADMIN_TOKEN.lower() in _WEAK_TOKENS or len(ADMIN_TOKEN) < 16:
    # توكن ضعيف أو مفقود: توليد قوي تلقائياً وحفظه في .env حتى لا يتغير كل تشغيل
    ADMIN_TOKEN = secrets.token_urlsafe(24)
    IS_GENERATED_TOKEN = True
    os.environ['ADMIN_TOKEN'] = ADMIN_TOKEN
    _persist_env('ADMIN_TOKEN', ADMIN_TOKEN)

# سرّ Webhook تيليجرام إجباري: لو البوت مضبوط والسرّ ناقص يُولَّد ويُحفظ تلقائياً
if telegram_bot.is_configured() and not os.environ.get('TELEGRAM_WEBHOOK_SECRET', '').strip():
    tg_secret = secrets.token_urlsafe(16)
    os.environ['TELEGRAM_WEBHOOK_SECRET'] = tg_secret
    _persist_env('TELEGRAM_WEBHOOK_SECRET', tg_secret)

TENANT_ID = os.environ.get('TENANT_ID', 'default').strip() or 'default'

# ---------------------------------------------------------------- حماية الإغراق (Rate Limiting)
# نافذة منزلقة لكل IP وبكل مسار على حدة — ودعم X-Forwarded-For خلف reverse proxy
RATE_LIMIT_PER_MINUTE = int(os.environ.get('RATE_LIMIT_PER_MINUTE', '30') or 30)
_RATE_LOCK = threading.Lock()
_RATE_HITS = {}  # "bucket|ip" -> deque[timestamps]


def _rate_limit_ok(ip, bucket='chat'):
    """يسمح بالطلب إذا لم يتجاوز IP حصة هذا المسار؛ ينظف الإدخالات القديمة دورياً."""
    key = '%s|%s' % (bucket, ip)
    now = time.time()
    with _RATE_LOCK:
        if len(_RATE_HITS) > 5000:
            for k in [k for k, v in _RATE_HITS.items() if not v or now - v[-1] > 120]:
                _RATE_HITS.pop(k, None)
        hits = _RATE_HITS.setdefault(key, deque())
        while hits and now - hits[0] > 60:
            hits.popleft()
        if len(hits) >= RATE_LIMIT_PER_MINUTE:
            return False
        hits.append(now)
        return True


class Handler(BaseHTTPRequestHandler):
    server_version = 'SupportAgent/1.0'

    # ---------------- helpers ----------------

    def _check_auth(self):
        auth = self.headers.get('Authorization', '')
        if not auth.startswith('Bearer '):
            return False
        token = auth[7:].strip()
        # مقارنة زمنية الثبات لمنع هجوم Timing
        return bool(token) and hmac.compare_digest(token, ADMIN_TOKEN)

    def _client_ip(self):
        # خلف reverse proxy: أول عنوان في X-Forwarded-For هو العميل الحقيقي
        xff = self.headers.get('X-Forwarded-For', '')
        if xff:
            return xff.split(',')[0].strip()
        return self.client_address[0] if self.client_address else '?'

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(body)))
        self.send_header('X-Content-Type-Options', 'nosniff')
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
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.end_headers()
        self.wfile.write(body)

    def _body_json(self, max_bytes=1024 * 1024):
        """قراءة جسم JSON مع سقف صارم (1MB) — منع استنفاد الذاكرة قبل أي فحص آخر."""
        try:
            length = int(self.headers.get('Content-Length') or 0)
        except ValueError:
            return None, 'invalid_content_length'
        if length > max_bytes:
            return None, 'payload_too_large'
        raw = self.rfile.read(length) if length else b'{}'
        try:
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
        elif path == '/api/analytics/gaps':
            if not self._check_auth():
                self._json({'error': 'unauthorized'}, 401)
                return
            self._json(analytics.compute_kb_gaps())
        elif path == '/api/webhook/whatsapp':
            # مصادقة تسجيل الـ Webhook: Meta ترسل GET مع hub.challenge ويجب إرجاعه حرفياً
            challenge = whatsapp_bot.verify_webhook({k: v[0] for k, v in qs.items()})
            if challenge is not None:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(challenge.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(challenge.encode('utf-8'))
                return
            self._json({'error': 'verification_failed_or_not_configured'}, 403)
        elif path == '/api/health':
            idx = pipeline.get_index()
            self._json({
                'ok': True,
                'tenant': os.environ.get('TENANT_ID', 'default').strip() or 'default',
                'kb_files': len(idx.sources()),
                'kb_chunks': len(idx.chunks),
                'llm_enabled': pipeline.ENABLE_LLM,
                'reranker_enabled': __import__('engine.reranker', fromlist=['is_enabled']).is_enabled()
            })
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

        # سقف حجم مبكر من الهيدر فقط (بدون قراءة الجسم) — رفع الملفات استثناء بحد 50MB خاص به
        if path != '/api/kb/upload':
            try:
                declared = int(self.headers.get('Content-Length') or 0)
            except ValueError:
                declared = 0
            if declared > 1024 * 1024:
                self._json({'error': 'payload_too_large'}, 413)
                return

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
                    # عزل اسم الملف: منع أي محاولة تجاوز مسار (Path Traversal)
                    safe_name = os.path.basename(fn.replace('\\', '/')).strip()
                    if not safe_name or safe_name.startswith('.'):
                        raise ValueError('invalid_filename: %r' % fn)
                    tmp_path = os.path.join(tmp_dir, safe_name)
                    with open(tmp_path, 'wb') as f:
                        f.write(payload)
                    try:
                        out_path, count = train.process_file(tmp_path, out_dir=target_out_dir)
                        created_md_files.append(out_path)
                        saved_files.append({'file': safe_name, 'sections': count})
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
                'sources': idx.sources(),
                'quality_report': _kb_quality_report(idx)
            }, 200)
            return

        data, err = self._body_json()
        if err:
            self._json({'error': err}, 400)
            return
        if path == '/api/chat':
            if not _rate_limit_ok(self._client_ip(), 'chat'):
                self._json({'error': 'rate_limited', 'message': 'عدد كبير من الطلبات، برجاء المحاولة بعد قليل.'}, 429)
                return
            message = str(data.get('message') or '')
            session_id = str(data.get('session_id') or '')[:64]
            enable_llm = data.get('enable_llm')
            if enable_llm is None and 'mode' in data:
                enable_llm = (data.get('mode') == 'llm')
            result = pipeline.handle_message(message, session_id=session_id, enable_llm=enable_llm)
            self._json(result, 200 if 'error' not in result else 400)
        elif path == '/api/chat/stream':
            if not _rate_limit_ok(self._client_ip()):
                self._json({'error': 'rate_limited', 'message': 'عدد كبير من الطلبات، برجاء المحاولة بعد قليل.'}, 429)
                return
            message = str(data.get('message') or '')
            session_id = str(data.get('session_id') or '')[:64]
            enable_llm = data.get('enable_llm')
            if enable_llm is None and 'mode' in data:
                enable_llm = (data.get('mode') == 'llm')

            self.send_response(200)
            self.send_header('Content-Type', 'text/event-stream; charset=utf-8')
            self.send_header('Cache-Control', 'no-cache')
            self.send_header('Connection', 'close')
            self.send_header('Access-Control-Allow-Origin', '*')
            self.end_headers()

            result = pipeline.handle_message(message, session_id=session_id, enable_llm=enable_llm)
            ans = result.get('answer', '')
            words = ans.split(' ')
            for i, w in enumerate(words):
                chunk = (w + ' ') if i < len(words) - 1 else w
                payload = json.dumps({'token': chunk, 'done': False}, ensure_ascii=False)
                try:
                    self.wfile.write(f"data: {payload}\n\n".encode('utf-8'))
                    self.wfile.flush()
                    time.sleep(0.015)
                except (BrokenPipeError, ConnectionResetError):
                    self.close_connection = True
                    return

            final_payload = json.dumps({'token': '', 'done': True, 'ticket': result}, ensure_ascii=False)
            try:
                self.wfile.write(f"data: {final_payload}\n\n".encode('utf-8'))
                self.wfile.flush()
            except (BrokenPipeError, ConnectionResetError):
                pass
            self.close_connection = True
            return
        elif path == '/api/feedback':
            if not _rate_limit_ok(self._client_ip(), 'feedback'):
                self._json({'error': 'rate_limited'}, 429)
                return
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
            if not secret:
                # الـ Webhook بلا سرّ = أي طرف يستطيع انتحال تحديثات — يُرفض دائماً
                self._json({'error': 'telegram_webhook_secret_not_configured'}, 503)
                return
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
        elif path == '/api/webhook/whatsapp':
            # مصادقة تسجيل الـ Webhook (GET) من Meta
            challenge = whatsapp_bot.verify_webhook({k: v[0] for k, v in qs.items()})
            if challenge is not None:
                self.send_response(200)
                self.send_header('Content-Type', 'text/plain; charset=utf-8')
                self.send_header('Content-Length', str(len(challenge.encode('utf-8'))))
                self.end_headers()
                self.wfile.write(challenge.encode('utf-8'))
                return
            if not whatsapp_bot.is_configured():
                self._json({'error': 'whatsapp_not_configured'}, 503)
                return
            # تحقق اختياري من توقيع Meta (يُفعَّل بضبط WHATSAPP_APP_SECRET)
            raw_body = self.rfile.read(int(self.headers.get('Content-Length') or 0))
            if not whatsapp_bot.verify_signature(raw_body, self.headers.get('X-Hub-Signature-256', '')):
                self._json({'error': 'invalid_signature'}, 403)
                return
            try:
                data = json.loads(raw_body.decode('utf-8'))
                result = whatsapp_bot.handle_update(data)
            except Exception as e:
                print('[WhatsApp Webhook Error] %s' % e, file=sys.stderr)
                result = {'ok': True, 'error': str(e)}
            # دائماً 200 OK لـ Meta لتأكيد الاستلام ومنع إعادة الإرسال
            self._json({'ok': True, 'result': result}, 200)
        else:
            self._json({'error': 'not_found'}, 404)


import webbrowser

def _kb_quality_report(idx):
    """تقرير جودة قاعدة المعرفة بعد الرفع: ينبه فوراً لو داتا العميل ضعيفة الفهرسة."""
    texts = [len(c.get('text', '')) for c in idx.chunks]
    total = len(texts)
    tiny = sum(1 for t in texts if t < 60)
    warnings = []
    if total == 0:
        warnings.append('لم تُفهرس أي أقسام — تأكد أن الملفات تحتوي نصاً قابلاً للقراءة.')
    if total and tiny / total > 0.3:
        warnings.append('أكثر من 30%% من الأقسام قصيرة جداً (<60 حرفاً) — قسّم المحتوى لعناوين أوضح.')
    if idx.sources() and len(idx.sources()) > 0 and total / max(len(idx.sources()), 1) < 2:
        warnings.append('عدد الأقسام لكل ملف قليل جداً — استخدم عناوين ## لكل فقرة لتحسين الاسترجاع.')
    return {
        'files': len(idx.sources()),
        'chunks': total,
        'avg_chunk_chars': round(sum(texts) / total) if total else 0,
        'tiny_chunks': tiny,
        'warnings': warnings,
    }


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

    # خيار HTTPS المؤسسي: ضع SSL_CERT_FILE و SSL_KEY_FILE في .env لتفعيله
    cert_file = os.environ.get('SSL_CERT_FILE', '').strip()
    key_file = os.environ.get('SSL_KEY_FILE', '').strip()
    scheme = 'http'
    if cert_file and key_file and os.path.isfile(cert_file) and os.path.isfile(key_file):
        import ssl
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(cert_file, key_file)
        server.socket = ctx.wrap_socket(server.socket, server_side=True)
        scheme = 'https'
        print('  🔒 تم تفعيل HTTPS بشهادة %s' % os.path.basename(cert_file))

    # تشغيل بوت تيليجرام التفاعلي تلقائياً في الخلفية بنظام Long Polling إذا كان مضبوطاً ولم يتم اختيار وضع Webhook
    tg_mode = os.environ.get('TELEGRAM_MODE', 'polling').strip().lower()
    if telegram_bot.is_configured() and tg_mode == 'polling':
        def start_tg_bot():
            while True:
                try:
                    import run_telegram_bot
                    run_telegram_bot.main()
                except BaseException as e:
                    import traceback
                    err_msg = traceback.format_exc()
                    print('  ⚠️ خطأ في تشغيل بوت تيليجرام: %s' % e, file=sys.stderr)
                    with open('server_error.log', 'a', encoding='utf-8') as f:
                        f.write(f'Telegram bot error: {e}\n{err_msg}\n')
                    time.sleep(5)

        tg_t = threading.Thread(target=start_tg_bot, daemon=True)
        tg_t.start()

    # فتح المتصفح تلقائياً للشات ولوحة التحكم
    def auto_open_browser():
        time.sleep(1.2)
        try:
            webbrowser.open('%s://localhost:%d/' % (scheme, port))
        except Exception:
            pass

    threading.Thread(target=auto_open_browser, daemon=True).start()

    store_name = os.environ.get('STORE_NAME', 'متجر النور')
    print('=================================================================')
    print(f'  🚀 نظام الدعم الذكي الشامل — {store_name} (All-In-One AI Suite)')
    print('  المستأجر (Tenant): %s' % os.environ.get('TENANT_ID', 'default'))
    print('  ملفات المعرفة: %d | أقسام مفهرسة: %d' % (len(idx.sources()), len(idx.chunks)))
    print('=' * 65)
    print('  🌐 واجهة الشات للعميل :  %s://localhost:%d/' % (scheme, port))
    print('  📊 لوحة التحكم للإدارة :  %s://localhost:%d/dashboard' % (scheme, port))
    print('  🔑 رمز الدخول للإدارة :  %s' % ADMIN_TOKEN)
    if telegram_bot.is_configured():
        print('  🤖 بوت تيليجرام       :  يعمل الآن ويستقبل الرسائل مباشرة في الوقت الفعلي!')
    else:
        print('  🤖 بوت تيليجرام       :  معطل (لم يتم تعيين TELEGRAM_BOT_TOKEN في .env)')
    print('=' * 65)
    print('  🟢 كل الخدمات تعمل بنجاح! (اضغط Ctrl+C للإيقاف في أي وقت)\n')

    while True:
        try:
            server.serve_forever()
        except KeyboardInterrupt:
            print('\nتم إيقاف السيرفر. سلام!')
            break
        except BaseException as e:
            import traceback
            err_msg = traceback.format_exc()
            print('FATAL SERVER EXCEPTION: %s' % e, file=sys.stderr)
            with open('server_error.log', 'a', encoding='utf-8') as f:
                f.write(f'Server loop error: {e}\n{err_msg}\n')
            time.sleep(1)


if __name__ == '__main__':
    main()
