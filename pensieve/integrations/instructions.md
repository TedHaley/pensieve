## Pensieve: search before you grep

Pensieve is connected as an MCP server (`search`, `read`, `similar`, `who_knows`, …). It indexes this machine's git
repos, documents and past agent sessions, and is scoped to the repo you started in.

- **Finding code: call Pensieve `search` first, before Grep/Glob/find.** One call ranks the repo by meaning,
  keywords and exact names together, and the right file is usually the first result. Then open that file directly.
  - Any of these work: a description ("where do we retry failed webhooks"), a name (`fetchCompanies`), an error
    message, or a mix ("fetchCompanies retry on 429"). Narrow with `kind:code`, `in:<repo or folder>`, `ext:py`;
    "double quotes" force exact text.
  - Use Grep afterwards only to list every usage of a name you already know, or to confirm.
- **Before writing new code,** search for code that already does something similar and call `similar(id)` on the
  best hit: reuse its patterns, and don't add a second copy.
- **"What did we decide / try before?"** Search `kind:session` for earlier agent sessions on the topic.
- **Who knows this area?** `who_knows("<topic>")` names the people who wrote the related code.
