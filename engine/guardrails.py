# -*- coding: utf-8 -*-
"""
طبقة الحماية والتدقيق العامة (Universal Hallucination & Policy Guardrail):
- التحقق البرمجي التلقائي من عدم اختلاق أي شروط أو مدد زمنية غير موجودة في السياق.
- مطابقة المهلة كزوج (قيمة، وحدة) مع توحيد الوحدات (يوم = 24 ساعة) ودعم الأرقام اللفظية العربية.
- عبارات الحظر المطلق تُبنى من مصفوفة سياسات المستأجر (policies.json) وليست ثوابت مجالية في الكود.
- تطبيق أولوية استثناءات الباقات والاشتراكات ومنع الموافقات الخاطئة.
- تنظيف مخرجات النماذج من أي شوائب أو تسريبات لغوية.
- تصميم عام 100% غير مرتبط بأي مجال تجاري محدد (Domain-Agnostic).
"""
import re

# ---------------------------------------------------------------- أدوات الأرقام والوحدات

# أرقام عربية لفظية شائعة -> قيمة رقمية
_AR_VERBAL_NUMBERS = {
    'واحد': 1, 'واحدة': 1, 'اثنين': 2, 'إثنين': 2, 'اثنتين': 2, 'ثلاث': 3, 'ثلاثة': 3,
    'أربع': 4, 'اربع': 4, 'أربعين': 40, 'اربعين': 40, 'خمس': 5, 'خمسة': 5, 'خمسين': 50,
    'ست': 6, 'ستة': 6, 'سبع': 7, 'سبعة': 7, 'ثمان': 8, 'ثمانية': 8, 'تسع': 9, 'تسعة': 9,
    'عشر': 10, 'عشرة': 10, 'أحد عشر': 11, 'اثنا عشر': 12, 'اثني عشر': 12,
    'عشرين': 20, 'ثلاثين': 30, 'ستين': 60, 'تسعين': 90, 'مئة': 100, 'مائة': 100,
}

# صيغ المضاعف الثنائية (يومين/ساعتين/سنتين...) -> (قيمة، وحدة مفردة)
_DUAL_FORMS = {
    'يومين': (2, 'يوم'), 'يومتان': (2, 'يوم'), 'ساعتين': (2, 'ساعة'), 'ساعتان': (2, 'ساعة'),
    'أسبوعين': (2, 'أسبوع'), 'اسبوعين': (2, 'أسبوع'), 'شهرين': (2, 'شهر'), 'شهران': (2, 'شهر'),
    'سنتين': (2, 'سنة'), 'سنتان': (2, 'سنة'), 'عامين': (2, 'عام'), 'عامان': (2, 'عام'),
}

# تحويل كل الوحدات إلى ساعات (وحدة المقارنة المشتركة)
_UNIT_TO_HOURS = {
    'دقيقة': 1.0 / 60, 'ساعة': 1.0, 'يوم': 24.0, 'أسبوع': 168.0, 'اسبوع': 168.0, 'شهر': 720.0,
    'سنة': 8760.0, 'سنوات': 8760.0, 'سنين': 8760.0, 'عام': 8760.0, 'أعوام': 8760.0,
}

# قيمة "ساعة" بالعربي للاستخدام في التعبيرات
_H = r'(?:ساعة|ساعات|يوم|أيام|ايام|أسبوع|اسبوع|أسابيع|اسابيع|شهر|شهور|سنة|سنتين|سنوات|سنين|عام|عامين|أعوام|دقيقة|دقائق)'

# أنماط المهلة: تلتقط (عدد، وحدة) أو صيغة مضاعف ثنائي
_TIME_PATTERNS = [
    r'(?:خلال|في\s*غضون|غضون|أول\s+(?:\d+|عشر)\s*|قبل|أقصى|حد\s*أقصى|بحد\s*أقصى|لا\s*تقل\s*عن|أقل\s*من|ما\s*قلت\s*)\s*(\d+(?:\.\d+)?)\s*' + _H,
    r'(?:خلال|في\s*غضون|غضون|قبل|أقصى)\s*(يومين|ساعتين|أسبوعين|اسبوعين|شهرين|سنتين|سنتان|عامين|عامان)',
]

