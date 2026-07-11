#!/usr/bin/env python3
"""Test script for job management functionality."""

import json
import os
import sys
from pathlib import Path

# Add the project root to the path
sys.path.insert(0, str(Path(__file__).parent))

from app import load_job_config, save_job_config, get_job_enabled_status

def test_job_config():
    """Test job configuration management."""
    print("Testing job configuration system...")

    # Load current config
    config = load_job_config()
    print(f"Current config: {config}")

    # Test getting status of non-existent job (should default to True)
    status = get_job_enabled_status("non_existent_job")
    print(f"Status of non-existent job: {status}")

    # Test setting a job as disabled
    config["jobs"]["sample_job"] = False
    save_job_config(config)

    # Verify it was saved
    new_config = load_job_config()
    print(f"Updated config: {new_config}")

    # Check the status of sample_job
    status = get_job_enabled_status("sample_job")
    print(f"Status of sample_job after disabling: {status}")

    # Test enabling it again
    config["jobs"]["sample_job"] = True
    save_job_config(config)

    status = get_job_enabled_status("sample_job")
    print(f"Status of sample_job after enabling: {status}")

    print("Job configuration test completed successfully!")


def test_unknown_job_rejected():
    """Regression: management endpoints must not create phantom job entries.

    A stale caller hitting /jobs/arm/toggle used to silently add an "arm"
    entry to job_config.json. Unknown jobs should now return 404 and change
    nothing. (Requires Flask and webhook_secret.json.)
    """
    from app import app as flask_app

    print("\nTesting unknown-job rejection...")
    secret_path = Path(__file__).parent / "webhook_secret.json"
    with open(secret_path) as f:
        headers = {"X-Webhook-Secret": json.load(f)["WEBHOOK_SECRET"]}

    # Snapshot so this test leaves job_config.json untouched.
    before = load_job_config()

    client = flask_app.test_client()
    for path in ("/jobs/arm/toggle", "/jobs/arm/enable", "/jobs/arm/disable"):
        resp = client.post(path, headers=headers)
        assert resp.status_code == 404, f"{path} -> {resp.status_code} (expected 404)"

    after = load_job_config()
    assert "arm" not in after.get("jobs", {}), "phantom 'arm' entry was created!"

    save_job_config(before)  # restore, just in case
    print("Unknown-job rejection test passed!")


if __name__ == "__main__":
    test_job_config()
    test_unknown_job_rejected()