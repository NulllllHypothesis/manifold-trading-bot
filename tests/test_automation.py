#!/usr/bin/env python3
"""
Test the automated trading system components.
"""

import sys
import os
import subprocess
from datetime import datetime
from unittest.mock import patch, MagicMock

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


def test_auto_resolve_markets_called():
    """
    Unit test: verify that resolve_positions.py invokes
    PaperTrader.auto_resolve_markets() exactly once.
    """
    print("\n🧪 Testing: auto_resolve_markets() is called exactly once")
    print("   " + "-"*40)

    script_path = os.path.join(ROOT_DIR, "scripts", "resolve_positions.py")
    if not os.path.exists(script_path):
        print("   ❌ SKIP — scripts/resolve_positions.py not found")
        return False

    # Insert project root into path so we can import the script's module context
    if ROOT_DIR not in sys.path:
        sys.path.insert(0, ROOT_DIR)

    mock_trader_instance = MagicMock()
    # Give the mock a real positions dict with one open position so main()
    # doesn't short-circuit with "No open positions to check." before calling
    # auto_resolve_markets(). Also disable the count_open_positions helper so
    # the script falls back to its local counter (simpler, no cross-check assert).
    mock_trader_instance.positions = {"test_market": [{"status": "OPEN"}]}
    del mock_trader_instance.count_open_positions  # force fallback to local counter
    mock_trader_instance.balance = 1000.0
    mock_trader_instance.auto_resolve_markets.return_value = 0

    try:
        with patch.dict("sys.modules", {}):
            # Patch PaperTrader wherever it is imported inside the script
            mock_paper_trader_cls = MagicMock(return_value=mock_trader_instance)

            # We need to discover the import name used in the script
            with open(script_path, "r") as fh:
                source = fh.read()

            # Determine the module path used for PaperTrader
            import re
            match = re.search(r"from\s+([\w.]+)\s+import\s+.*PaperTrader", source)
            if not match:
                match = re.search(r"import\s+([\w.]+\.PaperTrader)", source)

            if not match:
                print("   ⚠️  Could not locate PaperTrader import in script; skipping patch")
                return False

            module_path = match.group(1)

            with patch(f"{module_path}.PaperTrader", mock_paper_trader_cls):
                import importlib.util
                spec = importlib.util.spec_from_file_location("resolve_positions", script_path)
                mod = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(mod)
                # exec_module only defines functions; main() must be called explicitly
                # because the if __name__ == "__main__" guard does not fire here.
                try:
                    mod.main()
                except SystemExit:
                    pass

        call_count = mock_trader_instance.auto_resolve_markets.call_count
        if call_count == 1:
            print("   ✅ SUCCESS — auto_resolve_markets() called exactly once")
            return True
        else:
            print(f"   ❌ FAILED — auto_resolve_markets() called {call_count} time(s), expected 1")
            return False

    except Exception as e:
        print(f"   💥 ERROR during unit test: {e}")
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

    # Unit test: auto_resolve_markets() invocation
    unit_success = test_auto_resolve_markets_called()
    results.append(("unit:auto_resolve_markets_called", unit_success))

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
