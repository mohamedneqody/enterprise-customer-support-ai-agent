# -*- coding: utf-8 -*-
import sys
if sys.stdout.encoding != 'utf-8':
    sys.stdout.reconfigure(encoding='utf-8')
from engine import pipeline, reranker
from sentence_transformers import util

model = reranker.get_model()
index = pipeline.get_index()
q = 'عربية رخيصة عندكوا إيه؟'
matches = index.search(q, top_k=15)

texts = [f"{c.get('heading','')}\n{c.get('text','')}" for c, _ in matches]
q_emb = model.encode(q, convert_to_tensor=True)
doc_embs = model.encode(texts, convert_to_tensor=True)
sims = util.cos_sim(q_emb, doc_embs)[0].tolist()

ranked = sorted(zip(matches, sims), key=lambda x: x[1], reverse=True)
print('Pure BERT Dense Cosine Similarity:')
for (c, orig_s), b_score in ranked[:5]:
    h = c.get('heading', '')
    src = c.get('source', '')
    print(f' - {src} :: {h} | BERT Score: {round(b_score, 3)} | TF-IDF: {round(orig_s, 3)}')
