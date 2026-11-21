# -*- coding: utf-8 -*-
"""
train.py — "تدريب" النظام على ملفاتك الخاصة (بيغذي الـ RAG الموجود).

مدعوم: PDF | Excel (.xlsx) | JSON | CSV | MD | TXT
الملف بيتحول لأقسام في data/knowledge_base/train_<اسم-الملف>.md
وبعدها بيحاول يعيد الفهرسة أوتوماتيك لو السيرفر شغال.

أمثلة:
    python train.py faq.pdf products.xlsx data.json notes.txt
    python train.py ملفات_الشركه           (فولدر كامل)
    python train.py f.xlsx --api 9090      (البورت لو مغيره)

ملحوظة PDF: لو ملفك ممسوح ضوئياً (صور) النص مش هيتستخرج —
ولو فيه مكتبة pypdf أو pdfplumber متسطبة هيستخدمها لجودة أفضل.
"""
import csv
import json
import os
import re
import sys
import urllib.request
import zipfile
import zlib
import xml.etree.ElementTree as ET

BASE_DIR = os.path.dirname(os.path.abspath(__file__))


def get_tenant_id():
    return os.environ.get('TENANT_ID', 'default').strip() or 'default'


def get_kb_dir(tenant=None):
    tenant = tenant or get_tenant_id()
    tenant_dir = os.path.join(BASE_DIR, 'data', 'tenants', tenant)
    new_kb_dir = os.path.join(tenant_dir, 'knowledge_base')
    os.makedirs(new_kb_dir, exist_ok=True)
    old_kb_dir = os.path.join(BASE_DIR, 'data', 'knowledge_base')
    if tenant == 'default' and os.path.exists(old_kb_dir) and os.path.isdir(old_kb_dir) and old_kb_dir != new_kb_dir:
        for item in os.listdir(old_kb_dir):
            s = os.path.join(old_kb_dir, item)
            d = os.path.join(new_kb_dir, item)
            if not os.path.exists(d):
                try:
                    os.replace(s, d)
                except OSError:
                    pass
        try:
            if not os.listdir(old_kb_dir):
                os.rmdir(old_kb_dir)
            print('📦 [Migration] تم نقل ملفات المعرفة من %s إلى %s' % (old_kb_dir, new_kb_dir))
        except OSError:
            pass
    return new_kb_dir


SUPPORTED = ('.pdf', '.xlsx', '.json', '.csv', '.md', '.txt', '.docx')

# ---------------------------------------------------------------- أدوات عامة


def _clean_name(name):
    name = os.path.splitext(os.path.basename(name))[0]
    name = re.sub(r'[^\w\u0600-\u06FF]+', '_', name).strip('_')
    return name[:40] or 'ملف'


def write_sections(source_path, sections, out_dir=None):
    kb_dir = out_dir if out_dir else get_kb_dir()
    out_name = 'train_%s.md' % _clean_name(source_path)
    out_path = os.path.join(kb_dir, out_name)
    os.makedirs(kb_dir, exist_ok=True)
    with open(out_path, 'w', encoding='utf-8') as f:
        f.write(u'# مستورد من: %s\n\n' % os.path.basename(source_path))
        for s in sections:
            heading = (s['heading'] or u'قسم').strip().replace('\n', ' ')[:120]
            f.write(u'## %s\n%s\n\n' % (heading, s['text'].strip()))
    return out_path, len(sections)


# ---------------------------------------------------------------- MD / TXT
# ملف MD فيه عناوين ## بيتنسخ زي ما هو (المحرك بيقسمه لوحده)،
# وملف TXT من غير عناوين بيتقسم لفقرات كل واحدة قسم مستقل.

# ---------------------------------------------------------------- JSON

_Q_KEYS = ('question', 'q', 'سؤال', 'title', 'عنوان', 'name', 'issue')
_A_SKIP = ('question', 'q', 'سؤال', 'title', 'عنوان')


def _scalar(v):
    if isinstance(v, str):
        return v.strip()
    if v is None or isinstance(v, bool):
        return ''
    if isinstance(v, (int, float)):
        return str(v)
    return ''


