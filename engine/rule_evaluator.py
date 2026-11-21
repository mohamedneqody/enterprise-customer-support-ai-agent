# -*- coding: utf-8 -*-
"""
محرك التقييم الرمزي والحسابي العام (Generalized Symbolic Rule & Math Evaluator):
- يستخرج الأرقام، النسب، الكسور، والمدد الزمنية من السؤال بصورة عامة ومجردة.
- يقارنها بالعتبات والشروط الواردة في السياق، ومع تعذر ذلك يسقط تلقائياً على
  مصفوفة السياسات المستخرجة آلياً (policies.json) كمصدر ثانٍ موثوق للعتبات.
- يدعم:
  1. مقارنة النسب المئوية والكسور العربية (ربع، نصف، ثلث، إلخ) مع حماية من
     غموض الكلمات (مثل "عشر" بمعنى 10% مقابل "عشر أيام" بمعنى مدة).
  2. مقارنة المهل الزمنية بعد توحيد كل الوحدات إلى ساعات (48 ساعة = يومين).
  3. العمليات الحسابية الأساسية (الجمع، التقسيط، الخصم، رسوم التوصيل).
- يعيد نص حقيقة رقمية قطعية موثقة (Deterministic Fact) لدمجها في الـ Prompt.
"""
import re


# خريطة الكسور اللغوية في العربية
ARABIC_FRACTIONS = {
    'ربع': 25.0,
    'نصف': 50.0,
    'نص': 50.0,
    'ثلث': 33.33,
    'خمس': 20.0,
    'سدس': 16.67,
    'سبع': 14.28,
    'ثمن': 12.5,
    'تسع': 11.11,
    'عشر': 10.0
}

# لو تلت الكلمة الكسرية بوحدة زمن أو عد أو عملة فهي ليست نسبة (مثل: "عشر أيام")
_FRACTION_AMBIGUITY_GUARD = re.compile(
    r'\s*(?:الأيام|الايام|أيام|ايام|يوم|ساعة|ساعات|شهور|شهر|أشهر|اشهر|مرة|مرات|سنة|سنوات|جنيه|ريال|درهم|كيلو|كيلومتر|طلبات|طلبات)')

ARABIC_TIME_UNITS = {
    'يومين': (2, 'يوم'),
    'ساعتين': (2, 'ساعة'),
    'أسبوعين': (2, 'أسبوع'),
    'شهرين': (2, 'شهر')
}

# وحدة المقارنة المشتركة: كل شيء يتحول لساعات
_UNIT_HOURS = {'دقيقة': 1.0 / 60, 'دقائق': 1.0 / 60, 'ساعة': 1.0, 'ساعات': 1.0,
               'يوم': 24.0, 'أيام': 24.0, 'ايام': 24.0,
               'أسبوع': 168.0, 'اسبوع': 168.0, 'شهر': 720.0, 'شهور': 720.0}


def _to_hours(value, unit):
    factor = _UNIT_HOURS.get(unit)
    if factor is None:
        for known, f in _UNIT_HOURS.items():
            if unit.startswith(known[:3]):
                factor = f
                break
    return float(value) * (factor if factor is not None else 1.0)


def extract_user_percentage(query):
    """استخراج أي نسبة مئوية أو كسر لغوي من سؤال العميل (مع حماية الغموض)."""
    q_clean = query.lower()

    # 1. نسبة مئوية مباشرة بصيغة رقم
    pct_m = re.search(r'(\d+(?:\.\d+)?)\s*%', q_clean)
    if pct_m:
        return float(pct_m.group(1))

    # 2. كسر لغوي عربي — مع استبعاد الكلمة لو تلتها وحدة زمن/عد/عملة
    for frac_word, frac_val in ARABIC_FRACTIONS.items():
        m = re.search(r'\b' + frac_word + r'\b', q_clean)
        if m and not _FRACTION_AMBIGUITY_GUARD.match(q_clean[m.end():]):
            return frac_val

    return None


