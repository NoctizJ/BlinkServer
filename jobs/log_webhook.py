#!/usr/bin/env python3
"""Webhook job that writes an entry to the log via the logging engine.

Expects a JSON payload of the form::

    {
        "type": "blink",              # optional — may be null / omitted
        "text": "the message to log"  # required
    }

If ``type`` is null/omitted the logging engine uses its default type. The
actual write is still gated by the master ``"log"`` switch (job_config.json)
and the per-type switch (log_config.json), so a log may be suppressed even
when this webhook is enabled.
"""

try:
    # Normal import path when loaded as part of the jobs package.
    from jobs.log_engine import log as write_log
except ImportError:  # pragma: no cover - allows running this file directly
    from log_engine import log as write_log


def run(payload):
    if not isinstance(payload, dict):
        return {
            "error": "Invalid payload format",
            "message": "Payload must be a JSON object",
        }

    text = payload.get("text")
    if text is None:
        return {
            "error": "Missing text",
            "message": "Payload must include a 'text' field",
        }

    # `type` is nullable — the engine falls back to its default type.
    log_type = payload.get("type")

    written = write_log(log_type, text)

    return {
        "status": "ok",
        "written": written,
        "type": log_type or "default",
        "message": (
            "log entry written"
            if written
            else "log suppressed (disabled by the master 'log' switch or the type switch)"
        ),
    }


if __name__ == "__main__":
    # Simple smoke test / demo.
    print("typed   ->", run({"type": "blink", "text": "Front door camera armed."}))
    print("default ->", run({"type": None, "text": "No type provided; used default."}))
    print("missing ->", run({"type": "blink"}))
