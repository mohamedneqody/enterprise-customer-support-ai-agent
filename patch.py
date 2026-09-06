import sys
with open('engine/session.py', 'r', encoding='utf-8') as f:
    content = f.read()

replacement = '''    if is_followup:
        history = sess.get('history', [])
        last_query = history[-1]['query'] if history else ''
        if last_query:
            return f"[السياق من سؤالك السابق: {last_query}] {current_query}"
'''

start_str = '    if is_followup:'
end_str = '    return current_query'
start_idx = content.find(start_str)
end_idx = content.find(end_str, start_idx)

if start_idx != -1 and end_idx != -1:
    new_content = content[:start_idx] + replacement + '\n    return current_query' + content[end_idx + len(end_str):]
    with open('engine/session.py', 'w', encoding='utf-8') as f:
        f.write(new_content)
    print('Replaced')
else:
    print('Not found')
