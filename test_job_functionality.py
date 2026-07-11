#!/usr/bin/env python3
"""Test script for Blink job functionality."""

import json
import sys
import os

# Add the jobs directory to the path so we can import the module
sys.path.insert(0, os.path.join(os.path.dirname(__file__), 'jobs'))

def test_blink_arm_disarm_import():
    """Test that blink_arm_disarm module can be imported."""
    try:
        from blink_arm_disarm import run
        print("✅ blink_arm_disarm module imports successfully")
        return True
    except Exception as e:
        print(f"❌ Failed to import blink_arm_disarm: {e}")
        return False

def test_payload_validation():
    """Test payload validation logic."""
    try:
        from blink_arm_disarm import run

        # Test invalid payload (not a dict)
        result = run("invalid")
        assert "error" in result
        print("✅ Invalid payload handled correctly")

        # Test missing action
        result = run({})
        assert "error" in result
        print("✅ Missing action handled correctly")

        # Test invalid action
        result = run({"action": "invalid"})
        assert "error" in result
        print("✅ Invalid action handled correctly")

        return True
    except Exception as e:
        print(f"❌ Payload validation test failed: {e}")
        return False

def test_credential_loading():
    """Test credential loading functionality."""
    try:
        from blink_arm_disarm import load_credentials

        username, password = load_credentials()
        print(f"✅ Credentials loaded - Username: {username}")

        # This should work even without valid credentials (for testing)
        return True
    except Exception as e:
        print(f"❌ Credential loading failed: {e}")
        return False

def main():
    """Run all tests."""
    print("Testing Blink job functionality...")
    print("=" * 50)

    tests = [
        test_blink_arm_disarm_import,
        test_payload_validation,
        test_credential_loading
    ]

    passed = 0
    total = len(tests)

    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"❌ Test {test.__name__} failed with exception: {e}")

    print("=" * 50)
    print(f"Tests passed: {passed}/{total}")

    if passed == total:
        print("✅ All tests passed!")
        return 0
    else:
        print("❌ Some tests failed!")
        return 1

if __name__ == "__main__":
    sys.exit(main())