# -*- coding: utf-8 -*-
"""
engine/session.py — إدارة ذاكرة الجلسات الذكية (Stateful Session Memory & Slot Tracking).
يحفظ آخر 4 تبادلات (سؤال + إجابة) + الحالة الحوارية (Active State & Entities) لكل session_id.
"""
import os
import re
import threading
import time
from collections import OrderedDict

_LOCK = threading.Lock()
_MAX_SESSIONS = 1000
_MAX_HISTORY_PER_SESSION = 4
_SESSIONS = OrderedDict()  # "tenant:session_id" -> {'last_active': float, 'history': [...], 'state': {...}}


def _make_key(session_id, tenant_id=None):
    if not session_id:
        return ""
    tenant = tenant_id or os.environ.get('TENANT_ID', 'default').strip() or 'default'
    return f"{tenant}:{session_id}"


def get_history(session_id, tenant_id=None):
    """إرجاع قائمة بآخر التبادلات للـ session_id المعطى مع عزل المستأجر."""
    if not session_id:
        return []
    key = _make_key(session_id, tenant_id)
    with _LOCK:
        sess = _SESSIONS.get(key)
        if sess:
            sess['last_active'] = time.time()
            _SESSIONS.move_to_end(key)
            return list(sess.get('history', []))
        return []


def get_session_state(session_id, tenant_id=None):
    """إرجاع الكيانات والحالة النشطة للجلسة الحالية مع عزل المستأجر."""
    if not session_id:
        return {}
    key = _make_key(session_id, tenant_id)
    with _LOCK:
        sess = _SESSIONS.get(key)
        if sess:
            return dict(sess.get('state', {}))
        return {}


def _extract_entities_from_text(text):
    """استخراج الكيانات المفتاحية (فروع، سيارات، منتجات، أسماء) من النص للربط التتابعي."""
    entities = []
    # التقاط أسماء الفروع الصريحة فقط
    branch_matches = re.findall(r'فرع\s+([\u0621-\u064A\s]{3,20})(?:$|[,\.\n\?؟])', text)
    _EXCLUDE_BRANCHES = {'الفرع', 'المركز', 'المتجر', 'الموقع', 'الشركة', 'خدمة', 'الدعم', 'أقرب وقت', 'وقت ممكن', 'نفس اليوم'}
    for b in branch_matches:
        clean_b = b.strip()
        if len(clean_b.split()) <= 2 and clean_b not in _EXCLUDE_BRANCHES and not any(x in clean_b for x in _EXCLUDE_BRANCHES):
            entities.append(clean_b)
    
    # التقاط أسماء المنتجات أو السيارات أو الموديلات
    car_matches = re.findall(r'((?:هيونداي|تويوتا|كيا|مرسيدس|نيسان|بيجو|رينو|شيري|إم جي|ميني باص)\s+[a-zA-Z0-9\u0621-\u064A\s\-]+?)(?:$|[,\.\n\?؟\(\)]|\s+بكام|\s+سعره|\s+سعرها|\s+في|\s+و)', text)
    for c in car_matches:
        clean_c = c.strip()
        if len(clean_c) >= 3 and clean_c not in entities:
            entities.append(clean_c)
            
    return entities


def add_exchange(session_id, query, answer, matches=None, tenant_id=None):
    """إضافة تبادل وتحديث الحالة السياقية للجلسة (State Tracking) مع عزل المستأجر."""
    if not session_id or not query or not answer:
        return
    key = _make_key(session_id, tenant_id)
    with _LOCK:
        if key in _SESSIONS:
            sess = _SESSIONS[key]
            sess['last_active'] = time.time()
            sess['history'].append({'query': query, 'answer': answer})
            if len(sess['history']) > _MAX_HISTORY_PER_SESSION:
                sess['history'] = sess['history'][-_MAX_HISTORY_PER_SESSION:]
            _SESSIONS.move_to_end(key)
        else:
            if len(_SESSIONS) >= _MAX_SESSIONS:
                _SESSIONS.popitem(last=False)
            sess = {
                'last_active': time.time(),
                'history': [{'query': query, 'answer': answer}],
                'state': {}
            }
            _SESSIONS[key] = sess

        # تحديث الحالة والكيانات النشطة
        state = sess.setdefault('state', {})
        
        # استخراج الكيانات من السؤال والإجابة
        ents_in_q = _extract_entities_from_text(query)
        ents_in_a = _extract_entities_from_text(answer)
        all_ents = ents_in_q + [e for e in ents_in_a if e not in ents_in_q]
        
        if all_ents:
            state['last_entities'] = all_ents
            state['active_entity'] = all_ents[0]

        # فحص نمط المقارنة لتسجيل الطرفين (الأول والتاني)
        from engine.pipeline import extract_comparison_targets
        pair = extract_comparison_targets(query)
        if pair:
            state['comparison_pair'] = [pair[0], pair[1]]

        if matches:
            top_chunk, _ = matches[0]
            heading = top_chunk.get('heading', '')
            state['last_heading'] = heading
            state['last_source'] = top_chunk.get('source', '')
            state['last_headings'] = [c.get('heading', '') for c, _ in matches[:3]]
            if 'فرع' in heading:
                state['active_branch'] = heading.replace('فرع', '').strip()


