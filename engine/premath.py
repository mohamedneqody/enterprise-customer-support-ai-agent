# -*- coding: utf-8 -*-
"""
engine/premath.py — المحرك الحتمي قبل النموذج (Deterministic Pre-Math Engine).

أي سؤال فيه أرقام/كلمات مالية (خصم/استعجال/رسوم/إجمالي/عربون/بكام/إلغاء) يُحل
برمجياً من بيانات المستأجر نفسها قبل لمس النموذج التوليدي:
- يقرأ السجلات (key: value) من أي chunks الفهرس — مفاتيح بأي لغة تُصنَّف
  من الكلمات الدالة داخل اسم الحقل (سعر/خصم/رسوم/فرع/سلبية/متاح/price/fee...).
- يقسم السؤال المركب لأسئلة فرعية ويجيب كل جزء، ويعلّم غير المحلول فقط.
- نوايا: سعر مباشر، مقارنة (أرخص/الفرق) بحسم رقمي، استعجال، خصم مع استثناء
  صريح (الاستثناء يلغي العرض) ورسوم لا تدخل الخصم، عربون (ثابت/نسبة/لكيان)،
  نافذة إلغاء بقاعدتين متعارفتين (قبل N / خلال N) مع بيان المقارنة،
  توفر بالفرع (حظر مباشر/بسقف/متاح) بحسم أولي.

لا يحتوي أي اسم خدمة أو مجال — كل شيء يُشتق من الحقول والنصوص نفسها.
"""
import re


# ---------------------------------------------------------------- تطبيع خفيف

def _norm(s):
    s = (s or '').lower()
    s = re.sub(r'[\u064B-\u0652\u0670]', '', s)
    # إزالة زينة ماركداون (** __ *) — السياسات تتكتب بها وتكسر الأنماط
    s = re.sub(r'[*_`]+', ' ', s)
    s = s.replace('أ', 'ا').replace('إ', 'ا').replace('آ', 'ا').replace('ة', 'ه').replace('ى', 'ي')
    return re.sub(r'\s+', ' ', s).strip()


def _words_seq(text):
    """مثل _words لكن **مرتبة بترتيب النص** (قائمة لا مجموعة) — لأي منطق
    يعتمد على موضع الكلمات (أول 6 كلمات بعد مثير النفي مثلاً)؛ ترتيب set
    عشوائي بين العمليات وكان يجعل مطابقة الاستثناء متقلبة."""
    out = []
    seen = set()
    for w in re.findall(r'[\u0621-\u064a]{3,}', _norm(text or '')):
        w2 = re.sub(r'^(?:و|ف|ب|ك)?(?:ال|لل)', '', w)
        w2 = w2.lstrip('و')
        if len(w2) >= 3 and w2 not in seen:
            seen.add(w2)
            out.append(w2)
    return out


def _words(s):
    """كلمات دالة بعد قلع أدوات التعريف والعطف — عربي + لاتيني/أرقام."""
    out = set()
    for w in re.findall(r'[\u0621-\u064a]{3,}', _norm(s)):
        w2 = re.sub(r'^(?:و|ف|ب|ك)?(?:ال|لل)', '', w)
        w2 = w2.lstrip('و')
        if len(w2) >= 3:
            out.add(w2)
    for w in re.findall(r'[a-z0-9]{2,}', (s or '').lower()):
        if w not in ('the', 'and'):
            out.add(w)
    return out


def _words_prefix(s):
    """مثل _words لكن مقارنة البادئات مسموحة (بالليل→ليل يطابق ليلي)."""
    out = set()
    for w in re.findall(r'[\u0621-\u064a]{3,}', _norm(s)):
        w2 = re.sub(r'^(?:و|ف|ب|ك)?(?:ال|لل)', '', w)
        w2 = w2.lstrip('و')
        if len(w2) >= 3:
            out.add(w2)
    return out


def _overlap_prefix(a, b):
    """تقاطع ببادئة: (ليل، ليلي) يعتبر تطابقاً."""
    hit = 0
    for x in a:
        for y in b:
            if x == y or (len(x) >= 3 and len(y) >= 3 and (x.startswith(y) or y.startswith(x))):
                hit += 1
                break
    return hit


AR_NUM_WORDS = {'نصف': 0.5, 'واحد': 1, 'واحدة': 1, 'يوم': 1, 'يومين': 2, 'اتنين': 2, 'اثنين': 2,
                'تلاتة': 3, 'ثلاثة': 3, 'اربعة': 4, 'أربعة': 4, 'خمسة': 5, 'ستة': 6, 'سبعة': 7,
                'ثمانية': 8, 'تسعة': 9, 'عشرة': 10}


def _num(text):
    """أول رقم في النص (رقمي أو لفظي شائع)."""
    m = re.search(r'(\d+(?:[.,]\d+)?)', text or '')
    if m:
        return float(m.group(1).replace(',', '.'))
    for w, v in AR_NUM_WORDS.items():
        if re.search(r'\b%s\b' % w, _norm(text)):
            return float(v)
    return None


# ---------------------------------------------------------------- تصنيف الحقول

def _classify_field(fk):
    """تصنيف عام لاسم الحقل من الكلمات الدالة داخله (عربي/لاتيني/أي دومين).
    أسماء الحقول snake_case تُستعاد بعد ما _norm يحول _ لمسافة، وإلا انكسر
    كل الحقول اللاتينية (service_name → 'service name' → other)."""
    f = _norm(fk).replace(' ', '_')
    if 'فرع' in f or 'branch' in f:
        return 'branch'
    if 'سلبيه' in f or 'unavailable' in f or ('غير' in f and 'متاح' in f):
        return 'unav'
    if 'متاح' in f or 'available' in f:
        return 'avail'
    if 'عربون' in f or 'تأمين' in f or 'تامين' in f or f in ('deposit', 'security_deposit'):
        return 'deposit'
    if 'خصم' in f or 'discount' in f:
        return 'discount'
    if 'سعر' in f or 'price' in f or 'cost' in f or 'اجور' in f:
        return 'price'
    if 'رسوم' in f or 'fee' in f:
        return 'fee'
    if 'عمله' in f or 'currency' in f:
        return 'currency'
    if 'warranty' in f or 'ضمان' in f:
        return 'warranty'
    if 'مدة' in f or 'مده' in f or 'duration' in f or 'days' in f or 'يوم' == f:
        return 'duration'
    if 'مدينه' in f or 'city' in f:
        return 'city'
    if 'ساعات' in f or 'hours' in f:
        return 'hours'
    if 'تليفون' in f or 'هاتف' in f or 'phone' in f:
        return 'phone'
    if 'وحدة' in f or 'وحده' in f or 'unit' in f:
        return 'unit'
    if 'اسم' in f or f in ('name', 'title', 'product', 'item', 'service') or f.endswith('_name') or f.endswith('service'):
        return 'name'
    if 'ملاحظ' in f or 'notes' in f or 'note' in f or 'desc' in f or 'tags' in f:
        return 'notes'
    return 'other'


# ---------------------------------------------------------------- تحليل السجلات

