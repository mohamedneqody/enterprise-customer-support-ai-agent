# -*- coding: utf-8 -*-
"""
توليد تذاكر تجريبية (آخر 7 أيام) عشان الداشبورد يبان حي من أول لحظة.
بيستخدم نفس الـ pipeline الحقيقي: الأسئلة جوه المعرفة بتتحل، والخارجية بتتصعد،
والرسائل الغاضبة بتطلع تذاكر أولوية.

التشغيل:   python seed.py            (لو في تذاكر موجودة هيطلب --force)
           python seed.py --force
"""
import random
import sys
import time
from collections import Counter

from engine import pipeline, store

IN_KB = [
    'إزاي أتابع أوردر وشحنتي؟',
    'امتى بيوصل الطلب للقاهرة؟',
    'هل في دفع عند الاستلام؟',
    'عايز أرجع منتج وناخد الفلوس، إيه الخطوات؟',
    'نسيت كلمة المرور أعمل إيه؟',
    'كود الخصم بيتحط فين؟',
    'بتشحنوا السعودية؟',
    'المنتج وصل مكسور أعمل إيه؟',
    'الفلوس بترجع في قد إيه؟',
    'التقسيط متاح بأي شروط؟',
    'عايز فاتورة ضريبية للشركة',
    'أغير رقم الموبايل المسجل إزاي؟',
    'أسعار الشحن للمحافظات كام؟',
    'عملية الدفع فشلت والمبلغ اتحجز',
    'how do I track my order?',
    'what is your return policy?',
    'أقدر أغير عنوان التوصيل بعد الطلب؟',
    'الشحنة اتأخرت أعمل إيه؟',
]

OUT_KB = [
    'هل بتبيعوا عربيات؟',
    'إيه رأيكم في مباراة الأهلي النهاردة؟',
    'محتاج أشوف وظائف خالية عندكم؟',
    'إيه أسرع قرد في العالم؟',
    'سعر الدولار النهاردة كام؟',
    'تعملولي تصميم لوجو لمحل جديد؟',
    'عندي اقتراح لمنتج جديد يتنزل الصيف الجاي',
    'بتصلحوا أجهزة اللاب توب؟',
]

ANGRY = [
    'أنا غضبان جداً!! الطلب متأخر أسبوع كامل ومحدش بيرد على التليفون',
    'خدمة أسوأ ما يكون، ده نصب واحتيال بصراحة، هشتكي',
    'زهقت خلاص!! الطلب غلط للمرة التالتة والمنتج وصل مكسور',
    'مقبوض جداً من التأخير، الشحنة اتأخرت وعايز أعرف بتتحرك فين دلوقتي',
    'unacceptable service!! my order is late and nobody replies, worst store ever',
]


def main():
    try:
        sys.stdout.reconfigure(encoding='utf-8')
    except Exception:
        pass

    if store.load_tickets() and '--force' not in sys.argv:
        print('في تذاكر موجودة بالفعل في: %s' % store.get_tickets_file())
        print('شغل:  python seed.py --force  لو عايز تكتب بيانات تجريبية جديدة')
        return

    pipeline.get_index()  # تسخين
    random.seed(7)
    now = time.time()

    messages = list(IN_KB) + list(IN_KB) + list(OUT_KB) + ANGRY + ANGRY
    random.shuffle(messages)

    created = []
    for i, msg in enumerate(messages):
        ts = now - random.uniform(0, 6.9 * 86400)  # موزعة على آخر 7 أيام
        ticket = pipeline.handle_message(msg, session_id='seed-%d' % (i % 5), now=ts)
        if 'error' in ticket:
            continue
        # زمن رد واقعي للعرض (المحرك الفعلي أسرع بكثير)
        if ticket['status'] == 'resolved':
            fake_ms = random.randint(300, 1400)
            rating = random.choice([5, 5, 5, 4, 4, 3])
        elif ticket['status'] == 'priority':
            fake_ms = random.randint(120, 500)
            rating = random.choice([4, 3, 3, 2])
        else:
            fake_ms = random.randint(150, 600)
            rating = random.choice([3, 3, 2, 4])
        store.update_ticket(ticket['id'], response_ms=fake_ms)
        store.set_rating(ticket['id'], rating)
        created.append(ticket['status'])

    print('تم إنشاء %d تذكرة تجريبية على مدار آخر 7 أيام' % len(created))
    for status, count in sorted(Counter(created).items()):
        print('  %-10s : %d' % (status, count))
    print('شغل السيرفر:  python app.py  وافتح /dashboard')


if __name__ == '__main__':
    main()
