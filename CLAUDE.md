# Working rules for every agent (main session and subagents)

Follow the `opus-5-on-steriod-v5` skill (claude.ai account skill; load it with the Skill tool
when available). Run at medium effort. In short:

- Size the task first (question, trivial, standard, deliverable, large) and stay inside its budget.
- Orient cheaply: grep before reading, read regions not whole files, batch independent calls.
- Change the minimum: fix the cause, no drive-by refactors, match surrounding style.
- Verify once (the test or command that proves it), then report what it showed. No second pass.
- Don't spawn subagents to verify or several for one small job.
- Reply leads with the outcome, short, evidence included, no closing offer.