class KBRecords(object):
    """سجلات مهيكلة من chunks الفهرس + مسح نصي عام لكل chunk."""

    def __init__(self, chunks):
        self.records = []
        self.texts = []              # (نص، مصدر) لكل chunk — للمسح النصي العام
        self.extra_exclusions = []   # (نص، مصدر) للاستثناءات الصريحة
        self.currency = 'جنيه'       # يُكتشف من النصوص
        self._parse(chunks)

    def _parse(self, chunks):
        for c in chunks:
            text = c.get('text', '')
            self.texts.append((text, c.get('source', '')))
            fields = {}
            for line in text.splitlines():
                m = re.match(r'^\s*([\w_\u0621-\u064a%]+)\s*:\s*(.+)$', line.strip())
                if m and not line.strip().startswith('#'):
                    fields[m.group(1).strip()] = m.group(2).strip()
            # عملة النظام من نصوص المستندات نفسها
            if 'currency' in fields or any('عملة' in k for k in fields):
                cur = next((v for k, v in fields.items() if 'currency' in _norm(k) or 'عملة' in _norm(k)), '')
                cu = cur.upper()
                if 'EGP' in cu or 'جنيه' in cur:
                    self.currency = 'جنيه مصري'
                elif 'SAR' in cu or 'ريال' in cur:
                    self.currency = 'ريال سعودي'
                elif 'USD' in cu or 'دولار' in cur:
                    self.currency = 'دولار'
            if 'الأسعار' in text and 'بالجنيه المصري' in text:
                self.currency = 'جنيه مصري'
            # التقاط استثناءات صريحة من أي chunk (كل صيغ "لا ينطبق/مش بينطبق")
            for m in re.finditer(r'[^.\n]*(?:مستثن[ىا]|لا يسري|لا ينطبق|مش بينطبق|لا يشمل)[^.\n]*', text):
                seg = m.group(0).strip()
                if len(seg) > 15:
                    self.extra_exclusions.append((seg, c.get('source', '')))
            if len(fields) >= 2:
                rec = {
                    'source': c.get('source', ''),
                    'heading': c.get('heading', ''),
                    'fields': fields,
                    'resolved': {},
                    'text': text,
                    'words': _words(c.get('heading', '') + ' ' + text[:300]),
                }
                for fk, fv in fields.items():
                    kind = _classify_field(fk)
                    if kind in rec['resolved']:
                        # دمج القيم النصية من نفس النوع (فرع: B2 + فرع مدينة نصر؛
                        # سلبي: P06 + ستيكر فينيل قص + سبب) — الأول رقمي/معرف فيبقى
                        if kind in ('branch', 'unav', 'avail', 'notes'):
                            old = rec['resolved'][kind][1]
                            if fv and fv not in old:
                                rec['resolved'][kind] = (rec['resolved'][kind][0], old + ' / ' + fv)
                    else:
                        rec['resolved'][kind] = (fk, fv)
                self.records.append(rec)

    # ---- وصول عام بالتصنيف ----
    def get(self, rec, kind):
        """قيمة نصية من حقل مصنَّف (مع البحث التسامحي في كل الحقول)."""
        if not rec:
            return None
        if kind in rec['resolved']:
            return rec['resolved'][kind][1]
        for fk, fv in rec['fields'].items():
            if _classify_field(fk) == kind:
                rec['resolved'].setdefault(kind, (fk, fv))
                return fv
        return None

    def num(self, rec, kind):
        """قيمة رقمية من حقل مصنَّف."""
        if not rec:
            return None
        v = self.get(rec, kind)
        if v is None:
            return None
        m = re.search(r'(\d+(?:[.,]\d+)?)', str(v))
        return float(m.group(1).replace(',', '.')) if m else None

    def find(self, name_text, exclude_branches=False):
        """السجل الأقرب لاسم مذكور في السؤال — تقاطع مع كلمات اسم العنصر نفسه.
        سجل الفرع: اسمه هو قيمة حقل الفرع (تصنيف branch يسبق name)."""
        qw = _words(name_text)
        if not qw:
            return None
        best, best_score = None, 0
        for r in self.records:
            if exclude_branches and self.get(r, 'branch'):
                continue
            name_val = self.get(r, 'name') or self.get(r, 'branch') or ''
            name_w = _words(name_val)
            name_hit = len(qw & name_w) if name_w else 0
            if name_hit < 1:
                continue
            score = name_hit * 10 + len(qw & r['words'])
            if score > best_score:
                best, best_score = r, score
        return best if best_score >= 10 else None

    def find_service(self, name_text):
        return self.find(name_text, exclude_branches=True)

    def find_exclusion(self, entity_text, exclude_record=None):
        """استثناء صريح يغطي الكيان: يُطابق **عبارة المستثنى** — حتى 6 كلمات
        بعد مثير النفي (مستثنى: X / لا يشمل X) أو قبله (X مستثناة) —
        فلا يحظر سطر ملاحظات الخدمة خدمتها ذاتها لأنه ذكر استثناءً لغيرها."""
        ew = _words(entity_text)
        if not ew:
            return None
        trigger = re.compile(r'(?:مستثن[ىا]|لا يسري|لا ينطبق|مش بينطبق|لا يشمل|لا تدخل)')
        seen = set()
        for txt, src in self.texts:
            for m in trigger.finditer(txt):
                start = txt.rfind('\n', 0, m.start()) + 1
                end = txt.find('\n', m.end())
                end = len(txt) if end < 0 else end
                line = txt[start:end].strip()
                key = (src, line[:80])
                if key in seen:
                    continue
                seen.add(key)
                # عبارة المستثنى: أول 8 كلمات مرتبة بعد المثير، وإلا آخر 8 قبله
                # (_words_seq ترتيب النص؛ ترتيب set عشوائي بين العمليات)
                pre_words = _words_seq(line[:m.start()])[-8:]
                post_words = _words_seq(line[m.end():])[:8]
                hit = max(_pair_match(ew, post_words), _pair_match(ew, pre_words))
                if hit >= 2:
                    return line, src
        return None


# ---------------------------------------------------------------- تقسيم المركبات

_Q_SPLIT = re.compile(r'(?<=[؟?])\s*|،?\s+(?=و(?:ه[أاي]|تاخد|هاخد|عايز|هل|بكام|كام|العربون|الإجمالي|الاجمالي|هل))')


def split_composite(query):
    parts = [p.strip() for p in _Q_SPLIT.split(query) if p and p.strip()]
    return parts if len(parts) > 1 else [query.strip()]


# ---------------------------------------------------------------- المفاتيح الدلالية

_RUSH_KEYS = ('استعجال', 'مستعجل', 'عاجل', 'rush')
_DISCOUNT_KEYS = ('خصم', 'عرض', 'تخفيض', 'discount')
_PRICE_KEYS = ('بكام', 'بكم', 'كام', 'سعر', 'اسعار', 'تكلف', 'ثمن', 'هيدفع', 'هدفع', 'اجمالي', 'إجمالي',
               'ارخص', 'اغلي', 'اغلى', 'افضل', 'الفرق', 'مقارن', 'ايهما', 'أيهما', 'بالظبط')
