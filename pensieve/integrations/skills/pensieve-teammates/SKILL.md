---
name: pensieve-teammates
description: Suggest which teammates to talk to, using Pensieve's git-blame knowledge map. Use when the user is stuck in unfamiliar code, asks who owns or knows an area, is about to change code mostly written by others, or wants a reviewer.
---

# Connecting with the people who know the code

Pensieve knows who wrote which code (git blame weighted by relevance), who is still active, and which areas only
one person understands. Use it to point the user to people, not to replace them.

## Who knows this?

1. `who_knows("<topic in plain words>", repo="<repo>")`: people ranked by how much of the related code they
   wrote, with the files involved.
2. Prefer people still active in the repo (`team(repo)` lists recent committers and their areas). If the top
   author has left, `knowledge_gaps(repo)` gives an `ask` list of who still knows it best.
3. Tell the user, briefly: who, why (the files or areas they wrote), and what to ask them.

## Draft the question, don't send it

Offer a short message the user can send themselves:

- the context in one sentence (what they're changing and why),
- the specific question (the decision, invariant or history they need),
- the file(s) and line(s) it's about.

Never message, tag or email anyone yourself unless the user explicitly asks you to send it.

## When to bring it up unprompted

- The change touches an area where `knowledge_gaps` shows a bus factor of 1, or the main authors are inactive:
  suggest documenting what you learn, or looping in the remaining expert.
- The user has spent a while reverse-engineering code that an active teammate wrote: a five-minute conversation is
  often cheaper; say so once.
- A plan or PR changes shared code: name one or two natural reviewers from `who_knows`.
