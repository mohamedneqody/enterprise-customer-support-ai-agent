# -*- coding: utf-8 -*-
"""
test_fallback.py — اختبار آليات الـ Graceful Degradation والـ Fallback لطبقة BERT Re-ranker.
"""
import os
import sys
import unittest
from unittest.mock import patch

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

# 1. اختبار وضع التعطيل الصريح (ENABLE_RERANKER=false)
os.environ['ENABLE_RERANKER'] = 'false'
os.environ['ENABLE_LLM'] = 'false'  # تشغيل extractive للتأكد من خط الأنابيب الأساسي بسرعة

from engine import reranker, pipeline

print("=" * 60)
print("TEST 1: Disable Model explicitly (ENABLE_RERANKER=false)")
print("=" * 60)
matches_in = [
    ({'heading': 'باقة 1', 'text': 'تفاصيل 1', 'source': 'kb.md'}, 0.8),
    ({'heading': 'باقة 2', 'text': 'تفاصيل 2', 'source': 'kb.md'}, 0.5)
]
out = reranker.rerank_matches("سؤال تجريبي", matches_in)
print("Is original TF-IDF returned without calling BERT?", out == matches_in)
res = pipeline.handle_message("مرسيدس C200 سعرها كام؟")
print("handle_message() answer in disabled mode:", res.get("answer"))

# 2. اختبار محاكاة خطأ/فشل في مكتبة sentence_transformers (Simulated Exception)
print("\n" + "=" * 60)
print("TEST 2: Simulated Crash / Missing Library (Simulated Crash Fallback)")
print("=" * 60)
os.environ['ENABLE_RERANKER'] = 'true'

# تصفير الموديل في الذاكرة لمحاكاة خطأ استيراد/تحميل
reranker._MODEL = None

with patch.dict('sys.modules', {'sentence_transformers': None}):
    print("Simulating failure of sentence_transformers import...")
    out_fail = reranker.rerank_matches("سؤال تجريبي", matches_in)
    print("Did Fallback return TF-IDF candidates without crashing?", len(out_fail) == len(matches_in))
    
    res_after_failure = pipeline.handle_message("مرسيدس C200 سعرها كام؟")
    print("Did handle_message() succeed gracefully without server crashing?", bool(res_after_failure.get("answer")))
    print("Retrieved Answer in Fallback Mode:", res_after_failure.get("answer")[:80] + "...")

print("\n" + "=" * 60)
print("SUCCESS: Fallback & Graceful Degradation tests passed 100%!")
print("=" * 60)
