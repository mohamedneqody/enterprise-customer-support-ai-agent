import urllib.request
import json
import sys

queries = [
    'لو طلبت 12 قطعة من الكيبورد الميكانيكي سعره 1099 جنيه، الإجمالي بعد الخصم كام؟',
    'عايز أشحن طلب للقاهرة ولأسوان، بكام الشحن ويوصل إمتى لكل محافظة؟',
    'مين طورك؟',
    'ينفع أدفع بالدولار لو أنا مقيم بره مصر؟',
    'ينفع أدفع بالدولار لو أنا مقيم بره مصر؟',
    'ينفع أدفع بالدولار لو أنا مقيم بره مصر؟'
]

print("Starting tests...\n")
for i, q in enumerate(queries):
    req = urllib.request.Request(
        'http://localhost:9090/api/chat',
        data=json.dumps({'message': q, 'session_id': f'test-session-{i}'}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    with urllib.request.urlopen(req) as resp:
        ans = json.loads(resp.read().decode('utf-8'))
        print(f"--- السؤال {i+1} ---")
        print(f"سؤال: {q}")
        print(f"الرد: {ans.get('answer')}")
        print(f"المصادر: {ans.get('sources')}\n")
