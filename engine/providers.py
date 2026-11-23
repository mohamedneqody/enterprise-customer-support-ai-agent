# -*- coding: utf-8 -*-
"""
engine/providers.py — المحطة الثالثة: طبقة تعدد المزودين + التوجيه الذكي للنماذج + Context Caching.

يدعم بدون أي مكتبات خارجية (Standard Library فقط):
1. Ollama المحلي (الوضع الافتراضي — متوافق 100% مع الإعداد الحالي بلا تبديل نماذج في VRAM).
2. Google Gemini REST API (مع Explicit Context Caching لتوفير حتى 75% من تكلفة قواعد المعرفة الكبيرة).
3. OpenAI وأي مزود متوافق مع OpenAI API (OpenRouter / Groq / DeepSeek / Together / vLLM).
4. موجه نماذج حتمي (Deterministic Model Router) بزمن 0ms:
   - الأسئلة البسيطة -> FAST model (مثل gemini-2.5-flash / gpt-4o-mini).
   - الأسئلة المركبة أو الغاضبة أو محاولات Reflexion أو الثقة المتوسطة -> PRO model (مثل gemini-2.5-pro / gpt-4o).
"""
import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request

_CACHE_LOCK = threading.Lock()
# (tenant_id, model, kb_hash) -> {'name': 'cachedContents/...', 'expires_at': float}
_GEMINI_CACHES = {}

_COMPLEX_CUES_RE = re.compile(
    r'(?:تفاعل|تعارض|مقارنة|الفرق بين|في نفس الوقت|مع بعض|شرط|استثناء|حالتين|أكثر من|'
    r'بالتفصيل|خطوة بخطوة|خطة|تحليل|compare|interaction|conflict|exception|policy|multi)',
    re.IGNORECASE,
)


def get_provider(override=None):
    """تحديد المزود النشط: ollama (افتراضي) أو gemini أو openai أو auto."""
    raw = (override or os.environ.get('LLM_PROVIDER', 'ollama')).strip().lower()
    if raw == 'auto':
        if os.environ.get('GEMINI_API_KEY', '').strip():
            return 'gemini'
        if os.environ.get('OPENAI_API_KEY', '').strip():
            return 'openai'
        return 'ollama'
    if raw in ('ollama', 'gemini', 'openai'):
        return raw
    return 'ollama'


def is_routing_enabled():
    return os.environ.get('ENABLE_MODEL_ROUTING', 'true').strip().lower() in ('true', '1', 'yes')


def is_context_cache_enabled():
    return os.environ.get('ENABLE_CONTEXT_CACHE', 'true').strip().lower() in ('true', '1', 'yes')


def resolve_model(provider, tier='fast', model_override=None):
    """اختيار اسم النموذج الفعلي حسب المزود والمستوى (fast / pro)."""
    if model_override and str(model_override).strip():
        return str(model_override).strip()

    tier = 'pro' if (tier == 'pro' and is_routing_enabled()) else 'fast'

    if provider == 'gemini':
        fast_m = os.environ.get('GEMINI_FAST_MODEL', 'gemini-2.5-flash').strip() or 'gemini-2.5-flash'
        pro_m = os.environ.get('GEMINI_PRO_MODEL', 'gemini-2.5-pro').strip() or fast_m
        return pro_m if tier == 'pro' else fast_m

    if provider == 'openai':
        fast_m = os.environ.get('OPENAI_FAST_MODEL', 'gpt-4o-mini').strip() or 'gpt-4o-mini'
        pro_m = os.environ.get('OPENAI_PRO_MODEL', 'gpt-4o').strip() or fast_m
        return pro_m if tier == 'pro' else fast_m

    # Ollama المحلي: افتراضياً نفس الموديل لتجنب Model Thrashing إلا لو ضبط المستخدم OLLAMA_PRO_MODEL صراحةً
    base_m = os.environ.get('OLLAMA_MODEL', 'lfm2.5:latest').strip() or 'lfm2.5:latest'
    if tier == 'pro':
        pro_m = os.environ.get('OLLAMA_PRO_MODEL', '').strip()
        if pro_m:
            return pro_m
    return base_m


