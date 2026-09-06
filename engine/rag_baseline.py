# -*- coding: utf-8 -*-
"""
محرك RAG من الصفر (بدون أي مكتبات خارجية):
- تطبيع النصوص العربية والإنجليزية (همزات، تاء مربوطة، تشكيل، سواقة)
- Stemming خفيف للعربي + مجموعات مرادفات لتحسين الاستدعاء
- تقسيم ملفات المعرفة (Markdown) لقطع chunks
- تمثيل TF-IDF + بحث بالتشابه الكوني (Cosine Similarity)
"""
import math
import os
import re

# ---------------------------------------------------------------- تطبيع النص

_DIACRITICS = re.compile(u'[\u0640\u064B-\u0652\u0670]')
_TOKEN = re.compile(u'[a-z0-9\u0621-\u064a]+')
_REPLACEMENTS = ((u'\u0623', u'\u0627'), (u'\u0625', u'\u0627'), (u'\u0622', u'\u0627'),
                 (u'\u0649', u'\u064a'), (u'\u0629', u'\u0647'), (u'\u0624', u'\u0648'),
                 (u'\u0626', u'\u064a'))


_PREFIXES = ('وال', 'فال', 'بال', 'كال', 'ال', 'لل', 'بت', 'وت', 'و', 'ف', 'ب', 'ل', 'ك', 'ت')
_SUFFIXES = ('ها', 'هم', 'هن', 'ين', 'ية', 'ات', 'ون', 'ين', 'ي', 'ة', 'ه', 'وا')

def normalize(text):
    """توحيد شكل الحروف: lowercase + إزالة التشكيل + توحيد الهمزات والتاء المربوطة."""
    text = text.lower()
    text = _DIACRITICS.sub('', text)
    for c, r in _REPLACEMENTS:
        text = text.replace(c, r)
    text = re.sub(r'[^\w\s]', ' ', text)
    return text

def stem(token):
    """تجذير خفيف جداً: يزيل الـ prefix والـ suffix الشائعة."""
    protected_roots = {
        'تاريخ', 'تليفون', 'تطبيق', 'تفاصيل', 'توصيل', 'تاكيد', 'تغيير',
        'تعديل', 'تكلف', 'تقسيط', 'تحويل', 'تسليم', 'تاخير',
        'تقييم', 'تتبع', 'تجرب', 'تذكر', 'تذاكر', 'تسجيل', 'تفعيل',
        'تخفيض', 'بطاري', 'بنزين', 'بواب', 'برنامج', 'برامج', 'بيانات',
        'فرامل', 'فلتر', 'فلاتر', 'فاتور', 'فواتير', 'فرع', 'فروع', 'فحص',
        'كاوتش', 'كابل', 'كود', 'كارت', 'كروت', 'كمبروسر',
        'وساد', 'وسائد', 'وقت', 'ورش', 'لوح', 'لتر',
        'بكام', 'كاميرا', 'كاميرات', 'كام'
    }

    if token in ('بكام', 'كاميرا', 'كاميرات'):
        return token

    for p in _PREFIXES:
        if token.startswith(p) and len(token) - len(p) >= 3:
            if p in ('و', 'ف', 'ب', 'ل', 'ك', 'ت', 'بت', 'وت'):
                base = 'ت' + token[len(p):] if p in ('بت', 'وت') else token
                if any(base.startswith(r) for r in protected_roots):
                    continue
            token = token[len(p):]
            break
    for s in _SUFFIXES:
        if token.endswith(s) and len(token) - len(s) >= 3:
            token = token[:-len(s)]
            break
    return token


def tokenize(text):
    """نص -> قائمة tokens مطبعة ومجذعة (تستخدم للاستعلامات والفهرسة معاً)."""
    out = []
    for raw in _TOKEN.findall(normalize(text)):
        t = stem(raw)
        if len(t) >= 2:
            out.append(t)
    return out


def _clean_word(word):
    return stem(normalize(word))


