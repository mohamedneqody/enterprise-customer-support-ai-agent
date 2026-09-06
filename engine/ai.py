# -*- coding: utf-8 -*-
"""توليد إجابة العميل من القطع المسترجعة (Extractive Answer Generation)."""

PRIORITY_APOLOGY = ('نعتذر جداً عن التجربة دي 🙏 واضح إنك متضايق، '
                    'وحالتك اتحولت كأولوية قصوى لفريق الدعم.')

ESCALATION_NOTE = ('سؤالك للأسف خارج قاعدة معرفتي الحالية 🤔\n'
                   'حوّلتك لموظف بشري من فريق الدعم، وهيتم الرد عليك في أقرب وقت ممكن.')

PRIORITY_NOTE = 'تم فتح تذكرة أولوية 🚨 باسمك، وأقرب موظف هيتابعها فوراً.'


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
    """
    parts = []
    if status == 'priority':
        parts.append(PRIORITY_APOLOGY)

    if answerable and matches:
        top_chunk, top_score = matches[0]
        parts.append(top_chunk['text'].strip())
        if len(matches) > 1:
            c2, s2 = matches[1]
            if s2 >= 0.75 * top_score and c2['heading'] != top_chunk['heading']:
                parts.append(f"📌 **{c2['heading']}:**\n" + c2['text'].strip())
    else:
        parts.append(ESCALATION_NOTE)
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
