# -*- coding: utf-8 -*-
"""
محرك استخراج السياسات وهيكلة القيود التلقائي (Dynamic Policy & Constraint Extractor):
- يحلل مستندات قاعدة المعرفة عند رفعها أو فهرستها.
- يستخرج بصورة آلية:
  1. الاستثناءات وحالات عدم الشمول للباقات والاشتراكات.
  2. العتبات والحدود الرقمية (النسب المئوية، الساعات، الأيام).
  3. سياسات الحظر والمنع المطلق (Non-Refundable / Irreversible).
- يخزن السياسات بصيغة JSON لتمكين الاستعلام المنطقي الحتمي لأي مجال داتا جديد.
"""
import json
import os
import re

_POLICIES_CACHE = {}


def extract_policies_from_text(text, source=""):
    """استخراج منظم للسياسات والقيود من نص مستند واحد."""
    policies = {
        'exclusions': [],
        'thresholds': [],
        'absolute_prohibitions': [],
        'pricing_rules': []
    }
    
    lines = text.split('\n')
    for line in lines:
        line_s = line.strip()
        if not line_s:
            continue
            
        # 1. فحص الاستثناءات وعدم الشمول
        excl_match = re.search(r'(?:لا يشمل|لا تشمل|غير متوفر[ةي]? في|مستثن[اى]ة? من|لا يغطي|لا يتضمن)\s*([^،\.\n]+)', line_s)
        if excl_match:
            policies['exclusions'].append({
                'source': source,
                'raw': line_s,
                'excluded_target': excl_match.group(1).strip()
            })

        # 2. فحص العتبات والشروط الرقمية (نسب مئوية، ساعات، أيام)
        pct_threshold = re.search(r'(?:ألا يتجاوز|لا يتجاوز|ما أتمش أكتر من|أقل من|بحد أقصى|أول)\s*(\d+(?:\.\d+)?)\s*%', line_s)
        if pct_threshold:
            policies['thresholds'].append({
                'source': source,
                'type': 'percentage',
                'value': float(pct_threshold.group(1)),
                'raw': line_s
            })

        time_threshold = re.search(r'(?:قبل|خلال|في غضون|أقل من)\s*(\d+(?:\.\d+)?)\s*(ساعة|ساعات|يوم|أيام|ايام)', line_s)
        if time_threshold:
            val = float(time_threshold.group(1))
            unit = time_threshold.group(2)
            policies['thresholds'].append({
                'source': source,
                'type': 'time',
                'value': val,
                'unit': unit,
                'raw': line_s
            })

        # 3. فحص قرارات الحظر المطلق (Absolute Prohibitions)
        abs_match = re.search(r'(?:غير قابل للاسترداد|غير قابلة للاسترداد|غير مسترد[ة]?|لا يمكن الإلغاء|لا يوجد استرداد|لا يحق له الاسترجاع)[^،\.\n]*', line_s)
        if abs_match:
            policies['absolute_prohibitions'].append({
                'source': source,
                'rule': abs_match.group(0).strip(),
                'raw': line_s
            })
            
    return policies


def extract_policies_from_kb_dir(kb_dir):
    """استخراج السياسات وتجميعها من كافة ملفات المجلد."""
    combined = {
        'exclusions': [],
        'thresholds': [],
        'absolute_prohibitions': [],
        'pricing_rules': []
    }
    
    if not os.path.exists(kb_dir):
        return combined
        
    for fname in os.listdir(kb_dir):
        if fname.endswith(('.md', '.txt')):
            fpath = os.path.join(kb_dir, fname)
            try:
                with open(fpath, 'r', encoding='utf-8') as f:
                    content = f.read()
                file_p = extract_policies_from_text(content, source=fname)
                for k in combined:
                    combined[k].extend(file_p[k])
            except Exception as e:
                print(f"[PolicyExtractor Error in {fname}]: {e}")
                
    return combined


def get_tenant_policies(tenant_id='default', kb_dir=None, force_reload=False):
    """جلب سياسات الـ Tenant المهيكلة مع تخزين كاش سريع."""
    global _POLICIES_CACHE
    if not force_reload and tenant_id in _POLICIES_CACHE:
        return _POLICIES_CACHE[tenant_id]
        
    if not kb_dir:
        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        kb_dir = os.path.join(base_dir, 'data', 'tenants', tenant_id, 'knowledge_base')
        
    policies = extract_policies_from_kb_dir(kb_dir)
    _POLICIES_CACHE[tenant_id] = policies
    
    # حفظ نسخة مهيكلة في data/tenants/{tenant_id}/policies.json
    try:
        tenant_dir = os.path.dirname(kb_dir)
        json_path = os.path.join(tenant_dir, 'policies.json')
        with open(json_path, 'w', encoding='utf-8') as f:
            json.dump(policies, f, ensure_ascii=False, indent=2)
    except Exception:
        pass
        
    return policies
