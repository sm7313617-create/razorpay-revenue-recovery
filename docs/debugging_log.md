# Salvage — What Broke at 2 AM (Engineering Post-Mortem)

> First-person account of the two bugs that cost the most time during this buildathon. Written after the fact, while the pain is still fresh enough to be honest about.

---

## Bug 1 — "The Silent Escalation" (Task 4)

### Symptom
Every single Gemini call returned `"escalate"`. Clean. No exceptions. No stack traces. The pipeline ran to completion, logged cleanly, and the dashboard showed escalations everywhere. It looked like the model was just being extremely conservative.

### What We Thought Was Wrong
At first, I genuinely believed the prompt was too alarming. The system prompt had language about financial risk and irreversible actions, and I figured Gemini was reading that and deciding everything was too risky to touch. I spent about 30 minutes rewriting the prompt to be softer, less dramatic. The output didn't change.

Then I thought maybe the model tier was the issue — maybe `gemini-3.5-flash-lite` was too small and was just pattern-matching to "escalate" as a safe default. I nearly switched models before looking more carefully at the actual code.

### What Was Actually Wrong
`langchain-google-genai` returns `response.content` as a **list of part-dicts**, not a plain string. Something like:

```python
[{"type": "text", "text": "retry"}]
```

The code was calling `.strip()` directly on that list. In Python, calling `.strip()` on a list raises a `TypeError`. That exception was being caught by a broad `except Exception as e:` block that silently fell back to `"escalate"` as the default decision and logged nothing to stderr. The LLM path had never actually executed correctly — not once.

### How We Found It
I stopped trusting the agent's summary output and went back to the raw terminal. When I added a `print(response.content)` before the `.strip()` call and ran a single event, the list structure was immediately obvious. It took about 30 seconds to find once I actually looked at the raw value.

### Fix Applied
Before calling `.strip()`, check whether `response.content` is a list. If so, find the first item where `type == "text"` and extract its `.text` field:

```python
content = response.content
if isinstance(content, list):
    text_parts = [p["text"] for p in content if p.get("type") == "text"]
    content = text_parts[0] if text_parts else "escalate"
decision = content.strip().lower()
```

After the fix, Gemini started returning `retry`, `notify`, and `discount` for appropriate events. The 30 LLM decisions in the final run are genuine.

### Lesson for Fintech Systems
A broad `except` that swallows errors and continues with a "safe" default is **more dangerous than a crash** in a financial system. A crash surfaces the problem immediately — it's loud, obvious, and stops processing. A silent wrong decision propagates through 57 events undetected, gets logged as correct, and ends up in a dashboard that looks healthy. In money-adjacent code, if you catch a broad exception, you must at minimum log it at ERROR level with the full traceback. Failing silently is not failing safely.

---

## Bug 2 — "The Invisible Escalations" (Task 7)

### Symptom
The Recovery Analysis tab in the Streamlit dashboard showed **0 escalated bank_downtime events**. But the Exception Report tab (a separate query) showed **5 bank_downtime events, all escalated**. The same database, two tabs, contradictory counts.

### What We Thought Was Wrong
My first assumption was a dashboard rendering bug — maybe the Recovery Analysis tab was filtering by the wrong column, or there was an off-by-one in the query. I spent a while rewriting the Streamlit display logic, which didn't fix anything because the bug wasn't in the display — it was in `metrics.py`.

### What Was Actually Wrong
The `get_by_failure_code()` function in `metrics.py` used this logic to detect whether a payment had been escalated:

```python
is_escalated = not any(a.status == "success" for a in actions)
```

The reasoning was: "if no action has `status='success'`, then it must have been escalated." But `recovery_actions` stores escalations as:

```
action_taken = 'escalate'
status = 'success'
```

`status='success'` here means **"successfully dispatched to the human escalation queue"** — not "the payment was recovered." All 5 bank-downtime escalations had `status='success'`, so the guard evaluated them as non-escalated. They were invisible to the Recovery Analysis tab.

### How We Found It
I ran a direct SQL query on the database:

```sql
SELECT action_taken, status, COUNT(*)
FROM recovery_actions
GROUP BY action_taken, status;
```

That made the actual values obvious immediately. The issue wasn't in the query logic per se — it was in the assumption that `status='success'` means "payment recovered." It doesn't. It means "agent completed without error."

### Fix Applied
Changed the escalation detection to use `action_taken` directly, independent of `status`:

```python
is_escalated = any(a.action_taken == "escalate" for a in actions)
```

This is semantically correct: an event is escalated if the agent chose `escalate` as the action, regardless of whether that escalation dispatched cleanly or not. After the fix, the Recovery Analysis tab and the Exception Report tab agreed: 5 bank_downtime escalations.

### Lesson for Fintech Systems
Status columns in financial systems are dangerously overloaded. `"success"` might mean "transaction completed", "request dispatched", "record written", or "no exception thrown" — and those are very different things. Aggregation logic must be verified against **raw DB state**, not inferred from a status column whose semantics you haven't explicitly checked. The fastest path to the truth is always a direct SQL query; don't trust the application-layer interpretation until you've seen the actual values. In a real system, this kind of ambiguity would warrant a dedicated `recovery_outcome` column that is strictly about the financial result, separate from the `status` of the agent's execution.

---

*Total time lost to these two bugs: roughly 4 hours. Both were found within minutes once I stopped trusting the abstraction and looked at the raw data.*