_DEPOSIT_KEYS = ('عربون', 'مقدم', 'تأمين', 'deposit')
_BRANCH_KEYS = ('فرع', 'أعمله', 'اعمله', 'تقدر', 'ينفذ', 'متاح', 'تغطي', 'بيعمل', 'بيقدم',
                'اقدر', 'أقدر', 'بقدر', 'امكانيه', 'إمكانية', 'بالليل', 'ليلي', 'ليل',
                'يوجد', 'هل يوجد', 'مستوحى')
_REFUND_CONTEXT_KEYS = ('الغيت', 'الغاء', 'إلغاء', 'استرداد', 'يرجع', 'ترجع', 'برجع',
                        'مرجعش', 'براجع', 'ميرجع', 'تراجع', 'لغيت')
_WEATHER_KEYS = ('طقس', 'مطر', 'عاصف', 'شتاء_طقسي')
_AREA_RE = re.compile(r'(\d+(?:[.,]\d+)?)\s*(?:متر\s*مربع|م2|م²|متر مربع)')


_NORM_KEY_CACHE = {}


def _has(query, keys):
    """مطابقة المفاتيح على نص مطبَّع — والمفاتيح نفسها تُطبَّع مرة واحدة
    (تأمين/إلغاء/مستعجل... قد تكتب بهمزات وتفشل على النص المطبَّع)."""
    n = _norm(query)
    for k in keys:
        nk = _NORM_KEY_CACHE.get(k)
        if nk is None:
            nk = _norm(k)
            _NORM_KEY_CACHE[k] = nk
        if nk in n:
            return True
    return False


def _is_refund_context(*texts):
    return any(_has(t, _REFUND_CONTEXT_KEYS) for t in texts if t)


def _is_weather_context(*texts):
    return any(_has(t, _WEATHER_KEYS) for t in texts if t)


def _lev2(a, b):
    """مسافة ليفنشتاين مصغرة: True لو المسافة ≤2 لكلمتين ≥4."""
    if abs(len(a) - len(b)) > 2:
        return False
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1] <= 2


def _pair_match(a_words, b_words):
    """عدد الثنائيات المتطابقة بتسامح صرفي: تطابق تام، أو تشابه قوي
    (بادئة ≥2 + ليفنشتاين ≤2 لكلمتين ≥4) — يلتقط قضية/قضايا،
    علامة/علامات، جنائية/الجنائية دون فتح باب التطابق الفضفاض."""
    count = 0
    for x in a_words:
        for y in b_words:
            if x == y:
                count += 1
                break
            if len(x) >= 4 and len(y) >= 4:
                cp = 0
                for c1, c2 in zip(x, y):
                    if c1 != c2:
                        break
                    cp += 1
                if cp >= 2 and _lev2(x, y):
                    count += 1
                    break
    return count


def _fmt(x):
    return int(x) if float(x) == int(float(x)) else x


# ---------------------------------------------------------------- النوايا

def answer_warranty(records, part):
    """سؤال ضمان (كام شهر؟): قيمة حقل الضمان للخدمة المذكورة."""
    if 'ضمان' not in _norm(part):
        return None
    rec = records.find_service(part)
    if not rec:
        return None
    months = records.num(rec, 'warranty')
    if months is None:
        return None
    name = records.get(rec, 'name') or rec['heading'].split('(')[0].strip()
    return ('ضمان %s هو %s شهر من تاريخ الاستلام.' % (name, _fmt(months)), rec['source'])


def answer_duration(records, part, full_query=''):
    """سؤال مدة التنفيذ (بيخلص في كام يوم؟) — الكيان من الجزء أو السؤال الكامل."""
    if not _has(part, ('يخلص', 'خلاص', 'مدة', 'مده', 'تسليم', 'تنفيذ', 'كام يوم', 'بياخد')):
        return None
    rec = records.find_service(part) or records.find_service(full_query)
    if not rec:
        return None
    days = records.num(rec, 'duration')
    if days is None:
        return None
    name = records.get(rec, 'name') or rec['heading'].split('(')[0].strip()
    dv = records.get(rec, 'duration') or ''
    return ('مدة تنفيذ %s هي %s.' % (name, dv if not str(dv).replace('.','').isdigit() else '%s يوم' % _fmt(days)), rec['source'])


def answer_discount_for_amount(records, part, full_query):
    """'خصم كام على N جنيه؟' بدون خدمة محددة: يقارن المبلغ بعتبات العروض
    المستخرجة من نص العروض نفسه (فوق N جنيه...) — ولو لا ينطبق عرض يقول ذلك."""
    if not _has(part, _DISCOUNT_KEYS):
        return None, None
    if records.find_service(part) or records.find_service(full_query):
        return None, None  # فيه خدمة محددة → مسار الخصم العادي
    amount = None
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*جنيه', part) or re.search(r'(\d+(?:[.,]\d+)?)\s*جنيه', full_query or '')
    if m:
        amount = float(m.group(1).replace(',', '.'))
    if amount is None:
        return None, None
    src = None
    thresholds = []
    for txt, tsrc in records.texts:
        n = _norm(txt)
        if not _has(txt, _DISCOUNT_KEYS):
            continue
        for mm in re.finditer(r'(?:اكتر من|أكثر من|فوق|اعلى|أعلى)\s*(\d+(?:[.,]\d+)?)\s*جنيه', n):
            thresholds.append((float(mm.group(1)), tsrc))
            src = src or tsrc
    if src is None:
        for txt, tsrc in records.texts:
            if _has(txt, _DISCOUNT_KEYS):
                src = tsrc
                break
    if not thresholds:
        return ('مفيش عرض كميات أو خصم آلي على طلب %s %s في العروض الحالية — '
                'الخصومات المعلنة مرتبطة بشروط محددة (حسب ملف العروض%s).'
                % (_fmt(amount), records.currency, (': ' + src) if src else ''), src)
    min_th = min(t for t, _ in thresholds)
    if amount < min_th:
        return ('مفيش خصم آلي على طلب %s %s — خصم الكميات يبدأ من %s %s حسب العروض الحالية (المصدر: %s).'
                % (_fmt(amount), records.currency, _fmt(min_th), records.currency, src), src)
    return ('طلب %s %s يدخل في عرض الخصم المعلن (الحد %s %s) — التفاصيل في ملف العروض (المصدر: %s).'
            % (_fmt(amount), records.currency, _fmt(min_th), records.currency, src), src)