def enrich_followup_query(session_id, current_query):
    """ربط السؤال التتابعي بالسياق وحل الضمائر (Coreference Resolution) تلقائياً."""
    if not session_id or not current_query:
        return current_query
    
    with _LOCK:
        # الجلسات مخزنة بمفتاح مُقيّد بالمستأجر (tenant:session_id) — نفس مفتاح add_exchange
        sess = _SESSIONS.get(_make_key(session_id))
        if not sess:
            return current_query

        # 2-minute time limit check — انتهت المهلة: جلسة جديدة تماماً (حالة + تاريخ)
        if time.time() - sess.get('last_active', 0) > 120:
            sess['state'].clear()
            sess['history'] = []
            return current_query
            
        state = dict(sess.get('state', {}))
        history = list(sess.get('history', []))

    q_clean = current_query.strip()
    words = q_clean.split()
    # إزالة علامات الترقيم من الكلمات قبل المطابقة ("والتاني؟" تكشف الضمير أيضاً)
    clean_words = set()
    for w in words:
        w = re.sub(r'[^\w\u0621-\u064a]', '', w)
        w = w.lstrip('و').lstrip('ف')
        if w:
            clean_words.add(w)
    
    # 1. حل الضمائر الصريحة للأطراف (التاني / الأول / الثاني)
    comp_pair = state.get('comparison_pair') or []
    last_ents = state.get('last_entities') or []
    last_heads = state.get('last_headings') or []
    
    # فحص الإشارة للطرف الثاني — مع تمييز "التاني" الزمنية:
    # "ألغيت تاني يوم" (يتبعها وحدة زمنية) ليست إشارة لطرف مقارنة
    second_cues = {'التاني', 'الثاني', 'التانية', 'الثانية', 'تانى', 'تاني'}
    temporal_after = re.search(
        r'(?:التاني|الثاني|التانية|الثانية|تانى|تاني)\s*'
        r'(?:يوم|يومين|أيام|ايام|ساعة|ساعات|ساعه|أسبوع|اسبوع|شهر|شهور)',
        _norm_q := re.sub(r'\s+', ' ', q_clean))
    if any(w in second_cues for w in clean_words) and not temporal_after:
        target = None
        if len(comp_pair) >= 2:
            target = comp_pair[1]
        elif len(last_ents) >= 2:
            target = last_ents[1]
        elif len(last_heads) >= 2:
            target = last_heads[1]
        if target:
            # استبدال الضمير بالاسم الصريح سواء بدأ بحرف عطف أو بمفرده
            rewritten = re.sub(r'(?:\b|[وف])(?:التاني|الثاني|التانية|الثانية|تانى|تاني)\b', target, q_clean)
            return rewritten

    # فحص الإشارة للطرف الأول
    first_cues = {'الاول', 'الأول', 'الاولى', 'الأولى', 'اول', 'أول'}
    if any(w in first_cues for w in clean_words):
        target = None
        if len(comp_pair) >= 1:
            target = comp_pair[0]
        elif len(last_ents) >= 1:
            target = last_ents[0]
        elif len(last_heads) >= 1:
            target = last_heads[0]
        if target:
            rewritten = re.sub(r'(?:\b|[وف])(?:الاول|الأول|الاولى|الأولى|اول|أول)\b', target, q_clean)
            return rewritten

    # 2. مؤشرات الإشارة العامة (مع استثناء الإشارات الزمنية والظرفية مثل: الشهر ده، الأسبوع ده، السنة دي)
    non_temporal_text = re.sub(r'\b(?:الشهر|الاسبوع|الأسبوع|السنة|السنه|اليوم|الوقت|المرة|المره)\s+(?:ده|دي)\b', ' ', q_clean)
    words_for_cues = non_temporal_text.split()
    clean_cue_words = set()
    for w in words_for_cues:
        w = re.sub(r'[^\w\u0621-\u064a]', '', w)
        w = w.lstrip('و').lstrip('ف')
        if w:
            clean_cue_words.add(w)

    referential_prefixes = ['طيب و', 'طب و', 'طيب هو', 'طب هو', 'طيب هي', 'طب هي', 'طب ده', 'طيب ده', 'وهل ', 'طب ', 'طيب ']
    starts_with_prefix = any(q_clean.startswith(p) for p in referential_prefixes)
    
    referential_cues = {'ده', 'دي', 'هو', 'هي', 'دول', 'برضو', 'كمان', 'سعره', 'سعرها', 'مواعيدهم', 'بيشتغلوا', 'مكانهم', 'وبعدين', 'التاني', 'الثاني', 'التانية', 'الثانية', 'تاني', 'الاول', 'الأول'}
    has_cue = any(w in referential_cues for w in clean_cue_words)
    
    # إذا كان السؤال يحدد كياناً صريحاً بنفسه (مثل: باقة العروسة، فرع المعادي)، فهو سؤال متكامل
    has_explicit_subject = bool(re.search(r'^(?:باقة|خدمة|تأمين|اشتراك|كورس|سيارة|فرع|منتج|برنامج|عرض)\s+[\u0621-\u064A]', q_clean))
    is_followup = not has_explicit_subject and ((len(words) <= 5 and (has_cue or (starts_with_prefix and len(words) <= 4))) or (len(words) <= 7 and has_cue and starts_with_prefix))
    
    if is_followup:
        last_query = history[-1]['query'] if history else ''
        active_ent = state.get('active_entity')
        if active_ent and active_ent not in q_clean:
            return f"{q_clean} لـ {active_ent}"
        elif last_query:
            return f"[السياق من سؤالك السابق: {last_query}] {current_query}"

    return current_query


def clear_sessions():
    """مسح كل الجلسات (مفيد للاختبارات)."""
    with _LOCK:
        _SESSIONS.clear()