def json_to_sections(data):
    """أفضل شكل: قائمة {question, answer} — لكن أي JSON منظمتتعمل منه أقسام.
    القوائم المتداخلة داخل dict (مثل "faq": [{question,answer},...]) تُمشي
    عليها كعناصر مستقلة — القديم كان يَسقطها صامتاً (bug سقط به محتوى FAQ كامل)."""
    sections = []

    def walk(node):
        if isinstance(node, list):
            for item in node:
                walk(item)
        elif isinstance(node, dict):
            heading = ''
            for k in _Q_KEYS:
                if _scalar(node.get(k)):
                    heading = _scalar(node[k])
                    break
            lines = []
            for k, v in node.items():
                if isinstance(v, dict):
                    for k2, v2 in v.items():
                        if _scalar(v2):
                            lines.append(u'%s — %s: %s' % (k, k2, _scalar(v2)))
                elif isinstance(v, list):
                    # عناصر قائمة هيكلية (dict أو نصوص طويلة) = أقسام مستقلة، لا سطر واحد
                    for x in v:
                        if isinstance(x, dict):
                            walk(x)
                        elif _scalar(x) and len(_scalar(x)) > 40:
                            sections.append({'heading': _scalar(x)[:60], 'text': _scalar(x)})
                        elif _scalar(x):
                            lines.append(u'%s: %s' % (k, _scalar(x)))
                elif _scalar(v) and _scalar(v) != heading:
                    k_clean = str(k).lower().strip()
                    if k_clean in ('answer', 'a', 'إجابة', 'اجابة', 'الاجابة', 'الإجابة', 'رد', 'الرد', 'body', 'text', 'content', 'value', 'desc', 'description'):
                        lines.append(_scalar(v))
                    else:
                        lines.append(u'%s: %s' % (k, _scalar(v)))
            text = u'\n'.join(lines)
            if not text:
                text = heading or u'قسم'
            # تخطي الأقسام الفارغة الدلالياً (لا سؤالاً ولا إجابة)
            if text.strip() not in ('قسم', 'section', '') or heading:
                sections.append({'heading': heading or (lines[0][:60] if lines else u'قسم'),
                                 'text': text})
        else:
            s = _scalar(node)
            if s:
                sections.append({'heading': s[:60], 'text': s})

    walk(data)
    return sections


# ---------------------------------------------------------------- CSV / Excel

def _col_idx(letters):
    idx = 0
    for ch in letters:
        idx = idx * 26 + (ord(ch) - 64)
    return idx - 1


def csv_rows(path):
    rows = []
    with open(path, encoding='utf-8-sig', errors='replace', newline='') as f:
        for i, row in enumerate(csv.reader(f)):
            cells = {j: (c or '').strip() for j, c in enumerate(row)}
            if any(cells.values()):
                rows.append(cells)
    return rows


def _local(tag):
    return tag.rsplit('}', 1)[-1]


def xlsx_rows(path):
    """قراءة .xlsx بنفسنا (هو مجرد ZIP فيه XML) — من غير openpyxl."""
    z = zipfile.ZipFile(path)
    names = z.namelist()
    shared = []
    if 'xl/sharedStrings.xml' in names:
        root = ET.fromstring(z.read('xl/sharedStrings.xml'))
        for si in root.iter():
            if _local(si.tag) == 'si':
                shared.append(''.join((t.text or '') for t in si.iter()
                                      if _local(t.tag) == 't'))
    sheet_names = sorted(n for n in names
                         if re.match(r'xl/worksheets/sheet\d+\.xml$', n))
    all_rows = []
    for sh in sheet_names:
        root = ET.fromstring(z.read(sh))
        rows = []
        for row_el in root.iter():
            if _local(row_el.tag) != 'row':
                continue
            cells = {}
            for c in row_el:
                if _local(c.tag) != 'c':
                    continue
                m = re.match(r'([A-Z]+)', c.get('r') or '')
                col = _col_idx(m.group(1)) if m else len(cells)
                cell_type = c.get('t')
                val = ''
                for ch in c:
                    lt = _local(ch.tag)
                    if lt == 'v':
                        val = (ch.text or '').strip()
                        if cell_type == 's' and val.isdigit() and int(val) < len(shared):
                            val = shared[int(val)]
                    elif lt == 'is':
                        val = ''.join((x.text or '') for x in ch.iter()
                                      if _local(x.tag) == 't').strip()
                cells[col] = (val or '').strip()
            if any(cells.values()):
                rows.append(cells)
        if rows:
            all_rows.append((os.path.basename(sh), rows))
    return all_rows


