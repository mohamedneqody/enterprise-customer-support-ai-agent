import urllib.request, json, time, re
import sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8')

questions = [
    # يمكن إضافة الأسئلة الجديدة الخاصة بمركز صيانة السيارات هنا
    # تم تنظيف الأسئلة القديمة الخاصة بمتجر النور بناءً على طلب المستخدم
]

def ask(q, session_id):
    req = urllib.request.Request(
        'http://localhost:9090/api/chat',
        data=json.dumps({'message': q, 'session_id': session_id, 'enable_llm': True}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            return json.loads(resp.read().decode('utf-8'))
    except Exception as e:
        return {"answer": f"Error: {str(e)}", "sources": []}

with open("clean_room_test_results.md", "w", encoding="utf-8") as f:
    f.write("# تقرير اختبار الغرفة النظيفة (Clean Room Test)\n\n")
    for q_idx, q_text, s_id in questions:
        print(f"Testing Q{q_idx}...")
        res = ask(q_text, s_id)
        f.write(f"### Q{q_idx}: {q_text}\n")
        f.write(f"**Answer:**\n{res.get('answer', '')}\n\n")
        sources = res.get('sources', [])
        f.write(f"**Sources retrieved:** {', '.join(sources)}\n\n")
        f.write("---\n")
        time.sleep(0.5)

print("Test finished! Results written to clean_room_test_results.md")
