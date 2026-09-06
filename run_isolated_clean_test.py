# -*- coding: utf-8 -*-
import os, sys, json, time, shutil

if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')

os.environ['OLLAMA_TIMEOUT'] = '120'
os.environ['ENABLE_LLM'] = 'true'

query = 'إيه الفرق بين فئة اقتصادية وفئة SUV؟'

print('=====================================================')
print('🚀 بدء الاختبار النظيف المنعزل (Clean Isolated Test)')
print('=====================================================\n')

# 1. المرحلة الأولى: الخط الأساسي الحقيقي (True Baseline)
print('📌 1. تجهيز الخط الأساسي الحقيقي (بدون مرادفات عامية، وبدون Re-ranker)...')
shutil.copy('engine/rag_baseline.py', 'engine/rag.py')
os.environ['ENABLE_RERANKER'] = 'false'

from engine import pipeline, reranker
reranker._MODEL = None
pipeline.get_index().build()

t0 = time.time()
res_baseline = pipeline.handle_message(query, session_id='isolated_test_baseline')
lat_baseline = round((time.time() - t0) * 1000)
print('✅ انتهى اختبار الخط الأساسي.\n')

# 2. المرحلة الثانية: بعد التفعيل الكامل (Full Enhanced with BERT & Synonyms)
print('📌 2. تجهيز الوضع المطور الكامل (مع المرادفات ومع BERT Re-ranker)...')
shutil.copy('engine/rag_backup_enhanced.py', 'engine/rag.py')
os.environ['ENABLE_RERANKER'] = 'true'
reranker._MODEL = None
pipeline.get_index().build()

t1 = time.time()
res_enhanced = pipeline.handle_message(query, session_id='isolated_test_enhanced')
lat_enhanced = round((time.time() - t1) * 1000)
print('✅ انتهى اختبار الوضع المطور.\n')

# حفظ وعرض النتائج
final_comparison = {
    'query': query,
    'baseline': {
        'enable_reranker': False,
        'has_colloquial_synonyms': False,
        'confidence': res_baseline.get('confidence'),
        'status': res_baseline.get('status'),
        'sources': res_baseline.get('sources'),
        'answer': res_baseline.get('answer'),
        'latency_ms': lat_baseline
    },
    'enhanced': {
        'enable_reranker': True,
        'has_colloquial_synonyms': True,
        'confidence': res_enhanced.get('confidence'),
        'status': res_enhanced.get('status'),
        'sources': res_enhanced.get('sources'),
        'answer': res_enhanced.get('answer'),
        'latency_ms': lat_enhanced
    }
}

with open('clean_isolated_test_results.json', 'w', encoding='utf-8') as f:
    json.dump(final_comparison, f, ensure_ascii=False, indent=2)

print('🎯 تم حفظ النتائج في clean_isolated_test_results.json بنجاح!')
