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
import mimetypes
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


def _unique_name(storage, index=0):
    """Build a timestamped, sanitized, collision-safe filename for an upload.

    Some clients (notably iPhone Shortcuts) send a file part with no filename;
    in that case synthesize one and infer the extension from the MIME type.
    """
    name = secure_filename(storage.filename or "")
    if not name:
        ext = mimetypes.guess_extension(storage.mimetype or "") or ""
        name = f"upload{ext}"
    stamp = datetime.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    prefix = f"{stamp}-{index}" if index else stamp
    return f"{prefix}_{name}"


def _has_content(storage):
    """True if a file part carries any bytes (peeks without consuming it)."""
    stream = storage.stream
    pos = stream.tell()
    stream.seek(0, 2)  # seek to end
    size = stream.tell()
    stream.seek(pos)   # restore position for the later save()
    return size > 0


def run(payload):
    # Files arrive as multipart/form-data parts on request.files. Group by
    # field name so multiple files (even under the same field) are all kept.
    #
    # NOTE: FileStorage is "falsy" when it has no filename (its __bool__ is
    # bool(filename)), so we must test `is not None` — not `if f`. iPhone
    # Shortcuts frequently send a file part with an empty filename; we accept
    # any part that has a filename OR actual content, and drop only truly empty
    # (no-file-selected) parts.
    grouped = request.files.to_dict(flat=False)
    all_files = [
        f for flist in grouped.values() for f in flist
        if f is not None and (f.filename or _has_content(f))
    ]

    if not all_files:
        # Nothing landed in request.files. Echo back what *did* arrive so the
        # sender can tell whether the field was a File field in a Form body.
        return {
            "error": "no files",
            "message": ("No file parts received. In the iPhone Shortcut, set "
                        "Request Body to 'Form' and add a field of type 'File' "
                        "(not Text), then pick your photo/video as its value."),
            "debug": {
                "content_type": request.content_type,
                "file_fields": list(request.files.keys()),
                "form_fields": list(request.form.keys()),
            },
        }

    FILES_DIR.mkdir(exist_ok=True)

    saved = []
    for index, storage in enumerate(all_files):
        stored_as = _unique_name(storage, index)
        dest = FILES_DIR / stored_as
        storage.save(dest)
        saved.append({
            "stored_as": stored_as,
            "original": storage.filename or None,
            "bytes": dest.stat().st_size,
        })

    write_log("default", f"Received {len(saved)} file(s): "
              + ", ".join(s["stored_as"] for s in saved))

    return {"status": "ok", "count": len(saved), "files": saved}


if __name__ == "__main__":
    print("This job stores uploads in:", FILES_DIR)
    print("POST files as multipart/form-data to /webhook/upload. See Uploads.md.")
