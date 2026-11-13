#!/usr/bin/env python3
"""Write a file so that a crash never leaves half of it."""

import os
import threading
from pathlib import Path


def write_atomic(path: Path, text: str) -> None:
    """Write ``text`` to ``path`` via a temp file and a rename, creating parent folders.

    The temp name is unique per process and thread, so two writers racing on the same
    file never share one (and so never fail each other's rename)."""
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.{os.getpid()}.{threading.get_ident()}.tmp")
    try:
        tmp.write_text(text, encoding="utf-8")
        os.replace(tmp, path)
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