# تسميات عربية عامة لحقول الجداول الشائعة (تُستخدم لبناء جملة طبيعية لكل صف —
# تعمل مع أي دومين: خدمات، فروع، سياسات، فنادق، مكاتب قانون...)
_FIELD_LABELS_AR = {
    'service_name': 'الخدمة', 'name': 'الاسم', 'title': 'الاسم', 'product': 'المنتج',
    'item': 'الصنف', 'plan': 'الخطة', 'branch_name': 'الفرع', 'city': 'المدينة',
    'area': 'المنطقة/العنوان', 'address': 'العنوان', 'working_hours': 'مواعيد العمل',
    'phone': 'التليفون', 'unit': 'وحدة التسعير', 'price': 'السعر', 'price_egp': 'السعر',
    'cost': 'التكلفة', 'monthly_price': 'السعر الشهري', 'rate': 'السعر',
    'production_days': 'مدة التنفيذ', 'delivery_days': 'مدة التسليم', 'duration': 'المدة',
    'duration_minutes': 'المدة بالدقائق', 'days': 'الأيام', 'nights': 'الليالي',
    'warranty_months': 'الضمان', 'min_qty': 'أقل كمية', 'max_area_sqm': 'أقصى مساحة',
    'setup_fee_egp': 'رسوم الإعداد', 'rush_fee_pct': 'رسوم الاستعجال',
    'seasonal_offer_discount_pct': 'خصم العرض الموسمي', 'discount_pct': 'نسبة الخصم',
    'category': 'التصنيف', 'currency': 'العملة', 'services_available': 'الخدمات المتاحة',
    'services_unavailable': 'الخدمات غير المتاحة', 'unavailable_reason': 'سبب عدم التوفر',
    'conditions': 'الشروط', 'exclusion': 'مستثنى', 'excluded': 'مستثنى',
    'special_terms': 'شروط خاصة', 'coverage': 'التغطية', 'notes': 'ملاحظات',
    'answer': 'الإجابة', 'question': 'السؤال', 'description': 'الوصف',
    # حقول عربية أصلية (CSV عربي بالكامل)
    'اسم_الخدمة': 'الخدمة', 'اسم_الفرع': 'الفرع', 'اسم': 'الاسم',
    'السعر_جنيه': 'السعر', 'المدة': 'المدة', 'نسبة_الخصم_%': 'نسبة الخصم',
    'رسوم_إضافية_جنيه': 'رسوم إضافية', 'ملاحظات': 'ملاحظات',
    'المدينة': 'المدينة', 'ساعات_العمل': 'ساعات العمل', 'ملاحظة_سلبية': 'ملاحظة سلبية',
}


def _ar_label(field):
    """تسمية عربية لحقل أو الحقل نفسه — مع مطابقة دلالية عامة."""
    f = str(field).strip()
    fl = f.lower()
    if f in _FIELD_LABELS_AR:
        return _FIELD_LABELS_AR[f]
    if fl in _FIELD_LABELS_AR:
        return _FIELD_LABELS_AR[fl]
    # مطابقة دلالية: أي حقل يحوي كلمة دالة يأخذ تسميتها العامة
    for key, label in (('سعر', 'السعر'), ('خصم', 'الخصم'), ('رسوم', 'الرسوم الإضافية'),
                       ('مدة', 'المدة'), ('ملاحظ', 'ملاحظات'), ('عملة', 'العملة'),
                       ('مدينة', 'المدينة'), ('ساعات', 'ساعات العمل'), ('ضمان', 'الضمان')):
        if key in f:
            return label
    for k, v in _FIELD_LABELS_AR.items():
        if fl.endswith(k) or k in fl:
            return v
    return f


