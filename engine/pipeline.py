# -*- coding: utf-8 -*-
"""
قلب النظام: Customer Message -> Sentiment + RAG -> Confidence -> Decision.

المسارات:
  غاضب (anger >= 0.5)          => priority  (تذكرة أولوية)
  ثقة منخفضة (confidence<0.35) => escalated (موظف بشري)
  غير كده                      => resolved  (إجابة AI)
"""
import os
import re
import time
from datetime import date, datetime, timedelta

from engine import actions, ai, llm, policy_extractor, rag, reranker, sentiment, session, store

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def get_tenant_id():
    return os.environ.get('TENANT_ID', 'default').strip() or 'default'


def get_kb_dir(tenant=None):
    tenant = tenant or get_tenant_id()
    tenant_dir = os.path.join(BASE_DIR, 'data', 'tenants', tenant)
    new_kb_dir = os.path.join(tenant_dir, 'knowledge_base')
    os.makedirs(new_kb_dir, exist_ok=True)
    old_kb_dir = os.path.join(BASE_DIR, 'data', 'knowledge_base')
    if tenant == 'default' and os.path.exists(old_kb_dir) and os.path.isdir(old_kb_dir) and old_kb_dir != new_kb_dir:
        for item in os.listdir(old_kb_dir):
            s = os.path.join(old_kb_dir, item)
            d = os.path.join(new_kb_dir, item)
            if not os.path.exists(d):
                try:
                    os.replace(s, d)
                except OSError:
                    pass
        try:
            if not os.listdir(old_kb_dir):
                os.rmdir(old_kb_dir)
            print('📦 [Migration] تم نقل ملفات المعرفة من %s إلى %s' % (old_kb_dir, new_kb_dir))
        except OSError:
            pass
    return new_kb_dir


# عتبات قابلة للضبط
ANGER_THRESHOLD = 0.50
CONF_ESCALATE = 0.35
CONF_MIN_CUTOFF = 0.25
ENABLE_LLM = os.environ.get('ENABLE_LLM', 'true').lower() in ('true', '1', 'yes')

_INDEX_BY_TENANT = {}


def get_index():
    tenant = get_tenant_id()
    if tenant not in _INDEX_BY_TENANT:
        kb_dir = get_kb_dir(tenant)
        _INDEX_BY_TENANT[tenant] = rag.KnowledgeIndex(kb_dir).build()
        policy_extractor.get_tenant_policies(tenant, kb_dir)
    return _INDEX_BY_TENANT[tenant]


def reload_index():
    tenant = get_tenant_id()
    kb_dir = get_kb_dir(tenant)
    _INDEX_BY_TENANT[tenant] = rag.KnowledgeIndex(kb_dir).build()
    policy_extractor.get_tenant_policies(tenant, kb_dir, force_reload=True)
    return _INDEX_BY_TENANT[tenant]


def _confidence(query, matches):
    """الثقة = 0.45 * (تشابه الكوني المعايَر) + 0.55 * (تغطية كلمات السؤال)."""
    if not matches:
        return 0.0
    top_chunk, top_score = matches[0]
    nc = min(1.0, top_score / 0.35)
    orig, expanded = rag.content_tokens(query)
    if not orig:
        return 0.0
    
    primary_tokens = set(rag.tokenize(top_chunk.get('source', '') + ' ' + top_chunk['heading'] + ' ' + top_chunk['text']))
    if len(matches) > 1 and matches[1][1] >= top_score * 0.80:
        primary_tokens.update(rag.tokenize(matches[1][0].get('source', '') + ' ' + matches[1][0]['heading'] + ' ' + matches[1][0]['text']))
    primary_tokens = set(t for t in primary_tokens if t not in rag.STOPWORDS)
    
    matched_orig = sum(1 for t in orig if any(syn in primary_tokens for syn in rag.SYNONYMS.get(t, {t})))
    coverage = float(matched_orig) / len(orig)
    coverage = min(1.0, coverage)
    
    return round(0.45 * nc + 0.55 * coverage, 3)