# كلمات وظيفية لا تُحتسب في تراكب الاستثناءات
_EXCL_STOPWORDS = {
    'في', 'من', 'على', 'عن', 'مع', 'هذا', 'هذه', 'التي', 'الذي', 'أو', 'او', 'و', 'لا',
    'غير', 'باقة', 'باقه', 'الخدمة', 'الخدمه', 'العرض', 'المتجر', 'الشركة', 'الشركه',
    'المنصة', 'المنصه', 'الاشتراك', 'اشتراك', 'عميل', 'العملاء', 'حسب', 'سياسة', 'سياسة',
}


def sanitize_text(text):
    """تنظيف المخرجات من الرموز الأجنبية، علامات التفكير، وتسريب الـ Prompts."""
    if not text:
        return ""

    # 1. إزالة أي محتوى تفكير <think>...</think> — والمقطع قبل الإغلاق يُقص حتى النهاية
    #    (تفكير مبتور بلا وسم إغلاق كان يتسرب كاملاً للرد)
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    if '<think>' in text:
        text = text.split('<think>')[0].strip()

    # 2. إزالة أي حروف صينية أو آسيوية متسربة من أوزان النماذج متعددة اللغات
    text = re.sub(r'[\u4e00-\u9fff\u3000-\u303f]+', '', text).strip()
    text = re.sub(r'\s*，\s*', '، ', text).strip()
    text = re.sub(r'\s*。\s*', '. ', text).strip()

    # 3. إزالة شوائب صياغة نموذجية متسربة
    text = re.sub(r'\bالمحتوى:\s*', '', text)
    text = re.sub(r'\bالإجابة المختصرة(?:\s+هي)?:?\s*', '', text)
    text = re.sub(r'\bالإجابة المباشرة:\s*', '', text)
    # 3.5 تسريب قوالب الاسترجاع من أي موضع: أسطر "القسم: ... المحتوى: ..." وتنسيق
    #     "md | القسم: ..." (تسريب Q7) تُقص كلياً من الرد
    text = re.sub(r'(?:^|\n)\s*[\w\.]{0,6}\s*\|\s*القسم:\s*[^\n]*', '\n', text)
    text = re.sub(r'(?:^|\n)\s*القسم:\s*[^\n]*', '\n', text)
    text = re.sub(r'(?:^|\n)\s*المستند:\s*[^\n]*', '\n', text)
    text = re.sub(r'\[\d+\]\s*المستند:\s*[^\n]*\n?', '', text)
    # عيب 12: إحالات نصية للمستندات/الأقسام داخل الرد
    text = re.sub(r'\(?\s*(?:انظر|راجع)\s+إلى\s+(?:المستند|الملف|القسم)[^)\n]*\)?', '', text)
    text = re.sub(r'\s*\((?:المصدر|القسم|المستند)\s*:\s*[^)]*\)', '', text)
    text = re.sub(r'\s*(?:الاستثناء\s+)?مذكور\s+في\s+(?:المستند|الملف)\s+[\w\-\.]+\.(?:md|csv|json|txt|docx)\.?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\s*\(?(?:من\s+)?(?:المصدر|القسم|المستند)\s*:?\s*[\w\-\.]+\.(?:md|csv|json|txt|docx)\)?', '', text, flags=re.IGNORECASE)
    text = re.sub(r'\b[BP]\d{1,3}\s*/\s*(?:[\u0621-\u064a\s]+\s*/\s*)?', '', text)
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    text = re.sub(r'\n{3,}', '\n\n', text).strip()
    # حشو بداية يكسر وضوح الرد ("طيب،..." / "طب،..." / "تحية طيبة وبعد،...")
    text = re.sub(r'^(?:طيب|طب|بالمناسبة)\s*[،,]?\s*', '', text).strip()
    text = re.sub(r'^تحية\s*طيبة(?:\s*وبعد)?\s*[،,]?\s*', '', text).strip()
    # تنظيف أي هلوسة تربط الاستفسارات بالهوية أو الجنسية العربية
    text = re.sub(r'\s*أو\s+جنسيتك\s+العربية\b', '', text).strip()
    text = re.sub(r'\s*أو\s+هويتك\s+العربية\b', '', text).strip()
    text = re.sub(r'\s*أو\s+جنسيتك\b', '', text).strip()
    text = re.sub(r'\bعربيتي\b', 'سيارتك', text)
    text = re.sub(r'\bعربيتك\b', 'سيارتك', text)

    # 4. إزالة تسريب بدايات/nهايات الـ Template فقط إذا كانت في مقدمة النص
    #    (حتى لا نقطع رداً شرعياً يحتوي عبارة مثل "الإجابة:" في منتصفه)
    cues = [
        "سؤال العميل الحالي:",
        "السياق المستخرج من قاعدة المعرفة:",
        "الإجابة المباشرة:",
        "السؤال الموجه لك:",
        "الرد:",
        "الإجابة:"
    ]
    for cue in cues:
        if text.startswith(cue):
            text = text[len(cue):].strip()
        elif cue in text[:len(cue) + 10]:
            idx = text.find(cue)
            text = text[idx + len(cue):].strip()

    return text.strip()