def extract_user_time_value(query):
    """استخراج قيمة زمنية من السؤال (ساعات أو أيام أو دقائق أو صيغ ثنائية)."""
    q_clean = query.lower()

    for word, (val, unit) in ARABIC_TIME_UNITS.items():
        if word in q_clean:
            return float(val), unit

    m = re.search(r'(\d+(?:\.\d+)?)\s*(دقيقة|دقائق|ساعة|ساعات|يوم|أيام|ايام|أسبوع|اسبوع|شهر|شهور)', q_clean)
    if m:
        return float(m.group(1)), m.group(2)

    return None, None


def _policies_percentage_thresholds(policies):
    """جمع عتبات النسبة الفريدة من مصفوفة السياسات المستخرجة."""
    vals = set()
    for t in (policies or {}).get('thresholds', []):
        if t.get('type') == 'percentage' and t.get('value') is not None:
            vals.add(float(t['value']))
    return list(vals)


def _policies_time_thresholds_hours(policies):
    """جمع عتبات الوقت الفريدة من مصفوفة السياسات (موحدة بالساعات)."""
    hours = set()
    for t in (policies or {}).get('thresholds', []):
        if t.get('type') == 'time' and t.get('value') is not None:
            try:
                hours.add(round(_to_hours(float(t['value']), str(t.get('unit', 'ساعة'))), 6))
            except (TypeError, ValueError):
                continue
    return list(hours)


def _fmt_pct(v):
    return f"{int(v)}%" if float(v).is_integer() else f"{v}%"


def evaluate_percentage_threshold(query, context, policies=None):
    """
    مقارنة عامة لأي نسبة مئوية في السؤال بعتبة الاسترداد أو الإنجاز في السياق.
    مثال: 25% إنجاز مقابل حد أقصى 20% للاسترداد.
    مصادر العتبة بالترتيب: regex على السياق، ثم مصفوفة السياسات policies.json.
    """
    # استبعاد أسئلة الخصومات المباشرة
    if any(k in query for k in ['خصم', 'تخفيض', 'كوبون']):
        return None

    user_pct = extract_user_percentage(query)
    if user_pct is None:
        return None

    # البحث عن عتبة النسبة المئوية في السياق
    patterns = [
        r'(?:ألا\s*يتجاوز|لا\s*يتجاوز|ما\s*(?:أتمش|اتمش|يكونش\s*أتم)\s*أكتر\s*من|خلال\s*أول|بشرط\s*عدم\s*تجاوز|أقل\s*من)\s*(\d+(?:\.\d+)?)\s*%',
        r'(\d+(?:\.\d+)?)\s*%\s*(?:أو\s*أقل|كحد\s*أقصى|فقط)'
    ]

    thresholds = []
    for pat in patterns:
        for m in re.finditer(pat, context):
            thresholds.append(float(m.group(1)))

    thresholds = list(set(thresholds))
    if len(thresholds) != 1:
        # Fallback: العتبة من مصفوفة السياسات المستخرجة آلياً عند الرفع
        policy_thresholds = _policies_percentage_thresholds(policies)
        if len(policy_thresholds) == 1:
            thresholds = policy_thresholds
        else:
            return None

    th = thresholds[0]
    user_pct_disp = _fmt_pct(user_pct)
    th_disp = _fmt_pct(th)

    if user_pct > th:
        return f"لأ، {user_pct_disp} > {th_disp}، غير مؤهل للاسترداد."
    else:
        return f"نعم، {user_pct_disp} <= {th_disp}، مؤهل للاسترداد طبقاً للشروط."


