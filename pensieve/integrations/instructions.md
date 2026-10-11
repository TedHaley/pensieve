## Pensieve: search before you grep

Pensieve (MCP: `search`, `read`, `similar`, `who_knows`, …) indexes this machine's git repos, documents and past
agent sessions, scoped to the repo you started in.

- **To find code, call Pensieve `search` first, before Grep/Glob/find.** Write what the code does plus the names you
  would guess it uses: "retry failed webhook delivery with backoff retryWebhook". A bare name or an error message
  works too; "double quotes" force exact text, and `kind:code`, `in:<folder>`, `ext:py` narrow it.
- Each hit shows `path:line`, the enclosing definition and the line that matched. If one hit clearly shows what you
  need, use that file: don't grep to confirm. If two hits both fit (say, one defines a name and one uses it), read
  both before you choose. Grep afterwards only to list every usage of a name you already know.
- **Before writing new code,** search for code that already does something similar and call `similar(id)` on the
  best hit: reuse its patterns rather than add a second copy.
- **"What did we decide / try before?"** Search `kind:session`. **Who knows this area?** `who_knows("<topic>")`.