def _row_sentence(header, cells):
    """جملة طبيعية واحدة من صف جدول: "جلسة تصوير عائلية: السعر 900، المدة ساعة..."
    حتى يفهم النموذج والاسترجاع أي رقم يعني ماذا (بدل قيم مفصولة بفواصل).
    الاسم يُكشف دلالياً من اسم الحقل (اسم_الخدمة/اسم_الفرع/name/branch_name...)."""
    pairs = [(h, c) for h, c in zip(header, cells) if c and h]
    if not pairs:
        return ''

    def is_name_field(h):
        hl = str(h).lower().strip()
        return (hl in ('service_name', 'name', 'title', 'product', 'item', 'branch_name')
                or 'اسم' in hl or hl.endswith('name'))

    lead = ''
    rest = []
    for h, c in pairs:
        if is_name_field(h) and not lead:
            lead = c
            continue
        rest.append('%s %s' % (_ar_label(h), c))
    if not lead and rest:
        lead = ''
    sentence = u'%s: %s.' % (lead, '، '.join(rest)) if rest else u'%s.' % lead
    return re.sub(r'\s+', ' ', sentence).strip()


def table_to_sections(rows):
    """عمودين: الأول سؤال والتاني إجابة. أكتر من كده: أول عمود عنوان والباقي صفات.
    كل صف يُخرَج بجملة طبيعية بأسماء الحقول + السطور الخام key:value (للدقة)،
    والعنوان = اسم العنصر + المعرّف (وليس المعرّف وحده P01)."""
    if not rows:
        return []
    width = max(max(r) for r in rows) + 1
    grid = [[r.get(i, '') for i in range(width)] for r in rows]
    header = [h.strip() for h in grid[0]]
    body = grid[1:]
    if not any(header):  # أول صف فاضي؟ اعتبر كل الصفوف بيانات
        body = grid
        header = [u'عمود %d' % (i + 1) for i in range(width)]
    sections = []
    for row in body:
        cells = [c.strip() for c in row]
        if not any(cells):
            continue
        nonempty = [c for c in cells if c]
        if width == 1:
            sections.append({'heading': nonempty[0][:80], 'text': nonempty[0]})
        elif width == 2:
            heading = cells[0] or nonempty[0][:60]
            text = cells[1] if cells[1] else cells[0]
            sections.append({'heading': heading[:120], 'text': text})
        else:
            # عمود الاسم: مطابقة تامة/جزئية لاتيني وعربي (اسم_الخدمة/اسم_الفرع/الاسم...)
            name_idx = -1
            id_idx = -1
            for i, h in enumerate(header):
                hl = h.lower().strip()
                is_name = (hl in ('name', 'service_name', 'product', 'item', 'title', 'branch_name')
                           or 'اسم' in h or hl.endswith('name'))
                is_id = hl in ('id', 'code', 'sku', 'service_id', 'branch_id', 'item_id',
                               'product_id', 'كود', 'رقم')
                if name_idx < 0 and is_name:
                    name_idx = i
                if id_idx < 0 and is_id:
                    id_idx = i
            name_val = cells[name_idx] if name_idx >= 0 else ''
            id_val = cells[id_idx] if id_idx >= 0 else ''
            first_val = cells[0] or nonempty[0]
            if not name_val:
                name_val = first_val
            heading = f'{name_val} ({id_val})' if (id_val and id_val != name_val) else name_val[:120]

            # جملة طبيعية بأسماء الحقول (مطالبة الإثراء) + السطور الخام تحت الجملة
            sentence = _row_sentence(header, cells)
            lines = []
            for h, c in zip(header, cells):
                if c and h:
                    if h.lower() == 'warranty_months':
                        lines.append(u'%s: %s (شهور)' % (h, c))
                    else:
                        lines.append(u'%s: %s' % (h, c))
            raw = u'\n'.join(lines) or heading
            text = (sentence + u'\n' + raw) if sentence else raw
            sections.append({'heading': heading[:120], 'text': text})
    if not sections:
        joined = u'\n'.join(c for row in grid for c in row if c.strip())
        sections = [{'heading': u'بيانات مستوردة', 'text': joined or u'فارغ'}]
    return sections


# ---------------------------------------------------------------- PDF

