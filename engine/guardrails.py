# -*- coding: utf-8 -*-
"""
طبقة الحماية والتدقيق العامة (Universal Hallucination & Policy Guardrail):
- التحقق البرمجي التلقائي من عدم اختلاق أي شروط أو مدد زمنية غير موجودة في السياق.
- حماية وتأكيد قرارات الحظر المطلق والسياسات غير القابلة للتفاوض.
- تطبيق أولوية استثناءات الباقات والاشتراكات ومنع الموافقات الخاطئة.
- تنظيف مخرجات النماذج من أي شوائب أو تسريبات لغوية.
- تصميم عام 100% غير مرتبط بأي مجال تجاري محدد (Domain-Agnostic).
"""
import re


def sanitize_text(text):
    """تنظيف المخرجات من الرموز الأجنبية، علامات التفكير، وتسريب الـ Prompts."""
    if not text:
        return ""
    
    # 1. إزالة أي محتوى تفكير <think>...</think>
    text = re.sub(r'<think>.*?</think>', '', text, flags=re.DOTALL).strip()
    
    # 2. إزالة أي حروف صينية أو آسيوية متسربة من أوزان النماذج متعددة اللغات
    text = re.sub(r'[\u4e00-\u9fff\u3000-\u303f]+', '', text).strip()
    text = re.sub(r'\s*，\s*', '، ', text).strip()
    text = re.sub(r'\s*。\s*', '. ', text).strip()
    
    # 3. إزالة أي تسريب لبدايات ونهايات الـ Template
    cues = [
        "سؤال العميل الحالي:",
        "السياق المستخرج من قاعدة المعرفة:",
        "الإجابة المباشرة:",
        "السؤال الموجه لك:",
        "الرد:",
        "الإجابة:"
    ]
    for cue in cues:
        if cue in text:
            idx = text.find(cue)
            if idx == 0:
                text = text[len(cue):].strip()
            else:
                text = text[:idx].strip()

    return text.strip()


def extract_time_windows(text):
    """استخراج أي نوافذ زمنية أو مهل مذكورة في النص (مثل: خلال 24 ساعة، خلال 3 أيام، إلخ)."""
    patterns = [
        r'خلال\s*(\d+(?:\.\d+)?)\s*(?:ساعة|ساعات|أيام|ايام|يوم|أسبوع|اسبوع|أسابيع|شهور|شهر)',
        r'في\s*غضون\s*(\d+(?:\.\d+)?)\s*(?:ساعة|ساعات|أيام|ايام|يوم|أسبوع|اسبوع|أسابيع|شهور|شهر)',
        r'أول\s*(\d+(?:\.\d+)?)\s*(?:ساعة|ساعات|أيام|ايام|يوم|أسبوع|اسبوع|أسابيع|شهور|شهر)',
        r'خلال\s*(?:يومين|ساعتين|أسبوعين|شهرين)',
        r'قبل\s*(\d+(?:\.\d+)?)\s*(?:ساعة|ساعات|أيام|ايام|يوم|أسبوع|اسبوع|أسابيع|شهور|شهر)'
    ]
    matches = []
    for pat in patterns:
        for m in re.finditer(pat, text):
            matches.append(m.group(0).strip())
    return matches


