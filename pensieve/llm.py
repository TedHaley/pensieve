import json

import httpx

from . import settings


def complete(prompt: str, system: str = "", max_tokens: int = 200, temperature: float = 0.2, schema: dict | None = None):
    """One-shot completion. With `schema`, LM Studio constrains output to that JSON schema and we return the parsed object."""
    msgs = ([{"role": "system", "content": system}] if system else []) + [{"role": "user", "content": prompt}]
    body = {"model": settings.get("llm_model"), "messages": msgs, "max_tokens": max_tokens, "temperature": temperature,
            "reasoning_effort": "none"}
    if schema:
        body["response_format"] = {"type": "json_schema", "json_schema": {"name": "out", "strict": True, "schema": schema}}
    r = httpx.post(f"{settings.get('llm_url')}/chat/completions", timeout=600, json=body)
    r.raise_for_status()
    t = r.json()["choices"][0]["message"].get("content") or ""
    if "</think>" in t:
        t = t.split("</think>", 1)[1]
    t = t.strip()
    if schema:
        return json.loads(t[t.find("{"):t.rfind("}") + 1])
    return t
