# -*- coding: utf-8 -*-
"""
محرك التقييم الرمزي والحسابي العام (Generalized Symbolic Rule & Math Evaluator):
- يستخرج الأرقام، النسب، الكسور، والمدد الزمنية من السؤال بصورة عامة ومجردة.
- يقارنها بالعتبات والشروط الواردة في السياق أو جدول السياسات المستخرج.
- يدعم:
  1. مقارنة النسب المئوية والكسور العربية (ربع، نصف، ثلث، إلخ).
  2. مقارنة المهل الزمنية والساعات (مثل: 36 ساعة مقابل شرط 48 ساعة).
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

ARABIC_TIME_UNITS = {
    'يومين': (2, 'يوم'),
    'ساعتين': (2, 'ساعة'),
    'أسبوعين': (2, 'أسبوع'),
    'شهرين': (2, 'شهر')
}


def extract_user_percentage(query):
    """استخراج أي نسبة مئوية أو كسر لغوي من سؤال العميل."""
    q_clean = query.lower()
    
    # 1. نسبة مئوية مباشرة بصيغة رقم
    pct_m = re.search(r'(\d+(?:\.\d+)?)\s*%', q_clean)
    if pct_m:
        return float(pct_m.group(1))
        
    # 2. كسر لغوي عربي
    for frac_word, frac_val in ARABIC_FRACTIONS.items():
        if re.search(r'\b' + frac_word + r'\b', q_clean):
            return frac_val
            
    return None


def extract_user_time_value(query):
    """استخراج قيمة زمنية من السؤال (ساعات أو أيام)."""
    q_clean = query.lower()
    
    for word, (val, unit) in ARABIC_TIME_UNITS.items():
        if word in q_clean:
            return val, unit
            
    m = re.search(r'(\d+(?:\.\d+)?)\s*(ساعة|ساعات|يوم|أيام|ايام)', q_clean)
    if m:
        return float(m.group(1)), m.group(2)
        
    return None, None


def evaluate_percentage_threshold(query, context):
    """
    مقارنة عامة لأي نسبة مئوية في السؤال بعتبة الاسترداد أو الإنجاز في السياق.
    مثال: 25% إنجاز مقابل حد أقصى 20% للاسترداد.
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
    if len(thresholds) == 1:
        th = thresholds[0]
        user_pct_disp = f"{int(user_pct)}%" if user_pct.is_integer() else f"{user_pct}%"
        th_disp = f"{int(th)}%" if th.is_integer() else f"{th}%"
        
        # إذا كانت العتبة حد أقصى للإنجاز (مثل: استرداد بشرط ألا يتجاوز 20%)
        if user_pct > th:
            return f"لأ، {user_pct_disp} > {th_disp}، غير مؤهل للاسترداد."
        else:
            return f"نعم، {user_pct_disp} <= {th_disp}، مؤهل للاسترداد طبقاً للشروط."
            
    return None


def evaluate_time_advance_threshold(query, context):
    """
    مقارنة عامة للمدد الزمنية المسبقة للإلغاء/الحجز.
    مثال: العميل يسأل عن إلغاء قبل 36 ساعة، والشرط ينص على 'قبل 48 ساعة على الأقل'.
    """
    time_val, unit = extract_user_time_value(query)
    if time_val is None:
        return None
        
    # توحيد الوحدات بالساعات إذا كانت بالساعات
    if 'ساع' in unit:
        # البحث عن شرط "قبل X ساعة"
        m = re.search(r'(?:قبل|بمدة\s*لا\s*تقل\s*عن)\s*(\d+(?:\.\d+)?)\s*(?:ساعة|ساعات)', context)
        if m:
            required_hours = float(m.group(1))
            user_h_disp = f"{int(time_val)}"
            req_h_disp = f"{int(required_hours)}"
            if time_val < required_hours:
                return f"لأ، {user_h_disp} ساعة أقل من الحد الأدنى المطلوب للإلغاء المجاني ({req_h_disp} ساعة)، غير مؤهل للاسترداد."
            else:
                return f"نعم، {user_h_disp} ساعة تستوفي شرط الإلغاء المسبق ({req_h_disp} ساعة)، مؤهل للإلغاء/الاسترداد."
                
    return None


def evaluate_arithmetic_expressions(query, context):
    """
    حل العمليات الحسابية العامة (الجمع، التقسيط، الخصم المتعدد).
    يستبدل الحالات الخاصة في math_helper بحسابات عامة.
    """
    from engine import math_helper
    # استدعاء العمليات الحسابية المنطقية (الكيلومترات، الأقساط، التوصيل)
    return math_helper.extract_numbers_and_context(query, [( {'text': context, 'heading': '', 'source': ''}, 1.0 )])


def evaluate_all_rules(query, context, policies=None):
    """
    المعالج الشامل: يفحص كافة القواعد الرياضية والزمنية ويعيد نتيجة قطعية
    إن وجدت، أو None في حالة عدم وجود شرط حسابي.
    """
    # 1. تقييم نسب الإنجاز والاسترداد
    pct_res = evaluate_percentage_threshold(query, context)
    if pct_res:
        return f"[نتيجة المقارنة الحسابية المؤكدة قطيعاً: {pct_res}]"
        
    # 2. تقييم المهل الزمنية المسبقة
    time_res = evaluate_time_advance_threshold(query, context)
    if time_res:
        return f"[نتيجة المقارنة الزمنية المؤكدة قطيعاً: {time_res}]"
        
    # 3. تقييم العمليات الحسابية الأخرى (أسعار، تقسيط، رسوم)
    arithmetic_hint = evaluate_arithmetic_expressions(query, context)
    if arithmetic_hint:
        return arithmetic_hint
        
    return None
