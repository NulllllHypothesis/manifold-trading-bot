#!/usr/bin/env python3
"""
Setup cron jobs for automated trading system.
This creates OpenClaw cron jobs for:
1. Hourly market research (swap check runs inside this, not as a separate job)
2. Hourly position resolution (after research, before trading)
3. Hourly trading (after research and resolution)
4. Daily summary report
5. Weekly calibration harvest
6. Weekly EV accuracy report

Schedule per hour:
  :00 - Market Research (includes swap check at end if positions full)
  :10 - Position Resolution (frees slots before trading)
  :20 - Auto Trading (uses freed slots from resolution)
  19:00 daily  - Daily Summary Report
  Sun 02:00    - Weekly Calibration Harvest
  Mon 07:00    - Weekly EV Accuracy Report
"""

import sys
import os
from datetime import datetime, timedelta
import json

# Telegram channel ID loaded from environment variable.
# Set TELEGRAM_CHANNEL_ID in your environment or .env file.
# See .env.example for documentation.
TELEGRAM_CHANNEL_ID = os.environ.get('TELEGRAM_CHANNEL_ID', '-5240775171')

def create_cron_jobs():
    """Create OpenClaw cron jobs for automated trading"""

    jobs = []

    # Job 1: Hourly Market Research (at :00 of every hour)
    # An LLM agent is intentionally used here because auto_research.py produces
    # free-form analysis that benefits from natural language reasoning and
    # summarisation before results are stored for the trader.
    research_job = {
        "name": "Manifold Market Research",
        "schedule": {
            "kind": "cron",
            "expr": "0 * * * *",  # Every hour at minute 0
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Run hourly market research for Manifold trading bot. Execute: cd /home/hackathon/.openclaw/workspace && git fetch origin --quiet && git checkout main --quiet && git pull origin main --quiet && python3 automation/auto_research.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": TELEGRAM_CHANNEL_ID,
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("market-research-hourly", research_job))

    # Job 2: Position Resolution (at :10 of every hour, after research, before trading)
    # Runs at :10 to free up slots so they are available for trading at :20.
    #
    # NOTE: resolve_positions.py is a fully self-contained, deterministic script
    # that requires no LLM reasoning.  We therefore use a direct "exec" job so
    # that the command is run directly by the platform without invoking an LLM.
    # This avoids unnecessary latency, API cost, and the risk of a model
    # refusing or misinterpreting the instruction.
    #
    # If the platform does not support "exec" jobs and "agentTurn" is the only
    # available kind, replace the payload below with:
    #
    #   {
    #     "kind": "agentTurn",
    #     "systemPrompt": "You are a non-interactive shell executor. When given a
    #                      shell command you MUST run it exactly as written using
    #                      your bash tool and report its exit code. Do not
    #                      interpret, modify, or refuse the command.",
    #     "message": "Execute exactly: cd /home/hackathon/.openclaw/workspace && python3 scripts/resolve_positions.py",
    #     "model": "openai/gpt-4o-mini",   # cheaper/faster is fine for a pass-through
    #     "timeoutSeconds": 120
    #   }
    resolve_job = {
        "name": "Manifold Position Resolution",
        "schedule": {
            "kind": "cron",
            "expr": "10 * * * *",  # Every hour at minute 10
            "tz": "UTC"
        },
        "payload": {
            # "exec" runs the command directly without an LLM intermediary.
            # All resolution logic lives in resolve_positions.py; no model
            # reasoning is needed.
            "kind": "exec",
            "command": "bash",
            "args": [
                "-c",
                "cd /home/hackathon/.openclaw/workspace && git fetch origin --quiet && git checkout main --quiet && git pull origin main --quiet && python3 scripts/resolve_positions.py"
            ],
            "timeoutSeconds": 120
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": TELEGRAM_CHANNEL_ID,
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("position-resolution", resolve_job))

    # Job 3: Hourly Trading (at :20 of every hour, after research and resolution)
    # An LLM agent is intentionally used here because auto_trader.py relies on
    # the model's judgement to select and size trades from the research output.
    trading_job = {
        "name": "Manifold Auto Trading",
        "schedule": {
            "kind": "cron",
            "expr": "20 * * * *",  # Every hour at minute 20
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Execute automated trading based on latest research. Run: cd /home/hackathon/.openclaw/workspace && git fetch origin --quiet && git checkout main --quiet && git pull origin main --quiet && python3 automation/auto_trader.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": TELEGRAM_CHANNEL_ID,
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("auto-trading-hourly", trading_job))

    # Job 4: Daily Summary Report (at 19:00 UTC every day)
    # An LLM agent is intentionally used here because daily_summary.py produces
    # a human-readable narrative summary that benefits from natural language
    # generation before being posted to Telegram.
    summary_job = {
        "name": "Daily Trading Summary",
        "schedule": {
            "kind": "cron",
            "expr": "0 19 * * *",  # Daily at 19:00 UTC
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Generate and send daily trading summary to Telegram group. Execute: cd /home/hackathon/.openclaw/workspace && git fetch origin --quiet && git checkout main --quiet && git pull origin main --quiet && python3 automation/daily_summary.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": TELEGRAM_CHANNEL_ID,
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("daily-summary", summary_job))

    # NOTE: Position swap check is intentionally NOT a separate cron job.
    # run_swap_check() is called directly by auto_research.py at the end of
    # each hourly research run. This ensures swaps are only evaluated when
    # fresh opportunity data exists — not on a blind timer. The :40 standalone
    # cron was removed to prevent unnecessary API calls and Telegram noise.

    # Job 5: Weekly Calibration Harvest (Sundays at 02:00 UTC)
    # Fetches 2000+ resolved markets from Manifold and updates the calibration
    # table — giving the AI fresh crowd-bias corrections every week.
    harvest_job = {
        "name": "Weekly Calibration Harvest",
        "schedule": {
            "kind": "cron",
            "expr": "0 2 * * 0",   # Sundays at 02:00 UTC
            "tz": "UTC"
        },
        "payload": {
            "kind": "exec",
            "command": "bash",
            "args": [
                "-c",
                "cd /home/hackathon/.openclaw/workspace && git fetch origin --quiet && git checkout main --quiet && git pull origin main --quiet && python3 scripts/harvest_resolved.py --limit 2000 && python3 scripts/analyze_calibration.py"
            ],
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": TELEGRAM_CHANNEL_ID,
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("weekly-harvest", harvest_job))

    # Job 6: Weekly EV Accuracy Report (Mondays at 07:00 UTC)
    # Queries bet_outcomes table, computes estimated_ev vs actual_pnl,
    # and sends breakdown by strategy and confidence band to Telegram.
    ev_report_job = {
        "name": "Weekly EV Accuracy Report",
        "schedule": {
            "kind": "cron",
            "expr": "0 7 * * 1",   # Mondays at 07:00 UTC
            "tz": "UTC"
        },
        "payload": {
            "kind": "exec",
            "command": "bash",
            "args": [
                "-c",
                "cd /home/hackathon/.openclaw/workspace && git fetch origin --quiet && git checkout main --quiet && git pull origin main --quiet && python3 scripts/weekly_ev_report.py --telegram"
            ],
            "timeoutSeconds": 60
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": TELEGRAM_CHANNEL_ID,
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("weekly-ev-report", ev_report_job))

    return jobs

def save_jobs_to_file(jobs):
    """Save cron job definitions to file alongside this script"""
    jobs_data = {
        "created_at": datetime.now().isoformat(),
        "jobs": {job_id: job_def for job_id, job_def in jobs}
    }

    # Always write to automation/ directory regardless of CWD
    script_dir = os.path.dirname(os.path.abspath(__file__))
    jobs_file = os.path.join(script_dir, "cron_jobs_config.json")
    with open(jobs_file, "w") as f:
        json.dump(jobs_data, f, indent=2)

    print(f"✅ Cron job configurations saved to {jobs_file}")
    return jobs_file

def print_setup_instructions(jobs):
    """Print setup instructions"""
    print("\n" + "="*60)
    print("CRON JOB SETUP INSTRUCTIONS")
    print("="*60)

    print("\n📋 JOB SCHEDULE (all times UTC):")
    print("   :00  - Market Research (swap check runs inside this)")
    print("   :10  - Position Resolution (frees slots before trading)")
    print("   :20  - Auto Trading (uses freed slots from resolution)")
    print("   19:00 daily  - Daily Summary Report")
    print("   Sun 02:00    - Weekly Calibration Harvest")
    print("   Mon 07:00    - Weekly EV Accuracy Report")
    print()
    for job_id, job_def in jobs:
        name = job_def.get("name", job_id)
        schedule = job_def.get("schedule", {})
        if schedule.get("kind") == "cron":
            expr = schedule.get("expr", "N/A")
            print(f"  • {name}: {expr} (UTC)")

    print("\n🔧 SETUP STEPS:")
    print("  1. Test each script manually first:")
    print("     python3 automation/auto_research.py")
    print("     python3 scripts/resolve_positions.py")
    print("     python3 automation/auto_trader.py")
    print("     python3 automation/daily_summary.py")

    print("\n  2. Create OpenClaw cron jobs using:")
    print("     openclaw cron add --file automation/cron_jobs_config.json")

    print("\n  3. Or create manually with:")
    for job_id, job_def in jobs:
        print(f"\n     Job: {job_def['name']}")
        print(f"       openclaw cron add \\")
        print(f"         --name \"{job_def['name']}\" \\")
        print(f"         --schedule '{json.dumps(job_def['schedule'])}' \\")
        print(f"         --payload '{json.dumps(job_def['payload'])}' \\")
        print(f"         --delivery '{json.dumps(job_def['delivery'])}'")

    print("\n  4. Verify jobs are running:")
    print("     openclaw cron list")

    print("\n  5. Monitor logs:")
    print("     tail -f ~/.openclaw/logs/gateway.log")

    print("\n⚠️  IMPORTANT NOTES:")
    print("   • Make sure Python dependencies are installed")
    print("   • Verify Manifold API key is configured")
    print("   • Set TELEGRAM_CHANNEL_ID in your environment or .env file")
    print("     (see .env.example for reference)")
    print("   • Test with small amounts first")
    print("   • Monitor initial runs closely")
    print("   • The position-resolution job uses 'exec' (no LLM) because")
    print("     resolve_positions.py is fully deterministic and needs no")
    print("     model reasoning.  See the source comments for fallback")
    print("     instructions if your platform requires 'agentTurn'.")

    print("\n📞 SUPPORT:")
    print("   If jobs fail, check:")
    print("   • API connectivity")
    print("   • File permissions")
    print("   • Python environment")
    print("   • OpenClaw gateway status")

def main():
    """Main function"""
    print("🚀 SETTING UP AUTOMATED TRADING CRON JOBS")
    print("="*60)

    if TELEGRAM_CHANNEL_ID == '-5240775171':
        print("⚠️  WARNING: Using default TELEGRAM_CHANNEL_ID fallback value.")
        print("   Set the TELEGRAM_CHANNEL_ID environment variable to suppress this warning.")

    # Create job definitions
    jobs = create_cron_jobs()
    print(f"✅ Created {len(jobs)} cron job definitions")

    # Save to file
    jobs_file = save_jobs_to_file(jobs)

    # Print instructions
    print_setup_instructions(jobs)

    print(f"\n🎉 Setup complete! Next: Test scripts and create cron jobs.")
    return 0

if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:
        print(f"❌ Error: {e}")
        sys.exit(1)
