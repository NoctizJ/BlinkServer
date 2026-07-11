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

if __name__ == "__main__":
    test_job_config()