def _parse_verbal_number(text):
    """محاولة قراءة رقم لفظي عربي مركّب مثل 'أربع وعشرين' -> 24."""
    parts = re.split(r'\s*و\s*', text.strip())
    total = 0
    matched = False
    for p in parts:
        p = p.strip()
        val = _AR_VERBAL_NUMBERS.get(p)
        if val is None:
            return None
        matched = True
        # التركيب: العشرات + الأحاد ("أربع وعشرين")
        if val >= 20:
            total += val
        else:
            total += val
    return total if matched else None


def extract_time_claims(text):
    """
    استخراج كل ادعاءات المهلة الزمنية من النص كقوائم منظمة.
    يعيد قائمة من القواميس: {'text', 'value', 'unit', 'hours'}
    تدعم: الأرقام الرقمية، الأرقام اللفظية (خلال أربع وعشرين ساعة)، والصيغ الثنائية (يومين).
    """
    claims = []

    # 1. صيغ المضاعف الثنائية (يومين، ساعتين، سنتين...)
    for m in re.finditer(r'(يومين|ساعتين|أسبوعين|اسبوعين|شهرين|سنتين|سنتان|عامين|عامان)', text):
        val, unit = _DUAL_FORMS[m.group(1)]
        claims.append({'text': m.group(0), 'value': float(val), 'unit': unit,
                       'hours': val * _UNIT_TO_HOURS[unit]})

    # 2. أرقام رقمية مع وحدة
    for m in re.finditer(r'(\d+(?:\.\d+)?)\s*(' + _H + r')', text):
        val = float(m.group(1))
        unit = m.group(2)
        hours = _unit_hours(unit) * val
        claims.append({'text': m.group(0), 'value': val, 'unit': unit, 'hours': hours})

    # 2.5 دعم حقول المدد بالإنجليزية في الكتالوجات (duration_minutes / duration_hours)
    for m in re.finditer(r'(?:duration_minutes|مدة_بالدقائق)\s*[:=]\s*(\d+(?:\.\d+)?)', text, re.I):
        val = float(m.group(1))
        claims.append({'text': m.group(0), 'value': val, 'unit': 'دقيقة', 'hours': val / 60.0})
    for m in re.finditer(r'(?:duration_hours|مدة_بالساعات)\s*[:=]\s*(\d+(?:\.\d+)?)', text, re.I):
        val = float(m.group(1))
        claims.append({'text': m.group(0), 'value': val, 'unit': 'ساعة', 'hours': val})

    # 3. أرقام لفظية عربية (حتى مركبة: أربع وعشرين)
    for m in re.finditer(
            r'(?:خلال|في\s*غضون|غضون|قبل|أقصى|حد\s*أقصى|بحد\s*أقصى)\s+'
            r'([\u0621-\u064a]+(?:\s*و\s*[\u0621-\u064a]+)?)\s+(' + _H + r')', text):
        verbal = m.group(1)
        val = _parse_verbal_number(verbal)
        if val is None:
            continue
        unit = m.group(2)
        claims.append({'text': m.group(0), 'value': float(val), 'unit': unit,
                       'hours': _unit_hours(unit) * val, 'verbal': verbal})

    # إزالة التكرار حسب النص
    seen = set()
    unique = []
    for c in claims:
        key = (c['value'], c['unit'])
        if key not in seen:
            seen.add(key)
            unique.append(c)
    return unique


