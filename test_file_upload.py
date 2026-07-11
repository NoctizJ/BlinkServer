#!/usr/bin/env python3
"""Simple tests for the file-upload job (jobs/file_upload.py).

Requires Flask (run inside your venv):

    python3 test_file_upload.py

Each test points the job at a fresh temp files/ dir, so nothing is written to
the repo. A valid webhook_secret.json must exist (the endpoint requires it).
"""

import io
import json
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

import app
import jobs.file_upload as file_upload


def _secret():
    with open("webhook_secret.json") as f:
        return json.load(f)["WEBHOOK_SECRET"]


def _fresh_files_dir():
    tmp = Path(tempfile.mkdtemp(prefix="blink_upload_test_"))
    file_upload.FILES_DIR = tmp
    return tmp


def test_upload_saves_file():
    """A posted file is stored in files/ with its bytes intact."""
    tmp = _fresh_files_dir()
    client = app.app.test_client()
    resp = client.post(
        "/webhook/upload",
        data={"file": (io.BytesIO(b"hello world"), "photo.jpg")},
        headers={"X-Webhook-Secret": _secret()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    result = resp.get_json()["result"]
    assert result["count"] == 1
    stored = list(tmp.iterdir())
    assert len(stored) == 1
    assert stored[0].read_bytes() == b"hello world"
    assert stored[0].name.endswith("_photo.jpg")
    print("✅ test_upload_saves_file:", result["files"][0]["stored_as"])


def test_multiple_files():
    """Multiple files under the same field name are all saved."""
    tmp = _fresh_files_dir()
    client = app.app.test_client()
    resp = client.post(
        "/webhook/upload",
        data={"file": [
            (io.BytesIO(b"one"), "a.txt"),
            (io.BytesIO(b"two"), "b.txt"),
        ]},
        headers={"X-Webhook-Secret": _secret()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["result"]["count"] == 2
    assert len(list(tmp.iterdir())) == 2
    print("✅ test_multiple_files")


def test_requires_secret():
    """No secret -> 401 and nothing is written."""
    tmp = _fresh_files_dir()
    client = app.app.test_client()
    resp = client.post(
        "/webhook/upload",
        data={"file": (io.BytesIO(b"x"), "x.txt")},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 401, resp.get_json()
    assert list(tmp.iterdir()) == []
    print("✅ test_requires_secret")


def test_no_files():
    """A request with no file parts returns a clear error, saves nothing."""
    tmp = _fresh_files_dir()
    client = app.app.test_client()
    resp = client.post(
        "/webhook/upload",
        data={},
        headers={"X-Webhook-Secret": _secret()},
        content_type="multipart/form-data",
    )
    assert resp.status_code == 200, resp.get_json()
    assert resp.get_json()["result"]["error"] == "no files"
    assert list(tmp.iterdir()) == []
    print("✅ test_no_files")


def main():
    tests = [
        test_upload_saves_file,
        test_multiple_files,
        test_requires_secret,
        test_no_files,
    ]
    for t in tests:
        t()
    print(f"\nAll {len(tests)} file upload tests passed ✅")


if __name__ == "__main__":
    main()
