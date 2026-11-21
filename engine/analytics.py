# -*- coding: utf-8 -*-
"""
engine/analytics.py — طبقة تحليلات فجوات المعرفة (KB Gap Analytics).
تجمع الأسئلة التي فشل البوت في الإجابة عنها (تصعيد بسبب انخفاض الثقة أو
عدم يقين النموذج) وتجمعها في موضوعات متكررة، لتقترح على صاحب العمل
"أضف مقالة عن كذا في قاعدة المعرفة".
لا تمس الموديل ولا RAG — تحليل إحصائي خالص فوق التذاكر المخزنة.
"""
from collections import Counter

from engine import rag, store


# أسباب تُعتبر "فجوة معرفة" (البوت عجز عن الإجابة وليس مشكلة عميل)
_GAP_REASONS = {'very_low_confidence', 'bot_uncertain', 'negative_feedback'}


def _gap_tickets(tickets=None):
    tickets = tickets if tickets is not None else store.load_tickets()
    gaps = []
    for t in tickets:
        if t.get('status') != 'escalated':
            continue
        reason = t.get('escalated_reason')
        conf = t.get('confidence') or 0
        if reason in _GAP_REASONS or conf < 0.2:
            # تقييم سيء على إجابة واثقة = شكوى جودة، ليست فجوة معرفة
            if reason == 'negative_feedback' and conf >= 0.5:
                continue
            gaps.append(t)
    return gaps


def _topic_tokens(query):
    """أهم كلمات السؤال (بعد التطبيع والتجذيع واستبعاد كلمات الوظيف)."""
    toks = [t for t in rag.tokenize(query) if t not in rag.STOPWORDS and len(t) >= 3]
    # إزالة التكرار داخل السؤال الواحد حتى لا يكتسب السؤال الطويل وزناً زائداً
    return list(dict.fromkeys(toks))


def compute_kb_gaps(tickets=None, min_count=2, top_n=10):
    """تجميع أسئلة العجز في موضوعات متكررة مرتبة بالأكثر تكراراً.
    كل موضوع يعرض الكلمة المحورية + أشهر كلمة مصاحبة (سياق أوضح للمراجعة)."""
    gaps = _gap_tickets(tickets)
    token_counter = Counter()
    token_queries = {}
    co_occur = Counter()

    for t in gaps:
        q = (t.get('query') or '').strip()
        if not q:
            continue
        toks = set(_topic_tokens(q))
        for tok in toks:
            token_counter[tok] += 1
            token_queries.setdefault(tok, []).append(q)
        for a in toks:
            for b in toks:
                if a < b:
                    co_occur[(a, b)] += 1

    def companion(tok):
        best, best_n = '', 0
        for (a, b), n in co_occur.items():
            if tok in (a, b) and n > best_n:
                other = b if a == tok else a
                best, best_n = other, n
        return best if best_n >= min_count else ''

    topics = []
    for tok, count in token_counter.most_common():
        if count < min_count:
            continue
        samples = list(dict.fromkeys(token_queries[tok]))[:3]
        topics.append({
            'topic': tok,
            'companion': companion(tok),
            'count': count,
            'sample_queries': samples,
        })
        if len(topics) >= top_n:
            break

    return {
        'gap_tickets': len(gaps),
        'total_tickets': len(tickets if tickets is not None else store.load_tickets()),
        'topics': topics,
    }


def format_gap_report(top_n=10):
    """تقرير نصي جاهز للعرض أو الإرسال لصاحب العمل."""
    rep = compute_kb_gaps(top_n=top_n)
    lines = [
        '📊 تقرير فجوات قاعدة المعرفة',
        'أسئلة عجز البوت عنها: %d من %d تذكرة' % (rep['gap_tickets'], rep['total_tickets']),
    ]
    if not rep['topics']:
        lines.append('✅ لا توجد فجوات متكررة — قاعدة المعرفة تغطي أسئلة العملاء.')
    else:
        lines.append('أكثر الموضوعات تكراراً (يُنصح بإضافة مقالة لكل منها):')
        for i, t in enumerate(rep['topics'], 1):
            lines.append('%d. [%d مرة] "%s" — مثال: %s' %
                         (i, t['count'], t['topic'], t['sample_queries'][0][:80]))
    return '\n'.join(lines)
