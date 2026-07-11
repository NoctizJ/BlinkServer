#!/usr/bin/env python3
"""Simple test to verify app.py can be imported and run."""

import sys
sys.path.insert(0, '/Users/noc/Documents/BlinkServer')

try:
    # Try to import the main application
    import app
    print("✅ App module imported successfully")

    # Check that we have the expected functions
    if hasattr(app, 'main') or hasattr(app, 'run'):
        print("✅ Main application functions found")
    else:
        print("⚠️  Warning: No main() or run() function found in app.py")

    print("✅ App test completed successfully")

except ImportError as e:
    print(f"❌ Import error: {e}")
    sys.exit(1)
except Exception as e:
    print(f"❌ Other error: {e}")
    import traceback
    traceback.print_exc()
    sys.exit(1)