def answer_price(records, part):
    """سعر خدمة/خدمات مذكورة بالاسم — يدعم سؤالاً يسأل عن أكثر من خدمة
    ('X بكام، وY بكام؟'): يجيب عن كل خدمة مذكورة بالترتيب."""
    if not _has(part, _PRICE_KEYS) or _is_refund_context(part):
        return None
    # الكمية المطلوبة: رقم متبوع بكلمة وحدة قطعية في السؤال (100 كارت، 12 متر مربع)
    qty_m = re.search(r'(\d+)\s*(?:كارت|قطعة|قطعه|نسخة|نسخه|صورة|متر مربع|متر|كوب|دفتر|بوكس|علبة|علبه)', part)
    asked_qty = float(qty_m.group(1)) if qty_m else None
    # جمع كل الخدمات المذكورة عبر تقسيم على الفواصل/الواو
    found = []
    bits = re.split(r'\s*[،,]\s*|\s+و(?=[\u0621-\u064a])|\s+أم\s+|\s+ام\s+|\s+ولا\s+', part)
    seen = set()
    for bit in bits:
        rec = records.find_service(bit) or records.find_service(part)
        if not rec:
            continue
        price = records.num(rec, 'price')
        if price is None or rec['heading'] in seen:
            continue
        seen.add(rec['heading'])
        name = records.get(rec, 'name') or rec['heading'].split('(')[0].strip()
        unit = records.get(rec, 'unit') or ''
        unit_part = ''
        if unit:
            unit_part = ' %s' % unit if unit.startswith('لكل') else ' لكل %s' % unit
        fee = records.num(rec, 'fee')
        extra = (' (+رسوم إضافية %s %s حسب الملاحظات)' % (_fmt(fee), records.currency)) if fee and fee > 0 else ''
        # تسعير الكميات مقابل وحدة البيع (عيب «100 كارت بكام؟»):
        # عدد الوحدة يُقرأ من نص الوحدة نفسه (باكيت 500 كارت → 500)
        uc_m = re.search(r'(\d+)\s*(?:كارت|قطعة|قطعه|نسخة|نسخه|صورة|كوب|دفتر|بوكس)', unit or '')
        if asked_qty is not None and uc_m:
            uc = float(uc_m.group(1))
            if asked_qty < uc:
                found.append('السعر الموثق هو %s %s (%s). الكمية %s أقل من وحدة البيع (%s)، '
                             'والتسعير للكميات الأقل غير موثق في المستندات — سأحول سؤالك لموظف يؤكد السعر النهائي.'
                             % (_fmt(price), records.currency, unit, _fmt(asked_qty), unit))
                continue
            packs = asked_qty / uc
            if packs == int(packs):
                total = round(price * packs, 2)
                found.append('سعر %s للـ%s هو %s — إجمالي %s %s.'
                             % (name, unit, _fmt(price), _fmt(total), records.currency))
                continue
            # كمية غير مضاعفة الوحدة: تقدير نسبي معلن + تحويل للتأكيد
            total = round(price * packs, 2)
            found.append('تقدير حسابي: %s ÷ %s × %s = %s %s — ده تقدير من سعر الوحدة، '
                         'والسعر النهائي لكمية غير قياسية محتاج تأكيد من المكتب.'
                         % (_fmt(asked_qty), _fmt(uc), _fmt(price), _fmt(total), records.currency))
            continue
        found.append('سعر %s هو %s %s%s%s' % (name, _fmt(price), records.currency, unit_part, extra))
    if not found:
        return None
    return ('. '.join(found) + '.', found[0] and None) if False else ('. '.join(found) + '.', None)


def answer_currency(records, part, full_query):
    """سؤال عن العملة — الدليل من نصوص المستندات نفسها."""

    def money_hit(text):
        for tok in _norm(text).split():
            t2 = re.sub(r'^(?:و|ف|ب|ك)?(?:ال|لل)', '', tok).lstrip('و')
            if t2 in {'دولار', 'عمله', 'ريال', 'يورو', 'دينار'} or 'دولار' in t2:
                return True
        return False

    if not (money_hit(part) or money_hit(full_query or '')):
        return None, None
    for txt, src in records.texts:
        if re.search(r'الأسعار\s+بالجنيه\s+المصري|الأسعار\s+كلها\s+بالجنيه', _norm(txt)):
            return ('جميع الأسعار معلنة بالجنيه المصري، وهي عملة التسعير الوحيدة في المستندات (المصدر: %s).' % src, src)
    if records.currency != 'جنيه':
        return ('جميع الأسعار معلنة بـ%s وهي عملة التسعير المعتمدة.' % records.currency, None)
    return None, None


def answer_compare(records, part):
    """مقارنة أرخص/الفرق بين عنصرين — يدعم 'ولا/أم' و'وال' و'بين X وY' —
    تنتهي بجملة حسم رقمي صريحة."""
    n = _norm(part)
    if not any(k in n for k in ('ارخص', 'اغلي', 'اغلى', 'افضل', 'الفرق', 'فرق')):
        return None
    left = right = None
    m = re.search(r'(.+?)\s+(?:ولا|ام|أم)\s+(.+)', part)
    if m:
        left, right = m.group(1), m.group(2)
    elif 'بين' in n:
        # 'و' العطف تلتصق بالكلمة التالية (وتغطية/والورنيش) — نقسم عليها
        m2 = re.search(r'بين\s+(.+?)\s+و(?=[\u0621-\u064a])\s*(.+)', part)
        if m2:
            left, right = m2.group(1), m2.group(2)
    elif re.search(r'الفرق|فرق', n):
        bits = re.split(r'\s+وال', part)
        if len(bits) >= 2:
            left, right = bits[0], ' '.join(bits[1:])
    if not left or not right:
        return None
    left = re.sub(r'^[^,،:]*[،,:]\s*', '', left)
    left = re.sub(r'^(?:إيه|ايه|ما)?\s*(?:الفرق|فرق)(?:\s*في\s*\S+)?\s*(?:بين)?\s*', '', left).strip()
    r1 = records.find_service(left) or records.find_service(right)
    r2 = records.find_service(right)
    if not r1 or not r2 or r1 is r2:
        return None
    p1 = records.num(r1, 'price')
    p2 = records.num(r2, 'price')
    if p1 is None or p2 is None:
        return None
    n1 = records.get(r1, 'name') or r1['heading'].split('(')[0].strip()
    n2 = records.get(r2, 'name') or r2['heading'].split('(')[0].strip()
    # وحدة التسعير لكل طرف (باكيت 500 كارت مقابل 1000 نسخة) — عيب 4
    u1 = records.get(r1, 'unit') or ''
    u2 = records.get(r2, 'unit') or ''
    s1 = '%s (%s)' % (n1, u1) if u1 else n1
    s2 = '%s (%s)' % (n2, u2) if u2 else n2
    diff = round(abs(p1 - p2), 2)
    cheaper = s1 if p1 < p2 else s2
    if _has(part, ('الفرق', 'فرق')):
        return ('سعر %s هو %s %s، وسعر %s هو %s %s — الفرق بينهما %s %s.'
                % (s1, _fmt(p1), records.currency, s2, _fmt(p2), records.currency, _fmt(diff), records.currency),
                r1['source'])
    return ('سعر %s هو %s %s، وسعر %s هو %s %s — %s أرخص بمقدار %s %s.'
            % (s1, _fmt(p1), records.currency, s2, _fmt(p2), records.currency, cheaper, _fmt(diff), records.currency),
            r1['source'])