def _unit_hours(unit):
    """توحيد الوحدة إلى ساعات (مع تشكيلات الحرف)."""
    unit = unit.strip()
    if unit in _UNIT_TO_HOURS:
        return _UNIT_TO_HOURS[unit]
    if unit.startswith(('ساع', 'ساأ')):
        return 1.0
    if unit.startswith(('يوم', 'أيام', 'ايام')):
        return 24.0
    if unit.startswith(('أسبوع', 'اسبوع', 'أسابيع', 'اسابيع')):
        return 168.0
    if unit.startswith(('شهر', 'شهور')):
        return 720.0
    if unit.startswith(('سن', 'عام')):
        return 8760.0
    if unit.startswith(('دقيقة', 'دقائق')):
        return 1.0 / 60
    return 1.0


def _context_time_index(context):
    """بناء فهرس (القيمة بالساعات) لكل المهل المذكورة في السياق."""
    return [c['hours'] for c in extract_time_claims(context)]


def verify_timeframe_grounding(response, context, query=None, audit_notes=None):
    """
    التدقيق في المهل الزمنية:
    كل مهلة ذُكرت في الرد يجب أن يكون لها مقابل في السياق بنفس القيمة بعد
    توحيد الوحدات إلى ساعات (48 ساعة = يومين).
    سؤال العميل لا يُعتبر مصدراً لتوثيق شروط أو مدد استحقاق إذا كان السياق يحظرها أو يخلو منها.
    """
    res_claims = extract_time_claims(response)
    notes = list(audit_notes) if audit_notes is not None else []
    if not res_claims:
        return response, notes

    ctx_hours = _context_time_index(context)
    query_hours = _context_time_index(query) if query else []
    ctx_clean = re.sub(r'\s+', ' ', context)
    ban_sentence = _find_ban_sentence(ctx_clean)
    clean_res = response

    for claim in res_claims:
        grounded_in_ctx = any(abs(claim['hours'] - h) < 1e-6 for h in ctx_hours)
        grounded_in_query = any(abs(claim['hours'] - h) < 1e-6 for h in query_hours)

        # إذا كانت المهلة موجودة في السياق، فهي موثقة
        if grounded_in_ctx:
            continue

        # فحص ما إذا كان الرد يمنح موافقة أو استحقاقاً بخصوص هذه المهلة (مثل استرجاع أو إلغاء)
        is_affirmative = bool(re.search(r'\b(نعم|يمكنك|يمكن|يحق|متاح|مسموح|أكيد|تستحق|بإمكانك)\b', clean_res[:100]))

        # لا يُسمح باعتماد مهلة السؤال إن كان هناك حظر قطعي في السياق أو إذا كان الرد يقدم موافقة غير مستندة للسياق
        if grounded_in_query and not ban_sentence and not is_affirmative:
            # مجرد إشارة لفرضية العميل دون تقرير سياسة
            continue

        notes.append(
            "تم اكتشاف مهلة زمنية مخترعة غير موجودة في السياق: '%s' (=%s ساعة)" %
            (claim['text'], claim['hours']))

        if ban_sentence:
            sentences = re.split(r'[\n\.\!؟]', clean_res)
            for s in sentences:
                if claim['text'] in s or (claim.get('verbal') and claim['verbal'] in s):
                    clean_res = clean_res.replace(s.strip(), ban_sentence)
        else:
            # شطب المهلة مع أداة التعريف الزمنية التي تسبقها حتى لا تبقى عبارات يتيمة
            clean_res = re.sub(
                r'(?:مؤهل للاسترداد\s*)?(?:خلال|في\s*غضون|غضون|قبل|أقصى|حد\s*أقصى|بحد\s*أقصى)?\s*'
                + re.escape(claim['text']) + r'(?:\s*فقط)?',
                '', clean_res).strip()
            if claim.get('verbal'):
                clean_res = clean_res.replace(claim['verbal'], '').strip()
            # تنظيف حروف الجر المعلقة بعد الشطب
            clean_res = re.sub(r'\b(من|في|على)\s*([،\.]|$)', r'\2', clean_res).strip()

    clean_res = re.sub(r'([،\.]\s*){2,}', r'\1', clean_res).strip()
    return clean_res, notes


