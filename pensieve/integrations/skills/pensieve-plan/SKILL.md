---
name: pensieve-plan
description: Ground a plan for new code in what already exists and who knows it, using Pensieve. Use when planning a feature, refactor or new module, writing a design doc, or before starting a large change.
---

# Planning with prior art and expertise

Before proposing a design, spend a few Pensieve calls finding what already exists and who has context. Plans that
reuse existing patterns and name the right people get built faster and reviewed easier.

## Steps

1. **Prior art.** `search` for the capability in plain words, and for any names you expect it to need. On the best
   hits call `similar(id)` to find related implementations across the repo (and other indexed repos).
2. **Earlier attempts.** `search` with `kind:session` for previous agent sessions on the topic: decisions, dead
   ends, and why they were abandoned.
3. **Expertise.** `who_knows("<capability>", repo=...)` for the people who wrote the closest code; `team(repo)` to
   check they're still active; `unexplored(repo)` for nearby areas the user hasn't worked in yet.
4. **Risk.** `knowledge_gaps(repo)` flags areas the change touches whose authors have left.

## Put it in the plan

Add these sections (keep each short):

- **Prior art:** files/functions to reuse or follow, with paths; near-duplicates to avoid.
- **Earlier attempts:** what was tried before and what was learned (link the session if useful).
- **People to loop in:** one or two names, each with why (the code they wrote) and the question to ask them.
- **Knowledge risks:** areas with a single remaining expert or none; whether to document as part of the work.

Suggest conversations; never contact anyone yourself.