def select_route(query, *, status='resolved', confidence=1.0, is_medium_conf=False, retry_feedback=None, matches_count=0):
    """موجه حتمي سريع (0ms): يحدد هل الحالة تحتاج 'fast' أم 'pro'."""
    if not is_routing_enabled():
        return 'fast'
    q = (query or '').strip()
    if retry_feedback:
        return 'pro'
    if status == 'priority':
        return 'pro'
    if is_medium_conf or (0.25 <= float(confidence or 0.0) < 0.50):
        return 'pro'
    if len(q) >= 140:
        return 'pro'
    if (q.count('؟') + q.count('?')) >= 2:
        return 'pro'
    if _COMPLEX_CUES_RE.search(q) and (len(q) >= 60 or matches_count >= 4):
        return 'pro'
    return 'fast'


def invalidate_context_cache(tenant_id=None):
    """إبطال الكاش المخبأ عند تحديث قاعدة المعرفة (يُستدعى من pipeline.reload_index)."""
    with _CACHE_LOCK:
        if tenant_id is None:
            _GEMINI_CACHES.clear()
        else:
            for k in list(_GEMINI_CACHES.keys()):
                if k[0] == tenant_id:
                    _GEMINI_CACHES.pop(k, None)


def _get_or_create_gemini_cache(api_key, model, tenant_id, system_instruction, cacheable_context, timeout=20.0):
    """إنشاء أو استرجاع Gemini Explicit Context Cache لقواعد المعرفة الكبيرة."""
    if not (is_context_cache_enabled() and cacheable_context):
        return None
    min_chars = int(os.environ.get('CONTEXT_CACHE_MIN_CHARS', '4000') or 4000)
    combined_len = len(system_instruction or '') + len(cacheable_context)
    if combined_len < min_chars:
        return None

    ttl_sec = max(300, int(os.environ.get('CONTEXT_CACHE_TTL_SECONDS', '1800') or 1800))
    kb_hash = hashlib.sha256(((system_instruction or '') + '\n' + cacheable_context).encode('utf-8')).hexdigest()[:16]
    cache_key = (tenant_id or 'default', model, kb_hash)
    now = time.time()

    with _CACHE_LOCK:
        entry = _GEMINI_CACHES.get(cache_key)
        if entry and entry['expires_at'] > now + 30:
            return entry['name']

    url = f'https://generativelanguage.googleapis.com/v1beta/cachedContents?key={api_key}'
    body = {
        'model': f'models/{model}',
        'contents': [{'role': 'user', 'parts': [{'text': cacheable_context}]}],
        'ttl': f'{ttl_sec}s',
    }
    if system_instruction:
        body['systemInstruction'] = {'parts': [{'text': system_instruction}]}

    try:
        req = urllib.request.Request(
            url,
            data=json.dumps(body).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
        )
        with urllib.request.urlopen(req, timeout=min(timeout, 25.0)) as resp:
            if resp.status == 200:
                res = json.loads(resp.read().decode('utf-8'))
                name = res.get('name')
                if name:
                    with _CACHE_LOCK:
                        _GEMINI_CACHES[cache_key] = {'name': name, 'expires_at': time.time() + ttl_sec}
                    return name
    except Exception:
        return None
    return None


def _call_ollama(prompt, *, model, temperature, max_tokens, json_mode, stop, timeout):
    host = os.environ.get('OLLAMA_HOST', 'http://localhost:11434').rstrip('/')
    url = f'{host}/api/generate'
    options = {
        'temperature': temperature,
        'top_p': 0.9,
        'num_ctx': int(os.environ.get('OLLAMA_NUM_CTX', '4096') or 4096),
        'num_predict': max_tokens,
    }
    if stop:
        options['stop'] = list(stop)

    payload = {
        'model': model,
        'prompt': prompt,
        'stream': False,
        'keep_alive': -1,
        'think': False,
        'options': options,
    }
    if json_mode:
        payload['format'] = 'json'

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode('utf-8'))
        return {
            'text': (result.get('response') or '').strip(),
            'model': result.get('model') or model,
            'provider': 'ollama',
            'cached': False,
        }


