---
name: pensieve-search
description: Find code, docs or past agent sessions fast with Pensieve's MCP search instead of chains of grep. Use whenever you need to locate where something is implemented, find code similar to what you're about to write, look up an error message or identifier, or recall what an earlier session decided.
---

# Finding things with Pensieve

Pensieve's `search` ranks a whole repo by meaning, keywords and exact names in one call. On its benchmark the
right file is the first result for ~96% of error strings, ~92% of identifiers and ~67% of plain-English
descriptions, and agents using it solve more lookups with ~40% fewer tool calls than grep-first searching.

## Locate code

1. Call `search` with the request as you understand it. Good queries:
   - what it does: `where do we refresh expired OAuth tokens`
   - a name: `fetchCompanies`, `build_unified_record`
   - an error or log line, unquoted: `Entity value still exists`
   - a mix: `fetchCompanies retry on 429`
2. Open the top result's `path` at its `line` with your normal file reader. Check the next two or three results only
   if the first doesn't fit; they're usually siblings (a test, a twin `person_`/`company_` file).
3. If nothing fits, rephrase once in the codebase's vocabulary (names you saw in the results), then fall back to
   Grep. Don't run more than two searches for the same thing.

Filters: `kind:code` (just code), `kind:file` (documents), `kind:session` (agent history), `in:<repo or folder>`,
`ext:<extension>`, `-word` to exclude, `"exact phrase"` to require text.

## Use Grep for

- every usage of a name you already know (`search` returns the best files, not an exhaustive list);
- confirming an exact string before an edit.

## Before writing new code

`search` for code that already does something similar, then `similar(id)` on the best hit. Reuse its
helpers and conventions; mention near-duplicates you found rather than adding another.

## Recall earlier work

`search` with `kind:session` finds earlier agent sessions on a topic; `read(id)` shows the transcript and summary.
Use it when the user says "like we did before" or "what did we decide about …".