def answer_rush_total(records, part, full=''):
    """إجمالي مع رسوم استعجال — النتيجة أولاً ثم التفصيل.
    النسبة تأتي فقط من حقل استعجال صريح (% أو اسمه يذكر الاستعجال) —
    مبلغ الرسوم الإضافية (جنيه) لا يجوز تفسيره نسبةً أبداً (500 جنيه ≠ 500%)."""
    if not _has(part, _RUSH_KEYS) or _is_refund_context(part, full):
        return None
    rec = records.find_service(part) or records.find_service(full)
    if not rec:
        return None
    pct = None
    # نسبة الاستعجال: فقط من حقل معلن كنسبة (يشمل % أو اسمه يذكر الاستعجال)
    for fk, fv in rec['fields'].items():
        fk_n = _norm(fk)
        if '%' in str(fv) or 'استعجال' in fk_n or 'استعجال' in _norm(fv) or 'rush' in fk_n:
            pv = _num(fv)
            if pv is not None and 0 < pv <= 100:
                pct = pv
                break
    if pct is None or pct <= 0:
        return None, None
    base = _num(part)
    if base is None or not _has(part, ('جنيه', 'ج ')):
        base = records.num(rec, 'price')
    if base is None:
        return None, None
    total = round(base * (1 + pct / 100.0), 2)
    if not total > base:
        return None, None
    name = records.get(rec, 'name') or rec['heading'].split('(')[0].strip()
    fee = round(total - base, 2)
    return ('الإجمالي النهائي %s %s (%s + رسوم الاستعجال %s%% = %s %s).'
            % (_fmt(total), records.currency, _fmt(base), _fmt(pct), _fmt(fee), records.currency),
            rec['source'])


def answer_discount(records, part, full_query):
    """خصم عرض: الاستثناء الصريح يلغي العرض أولاً؛ وإلا حساب يبدأ بالإجمالي النهائي.
    حارس الاسترداد على الجزء نفسه فقط (جزء آخر مركب قد يذكر إلغاءً بلا صلة)."""
    _MEMBER_KEYS = ('عضويه', 'عضوية', 'ينطبق', 'بتدخل', 'تدخل', 'مستثنى', 'مستثني')
    if not (_has(part, _DISCOUNT_KEYS) or _has(part, _MEMBER_KEYS)) or _is_refund_context(part):
        return None, None
    rec = records.find_service(part) or records.find_service(full_query)
    if not rec:
        return None, None
    name = records.get(rec, 'name') or rec['heading'].split('(')[0].strip()
    excl = records.find_exclusion(name or part, exclude_record=rec)
    if excl:
        seg, src = excl
        return ('لا، %s — %s (المصدر: %s).' % (name, seg, src), src)
    pct = records.num(rec, 'discount')
    src = rec['source']
    if not pct:
        for txt, tsrc in records.texts:
            tn = _norm(txt)
            if _overlap_prefix(_words(name), _words(txt)) >= 1 and _has(txt, _DISCOUNT_KEYS):
                m = re.search(r'خصم\s*(\d+(?:\.\d+)?)\s*%', tn)
                if m:
                    pct = float(m.group(1))
                    src = tsrc
                    break
    if not pct or pct <= 0:
        return None, None
    price = records.num(rec, 'price')
    if price is None:
        return None, None
    # الكمية/المساحة من الجزء أو السؤال الكامل
    qty = _num(part)
    if qty is None or not re.search(r'\d', part):
        area = _AREA_RE.search(full_query or '')
        qty = float(area.group(1)) if area else None
    # شرط كمية/مساحة في ملاحظات الخدمة (فوق N قطعة/صورة)
    min_qty = None
    notes = records.get(rec, 'notes') or ''
    mn = re.search(r'(?:فوق|أكثر من|اكتر من)\s*(\d+(?:[.,]\d+)?)', _norm(notes))
    if mn:
        min_qty = float(mn.group(1))
    applicable = True
    if min_qty and qty is not None and qty < min_qty:
        applicable = False
    setup = records.num(rec, 'fee') or 0.0
    base = round(price * (qty if qty and qty > 1 else 1), 2)
    currency = records.currency
    if not applicable:
        total = base + setup
        return ('الإجمالي النهائي %s %s — الخصم %s%% لا ينطبق لأن الكمية %s أقل من الحد المطلوب (%s) '
                'حسب ملاحظات الخدمة (المصدر: %s).'
                % (_fmt(total), currency, _fmt(pct), _fmt(qty) if qty else 'غير محدد', _fmt(min_qty), src), src)
    discount_val = round(base * pct / 100.0, 2)
    after = round(base - discount_val, 2)
    total = after + setup
    setup_note = ''
    if setup > 0 and _has(txt_of_offer(src, records), ('لا يشمل', 'لا تدخل')):
        setup_note = '، ورسوم الإعداد/الإضافية %s %s تُضاف كاملة بدون خصم' % (_fmt(setup), currency)
    elif setup > 0:
        setup_note = '، وأي رسوم إضافية (%s %s) تُضاف كما هي' % (_fmt(setup), currency)
    return ('الإجمالي النهائي %s %s (سعر %s: %s %s × %s − خصم %s%% = %s %s%s).'
            % (_fmt(total), currency, name, _fmt(price), currency,
               _fmt(qty) if qty and qty > 1 else '1', _fmt(pct), _fmt(after), currency, setup_note), src)


def txt_of_offer(src, records):
    for txt, s in records.texts:
        if s == src:
            return txt
    return ''


def answer_arithmetic(records, part, full_query):
    """المحطة الحسابية العامة: عمليات صريحة بأرقام في السؤال (جمع/فرق/نسبة
    على مبلغ) تُحل هنا بدل الموديل — تفوض لـ engine.calc بشرط وجود رابط
    حسابي واضح، وأي غموض يعيد None للمسار اللغوي."""
    from engine import calc
    r = calc.try_calculate(part) or calc.try_calculate(full_query or '')
    if r and r[0]:
        return (r[0], 'calc')
    return None, None


