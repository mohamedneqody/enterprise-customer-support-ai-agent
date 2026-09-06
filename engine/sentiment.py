# -*- coding: utf-8 -*-
"""
تحليل غضب العميل (Sentiment مبسط بدون مكتبات):
كلمات الغضب + عدد مراتها + علامات التعجب + الحروف الكابيتال + الإيموجي.
النتيجة من 0 لـ 1؛ لو عدّت 0.5 العميل يعتبر "غاضب" => تذكرة أولوية.
"""
from engine.rag import normalize, stem, tokenize

_RAW_ANGER = [
    # عربي (بيتطبع وتتجذع نفس ما بيتطبع نص العميل)
    'غاضب', 'غاضبة', 'غضبان', 'غضبانة', 'معصب', 'متعصب', 'زعلان', 'زعلانة',
    'زهقت', 'تعبت', 'مقبوض', 'منفعل', 'مستفز', 'مستفزة', 'مقهور', 'قرفان',
    'فضيحة', 'نصب', 'احتيال', 'غش', 'سرقة', 'سرقوا', 'حرامية', 'سارقين',
    'أسوأ', 'اسوا', 'فظيع', 'كارثة', 'مبهدل', 'محرج', 'مهين', 'كفاية',
    'اشتكي', 'شكوى', 'محامي', 'قانوني', 'تهديد',
    # إنجليزي
    'angry', 'furious', 'terrible', 'worst', 'awful', 'unacceptable', 'scam',
    'fraud', 'ridiculous', 'disgusting', 'pathetic', 'annoyed', 'useless',
    'garbage', 'trash', 'hate', 'disgusted',
]

ANGER_WORDS = set(stem(normalize(w)) for w in _RAW_ANGER)

_ANGRY_EMOJIS = (u'\U0001F621', u'\U0001F620', u'\U0001F92C', u'\U0001F624')  # 😡😠🤬😤


def anger_score(message):
    """درجة الغضب من 0 لـ 1 (heuristic للعرض التعليمي).

    القاعدة: كل كلمة غضب بتضيف 0.45 (حتى لا تضيع في جملة طويلة)،
    مضاف إليها كثافة الكلمات الغاضبة في أول 12 كلمة.
    """
    msg = message or ''
    if not msg.strip():
        return 0.0
    tokens = [t for t in tokenize(msg) if len(t) >= 2]
    hits = sum(1 for t in tokens if t in ANGER_WORDS)
    if hits == 0:
        base = 0.0
    else:
        density = float(hits) / max(3, min(len(tokens), 12))
        base = min(0.85, 0.45 * hits + density)

    bonus = 0.0
    # علامات تعجب متكررة
    bangs = msg.count('!') + msg.count(u'\uff01')
    if bangs >= 4:
        bonus += 0.25
    elif bangs >= 2:
        bonus += 0.15
    # كابيتال في الإنجليزي
    letters = [c for c in msg if c.isalpha() and ord(c) < 128]
    if len(letters) >= 6:
        upper = sum(1 for c in letters if c.isupper())
        if float(upper) / len(letters) > 0.7:
            bonus += 0.15
    # إيموجي غضب
    if any(e in msg for e in _ANGRY_EMOJIS):
        bonus += 0.2
    return round(min(1.0, base + bonus), 3)
