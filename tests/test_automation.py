#!/usr/bin/env python3
"""
Test the automated trading system components.
"""

import sys
import os
import subprocess
from datetime import datetime

# Project root is one level up from tests/
ROOT_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

def run_test(script_name, description):
    """Run a test script and report results"""
    print(f"\n🧪 Testing: {description}")
    print(f"   Script: {script_name}")
    print("   " + "-"*40)

    start_time = datetime.now()

    try:
        # Run script from project root so relative data file paths work
        result = subprocess.run(
            ["python3", script_name],
            capture_output=True,
            text=True,
            timeout=60,
            cwd=ROOT_DIR
        )

        elapsed = (datetime.now() - start_time).total_seconds()

        if result.returncode == 0:
            print(f"   ✅ SUCCESS (took {elapsed:.1f}s)")
            # Show last few lines of output
            lines = result.stdout.strip().split('\n')
            if len(lines) > 5:
                print("   Last 5 lines of output:")
                for line in lines[-5:]:
                    print(f"     {line}")
            else:
                for line in lines:
                    print(f"     {line}")
        else:
            print(f"   ❌ FAILED (code: {result.returncode}, took {elapsed:.1f}s)")
            print(f"   Stderr: {result.stderr[:200]}...")

        return result.returncode == 0

    except subprocess.TimeoutExpired:
        print(f"   ⏰ TIMEOUT (60 seconds)")
        return False
    except Exception as e:
        print(f"   💥 ERROR: {e}")
        return False

def main():
    """Run all tests"""
    print("="*60)
    print("AUTOMATED TRADING SYSTEM - COMPONENT TESTS")
    print("="*60)

    tests = [
        ("automation/auto_research.py", "Market Research Script"),
        ("automation/auto_trader.py", "Auto Trading Script"),
        ("automation/daily_summary.py", "Daily Summary Script"),
        ("automation/setup_cron_jobs.py", "Cron Job Setup Script"),
        ("scripts/resolve_positions.py", "Position Resolution Script"),
    ]

    results = []

    for script, description in tests:
        script_path = os.path.join(ROOT_DIR, script)
        if os.path.exists(script_path):
            success = run_test(script, description)
            results.append((script, success))
        else:
            print(f"\n❌ Script not found: {script}")
            results.append((script, False))

    # Summary
    print("\n" + "="*60)
    print("TEST SUMMARY")
    print("="*60)

    total = len(results)
    passed = sum(1 for _, success in results if success)

    print(f"Total tests: {total}")
    print(f"Passed: {passed}")
    print(f"Failed: {total - passed}")

    if passed == total:
        print("\n🎉 ALL TESTS PASSED! Ready for cron job setup.")
        print("\nNext steps:")
        print("1. Review the cron job config: automation/cron_jobs_config.json")
        print("2. Create cron jobs: openclaw cron add --file automation/cron_jobs_config.json")
        print("3. Verify: openclaw cron list")
        print("4. Monitor: tail -f ~/.openclaw/logs/gateway.log")
    else:
        print("\n⚠️  SOME TESTS FAILED. Review errors before setting up cron jobs.")

    return 0 if passed == total else 1

if __name__ == "__main__":
    sys.exit(main())