_RE_LINE_OPS = re.compile(rb'\b(?:T\*|TD|Td|ET)\b')
_RE_TEXT = re.compile(
    rb'\[((?:[^\[\]\\]|\\.)*)\]\s*TJ'
    rb'|\(((?:[^()\\]|\\.)*)\)\s*(?:Tj|\x27|\x22)'
    rb'|<([0-9A-Fa-f\s]+)>\s*(?:Tj|\x27|\x22)'
    rb'|(\n)',
    re.S)
_RE_STR_IN_ARR = re.compile(rb'\(((?:[^()\\]|\\.)*)\)')

_ESCAPES = {0x6E: 10, 0x72: 13, 0x74: 9, 0x62: 8, 0x66: 12,
            0x28: 0x28, 0x29: 0x29, 0x5C: 0x5C}


def _pdf_unescape(b):
    out = bytearray()
    i = 0
    while i < len(b):
        c = b[i]
        if c == 0x5C and i + 1 < len(b):  # backslash
            n = b[i + 1]
            if n in _ESCAPES:
                out.append(_ESCAPES[n])
                i += 2
                continue
            if 0x30 <= n <= 0x37:  # octal \ddd
                j = i + 1
                digits = b''
                while j < len(b) and j <= i + 3 and 0x30 <= b[j] <= 0x37:
                    digits += bytes([b[j]])
                    j += 1
                out.append(int(digits, 8) & 0xFF)
                i = j
                continue
            out.append(n)
            i += 2
            continue
        out.append(c)
        i += 1
    try:
        return out.decode('utf-8')
    except UnicodeDecodeError:
        return out.decode('latin-1')


def _pdf_hex(h):
    h = re.sub(rb'\s', b'', h)
    try:
        data = bytes.fromhex(h.decode('ascii'))
    except ValueError:
        return ''
    if data[:2] == b'\xfe\xff':
        return data[2:].decode('utf-16-be', 'replace')
    return data.decode('latin-1')


def _stream_text(content):
    marked = _RE_LINE_OPS.sub(b'\n', content)
    parts = []
    for m in _RE_TEXT.finditer(marked):
        if m.group(4) is not None:
            parts.append('\n')
        elif m.group(1) is not None:
            parts.append(''.join(_pdf_unescape(x) for x in _RE_STR_IN_ARR.findall(m.group(1))))
        elif m.group(2) is not None:
            parts.append(_pdf_unescape(m.group(2)))
        elif m.group(3) is not None:
            parts.append(_pdf_hex(m.group(3)))
    return ''.join(parts)


def extract_pdf_text(path):
    for mod in ('pypdf', 'PyPDF2', 'pdfplumber'):
        try:
            module = __import__(mod)
            if mod == 'pdfplumber':
                with module.open(path) as pdf:
                    return '\n\n'.join((p.extract_text() or '') for p in pdf.pages)
            reader = module.PdfReader(path)
            return '\n\n'.join((p.extract_text() or '') for p in reader.pages)
        except ImportError:
            continue
        except Exception:
            continue
    raw = open(path, 'rb').read()
    texts = []
    for m in re.finditer(rb'stream[\r\n]+(.*?)[\r\n]*endstream', raw, re.S):
        data = m.group(1)
        try:
            data = zlib.decompress(data)
        except Exception:
            pass
        if b'Tj' in data or b'TJ' in data:
            t = _stream_text(data)
            if t.strip():
                texts.append(t)
    return '\n'.join(texts)


