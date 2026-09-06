import sys, re
sys.stdout.reconfigure(encoding='utf-8')

from engine import rag, pipeline, llm

idx = rag.KnowledgeIndex('data/tenants/default/knowledge_base').build()

questions = [
    "اشتركت النهاردة ولسه مادخلتش خالص، عايز أرجع فلوسي، ينفع؟",
    "اشتركت النهاردة ولسه مادخلتش، عايز أرجع فلوسي؟",
    "إيه فرق باقة الزوجين عن الاشتراك الفردي؟",
    "كارت الطالب بيوفر كام في السنة؟"
]

print("================================================================")
print("=== INVESTIGATION OF THE 3 FAILING QUESTIONS ===")
print("================================================================\n")

for q in questions:
    print(f"🔹 QUERY: {q}")
    
    # 1. Search targets decomposition if any
    targets = pipeline.extract_comparison_targets(q)
    print(f"   - Comparison Targets: {targets}")
    
    # 2. Raw Search
    matches = idx.search(q, top_k=5)
    conf = pipeline._confidence(q, matches)
    print(f"   - Confidence Score: {conf:.3f} (Threshold to escalate: < 0.35)")
    
    # Tokens & Synonyms
    orig, expanded = rag.content_tokens(q)
    print(f"   - Content Tokens (Orig): {orig}")
    print(f"   - Expanded Tokens: {expanded}")
    
    print("   - Top 3 Retrieval Matches:")
    for i, (chunk, score) in enumerate(matches[:3], 1):
        print(f"     [{i}] Score: {score:.4f} | Source: {chunk.get('source')} | Heading: {chunk.get('heading')}")
        snippet = chunk.get('text', '').replace('\n', ' ')[:140]
        print(f"         Snippet: {snippet}...")
    
    # Test if LLM receives it and what prompt it produces
    prompt = llm._build_prompt(q, matches)
    print("----------------------------------------------------------------\n")
