#!/usr/bin/env python3
"""Test script for Home Assistant integration."""

import os
import sys
import json

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from jobs.blink_arm_disarm import run

def test_arm_functionality():
    """Test arm functionality."""
    print("Testing arm functionality...")

    # Test payload
    payload = {
        "action": "arm"
    }

    result = run(payload)
    print(f"Arm result: {json.dumps(result, indent=2)}")

    return result

def test_disarm_functionality():
    """Test disarm functionality."""
    print("\nTesting disarm functionality...")

    # Test payload
    payload = {
        "action": "disarm"
    }

    result = run(payload)
    print(f"Disarm result: {json.dumps(result, indent=2)}")

    return result

def test_invalid_payload():
    """Test invalid payload."""
    print("\nTesting invalid payload...")

    # Test with no action
    payload = {
        "something": "else"
    }

    result = run(payload)
    print(f"Invalid payload result: {json.dumps(result, indent=2)}")

    return result

def test_invalid_action():
    """Test invalid action."""
    print("\nTesting invalid action...")

    # Test with invalid action
    payload = {
        "action": "invalid"
    }

    result = run(payload)
    print(f"Invalid action result: {json.dumps(result, indent=2)}")

    return result

if __name__ == "__main__":
    print("Running Home Assistant integration tests...")

    # Test all scenarios
    test_arm_functionality()
    test_disarm_functionality()
    test_invalid_payload()
    test_invalid_action()

    print("\nTests completed.")