def answer_deposit(records, part, full_query):
    """قيمة العربون: لكل كيان من نص السياسة إن ورد؛ وإلا القاعدة العامة (ثابت/نسبة).
    سؤال "تأمين X بيرجع؟" يُجاب من قاعدة عدم استرداد التأمين قبل أي حارس.
    أسئلة قناة الدفع (حساب بنكي/تحويل) ليست أسئلة قيمة عربون — مسارها قسم
    طرق الدفع في السياسة عبر الـLLM، فتخرج من هذه النية صراحة."""
    # حارس قناة الدفع: السؤال عن وسيلة التحويل/الحساب لا قيمة العربون
    if re.search(r'حساب(?:كم|ك)?\s*(?:ال)?بنكي|رقم\s+(?:الحساب|الحوالة)|أحول|احول|تحويل\s+على', _norm(part) + ' ' + _norm(full_query)):
        return None, None
    # 0.5) مبالغ لا تُسترد نهائياً (تأمين/طوارئ/استعجال) + سياق استرداد:
    # قاعدة عدم الاسترداد الصريحة تسبق كل الحواجز، مع كشف النص المتعارض وترجيحه
    if _has(part, ('تأمين', 'تامين', 'طوارئ', 'طواريا', 'استعجال', 'مستعجل')) and _is_refund_context(part):
        _NONREF = ('تامين', 'استعجال', 'طوارئ', 'طواريا')
        for txt, src in records.texts:
            n = _norm(txt)
            if not any(k in n for k in _NONREF):
                continue
            m = re.search(r'[^.\n]*(?:تامين|استعجال|طوارئ|طواريا)[^.\n]*(?:غير قابل للاسترداد|غير قابله للاسترداد|لا يرد|مش بترجع)[^.\n]*', n)
            if not m:
                continue
            seg = m.group(0).strip()
            answer = 'لا، %s (المصدر: %s).' % (seg, src)
            for t2, s2 in records.texts:
                if s2 == src:
                    continue
                n2 = _norm(t2)
                if not any(k in n2 for k in _NONREF):
                    continue
                for mm in re.finditer(r'[^.\n]*(?:تامين|استعجال|طوارئ|طواريا)[^.\n]*(?<!غير )قابله للاسترداد[^.\n]*', n2):
                    answer += (' ⚠️ يوجد نص آخر (من %s) يذكر قابلية الاسترداد — '
                               'يُرجَّح نص السياسة الصريح أعلاه كونه أخص وأحدث، '
                               'وحوّلت التأكيد النهائي لموظف بشري.' % s2)
                    break
            return (answer, src)
    if not _has(part, _DEPOSIT_KEYS) or _is_refund_context(part, full_query):
        return None, None
    rec = records.find_service(part) or records.find_service(full_query)
    entity_words = _words(records.get(rec, 'name')) if rec else _words(part)
    # 1) سطر سياسة يذكر الكيان + عربون + رقم — الرقم يُقرأ **بعد** كلمة عربون
    # (السطر قد يذكر السعر الكامل أولاً مثل "السعر 8000 والعربون 1000")
    for txt, src in records.texts:
        for line in txt.splitlines():
            ln = _norm(line)
            pos = ln.find('عربون')
            if pos < 0:
                pos = ln.find('تامين')
            if pos < 0:
                continue
            if entity_words and _overlap_prefix(entity_words, _words(line)) >= 1:
                m = re.search(r'(\d+(?:\.\d+)?)\s*جنيه', ln[pos:])
                if m:
                    dep = float(m.group(1))
                    # لو السطر يذكر سعراً كاملاً آخر + العلاقة (يُخصم من) → وضّح العلاقة
                    other = re.search(r'(\d+(?:\.\d+)?)\s*جنيه', ln[:pos])
                    full_price = float(other.group(1)) if other else None
                    rel = ('يُخصم من السعر الكامل وليس مبلغاً منفصلاً'
                           if ('يخصم' in ln or 'بيتخصم' in ln or 'مختلف عن' in ln
                               or 'ثابت' in ln or 'مش نسبه' in ln) else None)
                    if full_price and rel:
                        return ('العربون %s %s يُخصم من السعر الكامل %s %s وليس مبلغاً منفصلاً يُضاف عليه '
                                '(حسب وثيقة السياسة — المصدر: %s).'
                                % (_fmt(dep), records.currency, _fmt(full_price), records.currency, src), src)
                    return ('العربون المطلوب هو %s %s (حسب وثيقة السياسة — المصدر: %s).'
                            % (_fmt(dep), records.currency, src), src)
    # 1.5) تأمين معدات: قاعدة عدم الاسترداد الصريحة (سؤال "تأمينه بيرجع؟")
    if _has(part, ('تأمين', 'تامين')) and _is_refund_context(part):
        for txt, src in records.texts:
            n = _norm(txt)
            if 'تامين' in n and ('غير قابل للاسترداد' in n or 'لا يرد' in n):
                m = re.search(r'[^.\n]*تامين[^.\n]*(?:غير قابل للاسترداد|لا يرد)[^.\n]*', n)
                seg = m.group(0).strip() if m else 'التأمين غير قابل للاسترداد'
                return ('لا، %s (المصدر: %s).' % (seg, src), src)
    # 2) قاعدة عامة: ثابت تحت عتبة / نسبة فوقها
    # 2.5) جملة "باقي الخدمات لا تحتاج عربون" — الخدمة بلا سطر عربون خاص بها
    for txt, tsrc in records.texts:
        n = _norm(txt)
        if ('عربون' in n or 'تامين' in n) and re.search(r'باقي\s*الخدمات[^.\n]*(?:لا تحتاج عربون|لا يحتاج عربون)', n):
            rec2 = records.find_service(part) or records.find_service(full_query)
            if rec2 and records.num(rec2, 'price') is not None:
                name2 = records.get(rec2, 'name') or rec2['heading'].split('(')[0].strip()
                # إن لم يُذكر اسمها في أي سطر عربون مخصص → تنطبق عليها قاعدة الباقي
                has_own = False
                for line in txt.splitlines():
                    ln2 = _norm(line)
                    if ('عربون' in ln2 or 'تامين' in ln2) and \
                            _pair_match(_words(name2), _words(line)) >= 1 and \
                            re.search(r'\d', line):
                        has_own = True
                        break
                if not has_own:
                    return ('%s لا تحتاج عربون — تُدفع بالكامل عند الطلب (حسب وثيقة السياسة — المصدر: %s).'
                            % (name2, tsrc), tsrc)
    fixed = threshold = pct = None
    src = None
    for txt, tsrc in records.texts:
        n = _norm(txt)
        if 'عربون' not in n:
            continue
        m1 = re.search(r'العربون\s*\**\s*(\d+(?:\.\d+)?)\s*جنيه\s*ثابت', n)
        m2 = re.search(r'(\d+(?:\.\d+)?)\s*جنيه\s*فاكثر\s*:?\s*العربون\s*\**\s*(\d+(?:\.\d+)?)\s*%', n)
        if m1:
            fixed = float(m1.group(1))
        if m2:
            threshold, pct = float(m2.group(1)), float(m2.group(2))
        if m1 or m2:
            src = tsrc
    if not fixed:
        return None, None
    total_order = None
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*جنيه', full_query)
    if m:
        total_order = float(m.group(1).replace(',', '.'))
    if rec and total_order is None:
        up = records.num(rec, 'price')
        qty = _num(part)
        if up:
            total_order = round(up * (qty if qty and qty > 1 else 1), 2)
    if total_order is None:
        return ('العربون %s %s لأي طلب قيمته أقل من %s %s، و%s%% من قيمة الطلب للطلبات من %s %s فأكثر.'
                % (_fmt(fixed), records.currency, _fmt(threshold) if threshold else '—', records.currency,
                   _fmt(pct) if pct else '—', _fmt(threshold) if threshold else '—', records.currency), src)
    if threshold and pct and total_order >= threshold:
        dep = round(total_order * pct / 100.0, 2)
        return ('قيمة الطلب %s %s (من %s %s فأكثر) فالعربون %s%% = %s %s.'
                % (_fmt(total_order), records.currency, _fmt(threshold), records.currency, _fmt(pct), _fmt(dep), records.currency), src)
    return ('قيمة الطلب أقل من %s %s فالعربون ثابت %s %s.'
            % (_fmt(threshold) if threshold else 3000, records.currency, _fmt(fixed), records.currency), src)


