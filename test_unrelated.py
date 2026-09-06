import requests, json, sys, io
sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding='utf-8', write_through=True)

def test_unrelated(q1, q2, q3, sid):
    print(f'\n--- Unrelated: {sid} ---', flush=True)
    r1 = requests.post('http://localhost:9090/api/chat', json={'message': q1, 'session_id': sid, 'mode': 'llm'}, timeout=60)
    print(f'Q1: {q1}\nAnswer: {r1.json().get("answer", "")[:100]}...', flush=True)
    
    r2 = requests.post('http://localhost:9090/api/chat', json={'message': q2, 'session_id': sid, 'mode': 'llm'}, timeout=60)
    print(f'\nQ2: {q2}\nFULL_JSON:\n', json.dumps(r2.json(), ensure_ascii=False, indent=2), flush=True)

    r3 = requests.post('http://localhost:9090/api/chat', json={'message': q3, 'session_id': sid, 'mode': 'llm'}, timeout=60)
    print(f'\nQ3: {q3}\nFULL_JSON:\n', json.dumps(r3.json(), ensure_ascii=False, indent=2), flush=True)

test_unrelated('فيه فرع في الإسكندرية؟', 'تغيير البطارية بكام؟', 'إيه نظام التقسيط؟', 'test_session_unrelated')
