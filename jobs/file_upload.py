#!/usr/bin/env python3
"""Webhook job that accepts uploaded files and stores them in files/.

Send a POST with ``Content-Type: multipart/form-data`` and one or more file
parts (photos, videos, documents — any type). Files land in the gitignored
``files/`` folder at the repo root, each saved under a timestamped, collision-
safe name.

This job reads ``request.files`` directly from Flask's request context, so the
generic ``run(payload)`` contract is kept (``payload`` is unused here). Form
fields, if any, are available via ``request.form``.

From an iPhone Shortcut, use "Get Contents of URL" with Method POST, a
``X-Webhook-Secret`` header, and Request Body set to "Form" with a File field.
See Uploads.md for step-by-step instructions.
"""

import datetime
from pathlib import Path

from flask import request
from werkzeug.utils import secure_filename

try:
    # Audit trail via the logging engine.
    from jobs.log_engine import log as write_log
except ImportError:  # pragma: no cover - allows running this file directly
    from log_engine import log as write_log

# Repo root is the parent of the jobs/ folder that holds this module.
REPO_ROOT = Path(__file__).resolve().parent.parent
FILES_DIR = REPO_ROOT / "files"


def _unique_name(original, index=0):
    """Build a timestamped, sanitized, collision-safe filename."""
    name = secure_filename(original or "") or "upload"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    prefix = f"{stamp}-{index}" if index else stamp
    return f"{prefix}_{name}"


def run(payload):
    # Files arrive as multipart/form-data parts on request.files. Group by
    # field name so multiple files (even under the same field) are all kept.
    grouped = request.files.to_dict(flat=False)
    all_files = [f for flist in grouped.values() for f in flist]
    all_files = [f for f in all_files if f and f.filename]  # drop empty parts

    if not all_files:
        return {
            "error": "no files",
            "message": "Send one or more files as multipart/form-data (a Form body).",
        }

    FILES_DIR.mkdir(exist_ok=True)

    saved = []
    for index, storage in enumerate(all_files):
        stored_as = _unique_name(storage.filename, index)
        dest = FILES_DIR / stored_as
        storage.save(dest)
        saved.append({
            "stored_as": stored_as,
            "original": storage.filename,
            "bytes": dest.stat().st_size,
        })

    write_log("default", f"Received {len(saved)} file(s): "
              + ", ".join(s["stored_as"] for s in saved))

    return {"status": "ok", "count": len(saved), "files": saved}


if __name__ == "__main__":
    print("This job stores uploads in:", FILES_DIR)
    print("POST files as multipart/form-data to /webhook/upload. See Uploads.md.")