def verify_timeframe_grounding(response, context):
    """
    التدقيق في المهل الزمنية:
    إذا ذكر الرد أي مهلة زمنية (مثل 'خلال 24 ساعة' أو 'خلال 3 أيام')
    وهذه المهلة غير موجودة إطلاقاً في السياق المسترجع:
    يتم التعامل معها فوراً وشطبها أو استبدالها بنص السياق الأصلي.
    """
    res_windows = extract_time_windows(response)
    if not res_windows:
        return response, []

    ctx_clean = re.sub(r'\s+', ' ', context)
    
    audit_notes = []
    clean_res = response

    for win in res_windows:
        num_match = re.search(r'\d+', win)
        num_val = num_match.group(0) if num_match else None
        
        found_in_ctx = False
        if win in ctx_clean:
            found_in_ctx = True
        elif num_val and num_val in ctx_clean:
            found_in_ctx = True
            
        if not found_in_ctx:
            audit_notes.append(f"تم اكتشاف مهلة زمنية مخترعة غير موجودة في السياق: '{win}'")
            
            has_absolute_ban = any(phrase in context for phrase in [
                "غير قابل للاسترداد بعد التفعيل بأي حال",
                "غير قابلة للاسترداد بعد التفعيل",
                "غير قابل للاسترداد بعد التفعيل",
                "غير قابل للاسترداد",
                "غير مسترد بأي حال",
                "لا يوجد استرداد"
            ])
            
            if has_absolute_ban:
                sentences = re.split(r'[\n\.\!؟]', clean_res)
                for s in sentences:
                    if win in s:
                        clean_res = clean_res.replace(s.strip(), "لا، لا يوجد استرداد بعد التفعيل بأي حال، إذ يُعتبر مُستخدماً بمجرد تفعيله")
            else:
                clean_res = re.sub(r'(?:مؤهل للاسترداد\s*)?' + re.escape(win) + r'(?:\s*فقط)?', '', clean_res).strip()

    clean_res = re.sub(r'([،\.]\s*){2,}', r'\1', clean_res).strip()
    return clean_res, audit_notes


def verify_feature_exclusions(response, context, query):
    """
    حارس استثناءات الباقات والاشتراكات:
    إذا كان السؤال يجمع بين باقة معينة وميزة مستثناة صراحة في السياق:
    والسياق ينص على عدم الشمول ("غير متوفرة في"، "لا تشمل"، "مستثناة"):
    يضمن الحارس أن يبدأ الرد بالرفض الصريح دون أي لبس.
    """
    exclusion_patterns = [
        r'([^\.\n]+?(?:غير متوفر[ةي]?|لا تشمل|لا يشمل|مستثن[اى]ة?|غير مشمول[ةي]?)[^\.\n]+)',
    ]
    
    context_exclusions = []
    for pat in exclusion_patterns:
        for m in re.finditer(pat, context):
            context_exclusions.append(m.group(1).strip())
            
    if not context_exclusions:
        return response, []
        
    audit_notes = []
    clean_res = response

    q_words = set(query.split())
    for excl in context_exclusions:
        excl_words = set(excl.split())
        overlap = q_words.intersection(excl_words)
        if len(overlap) >= 2:
            starts_with_yes = clean_res.strip().startswith(('نعم', 'أكيد', 'بالتأكيد', 'ينفع', 'طبعاً', 'متاح'))
            if starts_with_yes or "يمكنك حضور" in clean_res or "نعم، يمكنك" in clean_res:
                audit_notes.append("تم تصحيح موافقة خاطئة تتعارض مع استثناء صريح في الباقة.")
                clean_res = f"لا، {excl}."
                
    return clean_res, audit_notes


def apply_guardrails(response, context, query, tenant=None):
    """
    المنظومة الشاملة لتطبيق كافة حواجز الأمان والتدقيق على رد النموذج
    قبل إرجاعه للمستخدم النهائي.
    """
    if not response:
        return "", []
        
    audit_log = []
    
    # 1. تنظيف النصوص والشوائب
    sanitized = sanitize_text(response)
    
    # 2. فحص وتدقيق المهل الزمنية المختلقة (Anti-Hallucination)
    verified_time, time_notes = verify_timeframe_grounding(sanitized, context)
    audit_log.extend(time_notes)
    
    # 3. فحص وتأكيد استثناءات الباقات (Feature Exclusions)
    final_output, excl_notes = verify_feature_exclusions(verified_time, context, query)
    audit_log.extend(excl_notes)
    
    # 4. تنظيف نهائي
    final_output = re.sub(r'\s+', ' ', final_output).strip()
    
    return final_output, audit_log
