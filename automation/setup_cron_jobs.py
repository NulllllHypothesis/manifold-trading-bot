#!/usr/bin/env python3
"""
Setup cron jobs for automated trading system.
This creates OpenClaw cron jobs for:
1. Hourly market research
2. Hourly trading (after research)
3. Daily summary report
"""

import sys
import os
from datetime import datetime, timedelta
import json

def create_cron_jobs():
    """Create OpenClaw cron jobs for automated trading"""

    jobs = []

    # Job 1: Hourly Market Research (at :00 of every hour)
    research_job = {
        "name": "Manifold Market Research",
        "schedule": {
            "kind": "cron",
            "expr": "0 * * * *",  # Every hour at minute 0
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Run hourly market research for Manifold trading bot. Execute: cd /home/hackathon/.openclaw/workspace && python3 automation/auto_research.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": "-5240775171",
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("market-research-hourly", research_job))

    # Job 2: Hourly Trading (at :15 of every hour, after research)
    trading_job = {
        "name": "Manifold Auto Trading",
        "schedule": {
            "kind": "cron",
            "expr": "15 * * * *",  # Every hour at minute 15
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Execute automated trading based on latest research. Run: cd /home/hackathon/.openclaw/workspace && python3 automation/auto_trader.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": "-5240775171",
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("auto-trading-hourly", trading_job))

    # Job 3: Daily Summary Report (at 19:00 UTC every day)
    summary_job = {
        "name": "Daily Trading Summary",
        "schedule": {
            "kind": "cron",
            "expr": "0 19 * * *",  # Daily at 19:00 UTC
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Generate and send daily trading summary to Telegram group. Execute: cd /home/hackathon/.openclaw/workspace && python3 automation/daily_summary.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 300
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": "-5240775171",
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("daily-summary", summary_job))

    # Job 4: Position Resolution (every 30 minutes at :30)
    resolve_job = {
        "name": "Manifold Position Resolution",
        "schedule": {
            "kind": "cron",
            "expr": "30 * * * *",  # Every hour at minute 30
            "tz": "UTC"
        },
        "payload": {
            "kind": "agentTurn",
            "message": "Check and resolve any settled Manifold positions. Execute: cd /home/hackathon/.openclaw/workspace && python3 scripts/resolve_positions.py",
            "model": "deepseek/deepseek-chat",
            "timeoutSeconds": 120
        },
        "sessionTarget": "isolated",
        "delivery": {
            "mode": "announce",
            "channel": "telegram",
            "to": "-5240775171",
            "bestEffort": True
        },
        "enabled": True
    }
    jobs.append(("position-resolution", resolve_job))

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

    print("\n📋 JOB SCHEDULE:")
    for job_id, job_def in jobs:
        name = job_def.get("name", job_id)
        schedule = job_def.get("schedule", {})
        if schedule.get("kind") == "cron":
            expr = schedule.get("expr", "N/A")
            print(f"  • {name}: {expr} (UTC)")

    print("\n🔧 SETUP STEPS:")
    print("  1. Test each script manually first:")
    print("     python3 automation/auto_research.py")
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
    print("   • Test with small amounts first")
    print("   • Monitor initial runs closely")

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
