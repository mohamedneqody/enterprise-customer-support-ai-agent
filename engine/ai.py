# -*- coding: utf-8 -*-
"""توليد إجابة العميل من القطع المسترجعة (Extractive Answer Generation)."""
import re

PRIORITY_APOLOGY = ('نعتذر جداً عن التجربة دي 🙏 واضح إنك متضايق، '
                    'وحالتك اتحولت كأولوية قصوى لفريق الدعم.')

ESCALATION_NOTE = ('سؤالك للأسف خارج قاعدة معرفتي الحالية 🤔\n'
                   'حوّلتك لموظف بشري من فريق الدعم، وهيتم الرد عليك في أقرب وقت ممكن.')

PRIORITY_NOTE = 'تم فتح تذكرة أولوية 🚨 باسمك، وأقرب موظف هيتابعها فوراً.'

_TECH_FIELD_RE = re.compile(r'^(?:id|tags|.*_id)$', re.I)
_KIND_AR = {'price': 'السعر', 'duration': 'المدة', 'warranty': 'الضمان',
            'fee': 'رسوم إضافية', 'discount': 'خصم', 'category': 'التصنيف',
            'city': 'المدينة', 'hours': 'ساعات العمل', 'branch': 'الفرع'}


def _friendly_chunk(chunk, max_attrs=4):
    """تحويل chunk خام (سجل CSV/JSON أو جدول MD) لجملة عربية قصيرة بأسماء
    عربية للحقول المفيدة، مع إخفاء الحقول التقنية (id/tags/*_id) — عيب 1."""
    from engine.premath import _classify_field
    text = chunk.get('text', '')
    fields = []
    for line in text.splitlines():
        m = re.match(r'^\s*([\w_\u0621-\u064a%]+)\s*:\s*(.+)$', line.strip())
        if m:
            fields.append((m.group(1), m.group(2).strip()))
    if not fields:
        if '|' in text:
            rows = [r for r in text.splitlines() if r.strip().startswith('|')
                    and not re.match(r'^\|\s*-{2,}', r)]
            keep = []
            for r in rows[:3]:
                cells = [c.strip() for c in r.strip('|').split('|')]
                keep.append(' — '.join(c for c in cells if c))
            return ' | '.join(keep) if keep else text.strip()[:1500]
        return text.strip()[:1500]
    name = ''
    attrs = []
    for fk, fv in fields:
        kind = _classify_field(fk)
        if kind == 'name':
            name = fv
            continue
        if kind == 'other' or _TECH_FIELD_RE.match(fk):
            continue  # إخفاء الحقول التقنية (id/tags/*_id)
        attrs.append((_KIND_AR.get(kind, ''), fv))
    if not attrs:
        prose = [ln.strip() for ln in text.splitlines()
                 if ln.strip() and not re.match(r'^\s*(?:id|tags|service_id|branch_id|unavailable_service_id)\s*:', ln.strip(), re.I)]
        if prose:
            return '\n'.join(prose)[:1500]
    if not name:
        name = chunk.get('heading', '').split('(')[0].strip() or 'الخدمة'
    out = name
    for label, fv in attrs[:max_attrs]:
        if label:
            out += '، %s %s' % (label, fv)
    return out + '.'


def _excerpt(text, limit=220):
    text = ' '.join(text.split())
    if len(text) <= limit:
        return text
    cut = text[:limit]
    if ' ' in cut:
        cut = cut[:cut.rfind(' ')]
    return cut + '…'


