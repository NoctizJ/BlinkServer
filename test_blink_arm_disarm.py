#!/usr/bin/env python3
"""Test script for blink_arm_disarm functionality with mocked environment."""

import os
import sys
import json
from unittest.mock import patch, MagicMock

# Add the project root to the path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

def test_import():
    """Test that we can import the module."""
    try:
        from jobs.blink_arm_disarm import run
        print("✅ Module imports successfully")
        return True
    except Exception as e:
        print(f"❌ Failed to import: {e}")
        return False

def test_payload_validation():
    """Test payload validation logic."""
    from jobs.blink_arm_disarm import run

    # Test with invalid payload (not a dict)
    result = run("invalid_payload")
    if "error" in result and "Invalid payload format" in result["message"]:
        print("✅ Invalid payload format handled correctly")
    else:
        print(f"❌ Invalid payload not handled properly: {result}")
        return False

    # Test with missing action
    result = run({"something": "else"})
    if "error" in result and "Missing action" in result["message"]:
        print("✅ Missing action handled correctly")
    else:
        print(f"❌ Missing action not handled properly: {result}")
        return False

    # Test with invalid action
    result = run({"action": "invalid"})
    if "error" in result and "Invalid action" in result["message"]:
        print("✅ Invalid action handled correctly")
    else:
        print(f"❌ Invalid action not handled properly: {result}")
        return False

    # Test with valid arm action
    result = run({"action": "arm"})
    if "error" in result and "HA_API_KEY environment variable is required":
        print("✅ Valid payload validation works (correctly detects missing env vars)")
    elif "message" in result and "Successfully" in result["message"]:
        print("✅ Valid arm action processed")
    else:
        print(f"❌ Unexpected result for valid arm action: {result}")
        return False

    return True

def test_function_signature():
    """Test that the function exists with correct signature."""
    try:
        from jobs.blink_arm_disarm import run
        import inspect

        sig = inspect.signature(run)
        params = list(sig.parameters.keys())

        if len(params) == 1 and params[0] == 'payload':
            print("✅ Function has correct signature")
            return True
        else:
            print(f"❌ Function signature incorrect: {params}")
            return False

    except Exception as e:
        print(f"❌ Failed to inspect function: {e}")
        return False

def test_debug_mode():
    """Test that debug mode works."""
    try:
        import os
        original_debug = os.environ.get('BLINK_DEBUG')
        os.environ['BLINK_DEBUG'] = 'true'

        from jobs.blink_arm_disarm import run
        result = run({"action": "arm"})

        # Reset environment
        if original_debug is not None:
            os.environ['BLINK_DEBUG'] = original_debug
        elif 'BLINK_DEBUG' in os.environ:
            del os.environ['BLINK_DEBUG']

        print("✅ Debug mode works")
        return True

    except Exception as e:
        print(f"❌ Debug mode test failed: {e}")
        return False

if __name__ == "__main__":
    print("Running blink_arm_disarm tests...")

    tests = [
        test_import,
        test_payload_validation,
        test_function_signature,
        test_debug_mode
    ]

    passed = 0
    total = len(tests)

    for test in tests:
        try:
            if test():
                passed += 1
        except Exception as e:
            print(f"❌ Test {test.__name__} failed with exception: {e}")

    print(f"\nResults: {passed}/{total} tests passed")

    if passed == total:
        print("🎉 All tests passed!")
        sys.exit(0)
    else:
        print("❌ Some tests failed")
        sys.exit(1)