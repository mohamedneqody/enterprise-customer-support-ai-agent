import re, urllib.request, json

text = open('1/hard_test_questions_round2.md', encoding='utf-8').read()
qs = re.findall(r'\*\*(\d+)\.\*\*\s*([^\n]+)', text)

results = []
for num, q in qs:
    q = q.strip()
    req = urllib.request.Request(
        'http://localhost:9090/api/chat',
        data=json.dumps({'message': q, 'session_id': f'test-20-{num}', 'enable_llm': True}).encode('utf-8'),
        headers={'Content-Type': 'application/json'}
    )
    try:
        with urllib.request.urlopen(req) as resp:
            ans = json.loads(resp.read().decode('utf-8'))
            results.append(f'سؤال {num}: {q}\nالرد: {ans.get("answer")}\nالثقة: {ans.get("confidence")}\nالمصادر: {ans.get("sources")}\n')
    except Exception as e:
        results.append(f'سؤال {num}: {q}\nالرد: خطأ - {e}\n')

with open('test_20_results.txt', 'w', encoding='utf-8') as f:
    f.write('\n'.join(results))
