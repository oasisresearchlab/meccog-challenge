"""One place that calls Claude: structured JSON output, adaptive thinking, refusal fallback.

Credentials resolve the SDK's usual way: ANTHROPIC_API_KEY, or a profile from `ant auth login`.
"""

from __future__ import annotations

import base64
import json
import os
from pathlib import Path

EXTRACT_MODEL = os.environ.get("MECCOG_EXTRACT_MODEL", "claude-opus-5-5")
SCREEN_MODEL = os.environ.get("MECCOG_SCREEN_MODEL", "claude-opus-5-5")

_client = None


def client():
    global _client
    if _client is None:
        import anthropic
        _client = anthropic.Anthropic(max_retries=4)
    return _client


def pdf_block(pdf: Path) -> dict:
    return {"type": "document",
            "source": {"type": "base64", "media_type": "application/pdf",
                       "data": base64.standard_b64encode(pdf.read_bytes()).decode()}}


def text_block(text: str, title: str = "source") -> dict:
    return {"type": "document", "title": title,
            "source": {"type": "text", "media_type": "text/plain", "data": text}}


def json_call(system: str, content: list[dict], schema: dict, *, model: str, effort: str,
              max_tokens: int = 64000) -> tuple[dict, dict]:
    """Return (parsed JSON, usage). Raises on refusal or truncation rather than returning partial JSON."""
    with client().beta.messages.stream(
        model=model,
        max_tokens=max_tokens,
        system=system,
        thinking={"type": "adaptive"},
        output_config={"effort": effort, "format": {"type": "json_schema", "schema": schema}},
        # On a safety decline (the bio classifier can fire on disease-mechanism papers),
        # re-run on Anthropic's recommended fallback model instead of failing the paper.
        betas=["server-side-fallback-2026-07-01"],
        fallbacks="default",
        messages=[{"role": "user", "content": content}],
    ) as stream:
        msg = stream.get_final_message()
    if msg.stop_reason == "refusal":
        raise RuntimeError(f"model declined: {getattr(msg.stop_details, 'explanation', '')}")
    if msg.stop_reason == "max_tokens":
        raise RuntimeError("output hit max_tokens; the JSON is truncated")
    text = next(b.text for b in msg.content if b.type == "text")
    usage = {"model": msg.model, "input_tokens": msg.usage.input_tokens,
             "output_tokens": msg.usage.output_tokens}
    return json.loads(text), usage