def answer_cancellation_window(records, part, full_query):
    """نافذة الإلغاء بقاعدتين (قبل N أيام / خلال N أيام): يربط رقم السؤال
    بالقاعدة الصحيحة رقمياً ويصرّح بالمقارنة — ويستبعد سياق الطقس."""
    if _is_weather_context(part, full_query):
        return None, None
    if not _is_refund_context(part) and not _has(part, ('قبل', 'خلال')):
        return None, None
    days = None
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:يوم|أيام|ايام)', part) or \
        re.search(r'(\d+(?:[.,]\d+)?)\s*(?:يوم|أيام|ايام)', full_query or '')
    if m:
        days = float(m.group(1))
    else:
        for w in ('يومين', 'اتنين', 'يوم واحد'):
            if w in _norm(part):
                days = 2.0 if 'يومين' in w or 'اتنين' in w else 1.0
    # أيام أو ساعات — كلاهما صالح (يُطبَّع لساعات لاحقاً)
    if 'قبل' not in _norm(part) and 'لغيت' not in _norm(part) and 'الغيت' not in _norm(part) and 'الغاء' not in _norm(part):
        return None, None
    # جمع القواعد الزمنية من نص السياسة — بوحدات مطبعة (أيام/ساعات) تُحوَّل لساعات
    before_rule = during_rule = None
    src = None
    _HOURS = {'يوم': 24.0, 'ايام': 24.0, 'ساعه': 1.0, 'ساعات': 1.0}
    _HOURS.setdefault('يوم', 24.0)
    for txt, tsrc in records.texts:
        n = _norm(txt)
        if 'الغاء' not in n and 'الغيت' not in n and 'استرداد' not in n:
            continue
        for pat, slot in ((r'الغاء\s*قبل\s*(\d+(?:\.\d+)?)\s*(يوم|ايام|ساعه|ساعات)\s*او\s*اكثر[^:\-\n]*[:\-]?\s*([^.\n]*)', 'before'),
                          (r'الغاء\s*خلال\s*(\d+(?:\.\d+)?)\s*(يوم|ايام|ساعه|ساعات)\s*او\s*اقل[^:\-\n]*[:\-]?\s*([^.\n]*)', 'during')):
            for mm in re.finditer(pat, n):
                val = float(mm.group(1)) * _HOURS.get(mm.group(2), 24.0)
                rule = (val, mm.group(3).strip())
                if slot == 'before':
                    before_rule = rule
                else:
                    during_rule = rule
                src = tsrc
    # صيغة "مهلة الإلغاء N وحدة" (نافذة واحدة: داخلها يُرد، بعدها لا) —
    # ومن جدول حالات الإلغاء نصّا النتيجتين
    window_rule = None
    for txt, tsrc in records.texts:
        n = _norm(txt)
        mw = re.search(r'(?:مهله|مدة|مده)\s*الالغاء\s*\**\s*(\d+(?:\.\d+)?)\s*(يوم|ايام|ساعه|ساعات)', n)
        if mw:
            val = float(mw.group(1)) * _HOURS.get(mw.group(2), 24.0)
            allow_out, deny_out = 'يُرد العربون بالكامل', 'العربون غير قابل للاسترداد'
            # نتائج من جدول حالات الإلغاء إن وُجد
            for line in txt.splitlines():
                ln2 = _norm(line)
                if '|' not in ln2:
                    continue
                if 'يرد كاملا' in ln2 or 'يسترد بالكامل' in ln2:
                    allow_out = ln2.split('|')[1].strip(' *') if len(ln2.split('|')) > 1 else allow_out
                if 'لا يرد' in ln2 or 'غير قابل للاسترداد' in ln2:
                    deny_out = ln2.split('|')[1].strip(' *') if len(ln2.split('|')) > 1 else deny_out
            window_rule = (val, allow_out, deny_out)
            src = tsrc
            break
    if not before_rule and not during_rule and not window_rule:
        return None, None
    # رقم السؤال: أيام أو ساعات → ساعات للمقارنة الموحدة
    days = None
    m = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:يوم|أيام|ايام)', part) or \
        re.search(r'(\d+(?:[.,]\d+)?)\s*(?:يوم|أيام|ايام)', full_query or '')
    hours = None
    mh = re.search(r'(\d+(?:[.,]\d+)?)\s*(?:ساعة|ساعات|ساعه)', part)
    if mh:
        hours = float(mh.group(1))
    if m:
        days = float(m.group(1))
    elif 'يومين' in _norm(part) or 'يوم واحد' in _norm(part):
        days = 2.0 if 'يومين' in _norm(part) else 1.0
    user_hours = (days * 24.0) if days is not None else hours
    if user_hours is None:
        return None, None
    # العرض بوحدة السياسة: أقل من 72 ساعة تُعرض بالساعات (48 ساعة لا '2 يوم')
    fmt_h = lambda h: ('%s ساعة' % _fmt(h)) if h < 72 and (h % 24 == 0 or h < 24) else (('٪s يوم' % _fmt(h / 24.0)).replace('٪','%') if h >= 24 else ('%s ساعة' % _fmt(h)))
    # العربون الثابت إن وُجد (ثابت قبل/بعد الرقم) — عيب 2: الرقم أولاً
    fixed = None
    for txt, tsrc in records.texts:
        n = _norm(txt)
        if 'عربون' not in n and 'تامين' not in n:
            continue
        for mm in (re.search(r'عربون[^.\n]{0,60}?ثابت\s*(\d+(?:\.\d+)?)\s*جنيه', n),
                   re.search(r'عربون[^.\n]{0,60}?(\d+(?:\.\d+)?)\s*جنيه\s*ثابت', n),
                   re.search(r'العربون[^.\n]{0,60}?مبلغ\s*ثابت\s*(\d+(?:\.\d+)?)\s*جنيه', n)):
            if mm:
                fixed = float(mm.group(1))
                break
        if fixed is not None:
            break

    if window_rule and src:
        val, allow_out, deny_out = window_rule
        if user_hours <= val:
            fixed_part = ('%s جنيه' % _fmt(fixed)) if fixed is not None else 'العربون'
            return ('هيُرد %s كاملاً — الإلغاء داخل مهلة الـ%s من الدفع (%s ≤ %s)، بشرط إن الطباعة ما بدأتش والبروفة ما اتعتمدتش. (المصدر: %s)'
                    % (fixed_part, fmt_h(val), fmt_h(user_hours), fmt_h(val), src), src)
        return ('لأ، المهلة %s وانتهت — العربون%s مش بيتسترد. (المصدر: %s)'
                % (fmt_h(val), (' %s' % _fmt(fixed)) if fixed is not None else '', src), src)

    if before_rule and user_hours >= before_rule[0]:
        outcome = before_rule[1] or 'يُسترد العربون بالكامل'
        return ('الإلغاء قبل %s من الموعد (وهو حالتك: قبل %s ≥ %s) — النتيجة: %s. (المصدر: %s)'
                % (fmt_h(before_rule[0]), fmt_h(user_hours), fmt_h(before_rule[0]),
                   outcome.rstrip('.,'), src), src)
    if during_rule and user_hours <= during_rule[0]:
        outcome = during_rule[1] or 'العربون غير قابل للاسترداد نهائياً'
        return ('الإلغاء خلال %s أو أقل من الموعد (وهو حالتك: %s ≤ %s) — النتيجة: %s. (المصدر: %s)'
                % (fmt_h(during_rule[0]), fmt_h(user_hours), fmt_h(during_rule[0]),
                   outcome.rstrip('.,'), src), src)
    return None, None