def extract_comparison_targets(query):
    """استخراج طرفي المقارنة بشكل عام ومجرد من أي سؤال مقارنة باللغة العربية."""
    context_match = re.search(r'\[السياق من سؤالك السابق:\s*(.+?)\]', query)
    target_text = context_match.group(1) if context_match else query
    
    clean = re.sub(r'[\?؟\.\!]', '', target_text).strip()
    
    # 1. نمط: بين X و Y
    if 'بين' in clean:
        after_bayn = clean.split('بين', 1)[1].strip()
        m = re.search(r'\s+(?:و(?=[\u0621-\u064A])|وبين\s+|أم\s+|أو\s+|ولا\s+)(.+)', after_bayn)
        if m:
            t1 = after_bayn[:m.start()].strip()
            t2 = m.group(1).strip()
            t2 = re.sub(r'\s+(?:من ناحية.*|في السعر.*|في التكلفة.*|في الضمان.*)$', '', t2).strip()
            if len(t1) >= 2 and len(t2) >= 2:
                return t1, t2

    # 2. نمط: فرق X عن Y
    if 'فرق' in clean and 'عن' in clean:
        m = re.search(r'فرق\s+(.+?)\s+عن\s+(.+)', clean)
        if m:
            t1 = m.group(1).strip()
            t2 = m.group(2).strip()
            t2 = re.sub(r'\s+(?:من ناحية.*|في السعر.*|في التكلفة.*|في الضمان.*)$', '', t2).strip()
            if len(t1) >= 2 and len(t2) >= 2:
                return t1, t2

    # 3. نمط: X ولا Y
    m2 = re.search(r'(.+?)\s+(?:ولا|أم)\s+(.+?)(?:\s+أحسن|\s+أفضل|\s+أرخص|\s+أغلى|\s+الفرق|$)', clean)
    if m2:
        return m2.group(1).strip(), m2.group(2).strip()
        
    return None