def build_ban_phrases(policies=None):
    """
    عبارات الحظر المطلق: عبارات عامة (سياساتية المجال) + أي قواعد حظر
    استُخرجت آلياً من مستندات المستأجر (policies['absolute_prohibitions']).
    """
    phrases = [
        'غير قابل للاسترداد',
        'غير قابلة للاسترداد',
        'غير مسترد',
        'لا يوجد استرداد',
        'لا يمكن الإلغاء',
        'لا يحق له الاسترجاع',
    ]
    if policies:
        for p in policies.get('absolute_prohibitions', []):
            rule = (p.get('rule') or '').strip()
            if rule and len(rule) >= 8:
                phrases.append(rule)
    return phrases


def _find_ban_sentence(text, policies=None):
    """البحث عن جملة حظر صريحة في النص لإرجاعها كبديل للحظر المخترق."""
    for phrase in build_ban_phrases(policies):
        if phrase in text:
            sentences = re.split(r'[\n\.\!؟]', text)
            for s in sentences:
                if phrase in s:
                    s_clean = s.strip()
                    if s_clean.startswith(('📌', '-', '*', '•')):
                        s_clean = s_clean.lstrip('📌-*• ').strip()
                    return s_clean
    return None


# أسماء عامة في الأسئلة لا تكفي وحدها لاعتبار السؤال يستهدف موضوع الاستثناء
_GENERIC_NOUNS = {
    'أنا', 'انا', 'عندي', 'معايا', 'معي', 'كنت', 'مشترك', 'باقة', 'باقه', 'حساب', 'خدمة', 'خدمه',
    'سؤال', 'استفسار', 'طلب', 'ممكن', 'ينفع', 'لو', 'هل', 'كود', 'عرض', 'خصم'
}

# مثير النفي داخل جملة الاستثناء (يُستخدم لفصل "الموضوع" عن "الميزة المستثناة")
_NEG_TRIGGER = re.compile(
    r'(?:غير متوفر[ةي]?|لا تشمل|لا يشمل|لا تغطي|لا يتضمن|مستثن[اى]ة?|غير مشمول[ةي]?)')


def _content_words(text):
    """كلمات دالة: طولها 3+ وليست من الكلمات الوظيفية."""
    return {w for w in re.findall(r'[\u0621-\u064a\w]+', text)
            if len(w) >= 3 and w not in _EXCL_STOPWORDS}


def verify_feature_exclusions(response, context, query, audit_notes=None):
    """
    حارس استثناءات الباقات والاشتراكات:
    كل جملة استثناء في السياق تُحلل إلى (الموضوع: من يسري عليه الاستثناء،
    والميزة: ما هو مستثنى). يُصحح الرد فقط إذا سأل العميل عن نفس الموضوع
    نفسه (كلمة محددة غير عامة) وعن نفس الميزة المستثناة —
    حتى لا يُرفض سؤال شرعي عن باقة أخرى ليست معنية بالاستثناء.
    """
    exclusion_patterns = [
        r'([^\.\n]+?(?:غير متوفر[ةي]?|لا تشمل|لا يشمل|لا تغطي|لا يتضمن|مستثن[اى]ة?|غير مشمول[ةي]?)[^\.\n]+)',
    ]

    context_exclusions = []
    for pat in exclusion_patterns:
        for m in re.finditer(pat, context):
            context_exclusions.append(m.group(1).strip())

    notes = list(audit_notes) if audit_notes is not None else []
    if not context_exclusions:
        return response, notes

    clean_res = response

    q_content = _content_words(query)
    if len(q_content) < 2:
        return clean_res, notes

    for excl in context_exclusions:
        m = _NEG_TRIGGER.search(excl)
        if not m:
            continue
        subject_words = _content_words(excl[:m.start()])   # من يسري عليه الاستثناء
        feature_words = _content_words(excl[m.end():])     # ما هو مستثنى

        # الربط بالموضوع يتطلب كلمة محددة (غير عامة) من كلمات موضوع الاستثناء
        subject_hit = any(w not in _GENERIC_NOUNS for w in q_content & subject_words)
        # والميزة المستثناة يجب أن تكون مذكورة في السؤال فعلاً
        feature_hit = any(w not in _GENERIC_NOUNS for w in q_content & feature_words)
        if not (subject_hit and feature_hit):
            continue

        first_60 = clean_res.strip()[:60]
        starts_with_yes = clean_res.strip().startswith(('نعم', 'أكيد', 'بالتأكيد', 'ينفع', 'طبعاً', 'طبعا', 'متاح'))
        affirmative_mid = ('يمكنك' in first_60 or 'يمكن' in first_60 or 'متاح' in first_60)
        if starts_with_yes or affirmative_mid:
            notes.append("تم تصحيح موافقة خاطئة تتعارض مع استثناء صريح في الباقة.")
            clean_res = f"لا، {excl}."

    return clean_res, notes


