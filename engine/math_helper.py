# -*- coding: utf-8 -*-
"""
engine/math_helper.py — مساعد الحسابات والكيانات المالية الذكي العام (Generalized Domain-Agnostic Financial Reasoner).
يحلل العمليات المالية وفق سياقها الدلالي العام (مبالغ، نسب مئوية، رسوم، فئات، مدد زمنية، وحدات) بدون أي أسماء منتجات مخصصة.
"""
import re


def _norm_ar(text):
    """توحيد وتبسيط النص العربي لمطابقة الفئات والشروط العامة."""
    if not text:
        return ''
    t = re.sub(r'[\u064B-\u065F\u0670]', '', text)
    t = re.sub(r'[أإآا]', 'ا', t)
    t = re.sub(r'ة', 'ه', t)
    t = re.sub(r'ى', 'ي', t)
    words = [re.sub(r'^ال', '', w) for w in t.split()]
    return ' '.join(words)


def extract_numbers_and_context(query, matches):
    """
    تحليل ذكي وعام لأي عمليات مالية ووحدات:
    1. الكيلومترات/الوحدات الإضافية: (عدد الوحدات × سعر الوحدة = المبلغ الإضافي).
    2. المدد والخصم المئوي: (سعر اليوم/الوحدة × عدد الأيام - نسبة الخصم = الإجمالي).
    3. التقسيط: (مبلغ / عدد شهور = قسط شهري).
    4. الخصم المئوي والرسوم: (سعر الصنف - نسبة الخصم + الرسوم الإضافية).
    """
    if not query or not matches:
        return None

    context_text = ' '.join([chunk.get('text', '') + ' ' + chunk.get('heading', '') for chunk, _ in (matches or [])])
    context_clean = re.sub(r'[\u064B-\u065F\u0670]', '', context_text)

    # 1. فحص حساب الكيلومترات أو الوحدات الإضافية (Extra Units / Distance Calculation)
    extra_units_match = re.search(r'(?:عديت|تجاوزت|زيادة|إضافي|اضافي|فوق الحد).*?(\d+)\s*(?:كيلو|كم|كيلومتر|ساعة|وحدة)', query)
    if not extra_units_match:
        extra_units_match = re.search(r'(\d+)\s*(?:كيلو|كم|كيلومتر|ساعة|وحدة)\s*(?:إضافي|اضافي|زيادة)', query)
    
    if extra_units_match:
        units_count = float(extra_units_match.group(1))
        # البحث عن سعر الوحدة الإضافية في السياق
        unit_rate_match = re.search(r'(\d+(?:\.\d+)?)\s*جنيه\s*لكل\s*(?:كيلومتر|كيلو|كم|ساعة|وحدة)\s*(?:إضافي|اضافي|فوق الحد)', context_text)
        if not unit_rate_match:
            unit_rate_match = re.search(r'(\d+(?:\.\d+)?)\s*جنيه\s*عن\s*كل\s*(?:كيلومتر|كيلو|كم|ساعة|وحدة)', context_text)
        
        if unit_rate_match:
            rate_per_unit = float(unit_rate_match.group(1))
            total_extra = round(units_count * rate_per_unit, 2)
            u_str = int(units_count) if units_count.is_integer() else units_count
            r_str = int(rate_per_unit) if rate_per_unit.is_integer() else rate_per_unit
            tot_str = int(total_extra) if total_extra.is_integer() else total_extra
            return (f"ملاحظة حسابية مؤكدة وملزمة للإجابة: العميل يسأل عن تكلفة الوحدات/الكيلومترات الزائدة ({u_str} كيلو بسعر {r_str} جنيه للكيلو الإضافي) "
                    f"= {u_str} × {r_str} = {tot_str} جنيه مصري تُدفع أو تُخصم من التأمين. "
                    f"أجب العميل مباشرة بأن المبلغ الإضافي هو {tot_str} جنيه مصري ولا تعتذر بنقص معلومات الحد الأصلي لأن الزيادة محددة صراحة.")

    # 2. فحص نية التقسيط (Installments)
    installment_match = re.search(r'(?:تقسيط|قسط|أقسط|اقسط|أدفع|ادفع|تقسيطها|قسطها).*?(\d+(?:\.\d+)?)\s*(?:جنيه|ج|LE|EGP)?\s*(?:على|علي)\s*(\d+)\s*(?:شهور|أشهر|شهر)', query)
    if installment_match:
        total_amount = float(installment_match.group(1))
        months = int(installment_match.group(2))
        
        t_str = str(int(total_amount)) if total_amount.is_integer() else str(total_amount)
        m_str = str(int(months)) if months.is_integer() else str(months)
        
        words = context_clean.split()
        t_found = any(t_str in re.findall(r'\d+(?:\.\d+)?', w) for w in words)
        m_found = any(m_str in re.findall(r'\d+(?:\.\d+)?', w) for w in words)
                
        if t_found and m_found and months > 0:
            monthly = round(total_amount / months, 2)
            monthly_str = int(monthly) if monthly.is_integer() else monthly
            total_str = int(total_amount) if total_amount.is_integer() else total_amount
            return (f"ملاحظة حسابية مؤكدة: نظام التقسيط المطلوب ({total_str} جنيه على {months} شهور) "
                    f"= {total_str} ÷ {months} = {monthly_str} جنيه شهرياً. "
                    f"لا تقم بطرح عدد الشهور من المبلغ إطلاقاً.")

    # 3. فحص مقارنة نسبة في السؤال بحد/عتبة في السياق (Percentage Threshold / Limit Comparison)
    fraction_map = {
        'ثلاثه ارباع': 75.0, 'ثلاث ارباع': 75.0, 'تلات ارباع': 75.0,
        'نصف': 50.0, 'نصفه': 50.0, 'نصه': 50.0,
        'ثلث': 33.33, 'ثلثه': 33.33, 'تلت': 33.33, 'تلته': 33.33,
        'ربع': 25.0, 'ربعه': 25.0,
        'خمس': 20.0, 'خمسه': 20.0,
        'عشر': 10.0, 'عشره': 10.0,
    }
    
    q_norm = _norm_ar(query)
    is_threshold_query = any(w in q_norm for w in ['اتممت', 'اكملت', 'خلصت', 'درست', 'شاهدت', 'استرداد', 'استرجاع', 'استرد', 'استرجع', 'مؤهل', 'استحقاق', 'تجاوزت', 'عديت', 'حد', 'نسبه'])
    has_fraction = any(p in q_norm for p in fraction_map.keys())
    is_discount_query = any(k in q_norm for k in ['خصم', 'تخفيض', 'كوبون', 'promo', 'discount']) and not any(r in q_norm for r in ['استرداد', 'استرجاع', 'استرد', 'استرجع', 'مؤهل'])

    if (is_threshold_query or has_fraction) and not is_discount_query:
        detected_pcts = set()
        for phrase, val in fraction_map.items():
            if phrase in q_norm:
                detected_pcts.add(val)
        raw_pcts = re.findall(r'(\d+(?:\.\d+)?)\s*(?:%|٪|بالمئه|بالمائة|بالمئة|في الميه|في المية|في المائة)', q_norm)
        for p in raw_pcts:
            detected_pcts.add(float(p))

        # حد النطاق: لو لم توجد نسبة أو كان هناك أكثر من نسبة متضاربة
        if len(detected_pcts) == 1:
            query_pct = list(detected_pcts)[0]
            threshold_patterns = [
                r'(?:اكتر\s*من|أكتر\s*من|اكثر\s*من|أكثر\s*من|يتجاوز|تتجاوز|لا\s*يتجاوز|لا\s*تتجاوز|ما\s*اتمش\s*اكتر\s*من|ما\s*أتمش\s*أكتر\s*من|ما\s*كملش\s*اكتر\s*من|ما\s*أكملش\s*أكتر\s*من|بحد\s*اقصي|بحد\s*أقصى|حد\s*اقصي|حد\s*أقصى|اقصي\s*حد|أقصى\s*حد)\s*(?:نسبه\s*|نسبة\s*)?(\d+(?:\.\d+)?)\s*(?:%|٪|بالمئه|بالمائة|بالمئة|في الميه|في المية|في المائة)',
                r'(\d+(?:\.\d+)?)\s*(?:%|٪)\s*(?:كحد\s*اقصي|كحد\s*أقصى|حد\s*اقصي|حد\s*أقصى|اقصي\s*حد|أقصى\s*حد)',
                r'لو\s*(?:اكمل|أكمل|اتم|أتم)\s*(?:اكتر\s*من|أكتر\s*من|اكثر\s*من|أكثر\s*من)\s*(\d+(?:\.\d+)?)\s*(?:%|٪)'
            ]
            thresholds = set()
            for pat in threshold_patterns:
                for m in re.finditer(pat, context_text):
                    thresholds.add(float(m.group(1)))

            if len(thresholds) == 1:
                threshold = list(thresholds)[0]
                p_str = f"{int(query_pct)}%" if query_pct.is_integer() else f"{query_pct}%"
                t_str = f"{int(threshold)}%" if threshold.is_integer() else f"{threshold}%"

                if query_pct > threshold:
                    return (
                        f"ملاحظة حسابية مؤكدة وملزمة للإجابة باللغة العربية: النسبة المذكورة في السؤال ({p_str}) تتجاوز الحد المذكور في السياق ({t_str}) "
                        f"({p_str} > {t_str})، لذا الشرط غير محقق والعميل غير مؤهل للاسترداد. "
                        f"أجب باللغة العربية حصراً بالتعبير: 'لأ، {p_str} > {t_str}، غير مؤهل للاسترداد' مع توضيح أن سياسة المنصة تشترط ألا يتجاوز الإتمام {t_str}."
                    )
                else:
                    return (
                        f"ملاحظة حسابية مؤكدة وملزمة للإجابة: النسبة المذكورة في السؤال ({p_str}) لا تتجاوز الحد المذكور في السياق ({t_str}) "
                        f"({p_str} <= {t_str})، لذا شرط النسبة محقق والعميل مؤهل للاسترداد إذا كان الطلب خلال فترة السماح المحددة."
                    )

    # 4. استخراج عدد الأيام أو الوحدات الزمنية من السؤال
    days_match = re.search(r'(\d+)\s*(?:أيام|ايام|يوم|أيام|ليالي|ليلة)', query)
    days_count = int(days_match.group(1)) if days_match else 1

    # 4. استخراج النسب المئوية (Percentage Discounts)
    pct_matches = [float(p) for p in re.findall(r'(\d+(?:\.\d+)?)\s*(?:%|٪|بالمئة|في المية|في المائة)', query)]
    discount_intent = any(k in query for k in ['خصم', 'عرض', 'كوبون', 'تخفيض', 'تخفيضات', 'عروض', 'تتخصم', 'تخصم', 'promo', 'discount'])
    if not pct_matches and discount_intent:
        ctx_pcts = re.findall(r'(?:خصم|تخفيض|بنسبة|discount)\s*(\d+(?:\.\d+)?)%', context_text)
        if ctx_pcts:
            pct_matches = [float(p) for p in set(ctx_pcts)]

    # 5. استخراج الأسعار الأساسية من السياق (daily_rate_egp, price_egp, etc.)
    base_daily_rates = []
    for chunk, _ in matches[:4]:
        t = chunk.get('text', '')
        r_match = re.search(r'(?:daily_rate_egp|price_egp|rate_egp|price_egp_monthly|price):\s*(\d+(?:\.\d+)?)', t)
        if r_match:
            val = float(r_match.group(1))
            if val not in base_daily_rates:
                base_daily_rates.append(val)

    # 6. حسبة الأيام المتعددة مع الخصم (Multi-day rate with discount)
    if base_daily_rates and days_count > 1 and pct_matches:
        daily_price = base_daily_rates[0]
        pct = pct_matches[0]
        subtotal = daily_price * days_count
        disc_val = round(subtotal * (pct / 100.0), 2)
        final_total = round(subtotal - disc_val, 2)
        
        d_str = int(daily_price) if daily_price.is_integer() else daily_price
        sub_str = int(subtotal) if subtotal.is_integer() else subtotal
        disc_str = int(disc_val) if disc_val.is_integer() else disc_val
        tot_str = int(final_total) if final_total.is_integer() else final_total
        pct_str = int(pct) if pct.is_integer() else pct
        
        return (f"ملاحظة حسابية مؤكدة: حساب الإجمالي لـ {days_count} أيام بسعر يومي {d_str} جنيه مع خصم {pct_str}%: "
                f"1) الإجمالي قبل الخصم: {d_str} × {days_count} = {sub_str} جنيه. "
                f"2) قيمة الخصم ({pct_str}%): {sub_str} × {pct_str}% = {disc_str} جنيه. "
                f"3) الإجمالي النهائي قبل التأمين: {sub_str} - {disc_str} = {tot_str} جنيه مصري. "
                f"التزم بهذه المعادلة والناتج {tot_str} جنيه بالضبط.")

    # 7. الحساب العام للمبلغ الواحد مع الخصم والرسوم
    query_nums = [float(n) for n in re.findall(r'\b\d+(?:\.\d+)?\b', query) if float(n) not in pct_matches and int(n) != days_count]
    item_prices = query_nums if query_nums else base_daily_rates
    
    # استخراج الرسوم الإضافية
    additional_fee = 0.0
    fee_match = re.search(r'(?:رسوم|مصاريف|تكلفة)\s*(?:التوصيل|الزيارة|الشحن|الخدمة|الإضافية)\s*(\d+(?:\.\d+)?)\s*جنيه', context_text)
    has_fee_mention = any(k in query for k in ['توصيل', 'شحن', 'زيارة', 'خدمة', 'رسوم', 'مصاريف'])
    if fee_match and has_fee_mention:
        additional_fee = float(fee_match.group(1))

    if item_prices and pct_matches:
        price = item_prices[0]
        pct = pct_matches[0]
        discount_val = round(price * (pct / 100.0), 2)
        price_after_disc = round(price - discount_val, 2)
        total_with_fee = round(price_after_disc + additional_fee, 2)
        
        p_str = int(price) if price.is_integer() else price
        d_str = int(discount_val) if discount_val.is_integer() else discount_val
        pad_str = int(price_after_disc) if price_after_disc.is_integer() else price_after_disc
        pct_str = int(pct) if pct.is_integer() else pct
        
        if additional_fee > 0:
            fee_str = int(additional_fee) if additional_fee.is_integer() else additional_fee
            tot_str = int(total_with_fee) if total_with_fee.is_integer() else total_with_fee
            return (f"ملاحظة حسابية مؤكدة: سعر الصنف ({p_str} ج) بعد خصم {pct_str}% ({d_str} ج) = {pad_str} ج. "
                    f"رسوم التوصيل/الخدمة ({fee_str} ج) تُضاف كاملة ولا تخضع للخصم. "
                    f"الإجمالي النهائي = {pad_str} + {fee_str} = {tot_str} جنيه مصري.")
        else:
            return (f"ملاحظة حسابية مؤكدة: السعر ({p_str} ج) بعد خصم {pct_str}% ({d_str} ج) = {pad_str} جنيه مصري.")

    return None