def handle_message(message, session_id='', now=None, enable_llm=None):
    """معالجة رسالة عميل كاملة: تحليل + بحث متعدد المراحل + Re-ranking + قرار + تذكرة."""
    started = time.perf_counter()
    message = (message or '').strip()[:2000]
    if not message:
        return {'error': 'empty_message'}
    now = time.time() if now is None else now

    # 1. التخصيب التتابعي وحل الضمائر السياقية (Coreference Resolution)
    history = session.get_history(session_id) if session_id else []
    enriched_query = session.enrich_followup_query(session_id, message)
    search_query = enriched_query if enriched_query != message else message
    
    # 2. البحث متعدد المسارات (Multi-Hop / Multi-Target Search)
    comp_targets = extract_comparison_targets(search_query)
    candidates_dict = {}
    entity_chunks = []

    def add_candidate(chunk, score):
        k = (chunk.get('source', ''), chunk.get('heading', ''))
        if k not in candidates_dict or score > candidates_dict[k][1]:
            candidates_dict[k] = (chunk, float(score))

    index = get_index()

    if comp_targets:
        t1, t2 = comp_targets
        m1 = index.search(t1, top_k=3)
        m2 = index.search(t2, top_k=3)
        m_all = index.search(search_query, top_k=4)
        for pair in zip(m1[:2], m2[:2]):
            for c, s in pair:
                add_candidate(c, s)
        for m_list in [m1, m2, m_all]:
            for c, s in m_list:
                add_candidate(c, s)
    else:
        # بحث مباشر
        base_matches = index.search(search_query, top_k=8)
        for c, s in base_matches:
            add_candidate(c, s)

        # Multi-Hop Entity & Category Anchor Injection:
        query_norm = rag.normalize(search_query)
        q_tokens = [t for t in rag.tokenize(search_query) if t not in rag.STOPWORDS]
        entity_chunks = []
        for chunk in index.chunks:
            h_norm = rag.normalize(chunk.get('heading', ''))
            h_words = [w for w in h_norm.split() if len(w) >= 3 and w not in rag.STOPWORDS]
            
            # مطابقة اسم الكيان الصريح
            if len(h_words) >= 2 and all(w in query_norm for w in h_words[:2]):
                entity_chunks.append((chunk, 0.95))
                add_candidate(chunk, 0.95)
            # مطابقة الفئة أو الوصف الدلالي بالمرادفات (مثل: رخيصة/اقتصادية، فاخرة، ميني باص)
            elif q_tokens:
                chunk_all_tokens = set(rag.tokenize(chunk.get('heading', '') + ' ' + chunk.get('text', '')))
                for qt in q_tokens:
                    syns = rag.SYNONYMS.get(qt, {qt})
                    if any(s in chunk_all_tokens for s in syns) and any(kw in chunk.get('text', '') for kw in ('الفئة', 'سعر', 'rate', 'مواصفات', 'باقة')):
                        add_candidate(chunk, 0.85)

    candidates = list(candidates_dict.values())

    # 3. إعادة الترتيب الدلالي المتقدم باستخدام BERT / Sentence-Transformers (Re-Ranking)
    matches = reranker.rerank_matches(search_query, candidates, top_k=5)
    
    # ضمان وجود بطاقة الكيان المعني في السياق للعمليات الحسابية
    if entity_chunks:
        seen_heads = set(c.get('heading') for c, _ in matches)
        for ec, sc in entity_chunks:
            if ec.get('heading') not in seen_heads:
                matches.insert(0, (ec, sc))
    matches = matches[:5]

    confidence = _confidence(search_query, matches)

    if enable_llm is not None:
        use_llm = bool(enable_llm)
    else:
        use_llm = ENABLE_LLM or os.environ.get('ENABLE_LLM', 'true').lower() in ('true', '1', 'yes')

    # Sentiment Analysis (LLM-based)
    sentiment_tag = 'neutral'
    if use_llm:
        sentiment_tag = llm.analyze_sentiment(message)
    else:
        if sentiment.anger_score(message) >= ANGER_THRESHOLD:
            sentiment_tag = 'angry'

    anger = 1.0 if sentiment_tag == 'angry' else 0.0

    if sentiment_tag == 'angry':
        status, reason = 'priority', 'angry_customer'
    elif confidence < CONF_MIN_CUTOFF:
        status, reason = 'escalated', 'very_low_confidence'
    elif confidence < CONF_ESCALATE:
        status, reason = 'resolved', None
    else:
        status, reason = 'resolved', None

    answerable = status == 'resolved' or confidence >= CONF_MIN_CUTOFF

    # 4. فحص الأسئلة الحوارية والترحيب
    chitchat = ai.check_chitchat(message)
    if chitchat:
        confidence = 1.0
        status, reason = 'resolved', None
        answerable = True
        sources = [chitchat['source']]
        if use_llm:
            mock_chunk = {'source': 'about_and_identity.md', 'heading': 'من نحن وما هوية المساعد', 'text': chitchat['answer']}
            answer, model_used = llm.generate_answer(message, [(mock_chunk, 1.0)], status, answerable, history=history)
        else:
            answer = chitchat['answer']
            model_used = 'extractive_chitchat'
        response_ms = int((time.perf_counter() - started) * 1000)
        if session_id:
            session.add_exchange(session_id, message, answer, matches=[(mock_chunk, 1.0)] if use_llm else None)
        ticket = {
            'id': store.next_id(),
            'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
            'session_id': session_id,
            'channel': 'web',
            'query': message,
            'answer': answer,
            'status': status,
            'confidence': confidence,
            'anger': anger,
            'sources': sources,
            'escalated_reason': reason,
            'response_ms': response_ms,
            'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
            'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds') if status == 'resolved' else None,
            'model_used': model_used
        }
        store.add_ticket(ticket)
        return ticket

    # 5. توليد الإجابة
    sources = []
    seen_src = set()
    for chunk, _ in matches[:4]:
        heading = chunk.get('heading', '').strip()
        source = chunk.get('source', '').strip()
        label = f"{source} :: {heading}" if heading else source
        if label not in seen_src:
            seen_src.add(label)
            sources.append(label)

    if use_llm:
        answer, model_used = llm.generate_answer(
            message,
            matches,
            status,
            answerable,
            history=history,
            is_medium_conf=(CONF_MIN_CUTOFF <= confidence < CONF_ESCALATE)
        )
    else:
        answer = ai.build_answer(matches, status, answerable)
        model_used = 'extractive_rag'

    response_ms = int((time.perf_counter() - started) * 1000)

    if session_id:
        session.add_exchange(session_id, message, answer, matches=matches)

    ticket = {
        'id': store.next_id(),
        'ts': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'session_id': session_id,
        'channel': 'web',
        'query': message,
        'answer': answer,
        'status': status,
        'confidence': confidence,
        'anger': anger,
        'sources': sources,
        'escalated_reason': reason,
        'response_ms': response_ms,
        'created_at': datetime.fromtimestamp(now).isoformat(timespec='seconds'),
        'resolved_at': datetime.fromtimestamp(now).isoformat(timespec='seconds') if status == 'resolved' else None,
        'model_used': model_used
    }
    store.add_ticket(ticket)
    return ticket


def compute_stats():
    tickets = store.load_tickets()
    total = len(tickets)
    resolved = sum(1 for t in tickets if t.get('status') == 'resolved')
    escalated = sum(1 for t in tickets if t.get('status') == 'escalated')
    unresolved = total - resolved - escalated
    avg_conf = (sum(t.get('confidence', 0) for t in tickets) / total) if total else 0.0
    avg_resp = (sum(t.get('response_ms', 0) for t in tickets) / total) if total else 0.0
    
    return {
        'total_tickets': total,
        'resolved': resolved,
        'escalated': escalated,
        'unresolved': unresolved,
        'avg_confidence': round(avg_conf, 2),
        'avg_response_ms': round(avg_resp),
        'resolution_rate': round(resolved / total * 100, 1) if total else 0.0
    }

