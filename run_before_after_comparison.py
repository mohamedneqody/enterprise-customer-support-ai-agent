# -*- coding: utf-8 -*-
"""
run_before_after_comparison.py — تشغيل اختبار مقارنة قبل/بعد (Before vs After BERT Re-ranker)
مع استخراج المصادر ودرجة الثقة والإجابة التوليدية بدقة كاملة.
"""
import os
import sys
import json
import time

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

os.environ['OLLAMA_TIMEOUT'] = '90'
os.environ['ENABLE_LLM'] = 'true'

test_questions = [
    {
        "id": "problem_1_keyword_hijacking",
        "title": "مشكلة 1: اختطاف الكلمات المفتاحية (Keyword Hijacking)",
        "query": "إيه الفرق بين فئة اقتصادية وفئة SUV؟",
        "session_id": "test_sess_p1"
    },
    {
        "id": "problem_2_colloquial_semantic",
        "title": "مشكلة 2: الفهم الدلالي مع الصياغة العامية (Colloquial Semantic)",
        "query": "عربية رخيصة عندكوا إيه؟",
        "session_id": "test_sess_p2"
    },
    {
        "id": "problem_3a_compound_math",
        "title": "مشكلة 3-أ: الحسبة المركبة متعددة المسارات (كيا بيكانتو + خصم مبكر)",
        "query": "حجزت كيا بيكانتو 3 أيام بخصم الحجز المبكر 10% (حجزت من أسبوعين)، الإجمالي كام قبل التأمين؟",
        "session_id": "test_sess_p3a"
    },
    {
        "id": "problem_3b_extra_km",
        "title": "مشكلة 3-ب: الكيلومترات الإضافية والوحدات (50 كم زيادة)",
        "query": "عديت الكيلومترات المسموحة بـ50 كيلو، هدفع كام إضافي؟",
        "session_id": "test_sess_p3b"
    }
]

def run_suite(enable_reranker):
    os.environ['ENABLE_RERANKER'] = 'true' if enable_reranker else 'false'
    
    from engine import pipeline, reranker
    pipeline.get_index().build()
    reranker._MODEL = None  # Reset model cache
    
    suite_results = []
    for item in test_questions:
        t0 = time.time()
        res = pipeline.handle_message(item["query"], session_id=item["session_id"] + ("_after" if enable_reranker else "_before"))
        latency = round((time.time() - t0) * 1000)
        
        suite_results.append({
            "id": item["id"],
            "title": item["title"],
            "query": item["query"],
            "confidence": res.get("confidence", 0),
            "sources": res.get("sources", []),
            "answer": res.get("answer", ""),
            "model_used": res.get("model_used", ""),
            "latency_ms": latency
        })
    return suite_results

print("🚀 1. جاري تشغيل الاختبار الأول: الوضع القديم (ENABLE_RERANKER=false)...")
before_results = run_suite(enable_reranker=False)

print("🚀 2. جاري تشغيل الاختبار الثاني: الوضع المطور (ENABLE_RERANKER=true)...")
after_results = run_suite(enable_reranker=True)

comparison = {
    "before": before_results,
    "after": after_results
}

with open("before_after_results.json", "w", encoding="utf-8") as f:
    json.dump(comparison, f, ensure_ascii=False, indent=2)

print("✅ تم حفظ نتائج المقارنة بنجاح في before_after_results.json")