def evaluate_time_advance_threshold(query, context, policies=None):
    """
    مقارنة عامة للمدد الزمنية المسبقة للإلغاء/الحجز.
    مثال: العميل يسأل عن إلغاء قبل 36 ساعة، والشرط ينص على 'قبل 48 ساعة على الأقل'.
    المقارنة بعد توحيد الوحدات إلى ساعات: أيام/أسابيع/صيغ ثنائية (يومين) كلها مدعومة.
    """
    time_val, unit = extract_user_time_value(query)
    if time_val is None:
        return None
    user_hours = _to_hours(time_val, unit)

    # البحث عن شرط زمني مسبق في السياق (رقمي أو صيغة ثنائية)
    required_hours = None
    m = re.search(r'(?:قبل|بمدة\s*لا\s*تقل\s*عن|لا\s*تقل\s*عن|في\s*غضون)\s*(\d+(?:\.\d+)?)\s*(ساعة|ساعات|يوم|أيام|ايام|أسبوع|اسبوع|شهر|شهور)', context)
    if m:
        required_hours = _to_hours(float(m.group(1)), m.group(2))
    else:
        dual = re.search(r'(?:قبل|لا\s*تقل\s*عن|في\s*غضون)\s*(يومين|ساعتين|أسبوعين|اسبوعين|شهرين)', context)
        if dual:
            val, u = ARABIC_TIME_UNITS[dual.group(1)]
            required_hours = _to_hours(val, u)

    if required_hours is None:
        # Fallback: العتبة الزمنية من مصفوفة السياسات المستخرجة
        policy_hours = _policies_time_thresholds_hours(policies)
        if len(policy_hours) == 1:
            required_hours = policy_hours[0]
        else:
            return None

    user_disp = _fmt_hours(user_hours)
    req_disp = _fmt_hours(required_hours)
    if user_hours < required_hours:
        return (f"لأ، {user_disp} أقل من الحد الأدنى المطلوب للإلغاء المجاني ({req_disp})، "
                f"غير مؤهل للاسترداد.")
    else:
        return (f"نعم، {user_disp} تستوفي شرط الإلغاء المسبق ({req_disp})، "
                f"مؤهل للإلغاء/الاسترداد.")