def answer_branch(records, part, full_query):
    """توفر بالفرع — أول جملة حسم (لا/نعم) ثم السبب. يدعم حظر مباشر، بسقف،
    ونعمَ متاح، ومطابقة بادئة للبند السلبي (بالليل→ليلي)."""
    if not _has(part, _BRANCH_KEYS):
        return None, None
    branch_rec = None
    for r in records.records:
        bn = records.get(r, 'branch')
        if not bn:
            continue
        bw = _words(bn)
        for text in (part, full_query):
            if bw and len(bw & _words(text)) >= 2:
                branch_rec = r
                break
        if branch_rec:
            break
    if not branch_rec:
        return None, None
    branch_name = records.get(branch_rec, 'branch') or branch_rec['heading']
    # الخدمة المطلوبة تُبحث في الجزء نفسه فقط: في الأسئلة المركبة كيان الجزء
    # المستقل (أصور بالليل؟) لا يجوز استعارة كيان جزء آخر (باقة الفرح)
    target = records.find_service(part)
    target_name = records.get(target, 'name') if target else None
    area = _AREA_RE.search(part) or _AREA_RE.search(full_query)
    asked_area = float(area.group(1)) if area else None
    unav = records.get(branch_rec, 'unav') or ''
    # حظر مرتبط بكلمات السؤال نفسها حتى مع وجود هدف كتالوجي (بالليل→ليلي)
    qw = _words_prefix(part) | _words_prefix(full_query or '')
    if unav and target_name:
        tw = _words(target_name)
        for seg in re.split(r'\s*/\s*', unav):
            sw = _words(seg)
            swp = _words_prefix(seg)
            # (أ) الخدمة مذكورة في البند السلبي — بمطابقة صرفية متسامحة
            # (قضية/قضايا، علامة/علامات) لا حرفياً فقط
            if _pair_match(tw, sw) >= 2:
                cap_m = re.search(r'اكبر\s*من\s*(\d+(?:[.,]\d+)?)\s*(?:م2|م²|متر|م\b)', _norm(seg))
                cap = float(cap_m.group(1)) if cap_m else None
                if cap is not None and asked_area is not None:
                    if asked_area > cap:
                        return ('لا، %s ينفذ في %s لحد %s متر مربع في الطلب الواحد فقط، والمطلوب %s متر مربع — '
                                'ممكن تنفيذه في فرع آخر (المصدر: %s).'
                                % (target_name, branch_name, _fmt(cap), _fmt(asked_area), branch_rec['source']),
                                branch_rec['source'])
                    return ('نعم، %s متاحة في %s لحد %s متر مربع في الطلب الواحد، والمطلوب %s متر مربع. (المصدر: %s)'
                            % (target_name, branch_name, _fmt(cap), _fmt(asked_area), branch_rec['source']),
                            branch_rec['source'])
                reason = records.get(branch_rec, 'notes') or unav
                return ('لا، %s غير متاحة في %s. السبب: %s (المصدر: %s).'
                        % (target_name, branch_name, reason or unav, branch_rec['source']), branch_rec['source'])
            # (ب) كلمات الجزء نفسها تصادم البند السلبي (أصور بالليل ↔ الاستوديو الليلي)
            # — فقط حين لا توجد خدمة كتالوجية مطابقة (وإلا كان حظراً كاذباً)
            if 'غير متاح' in _norm(seg) and target_name is None:
                pwp = _words_prefix(part)
                if _overlap_prefix(pwp, swp) >= 1:
                    reason = records.get(branch_rec, 'notes') or unav
                    return ('لا، غير متاح في %s. السبب: %s (المصدر: %s).'
                            % (branch_name, reason or unav, branch_rec['source']), branch_rec['source'])
    # حظر مرتبط بكلمات السؤال نفسها (خدمة غير مسجلة بالكتالوج مثل الاستوديو الليلي)
    if unav and not target_name:
        qw = _words_prefix(part)
        for seg in re.split(r'\s*/\s*', unav):
            sw = _words_prefix(seg)
            if _overlap_prefix(qw, sw) >= 1:
                reason = records.get(branch_rec, 'notes') or unav
                return ('لا، غير متاح في %s. السبب: %s (المصدر: %s).'
                        % (branch_name, reason or unav, branch_rec['source']), branch_rec['source'])
    # حسم إيجابي: الخدمة مسجلة بالكتالوج ولم تُذكر في سلبيات هذا الفرع
    # (الذُكر المانع = مطابقة صرفية بكلمتين — "تصوير" المشتركة ليست مانعاً)
    if target_name:
        if _pair_match(_words(target_name), _words(unav)) < 2:
            return ('نعم، %s متاحة في %s حسب ملف الفروع — لم تُذكر ضمن الخدمات غير المتاحة هناك. (المصدر: %s)'
                    % (target_name, branch_name, branch_rec['source']), branch_rec['source'])
    return None, None


# ---------------------------------------------------------------- الواجهة

def handle(query, index_chunks):
    """المدخل العام: سؤال + كل chunks الفهرس → {'forced_answer', 'parts', 'resolved_all'}"""
    if not _has(query, _PRICE_KEYS + _DISCOUNT_KEYS + _DEPOSIT_KEYS + _BRANCH_KEYS):
        return {'forced_answer': None, 'parts': [], 'resolved_all': False}

    records = KBRecords(index_chunks)
    parts = split_composite(query)
    answers = []
    resolved = 0
    for part in parts:
        ans = None
        for fn in (lambda p: answer_cancellation_window(records, p, query),
                   lambda p: answer_compare(records, p),
                   lambda p: answer_rush_total(records, p, query),
                   lambda p: answer_currency(records, p, query),
                   lambda p: answer_discount(records, p, query),
                   lambda p: answer_discount_for_amount(records, p, query),
                   lambda p: answer_arithmetic(records, p, query),
                   lambda p: answer_deposit(records, p, query),
                   lambda p: answer_warranty(records, p),
                   lambda p: answer_duration(records, p, query),
                   lambda p: answer_branch(records, p, query),
                   lambda p: answer_price(records, p)):
            try:
                r = fn(part)
            except Exception:
                r = None
            if isinstance(r, tuple) and r[0]:
                ans = r
                break
            if isinstance(r, str):
                ans = (r, None)
                break
        if ans:
            resolved += 1
            answers.append(ans[0])
        else:
            answers.append(None)

    if resolved == 0:
        return {'forced_answer': None, 'parts': parts, 'resolved_all': False}

    out_lines = []
    if len(parts) == 1:
        forced = answers[0]
    else:
        idx = 1
        for part, ans in zip(parts, answers):
            if ans:
                out_lines.append('%d) %s' % (idx, ans))
                idx += 1
            else:
                out_lines.append('• بخصوص "%s": هذه النقطة سأحولها لموظف الدعم لتأكيدها لك.' % part[:60])
        forced = '\n'.join(out_lines)
    return {'forced_answer': forced, 'parts': parts, 'resolved_all': resolved == len(parts)}
