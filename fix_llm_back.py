path = r'D:\مدن algorthim1\07-customer-support-ai-agent-clean\engine\llm.py'
with open(path, 'r', encoding='utf-8') as f:
    text = f.read()
text = text.replace('return f"FALLBACK TRIGGERED"', 'return ai.build_answer(matches, status, answerable)')
with open(path, 'w', encoding='utf-8') as f:
    f.write(text)
