import requests, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)

def test_pair(q1, q2, sid):
    print(f'\n--- Zoug: {sid} ---', flush=True)
    r1 = requests.post('http://localhost:9090/api/chat', json={'message': q1, 'session_id': sid, 'mode': 'llm'}, timeout=200)
    print(f'Q1: {q1} -> {r1.json().get("answer")[:50]}...', flush=True)
    
    r2 = requests.post('http://localhost:9090/api/chat', json={'message': q2, 'session_id': sid, 'mode': 'llm'}, timeout=200)
    print(f'\nQ2: {q2}', flush=True)
    print(json.dumps(r2.json(), ensure_ascii=False, indent=2), flush=True)

test_pair('إيه الفرق بين تغيير زيت عادي وفُل صناعي؟', 'طيب التاني بكام؟', 'test_session_11')
test_pair('إيه مواعيد فرع مدينة نصر؟', 'وهل بيشتغلوا يوم الجمعة؟', 'test_session_12')
