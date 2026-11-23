# -*- coding: utf-8 -*-
"""
engine/judge.py — المحطة الثامنة: مُحكّم آلي (LLM-as-a-Judge).

بعد إرسال الإجابة، في خيط خلفي منفصل (بلا أي أثر على زمن الرد)، يُرسل
(السؤال + الإجابة + مقاطع المعرفة المصدرية) للنموذج المُحكِّم الذي يُرجع
JSON صارم: هل كل ادعاءات وأرقام الإجابة مدعومة نصاً من المقاطع؟

النتيجة:
- تُسجَّل في سجل التدقيق audit_log.jsonl (مسار المساءلة).
- الإجابات غير المدعومة تُعلَّم على التذكرة (judge_flagged=true) لتظهر
  في الداشبورد ولأي فحص جودة لاحق — ولا يُرسل شيء للعميل بعد إرسال الرد.

فشل المُحكّم نفسه لا يُكسر النظام أبداً (silent-by-design).
"""
import json
import os
import re
import threading

from engine import audit, providers, store

_ENABLED = os.environ.get('ENABLE_JUDGE', 'true').lower() in ('true', '1', 'yes')
_TIMEOUT = float(os.environ.get('JUDGE_TIMEOUT', '60'))
_TOP_CTX = 3

_PROMPT = """أنت مُحكّم جودة صارم لإجابات مساعد خدمة عملاء.
قارن الإجابة المُقيَّمة بمقاطع المعرفة المرجعية أدناه.
القاعدة: supported=true فقط لو كل الأرقام والأسعار والادعاءات في الإجابة
موجودة نصاً في المقاطع أو نتيجة حساب مباشرة لها. أي رقم أو ادعاء غير موجود
في المقاطع = false مع ذكر المشكلة بالعربية.
أعد JSON فقط بدون أي نص آخر:
{"supported": true, "problems": []}
أو
{"supported": false, "problems": ["وصف المشكلة بالعربية"]}"""


def parse_verdict(text):
    """تحليل مخرجات المُحكّم بتحمل حدود: أسوار ``` ورموز زائدة وفشل كامل.
    فشل التحليل يعيد None (غير محكوم) — لا يُعلَّم شيء."""
    if not text:
        return None
    m = re.search(r'\{.*\}', text, re.DOTALL)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except (ValueError, TypeError):
        return None
    supported = d.get('supported')
    if isinstance(supported, str):
        supported = supported.strip().lower() in ('true', 'yes', '1', 'نعم')
    if not isinstance(supported, bool):
        return None
    problems = d.get('problems') or d.get('issues') or []
    if not isinstance(problems, list):
        problems = [str(problems)]
    return {'supported': supported, 'problems': [str(p)[:200] for p in problems]}


def _call_judge_llm(prompt):
    """استدعاء نموذج المُحكّم عبر طبقة providers (يدعم JUDGE_PROVIDER وJUDGE_MODEL مستقلين)."""
    j_prov = os.environ.get('JUDGE_PROVIDER', '').strip() or None
    j_model = os.environ.get('JUDGE_MODEL', '').strip() or None
    res = providers.complete(
        prompt,
        tier='pro' if (j_prov in ('gemini', 'openai') and not j_model) else 'fast',
        provider=j_prov,
        model_override=j_model,
        temperature=0.0,
        max_tokens=220,
        json_mode=True,
        timeout=_TIMEOUT,
    )
    return res.get('text', '')


def grade_now(query, answer, contexts):
    """حكم متزامن (للاستخدام اليدوي/الاختبارات) — يعيد verdict dict أو None."""
    ctx = '\n\n'.join('[%d] %s' % (i, c[:600]) for i, c in enumerate(contexts[:_TOP_CTX], 1))
    if not ctx.strip():
        return None
    prompt = '%s\n\nمقاطع المعرفة المرجعية:\n%s\n\nسؤال العميل: %s\nالإجابة المُقيَّمة: %s\nJSON:' % (
        _PROMPT, ctx, query[:300], answer[:800])
    try:
        raw = _call_judge_llm(prompt)
    except Exception as e:
        audit.log_event({'type': 'judge_error', 'error': str(e)[:200]})
        return None
    return parse_verdict(raw)


def grade_async(ticket_id, query, answer, contexts):
    """تشغيل المُحكّم في خيط خلفي — يُستدعى بعد إرسال الرد للعميل."""
    if not _ENABLED:
        return
    threading.Thread(target=_grade, args=(ticket_id, query, answer, contexts),
                     daemon=True).start()


def _grade(ticket_id, query, answer, contexts):
    try:
        verdict = grade_now(query, answer, contexts)
        if verdict is None:
            audit.log_event({'type': 'judge', 'ticket_id': ticket_id,
                             'verdict': 'unjudged'})
            return
        audit.log_event({'type': 'judge', 'ticket_id': ticket_id,
                         'supported': verdict['supported'],
                         'problems': verdict['problems']})
        store.update_ticket(ticket_id, judge_supported=verdict['supported'],
                            judge_flagged=not verdict['supported'],
                            judge_problems='؛ '.join(verdict['problems'])[:300])
    except Exception as e:
        try:
            audit.log_event({'type': 'judge_error', 'ticket_id': ticket_id,
                             'error': str(e)[:200]})
        except Exception:
            pass
