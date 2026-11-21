# -*- coding: utf-8 -*-
"""
engine/calc.py — آلة حاسبة آمنة للعربية (Deterministic Safe Calculator).

تحل أي عملية حسابية **صريحة** في السؤال — بلا eval وبلا تخمين:
- جمع مبالغ مذكورة: «450 و300 جنيه... الإجمالي كام؟» → 750
- زيادة نسبة على مبلغ: «450 ورسوم 25%... إجمالي؟» → 562.5
- خصم نسبة من مبلغ: «خصم 20% من 1800» → 1440
- طرح/ضرب/قسمة بكلمات صريحة (ناقص / في / على / × / ÷)
كل نتيجة تمر على حارس الصحة الحسابية، وأي غموض = رفض (None) لا تخمين.
"""
import re

from engine.premath import _norm


def _nums(text):
    """كل الأرقام بالترتيب [(قيمة، موضع البداية)]."""
    return [(float(m.group(1).replace(',', '.')), m.start(), m.end())
            for m in re.finditer(r'(\d+(?:[.,]\d+)?)', text)]


def _fmt(x):
    x = float(x)
    return int(x) if x == int(x) else x


def _sane(op, operands, result):
    """حارس الصحة: الجمع > كل معامل، الطرح ≥ 0 وأقل من الأكبر، الضرب ≥ الأكبر
    (لمعامل ≥ 1)، القسمة ≤ البسط — وإلا النتيجة مرفوضة."""
    res = float(result)
    nums = [float(n) for n in operands]
    if op == 'add':
        return res > max(nums)
    if op == 'sub':
        return 0 <= res < max(nums)
    if op == 'mul':
        return res >= max(nums)
    if op == 'div':
        return all(abs(n) > 1e-9 for n in nums[1:]) and res <= nums[0]
    return False


def _has_currency(text):
    return bool(re.search(r'جنيه|ج\b|ج\.|مصري', text or ''))


def try_sum(query):
    """جمع مبالغ مذكورة صراحة: «الاستشارة 300 ومراجعة العقد 250 جنيه... كام؟» → 550.
    يستبعد أرقام المدة/الزمن (مدة 2، ساعة) حتى لا يجمع تفاحة وبرتقالاً."""
    n = _norm(query)
    if not re.search(r'اجمالي|إجمالي|كام|هدفع|هيدفع|المجموع', n):
        return None
    if '%' in n:
        return None  # النسب لمسار آخر (percent_add / percent_sub)
    raw = [(float(m.group(1)), m.start(), m.end())
           for m in re.finditer(r'(\d+(?:[.,]\d+)?)', n)]
    # استبعاد أرقام قريبة من كلمات المدة/الزمن
    duration_pos = [m.start() for m in re.finditer(
        r'(?:مدة|المدة|مده|ساعه|ساعة|دقيقة|دقيقه|شهر|يوم|اسبوع|أسبوع)', n)]
    amounts = [(v, a, b) for v, a, b in raw
               if v > 0 and not any(abs(a - dp) < 14 for dp in duration_pos)]
    if len(amounts) < 2:
        return None
    # رابط الجمع: 'و' أو '+' أو '،' داخل الفجوة بين مبلغين متتاليين (≤45 حرفاً)
    joined = False
    for (v1, a1, b1), (v2, a2, b2) in zip(amounts, amounts[1:]):
        gap = n[b1:a2]
        if len(gap) <= 45 and re.search(r'و|\+|،|,', gap):
            joined = True
            break
    if not joined:
        return None
    total = round(sum(v for v, _, _ in amounts), 2)
    if not _sane('add', [v for v, _, _ in amounts], total):
        return None
    parts = ' + '.join(str(_fmt(v)) for v, _, _ in amounts)
    return ('الإجمالي النهائي %s جنيه (%s = %s).'
            % (_fmt(total), parts, _fmt(total)), 'calc')


def try_percent_add(query):
    """زيادة نسبة على مبلغ: «450 ورسوم 25%... إجمالي؟» → 562.5."""
    n = _norm(query)
    m = re.search(
        r'(\d+(?:[.,]\d+)?)\s*جنيه[^0-9%]{0,60}?'
        r'(\d+(?:[.,]\d+)?)\s*(?:%|في الميه|بالمئه)'
        r'[^0-9]{0,40}?(?:رسوم|زياده|اضافه|استعجال)?', n)
    if not m:
        return None
    base, pct = float(m.group(1)), float(m.group(2))
    if not (0 < pct <= 100) or base <= 0:
        return None
    total = round(base * (1 + pct / 100.0), 2)
    if not _sane('mul', [base, 1 + pct / 100.0], total):
        return None
    fee = round(total - base, 2)
    return ('الإجمالي النهائي %s جنيه مصري (%s + %s%% رسوم/زيادة = %s جنيه).'
            % (_fmt(total), _fmt(base), _fmt(pct), _fmt(fee)), 'calc')


def try_percent_sub(query):
    """خصم نسبة من مبلغ صريح: «خصم 20% من 1800» → 1440."""
    n = _norm(query)
    m = re.search(r'خصم\s*(\d+(?:[.,]\d+)?)\s*(?:%|في الميه|بالمئه)\s*من\s*(\d+(?:[.,]\d+)?)', n)
    if not m:
        return None
    pct, base = float(m.group(1)), float(m.group(2))
    if not (0 < pct < 100) or base <= 0:
        return None
    after = round(base * (1 - pct / 100.0), 2)
    if not _sane('sub', [base], after):
        return None
    disc = round(base - after, 2)
    return ('بعد خصم %s%% من %s جنيه = %s جنيه (الخصم %s جنيه).'
            % (_fmt(pct), _fmt(base), _fmt(after), _fmt(disc)), 'calc')


def try_diff(query):
    """الفرق بين رقمين صريحين: «الفرق بين 1200 و450» → 750."""
    n = _norm(query)
    if not re.search(r'الفرق|فرق|الاختلاف', n):
        return None
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:جنيه)?\s*(?:و|\+)\s*(\d+(?:[.,]\d+)?)', n)
    if not m:
        return None
    a, b = float(m.group(1)), float(m.group(2))
    diff = round(abs(a - b), 2)
    if not _sane('sub', [a, b], diff):
        return None
    return ('الفرق بين %s و%s هو %s جنيه.' % (_fmt(a), _fmt(b), _fmt(diff)), 'calc')


def try_calculate(query):
    """المحاولة بالترتيب — أول نمط يطابق يفوز، وأي غموض = None.
    'الفرق' تُفحص قبل الجمع حتى لا يُجمع طرفا مقارنة."""
    for fn in (try_percent_add, try_percent_sub, try_diff, try_sum):
        try:
            r = fn(query)
        except Exception:
            r = None
        if r:
            return r
    return None