STOPWORDS = set(_clean_word(w) for w in [
    'في', 'من', 'على', 'عن', 'مع', 'هذا', 'هذه', 'ذلك', 'تلك', 'انا', 'ان', 'انت',
    'هو', 'هي', 'احنا', 'هم', 'او', 'و', 'ثم', 'لا', 'نعم', 'كل', 'بعض', 'غير',
    'قد', 'كان', 'كانت', 'يكون', 'تكون', 'عندي', 'بين', 'الى', 'لدي', 'ايضا',
    'جدا', 'فقط', 'عندما', 'لكن', 'ما', 'هل', 'يا', 'ان', 'ايه', 'ماذا', 'ماهي',
    'طب', 'ده', 'دي', 'دول', 'كده', 'معاه', 'معاها', 'عشان', 'بقى', 'بقي',
    'وهل', 'فهل', 'ياريت', 'لو', 'سمحت', 'ممكن', 'عاوزك', 'عايزك', 'قولي',
    'وضحلي', 'اشرحلي', 'تلخصلي', 'لخصلي', 'لخص', 'فرق', 'مقارنة', 'مقارنه',
    'نقطتين', 'سريعتين', 'سريع', 'سريعة', 'بسرعة', 'اللي', 'اللى', 'عندكم',
    'عندك', 'فيها', 'فيه', 'منهم', 'بينهم', 'عايز', 'عاوز', 'اريد', 'محتاج',
    'the', 'a', 'an', 'is', 'are', 'was', 'were', 'be', 'been', 'to', 'of', 'in',
    'on', 'at', 'for', 'with', 'and', 'or', 'i', 'you', 'your', 'my', 'we', 'our',
    'it', 'its', 'do', 'does', 'did', 'can', 'could', 'will', 'would', 'have',
    'has', 'this', 'that', 'these', 'those',
])

# مرادفات عامية/فصحى/إنجليزي عشان الاستدعاء يشتغل مع اختلاف صيغة السؤال
_RAW_GROUPS = [
    ['شحن', 'توصيل', 'يشحن', 'بشحن', 'بيشحن', 'شحنه'],
    ['استرجاع', 'ارجاع', 'استرداد', 'استبدال', 'رجوع'],
    ['فلوس', 'مبلغ', 'استرداد'],
    ['خصم', 'كوبون', 'كود', 'عرض', 'عروض'],
    ['سعر', 'اسعار', 'تكلفة', 'تكلفه', 'كام', 'ثمن', 'بكام'],
    ['تفاصيل', 'مواصفات', 'امكانيات', 'معلومات', 'ميزة', 'مميزات'],
    ['اوردر', 'طلب', 'طلبات', 'اوردرات'],
    ['باسورد', 'كلمة', 'المرور', 'دخول', 'اكونت', 'حساب'],
    ['تتبع', 'متابعة', 'متابعه', 'اتبع'],
    ['فين', 'اين', 'مكان'],
    ['ازاي', 'ازي', 'كيف', 'طريقة', 'طريقه'],
    ['اعمل', 'اعملي', 'اجراء', 'خطوات', 'طريقة'],
    ['ايه', 'ماذا', 'ماهي'],
    ['امتى', 'امتي', 'متى', 'مدة', 'مده', 'وقت'],
    ['ليه', 'لماذا', 'سبب'],
    ['عايز', 'عاوز', 'اريد', 'محتاج', 'ازاي'],
    ['موبايل', 'هاتف', 'تليفون', 'جوال', 'smartphones', 'smartphone'],
    ['لابتوب', 'كمبيوتر', 'laptop', 'dell', 'ديل'],
    ['سامسونج', 'samsung', 'جالاكسي', 'galaxy'],
    ['شاومي', 'xiaomi', 'ريدمي', 'redmi'],
    ['ابل', 'ماك', 'apple', 'macbook'],
    ['اسمك', 'اسم', 'مين', 'انت', 'هويتك', 'نفسك', 'شخصيتك', 'مين_انت'],
    ['صنعك', 'صانع', 'طورك', 'مطور', 'مبرمج', 'شركة', 'صانعة', 'صنع', 'برمجك'],
    ['وظيفة', 'وظيفتك', 'بتعمل', 'شغل', 'دورك', 'بتساعد', 'خدماتك'],
    ['سلام', 'مرحبا', 'اهلا', 'صباح', 'مساء', 'ازيك', 'عامل', 'هاي', 'hello', 'hi'],
    ['شكرا', 'تسلم', 'مشكور', 'الف_شكر', 'thanks', 'thank'],
    ['تالف', 'مكسور', 'معطوب', 'خربان'],
    ['دفع', 'فيزا', 'محفظة', 'محفظه', 'كاش', 'تقسيط', 'فاليو'],
    ['فاتورة', 'فاتوره', 'ضريبية', 'ضريبيه'],
    ['track', 'tracking', 'order'],
    ['return', 'refund', 'exchange'],
    ['فحص', 'تحليل', 'تحاليل', 'فحوصات', 'كشف', 'اختبار'],
    ['كاميرا', 'كاميرات', 'camera'],
]

SYNONYMS = {}
for _group in _RAW_GROUPS:
    _clean = set(_clean_word(w) for w in _group)
    for _w in _clean:
        SYNONYMS[_w] = _clean


def expand(tokens):
    """توسيع الاستعلام بالمرادفات (بيستخدم مع جزء الاستعلام بس)."""
    out = set(tokens)
    for t in tokens:
        g = SYNONYMS.get(t)
        if g:
            out |= g
    return out