def build_answer(matches, status, answerable):
    """تركيب الإجابة النهائية حسب الحالة والثقة.

    matches: قائمة (chunk, score) مرتبة
    status: resolved | escalated | priority
    answerable: هل الثقة كافية للإجابة من قاعدة المعرفة؟
    عيب 1: لا نسكب السجل الخام — صياغة ودية بحقول عربية مفيدة فقط.
    """
    parts = []
    if status == 'priority':
        parts.append(PRIORITY_APOLOGY)

    if answerable and matches:
        top_chunk, top_score = matches[0]
        parts.append(_friendly_chunk(top_chunk))
        if len(matches) > 1:
            c2, s2 = matches[1]
            if s2 >= 0.75 * top_score and c2['heading'] != top_chunk['heading']:
                parts.append(f"📌 **{c2['heading']}:**\n" + _friendly_chunk(c2))
    else:
        # عيب 9: تصعيد موحد، ومعه المعلومة الرسمية المتاحة إن استرجعها البحث
        # (مثل طرق الدفع الرسمية) — من غير أي رقم أو مبلغ مخترع.
        # chunks الحقن (0.9) تُستبعد: هي لفحص التعارض لا لإرفاق معلومات
        organic = [m for m in matches if m[1] < 0.89] or matches
        parts.append(ESCALATION_NOTE)
        if organic and organic[0][1] >= 0.30:
            info = _friendly_chunk(organic[0][0])
            if info and not any(w in info for w in ('service_id', 'branch_id', 'tags')):
                parts.append('وللمعلومة الرسمية المتاحة عندنا: ' + info)
        if status == 'priority':
            parts.append(PRIORITY_NOTE)
        return '\n\n'.join(parts)

    if status == 'priority':
        parts.append(PRIORITY_NOTE + ' لو محتاج أي توضيح تاني أنا تحت أمرك.')
    return '\n\n'.join(parts)


def check_chitchat(message):
    """التعرف على الأسئلة الشائعة، الهوية، التحيات، والشتائم للرد عليها فوراً برمجياً بشكل ديناميكي لأي شركة."""
    import os
    store_name = os.environ.get('STORE_NAME', '').strip()
    target_name = store_name if store_name else "خدمة العملاء"
    msg = (message or '').strip().lower()
    
    # 1. أسئلة الهوية والاسم
    if any(k in msg for k in ['اسمك', 'اسمك ايه', 'مين انت', 'انت مين', 'من انت', 'عرف نفسك', 'ما اسمك', 'who are you', 'what is your name']):
        return {
            'handled': True,
            'answer': f'أنا **المساعد الذكي لخدمة العملاء (AI Customer Support Assistant)** الخاص بـ {target_name}.\nوظيفتي مساعدتك على مدار 24 ساعة في الإجابة عن جميع استفساراتك، الخدمات، الأسعار، السياسات، والمواعيد.',
            'source': 'رد تلقائي مبرمج (Built-in Response)'
        }
    
    # 2. أسئلة الشركة المصنعة والمطور
    if any(k in msg for k in ['صنعك', 'صانعك', 'صانعة', 'برمجك', 'طورك', 'مطورك', 'مين عملك', 'من صنعك', 'who made you', 'who created you']):
        return {
            'handled': True,
            'answer': 'تم تطويري وبرمجتي بواسطة **مهندس ذكاء اصطناعي (AI Engineer)** باستخدام أحدث تقنيات الـ RAG والأنظمة الوكيلة (Agentic Multi-Layer Intelligence)، لتوفير دعم فني ذكي وآلي للعملاء 24/7.',
            'source': 'رد تلقائي مبرمج (Built-in Response)'
        }
    
    # 3. التحيات والترحيب
    if any(k in msg for k in ['اهلا', 'مرحبا', 'سلام', 'السلام عليكم', 'هاي', 'صباح الخير', 'مساء الخير', 'ازيك', 'عامل ايه']):
        return {
            'handled': True,
            'answer': f'أهلاً وسهلاً بك في {target_name}! 👋 يسعدني جداً مساعدتك، تفضل بسؤالك عن أي خدمة أو استفسار وسأجيبك فوراً.',
            'source': 'رد تلقائي مبرمج (Built-in Response)'
        }
    
    # 4. عبارات الشكر
    if any(k in msg for k in ['شكرا', 'شكراً', 'تسلم', 'مشكور', 'الف شكر', 'ألف شكر', 'thanks', 'thank you']):
        return {
            'handled': True,
            'answer': 'العفو يا فندم! 💚 أنا في خدمتك دائماً، لو محتاج أي مساعدة إضافية في أي وقت أنا تحت أمرك.',
            'source': 'رد تلقائي مبرمج (Built-in Response)'
        }
    
    return None
