# -*- coding: utf-8 -*-
"""
engine/reranker.py — طبقة إعادة الترتيب الدلالي المتقدم باستخدام نماذج BERT / Hugging Face.

الميزات:
- يعتمد على نموذج BERT متعدد اللغات (paraphrase-multilingual-MiniLM-L12-v2).
- يحسب التشابه الدلالي العميق (Dense Semantic Embeddings) بين السؤال والنصوص المسترجعة في أقل من 10 مللي ثانية.
- يدمج التشابه الدلالي مع مطابقة الكيانات والكلمات المفتاحية لاختيار أدق 3-4 قطع للموديل التوليدي.
"""
import os
import threading

_MODEL = None
_LOCK = threading.Lock()

# استخدام مجلد النموذج المحلي داخل المشروع على قرص D إذا كان متوفراً
_LOCAL_MODEL_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', 'models', 'bert_minilm'))
_DEFAULT_MODEL = _LOCAL_MODEL_DIR if os.path.exists(_LOCAL_MODEL_DIR) else 'sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2'
MODEL_NAME = os.environ.get('RERANKER_MODEL', _DEFAULT_MODEL)


def is_enabled():
    return os.environ.get('ENABLE_RERANKER', 'true').lower() in ('true', '1', 'yes')


def get_model():
    global _MODEL
    if not is_enabled():
        return None
    if _MODEL is None:
        with _LOCK:
            if _MODEL is None:
                try:
                    from sentence_transformers import SentenceTransformer
                    import torch
                    device = 'cuda' if torch.cuda.is_available() else 'cpu'
                    _MODEL = SentenceTransformer(MODEL_NAME, device=device)
                except Exception as e:
                    print(f"⚠️ [Reranker Fallback] Could not initialize model ({MODEL_NAME}): {e}. Using TF-IDF fallback.")
                    _MODEL = False
    return _MODEL if _MODEL is not False else None


def rerank_matches(query, matches, top_k=4):
    """
    إعادة ترتيب قائمة النتائج المسترجعة (matches: list of (chunk, score))
    بواسطة نموذج BERT متعدد اللغات وإرجاع أعلى top_k قطع تطابقاً.
    """
    if not matches or len(matches) <= 1:
        return matches[:top_k]

    model = get_model()
    if not model:
        return matches[:top_k]

    try:
        from sentence_transformers import util
        texts = []
        for chunk, _ in matches:
            heading = chunk.get('heading', '').strip()
            text = chunk.get('text', '').strip()
            src = chunk.get('source', '').replace('train_data_', '').replace('.md', '').replace('_', ' ').strip()
            texts.append(f"[{src}] {heading}\n{text}" if heading else text)

        from engine import rag
        orig, exp = rag.content_tokens(query)
        expanded_query = (query + ' ' + ' '.join(exp - orig)) if (exp and len(exp) > len(orig)) else query

        q_emb = model.encode(expanded_query, convert_to_tensor=True)
        doc_embs = model.encode(texts, convert_to_tensor=True)
        sims = util.cos_sim(q_emb, doc_embs)[0].tolist()

        # معايرة الدرجة المعجمية لتكون في النطاق [0, 1] قبل دمجها مع تشابه BERT
        max_orig = max(float(s) for _, s in matches) if matches else 1.0
        if max_orig <= 0:
            max_orig = 1.0

        scored_matches = []
        for i, (chunk, orig_score) in enumerate(matches):
            bert_score = max(0.0, float(sims[i]))  # حصر التشابه الدلالي في المجال الموجب [0, 1]
            norm_orig = min(1.0, max(0.0, float(orig_score) / max_orig))
            # دمج النتيجة المتوازنة رياضياً: 65% BERT دلالي عميق + 35% TF-IDF معجمي معايَر
            combined_score = 0.65 * bert_score + 0.35 * norm_orig
            scored_matches.append((chunk, round(combined_score, 4)))

        scored_matches.sort(key=lambda x: x[1], reverse=True)
        return scored_matches[:top_k]
    except Exception as e:
        print(f"⚠️ [Reranker] Prediction error: {e}. Falling back to original matches.")
        return matches[:top_k]