def content_tokens(query):
    """tokens المهمة من استعلام: (الأصلية، الموسعة بالمرادفات)."""
    orig = set(t for t in tokenize(query) if t not in STOPWORDS)
    return orig, (expand(orig) if orig else set())


# ---------------------------------------------------------------- تقسيم الملفات

def _parse_markdown(path):
    """كل قسم (## عنوان) يبقى chunk مستقل: عنوانه السؤال وجسمه الإجابة."""
    with open(path, encoding='utf-8') as f:
        lines = f.read().splitlines()
    source = os.path.basename(path)
    chunks = []
    heading = None
    body = []

    def flush():
        text = '\n'.join(body).strip()
        if heading and text:
            chunks.append({'source': source, 'heading': heading, 'text': text})
        elif text:
            # ملف من غير عناوين: كل فقرة (سطر فاضي بينهم) قسم مستقل
            for para in re.split(r'\n\s*\n', text):
                para = para.strip()
                if para:
                    chunks.append({'source': source,
                                   'heading': para.split('\n')[0][:60],
                                   'text': para})

    for line in lines:
        if line.lstrip().startswith('#'):
            flush()
            heading = line.lstrip().lstrip('#').strip()
            body = []
        else:
            body.append(line)
    flush()
    return chunks


# ---------------------------------------------------------------- الفهرس

class KnowledgeIndex(object):
    """فهرس TF-IDF فوق قطع قاعدة المعرفة."""

    def __init__(self, kb_dir):
        self.kb_dir = kb_dir
        self.chunks = []
        self.vectors = []
        self.norms = []

    def build(self):
        self.chunks, self.vectors, self.norms = [], [], []
        df = {}
        for name in sorted(os.listdir(self.kb_dir)):
            if not name.lower().endswith(('.md', '.txt')):
                continue
            for chunk in _parse_markdown(os.path.join(self.kb_dir, name)):
                tokens = [t for t in tokenize(chunk['heading'] + '\n' + chunk['text'])
                          if t not in STOPWORDS]
                if not tokens:
                    continue
                self.chunks.append(chunk)
                tf = {}
                for t in tokens:
                    tf[t] = tf.get(t, 0) + 1
                vec = {}
                for t, c in tf.items():
                    df[t] = df.get(t, 0) + 1
                    vec[t] = float(c) / len(tokens)
                self.vectors.append(vec)
        n = len(self.chunks)
        for i in range(n):
            norm = 0.0
            for t, w in self.vectors[i].items():
                w = w * (math.log((n + 1.0) / (df[t] + 1.0)) + 1.0)
                self.vectors[i][t] = w
                norm += w * w
            self.norms.append(math.sqrt(norm) or 1.0)
        return self

    def search(self, query, top_k=4):
        """بحث كوني: بيرجع قائمة (chunk, score) مرتبة تنازلياً."""
        orig, expanded = content_tokens(query)
        if not expanded:
            return []
        qvec = {}
        for t in orig:
            qvec[t] = qvec.get(t, 0.0) + 1.0
        for t in (expanded - orig):
            qvec[t] = qvec.get(t, 0.0) + 0.6
        total = float(len(qvec))
        qvec = {t: c / total for t, c in qvec.items()}
        qnorm = math.sqrt(sum(w * w for w in qvec.values())) or 1.0
        scored = []
        for i, vec in enumerate(self.vectors):
            dot = 0.0
            for t, w in qvec.items():
                v = vec.get(t)
                if v:
                    dot += w * v
            if dot > 0.0:
                base_score = dot / (qnorm * self.norms[i])
                chunk_tokens = set(tokenize(self.chunks[i].get('heading', '') + ' ' + self.chunks[i].get('text', '')))
                matched_concepts = sum(1 for t in orig if any(syn in chunk_tokens for syn in SYNONYMS.get(t, {t})))
                concept_ratio = (float(matched_concepts) / len(orig)) if orig else 1.0
                if concept_ratio >= 0.99:
                    base_score *= 2.0
                elif concept_ratio >= 0.5:
                    base_score *= (1.0 + concept_ratio)
                heading_tokens = set(tokenize(self.chunks[i].get('heading', '')))
                exact_hits = sum(1 for t in orig if t in heading_tokens)
                if exact_hits > 0:
                    base_score *= (1.0 + 0.6 * exact_hits)
                scored.append((base_score, i))
        scored.sort(reverse=True)
        return [(self.chunks[i], s) for s, i in scored[:top_k]]

    def sources(self):
        counts = {}
        for c in self.chunks:
            counts[c['source']] = counts.get(c['source'], 0) + 1
        return [{'file': f, 'sections': counts[f]} for f in sorted(counts)]