def paragraphs_to_sections(text, min_len=40):
    """تجميع سطور الـ PDF/TXT في فقرات منطقية:
    - الأسطر الفاصلة (====== أو ———) تُتجاهل كزينة ولا تدخل في العناوين.
    - الفقرة تنتهي بعلامة نهاية أو سطر فاصل؛ الشرائط القصيرة (شروط العرض
      المبتورة) تُدمج في الفقرة السابقة حتى يبقى كل عرض/كيان chunk واحد
      مكتمل (شرطه + استثناؤه + شروطه الخاصة)."""
    lines = [l.strip() for l in text.splitlines()]
    # تجاهل أسطر الزينة الفاصلة بالكامل
    lines = [l for l in lines if not re.fullmatch(r'[=\-—_·•]{4,}', l)]
    paras, cur = [], []
    for l in lines:
        if not l:
            if cur:
                paras.append(' '.join(cur))
                cur = []
            continue
        cur.append(l)
        if l[-1] in u'.؟!…:;':
            paras.append(' '.join(cur))
            cur = []
    if cur:
        paras.append(' '.join(cur))

    def heading_of(p):
        p_clean = re.sub(r'^[\s=\-—]+|[\s=\-—]+$', '', p)
        h = p_clean[:70]
        if len(p_clean) > 70:
            cut = h.rfind(' ')
            if cut > 30:
                h = h[:cut]
        return h.strip() + (u'…' if len(p_clean) > 70 else '')

    # دمج الشرائط القصيرة في الفقرة السابقة (عرض واحد = شرطه واستثناؤه معاً)
    merged = []
    NUMBERED_ENTITY = re.compile(r'^(?:عرض|قسم|بند|فصل|article|section|offer|item)\s*[\d一二三]+\s*[:：]', re.I)
    last_entity = -1  # فهرس آخر كتلة كيان مرقّم مفتوحة
    for p in paras:
        p_clean = re.sub(r'^[\s=\-—]+|[\s=\-—]+$', '', p).strip()
        if not p_clean:
            continue
        # فقرة "عرض N: ..." تفتح كتلة كيان مرقّم جديدة دائماً
        if NUMBERED_ENTITY.match(p_clean):
            merged.append(p_clean)
            last_entity = len(merged) - 1
            continue
        # كل ما يلي كياناً مرقّماً وحتى الكيان التالي يخصه (بنوده وشروطه
        # واستثناؤه وعربونه) — يُلحق بكتلته مهما كانت أرقامه (عيب 6:
        # بند «العربون غير قابل للاسترداد» كان يسقط لوحده كchunk يتيم)
        if merged and last_entity >= 0 and len(p_clean) < 300:
            merged[last_entity] = merged[last_entity].rstrip('.') + '. ' + p_clean
            continue
        merged.append(p_clean)

    sections, buf = [], u''
    for p in merged:
        buf = (buf + u' ' + p).strip()
        if len(buf) >= min_len:
            sections.append({'heading': heading_of(buf), 'text': buf})
            buf = u''
    if len(buf) >= 20:
        sections.append({'heading': heading_of(buf), 'text': buf})
    return sections


# ---------------------------------------------------------------- DOCX (Word)

def extract_docx_text(path):
    """استخراج النص من ملف Word (.docx) بدون أي مكتبات خارجية (zipfile + xml.etree)."""
    with zipfile.ZipFile(path) as z:
        if 'word/document.xml' not in z.namelist():
            return ''
        root = ET.fromstring(z.read('word/document.xml'))
        paragraphs = []
        for p in root.iter():
            if _local(p.tag) == 'p':
                texts = []
                for t in p.iter():
                    if _local(t.tag) == 't' and t.text:
                        texts.append(t.text)
                para_text = ''.join(texts).strip()
                if para_text:
                    paragraphs.append(para_text)
        return '\n\n'.join(paragraphs)


# ---------------------------------------------------------------- المعالجة العامة

def process_file(path, out_dir=None):
    """معالجة ملف واحد وتحويله لملف Markdown في قاعدة المعرفة.
    يرجع (out_path, count_sections).
    """
    ext = os.path.splitext(path)[1].lower()
    if ext not in SUPPORTED:
        raise ValueError('صيغة الملف غير مدعومة: %s' % (ext or 'بدون امتداد'))
    
    target_dir = out_dir if out_dir else get_kb_dir()
    os.makedirs(target_dir, exist_ok=True)

    if ext == '.md':
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        out = os.path.join(target_dir, u'train_%s.md' % _clean_name(path))
        with open(out, 'w', encoding='utf-8') as f:
            f.write(text.rstrip() + '\n')
        return out, 1
    elif ext == '.txt':
        with open(path, encoding='utf-8', errors='replace') as f:
            text = f.read()
        return write_sections(path, paragraphs_to_sections(text), out_dir)
    elif ext == '.json':
        with open(path, encoding='utf-8-sig') as f:
            data = json.load(f)
        return write_sections(path, json_to_sections(data), out_dir)
    elif ext == '.csv':
        return write_sections(path, table_to_sections(csv_rows(path)), out_dir)
    elif ext == '.xlsx':
        sections = []
        for sheet_name, rows in xlsx_rows(path):
            sections.extend(table_to_sections(rows))
        return write_sections(path, sections, out_dir)
    elif ext == '.pdf':
        text = extract_pdf_text(path)
        if len(text.strip()) < 30:
            print(u'[!] %s: النص المستخرج قليل جداً — غالباً PDF ممسوح ضوئياً (صور) أو معقد.' % os.path.basename(path))
        return write_sections(path, paragraphs_to_sections(text), out_dir)
    elif ext == '.docx':
        text = extract_docx_text(path)
        return write_sections(path, paragraphs_to_sections(text), out_dir)
    else:
        raise ValueError('صيغة غير مدعومة: %s' % ext)