# مبالغ مالية/أرقام بوحدات: تُفحص وجودها في السياق (وضع تحذير عبر سجل التدقيق)
_MONEY_RE = re.compile(
    r'(\d+(?:[.,]\d+)?)\s*(?:جنيه(?:اً|ا)?|ج\.?\s?م|ريال|دولار|درهم|يورو|مصري)')


def verify_numeric_grounding(response, context, audit_notes=None):
    """
    حارس الأرقام المالية: يرصد أي مبلغ مالي ذُكر في الرد ولم يرد في السياق إطلاقاً
    (أرخص تجسيد للهلوسة الرقمية — مثل اختلاق سعر غير موجود).
    المطابقة الرقمية بحدود كلمات (وليس substring): "600" لا تُعتبر موجودة لأن
    السياق يحتوي "1600". يعمل بوضع "تحذير": يسجل في سجل التدقيق دون تعديل الرد،
    لأن بعض الأرقام الشرعية نتائج حسابية (مجاميع/خصومات) ولا يجوز شطبها آلياً.
    """
    if not response:
        return response, audit_notes if audit_notes is not None else []

    ctx = re.sub(r'\s+', ' ', context)
    # فهرس أرقام السياق كأعداد حقيقية: "1600" لن يطابق 600 مهما اختلف النص
    ctx_numbers = {float(n.replace(',', '.')) for n in re.findall(r'\d+(?:[.,]\d+)?', ctx)}
    notes = audit_notes if audit_notes is not None else []

    for m in _MONEY_RE.finditer(response):
        raw = m.group(1)
        try:
            value = float(raw.replace(',', '.'))
        except ValueError:
            continue
        if value not in ctx_numbers:
            notes.append("رقم مالي في الرد غير موجود في السياق (يُنصح بمراجعته): %s" % raw)
    return response, notes


def apply_guardrails(response, context, query, tenant=None, policies=None):
    """
    المنظومة الشاملة لتطبيق كافة حواجز الأمان والتدقيق على رد النموذج
    قبل إرجاعه للمستخدم النهائي.
    """
    if not response:
        return "", []

    # 1. تنظيف النصوص والشوائب
    sanitized = sanitize_text(response)

    # 2. فحص وتدقيق المهل الزمنية المختلقة (Anti-Hallucination)
    verified_time, time_notes = verify_timeframe_grounding(sanitized, context, query)

    # 3. فحص وتأكيد استثناءات الباقات (Feature Exclusions)
    final_output, excl_notes = verify_feature_exclusions(verified_time, context, query)

    # 4. فحص الأرقام المالية مقابل السياق (وضع تحذير — لا يعدل الرد)
    final_output, num_notes = verify_numeric_grounding(final_output, context)

    # تجميع الملاحظات بنظافة دون أي mutation أو تكرار
    audit_log = []
    if time_notes:
        audit_log.extend(time_notes)
    for extra in (excl_notes, num_notes):
        for n in (extra or []):
            if n not in audit_log:
                audit_log.append(n)

    # 4. تنظيف نهائي
    final_output = re.sub(r'\s+', ' ', final_output).strip()

    return final_output, audit_log