def _call_gemini(prompt, *, system_instruction, model, temperature, max_tokens, json_mode, stop, timeout, tenant_id, cacheable_context):
    api_key = os.environ.get('GEMINI_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('GEMINI_API_KEY is not configured')

    cached_name = _get_or_create_gemini_cache(
        api_key, model, tenant_id, system_instruction, cacheable_context, timeout=timeout
    )

    url = f'https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={api_key}'
    gen_cfg = {
        'temperature': temperature,
        'maxOutputTokens': max_tokens,
    }
    if json_mode:
        gen_cfg['responseMimeType'] = 'application/json'
    if stop:
        gen_cfg['stopSequences'] = list(stop)[:5]

    payload = {
        'contents': [{'role': 'user', 'parts': [{'text': prompt}]}],
        'generationConfig': gen_cfg,
    }
    if cached_name:
        payload['cachedContent'] = cached_name
    elif system_instruction:
        payload['systemInstruction'] = {'parts': [{'text': system_instruction}]}

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={'Content-Type': 'application/json'},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode('utf-8'))
        cands = result.get('candidates') or []
        parts = (cands[0].get('content') or {}).get('parts') or [] if cands else []
        text = ''.join(p.get('text', '') for p in parts).strip()
        return {
            'text': text,
            'model': model,
            'provider': 'gemini',
            'cached': bool(cached_name),
        }


def _call_openai(prompt, *, system_instruction, model, temperature, max_tokens, json_mode, stop, timeout):
    api_key = os.environ.get('OPENAI_API_KEY', '').strip()
    if not api_key:
        raise RuntimeError('OPENAI_API_KEY is not configured')

    base_url = os.environ.get('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/')
    url = f'{base_url}/chat/completions'
    messages = []
    if system_instruction:
        messages.append({'role': 'system', 'content': system_instruction})
    messages.append({'role': 'user', 'content': prompt})

    payload = {
        'model': model,
        'messages': messages,
        'temperature': temperature,
        'max_tokens': max_tokens,
    }
    if json_mode:
        payload['response_format'] = {'type': 'json_object'}
    if stop:
        payload['stop'] = list(stop)[:4]

    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode('utf-8'),
        headers={
            'Content-Type': 'application/json',
            'Authorization': f'Bearer {api_key}',
        },
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        result = json.loads(resp.read().decode('utf-8'))
        choices = result.get('choices') or []
        text = ((choices[0].get('message') or {}).get('content') or '').strip() if choices else ''
        usage = result.get('usage') or {}
        prompt_details = usage.get('prompt_tokens_details') or {}
        return {
            'text': text,
            'model': result.get('model') or model,
            'provider': 'openai',
            'cached': bool(prompt_details.get('cached_tokens', 0) > 0),
        }


def complete(
    prompt,
    *,
    system_instruction='',
    tier='fast',
    provider=None,
    model_override=None,
    temperature=0.0,
    max_tokens=700,
    json_mode=False,
    stop=None,
    timeout=60.0,
    tenant_id=None,
    cacheable_context=None,
):
    """واجهة موحدة لاستدعاء أي مزود LLM مع التوجيه والكاش."""
    prov = get_provider(provider)
    model = resolve_model(prov, tier=tier, model_override=model_override)

    if prov == 'gemini':
        res = _call_gemini(
            prompt,
            system_instruction=system_instruction,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            stop=stop,
            timeout=timeout,
            tenant_id=tenant_id,
            cacheable_context=cacheable_context,
        )
    elif prov == 'openai':
        res = _call_openai(
            prompt,
            system_instruction=system_instruction,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            stop=stop,
            timeout=timeout,
        )
    else:
        full_prompt = f'{system_instruction}\n\n{prompt}' if system_instruction else prompt
        res = _call_ollama(
            full_prompt,
            model=model,
            temperature=temperature,
            max_tokens=max_tokens,
            json_mode=json_mode,
            stop=stop,
            timeout=timeout,
        )

    res['route'] = tier
    return res


def get_status():
    """ملخص حالة المزود والتوجيه والكاش لعرضه في /api/health."""
    prov = get_provider()
    return {
        'provider': prov,
        'fast_model': resolve_model(prov, 'fast'),
        'pro_model': resolve_model(prov, 'pro'),
        'routing_enabled': is_routing_enabled(),
        'context_cache_enabled': is_context_cache_enabled() and prov == 'gemini',
    }