# ---------------------------------------------------------------- التشغيل


def try_reload(port):
    url = 'http://localhost:%d/api/kb/reload' % port
    req = urllib.request.Request(url, data=b'{}',
                                 headers={'Content-Type': 'application/json'})
    try:
        with urllib.request.urlopen(req, timeout=3) as resp:
            info = json.loads(resp.read().decode('utf-8'))
        print(u'[OK] تم تحديث الفهرس والسيرفر شغال — إجمالي الأقسام: %d' % info.get('total_chunks', '?'))
        return True
    except Exception:
        print(u'! السيرفر مش شغال أو مش على البورت ده.')
        print(u'  بعد ما تشغله: اضغط زرار "حدّث المعرفة" في الداشبورد، أو شغل:')
        print(u'      curl -X POST http://localhost:%d/api/kb/reload' % port)
        return False


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8', errors='replace')
    except Exception:
        pass

    argv = list(sys.argv[1:])
    port = 9090
    if '--api' in argv:
        i = argv.index('--api')
        if i + 1 < len(argv):
            try:
                port = int(argv[i + 1])
            except ValueError:
                pass
            del argv[i:i + 2]
        else:
            del argv[i]

    replace_mode = False
    if '--replace' in argv or '--overwrite' in argv or '--clean' in argv:
        replace_mode = True
        argv = [a for a in argv if a not in ('--replace', '--overwrite', '--clean')]

    files = []
    for a in argv:
        if os.path.isdir(a):
            for root, dirs, names in os.walk(a):
                if 'test' in os.path.basename(root).lower():
                    continue
                for n in names:
                    if n.lower().startswith(('test_', 'hard_test')):
                        print(u'[!] تجاهل ملف اختبار: %s' % n)
                        continue
                    files.append(os.path.join(root, n))
        else:
            if not os.path.exists(a):
                print(u'[X] الملف مش موجود: %s' % a)
                continue
            ext = os.path.splitext(a)[1].lower()
            if ext not in SUPPORTED:
                continue
            if os.path.basename(a).lower().startswith(('test_', 'hard_test')):
                print(u'[!] تجاهل ملف اختبار: %s' % a)
                continue
            files.append(a)

    files = [f for f in files if os.path.splitext(f)[1].lower() in SUPPORTED]
    if not files:
        print(u'استخدام: python train.py <ملف أو فولدر أو أكتر> [--api البورت] [--replace]')
        print(u'مثال : python train.py faq.pdf products.xlsx data.json document.docx --replace')
        return

    if replace_mode:
        kb_dir = get_kb_dir()
        print(u'🗑️ تم تفعيل وضع الاستبدال: جاري مسح الملفات القديمة من %s...' % kb_dir)
        for old_f in os.listdir(kb_dir):
            old_path = os.path.join(kb_dir, old_f)
            if os.path.isfile(old_path):
                try:
                    os.remove(old_path)
                except OSError:
                    pass

    print(u'هبدأ تحويل %d ملف...' % len(files))
    done = 0
    for path in files:
        try:
            out, count = process_file(path)
            done += 1
            print(u'[OK] %s  ->  %s (%d قسم)' % (os.path.basename(path), os.path.basename(out), count))
        except Exception as e:
            print(u'[X] %s فشل: %s' % (os.path.basename(path), e))
            continue

    if done:
        print(u'\nاتحوّل %d ملف لقاعدة المعرفة. جرّب أسئلتك دلوقتي في الشات!' % done)
        try_reload(port)


if __name__ == '__main__':
    main()
