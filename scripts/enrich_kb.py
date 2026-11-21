# -*- coding: utf-8 -*-
"""
scripts/enrich_kb.py — إثراء قاعدة معرفة موجودة (مستوردة بنسخة قديمة من train.py):
1) أقسام key:value (صفوف CSV) → جملة طبيعية بأسماء الحقول + عنوان من اسم العنصر.
2) شظايا مبتورة (شروط عروض قطعها التقسيم القديم) → تُدمج في العرض السابق.
3) يعمل على أي دومين (يقرأ الحقول الموجودة لا اسماء مجالات).
التشغيل:  python scripts/enrich_kb.py  ثم أعِد تحميل المعرفة من الداشبورد.
"""
import io
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import train  # noqa: E402  (نستخدم _row_sentence و _ar_label من نفس المنطق)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


def parse_sections(text):
    """## عنوان + جسم لكل قسم."""
    sections = []
    cur_head, cur_body = None, []
    for line in text.splitlines():
        if line.startswith('## '):
            if cur_head is not None:
                sections.append((cur_head, '\n'.join(cur_body).strip()))
            cur_head, cur_body = line[3:].strip(), []
        elif cur_head is not None:
            cur_body.append(line)
    if cur_head is not None:
        sections.append((cur_head, '\n'.join(cur_body).strip()))
    return sections


def looks_structured(body):
    """هل الجسم كتلة key:value (صف CSV)؟"""
    lines = [l for l in body.splitlines() if l.strip()]
    kv = sum(1 for l in lines if re.match(r'^[\w_]+\s*:\s*\S', l.strip()))
    return len(lines) >= 2 and kv >= max(2, len(lines) // 2)


def enrich_section(head, body):
    """إثراء قسم واحد: يعيد (عنوان جديد، جسم جديد) — أو نفسه إن كان مكتملًا."""
    head = re.sub(r'^[\s=\-—]+|[\s=\-—]+$', '', head).strip() or head
    if looks_structured(body):
        # حماية التكرار: لو أول سطر جملة طبيعية (ليست key:value) فهو مُثرى سابقاً
        first = body.splitlines()[0].strip()
        if not re.match(r'^[\w_]+\s*:', first):
            return head, body
        # حقول الصف → جملة طبيعية
        fields = []
        for line in body.splitlines():
            m = re.match(r'^\s*([\w_]+)\s*:\s*(.+)$', line.strip())
            if m:
                fields.append((m.group(1), m.group(2).strip()))
        if not fields:
            return head, body
        header = [f[0] for f in fields]
        cells = [f[1] for f in fields]
        sentence = train._row_sentence(header, cells)
        # اسم العرض: من حقل الاسم + المعرّف القديم إن وجد
        name = ''
        for h, c in fields:
            if h.lower() in ('service_name', 'name', 'branch_name', 'title', 'product'):
                name = c
                break
        id_m = re.search(r'\(([^)]{2,12})\)', head)
        new_head = '%s (%s)' % (name, id_m.group(1)) if (name and id_m) else (name or head)
        new_body = (sentence + '\n' + body.strip()) if sentence else body
        return new_head[:120], new_body
    return head, body


def is_fragment(head, body):
    """شظية قصيرة بلا حقول: بند عروض قطعه التقسيم القديم (يُدمج في السابق).
    عناوين تبدأ بـ '=' هي بدايات عروض جديدة — ليست شظايا."""
    h = head.strip()
    if h.startswith('='):
        return False
    if h.startswith(('-', '—')):
        return True
    if len(body) < 90 and '…' in h:
        return True
    return False


def enrich_file(path):
    raw = io.open(path, encoding='utf-8').read()
    lines = raw.splitlines()
    title = lines[0] if lines and lines[0].startswith('# ') else ''
    sections = parse_sections(raw)
    if not sections:
        return 0
    out = [title, '']
    merged_count = 0
    prev = None  # (head, body)
    for head, body in sections:
        if prev is not None and is_fragment(head, body):
            # دمج الشظية في القسم السابق (عرض واحد مكتمل)
            frag = re.sub(r'^[-=—•*\s]+', '', body).strip() or head.strip('=-— ')
            pb = prev[1].rstrip()
            if not pb.endswith(('.', '!', '؟', '…', ':')):
                pb += '.'
            prev = (prev[0], pb + ' ' + frag)
            merged_count += 1
            continue
        if prev is not None:
            out += ['## %s' % prev[0], prev[1], '']
        prev = enrich_section(head, body)
    if prev is not None:
        out += ['## %s' % prev[0], prev[1], '']
    io.open(path, 'w', encoding='utf-8').write('\n'.join(out).rstrip() + '\n')
    return merged_count


def main():
    kb_dir = train.get_kb_dir()
    total_merged = 0
    for fname in sorted(os.listdir(kb_dir)):
        if not fname.endswith(('.md', '.txt')):
            continue
        path = os.path.join(kb_dir, fname)
        merged = enrich_file(path)
        total_merged += merged
        print('[OK] %s — شظايا مدمجة: %d' % (fname, merged))
    print('تم — أعِد تحميل المعرفة (زر التحديث أو /api/kb/reload). مجموع الدمج: %d' % total_merged)


if __name__ == '__main__':
    main()