def _fmt_hours(hours):
    """عرض مدة موحدة بالأيام أو الساعات أيهما أوضح."""
    if hours >= 24 and abs(hours % 24) < 1e-6:
        days = int(hours // 24)
        if days == 1:
            return "يوم (24 ساعة)"
        if days == 2:
            return "يومين (48 ساعة)"
        return f"{days} أيام ({int(hours)} ساعة)"
    return f"{int(hours)} ساعة" if float(hours).is_integer() else f"{hours} ساعة"


def evaluate_arithmetic_expressions(query, context):
    """
    حل العمليات الحسابية العامة (الجمع، التقسيط، الخصم المتعدد).
    يستبدل الحالات الخاصة في math_helper بحسابات عامة.
    """
    from engine import math_helper
    # استدعاء العمليات الحسابية المنطقية (الكيلومترات، الأقساط، التوصيل)
    return math_helper.extract_numbers_and_context(query, [( {'text': context, 'heading': '', 'source': ''}, 1.0 )])


def evaluate_claim_limit(query, context):
    """فحص الحد الأقصى للمطالبات السنوية مقارنة بعدد مطالبات العميل."""
    m_limit = re.search(r'الحد الأقصى للمطالبات سنوياً:\s*(\d+)\s*مطالبات', context)
    if not m_limit:
        return None
    max_claims = int(m_limit.group(1))
    
    claim_num = None
    if re.search(r'\b(?:رابع[ةه]|الرابع[ةه])\b|مطالب[ةه]\s*4\b', query):
        claim_num = 4
    elif re.search(r'\b(?:خامس[ةه]|الخامس[ةه])\b|مطالب[ةه]\s*5\b', query):
        claim_num = 5
    elif re.search(r'\b(?:ثالث[ةه]|الثالث[ةه])\b|مطالب[ةه]\s*3\b', query):
        claim_num = 3
    elif re.search(r'\b(?:ثاني[ةه]|الثاني[ةه])\b|مطالب[ةه]\s*2\b', query):
        claim_num = 2
        
    if claim_num is not None:
        if claim_num <= max_claims:
            return f"نعم، العملية أو المطالبة رقم ({claim_num}) مقبولة لأن الحد الأقصى السنوي المسموح هو ({max_claims}) في السنة."
        else:
            return f"لا، العملية أو المطالبة رقم ({claim_num}) لا تُقبل لأنها تتجاوز الحد الأقصى السنوي المسموح ({max_claims}) في السنة."
    return None


def evaluate_delay_compensation(query, context):
    """
    تقييم شروط تأخير تقديم الخدمة ومقارنتها بالحد الأدنى للتعويض:
    لو سأل العميل: لو تأخرت X دقيقة/ساعة هل فيه تعويض/خصم؟
    والسياق ينص: لو تأخرت أكثر من Y دقيقة/ساعة يحصل على خصم Z%.
    """
    if not any(k in query for k in ['تأخر', 'تاخر', 'اتأخر', 'اتاخر', 'تأخير', 'تاخير']):
        return None
    if not any(k in query for k in ['تعويض', 'خصم', 'استرداد', 'فلوس', 'حق', 'تلقائ']):
        return None
    
    user_val, user_unit = extract_user_time_value(query)
    if user_val is None:
        return None
    user_hours = _to_hours(user_val, user_unit)
    
    # البحث عن شرط التأخير ونسبة الخصم/التعويض في السياق
    # مثال: تأخرت ... أكثر من 15 دقيقة ... خصم 10%
    m = re.search(r'(?:تأخرت?|تأخير|تاخرت?|تاخير).*?(?:أكتر\s*من|أكثر\s*من|تجاوز|تزيد\s*عن|أكبر\s*من)\s*(\d+(?:\.\d+)?)\s*(دقيقة|دقائق|ساعة|ساعات).*?(?:خصم|تعويض).*?(\d+(?:\.\d+)?)\s*%', context)
    if m:
        th_val = float(m.group(1))
        th_unit = m.group(2)
        th_hours = _to_hours(th_val, th_unit)
        pct = float(m.group(3))
        pct_disp = f"{int(pct)}%" if pct.is_integer() else f"{pct}%"
        
        if user_hours >= th_hours:
            return (f"نعم، مدة التأخير ({int(user_val)} {user_unit}) تستوفي شرط التعويض "
                    f"(أكثر من {int(th_val)} {th_unit})، والتعويض المستحق بموجب السياسة هو خصم قدره {pct_disp} تحديداً.")
        else:
            return (f"لا، مدة التأخير ({int(user_val)} {user_unit}) أقل من الحد الأدنى للتعويض "
                    f"({int(th_val)} {th_unit}).")
    return None


def evaluate_unconditional_exception(query, context):
    """
    فحص وتأكيد الاستثناءات الصريحة غير المشروطة (Unconditional Exceptions):
    يبحث عن أنماط الاستثناء القطعي في السياق (مثل: مستثنى دائماً، معفى تماماً،
    بغض النظر عن، حالة خاصة، استثناء صريح)، ويتحقق مما إذا كان استفسار العميل
    يستهدف نفس موضوع الاستثناء لتقرير إعفائه أو تطبيق الاستثناء بحسم.
    تصميم عام 100% بدون أي كلمات مفتاحية لمجال بعينه.
    """
    patterns = [
        r'(?:مستثن[اى]ة?|معف[اى]ة?)\s*(?:دائماً|تماماً|بشكل\s*قاطع|تلقائياً)',
        r'بغض\s*النظر\s*عن\s*([^\.\n،\)]+)',
        r'(?:استثناء\s*خاص|حالة\s*خاصة)\s*[:\-]?\s*([^\.\n]+)',
        r'(?:إلا|باستثناء)\s+(?:في\s+حالة\s+)?([^\.\n،\)]+)'
    ]
    
    found_exceptions = []
    for pat in patterns:
        for m in re.finditer(pat, context):
            clause = m.group(0).strip().rstrip(')]}')
            end_idx = m.end()
            if end_idx < len(context) and context[end_idx:end_idx + 2].startswith(('\n', '\r\n')):
                remainder = context[end_idx:].strip()
                if remainder and not remainder.startswith('#'):
                    next_clause = re.split(r'\n\#+|\n\n', remainder)[0].strip()
                    first_sent = re.split(r'[\.\n]', next_clause)[0].strip()
                    if first_sent:
                        clause = f"{clause}: {first_sent}".strip()
            found_exceptions.append(clause)
            
    if not found_exceptions:
        return None
        
    q_words = set(re.findall(r'[\u0621-\u064A\w]{3,}', query))
    stopwords = {'هل', 'لو', 'في', 'من', 'على', 'عن', 'مع', 'هذا', 'هذه', 'بين', 'ممكن', 'ينفع', 'حالة', 'خاصة', 'سؤال', 'استفسار'}
    q_tokens = q_words - stopwords
    
    if not q_tokens:
        return None
        
    for exc in found_exceptions:
        exc_tokens = set(re.findall(r'[\u0621-\u064A\w]{3,}', exc)) - stopwords
        overlap = q_tokens & exc_tokens
        if len(overlap) >= 1:
            return f"نعم، ينطبق الاستثناء الصريح الوارد في الشروط ({exc})، وتتم المعاملة وفق هذا الاستثناء بحسم."
            
    return None


def _sane_arithmetic(op, operands, result):
    """
    حارس الصحة الحسابية الإجباري: أي عملية تعيد نتيجة يجب أن تكون منطقية حسابياً
    فعلاً — الطرح يُنتج أصغر من أكبر معامل (وغير سالب للفروق)، والجمع يُنتج أكبر
    من كل معامل، والضرب في معامل > 1 يُنتج أكبر من الأساس. غير ذلك: نرفض النتيجة.
    """
    nums = [float(x) for x in operands if x is not None]
    if not nums or result is None:
        return False
    res = float(result)
    if op == 'sub':
        return 0 <= res < max(nums)
    if op == 'add':
        return res > max(nums)
    if op == 'mul':
        return res >= max(nums)
    if op == 'div':
        return all(abs(n) > 1e-9 for n in nums) and res <= max(nums)
    return True


def _parse_service_blocks(context):
    """تقسيم السياق لبطاقات خدمات: كل بطاقة (اسم، سعر price_egp، نسبة طوارئ).

    يدعم شكلين:
    1) ملفات مجمعة بعناوين ## — الاسم من سطر العنوان والحقول بعده.
    2) قطع فهرس RAG — نص مباشر "name: ...\nprice_egp: ..." بلا ##.

    name = اسم نظيف من الأقواس للمطابقة، display = النص الأصلي للعرض.
    """
    blocks = []
    for m in re.finditer(r'##\s*(.+?)\n(.*?)(?=\n##|\Z)', context, re.DOTALL):
        raw_name = re.sub(r'\s+', ' ', m.group(1)).strip()
        body = m.group(2)
        price_m = re.search(r'price_egp:\s*(\d+(?:\.\d+)?)', body)
        emg_m = re.search(r'emergency_surcharge_pct:\s*(\d+(?:\.\d+)?)', body)
        if raw_name and price_m:
            blocks.append({
                'name': re.sub(r'\s+', ' ', re.sub(r'\([^)]*\)', ' ', raw_name)).strip(),
                'display': raw_name,
                'price': float(price_m.group(1)),
                'emergency_pct': float(emg_m.group(1)) if emg_m else None,
            })

    # شكل قطع الفهرس: name: X على أول سطر وprice_egp بعده (بلا ##)
    for m in re.finditer(r'name:\s*([^\n]+)\n(?:(?!name:|##)[^\n]*\n)*?price_egp:\s*(\d+(?:\.\d+)?)', context):
        raw_name = re.sub(r'\s+', ' ', m.group(1)).strip()
        price = float(m.group(2))
        # تجاهل بطاقة اسمها يحوي "price" (خطأ تحليل) أو مكررة
        if not raw_name or raw_name in (b['display'] for b in blocks):
            continue
        emg_m = re.search(r'emergency_surcharge_pct:\s*(\d+(?:\.\d+)?)', m.group(0))
        blocks.append({
            'name': re.sub(r'\s+', ' ', re.sub(r'\([^)]*\)', ' ', raw_name)).strip(),
            'display': raw_name,
            'price': price,
            'emergency_pct': float(emg_m.group(1)) if emg_m else None,
        })
    return blocks


def _strip_affixes(words):
    """تنظيف كلمات المطابقة: قلع أدوات التعريف والعطف (ال/و/أو) —
    "الكشف العام" في السؤال يطابق "كشف عام" في الكتالوج."""
    out = set()
    for w in words:
        w2 = w
        if w2.startswith('ال') and len(w2) > 4:
            w2 = w2[2:]
        w2 = w2.lstrip('و')
        if len(w2) >= 3:
            out.add(w2)
            # أيضاً الشكل بعد قلع ال فقط دون و (والأشعة -> أشعة)
            if w2.startswith('ال') and len(w2) > 4:
                out.add(w2[2:])
    return out


def _service_query_hit(query, service_name):
    """هل اسم الخدمة مذكور في السؤال (بمطابقة كلمات دالة بعد قلع أدوات التعريف)؟"""
    q_words = _strip_affixes(re.findall(r'[\u0621-\u064A]{3,}', query))
    s_words = _strip_affixes(re.findall(r'[\u0621-\u064A]{3,}', service_name))
    return bool(s_words) and bool(q_words & s_words)


def evaluate_comparison_price_diff(query, context):
    """
    المطلوب 1 — الفرق بين سعرين لخدمتين مختلفتين:
    طرح حتمي مباشر من السعرين الأساسيين في الكتالوج (price_egp) —
    بلا عربون أو خصم أو رسوم إلا لو طُلبت صراحة. النتيجة تُحقن كحقيقة قطعية
    بعد اجتياز حارس الصحة الحسابية (الطرح أصغر من الأكبر وغير سالب).
    """
    if not re.search(r'فرق|الفارق|أرخص|اغلى|أغلى', query):
        return None
    blocks = _parse_service_blocks(context)
    if len(blocks) < 2:
        return None
    # الخدمتان المذكورتان صراحة في السؤال فقط
    hits = [b for b in blocks if _service_query_hit(query, b['name'])]
    if len(hits) < 2:
        return None
    p1, p2 = hits[0]['price'], hits[1]['price']
    diff = round(abs(p2 - p1), 2)
    if not _sane_arithmetic('sub', [p1, p2], diff):
        return None
    hi, lo = (hits[0], hits[1]) if p1 >= p2 else (hits[1], hits[0])
    d_str = int(diff) if diff == int(diff) else diff
    hi_p = int(hi['price']) if hi['price'] == int(hi['price']) else hi['price']
    lo_p = int(lo['price']) if lo['price'] == int(lo['price']) else lo['price']
    return {'fact': (f"الفرق في السعر بين {hi['display']} ({hi_p} جنيه) و{lo['display']} ({lo_p} جنيه) "
                     f"= {hi_p} - {lo_p} = {d_str} جنيه بالضبط، محسوب من السعرين الأساسيين في الكتالوج."),
            # نص الإجابة خالٍ تماماً من كلمة "عربون" — النموذج لا يجب أن يراها أصلاً
            'final_answer': (f"الفرق في السعر بين {hi['display']} و{lo['display']} هو {d_str} جنيه: "
                             f"{hi['display']} {hi_p} جنيه، و{lo['display']} {lo_p} جنيه.")}


def evaluate_emergency_surcharge(query, context):
    """
    المطلوب 3 — الحساب الحتمي لرسوم الطوارئ:
    لو السؤال يذكر الطوارئ وخدمة لها emergency_surcharge_pct > 0 في الكتالوج:
    الإجمالي = السعر × (1 + النسبة/100). يُرفض لو النتيجة ليست أكبر من السعر.
    """
    if not re.search(r'طارئ|طوارئ|طارئة|emergency', query, re.IGNORECASE):
        return None
    for b in _parse_service_blocks(context):
        if b['emergency_pct'] and b['emergency_pct'] > 0 and _service_query_hit(query, b['name']):
            total = round(b['price'] * (1 + b['emergency_pct'] / 100.0), 2)
            if not _sane_arithmetic('mul', [b['price'], 1 + b['emergency_pct'] / 100.0], total):
                continue
            t_str = int(total) if total == int(total) else total
            p_str = int(b['price']) if b['price'] == int(b['price']) else b['price']
            e_str = int(b['emergency_pct']) if b['emergency_pct'] == int(b['emergency_pct']) else b['emergency_pct']
            surcharge = round(total - b['price'], 2)
            s_str = int(surcharge) if surcharge == int(surcharge) else surcharge
            return {'fact': (f"حساب رسوم الطوارئ لخدمة {b['name']}: السعر الأساسي {p_str} جنيه + نسبة طوارئ "
                             f"{e_str}% = {p_str} × (1 + {e_str}/100) = {t_str} جنيه إجمالي بالضبط."),
                    'final_answer': (f"سعر {b['display']} الأساسي {p_str} جنيه، ورسوم الحالة الطارئة {e_str}% "
                                     f"({s_str} جنيه)، فالإجمالي الكلي = {t_str} جنيه.")}
    return None


def evaluate_all_rules(query, context, policies=None):
    """
    المعالج الشامل: يفحص كافة القواعد الرياضية والزمنية ويعيد نتيجة قطعية
    إن وجدت، أو None في حالة عدم وجود شرط حسابي.
    """
    # 0. المقارنات السعرية بين خدمتين — الأولوية القصوى (فرق مباشر من الأسعار الأساسية)
    diff_res = evaluate_comparison_price_diff(query, context)
    if diff_res:
        return f"[نتيجة المقارنة السعرية المؤكدة قطعياً: {diff_res}]"

    # 0.5 حساب رسوم الطوارئ من الكتالوج
    emg_res = evaluate_emergency_surcharge(query, context)
    if emg_res:
        return f"[نتيجة حساب رسوم الطوارئ المؤكدة قطعياً: {emg_res}]"

    # 1. تقييم نسب الإنجاز والاسترداد
    pct_res = evaluate_percentage_threshold(query, context, policies)
    if pct_res:
        return f"[نتيجة المقارنة الحسابية المؤكدة قطعياً: {pct_res}]"

    # 2. تقييم المهل الزمنية المسبقة
    time_res = evaluate_time_advance_threshold(query, context, policies)
    if time_res:
        return f"[نتيجة المقارنة الزمنية المؤكدة قطعياً: {time_res}]"

    # 2.5 تقييم شروط تعويض التأخير
    delay_res = evaluate_delay_compensation(query, context)
    if delay_res:
        return f"[نتيجة فحص شرط التأخير والتعويض المؤكدة قطعياً: {delay_res}]"

    # 2.6 تقييم الاستثناءات الصريحة غير المشروطة
    exc_res = evaluate_unconditional_exception(query, context)
    if exc_res:
        return f"[نتيجة فحص الاستثناء الصريح المؤكدة قطعياً: {exc_res}]"

    # 3. تقييم حدود المطالبات السنوية
    claim_res = evaluate_claim_limit(query, context)
    if claim_res:
        return f"[نتيجة فحص حد المطالبات المؤكدة قطعياً: {claim_res}]"

    # 4. تقييم العمليات الحسابية الأخرى (أسعار، تقسيط، رسوم)
    arithmetic_hint = evaluate_arithmetic_expressions(query, context)
    if arithmetic_hint:
        return arithmetic_hint

    return None


