#!/usr/bin/env python3
"""
Trading Agent Daemon — runs as a persistent background service
Posts to Discord directly via webhook. No agent spawning, minimal token cost.
"""

import time, os, sys, json, logging
from datetime import datetime, timezone
import requests

# ── Config ──────────────────────────────────────────────────────────────────
TRADING_DIR = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, TRADING_DIR)

CHECK_INTERVAL = 60        # seconds between price checks
RESEARCH_INTERVAL = 3600   # seconds between research runs (1 hour)
HOURLY_REPORT_INTERVAL = 3600  # send status report every hour

# Discord webhook — set via env or .env file
DISCORD_WEBHOOK = os.environ.get("TRADING_DISCORD_WEBHOOK", "")

LOG_FILE = os.path.join(TRADING_DIR, "daemon.log")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler(sys.stdout),
    ]
)
log = logging.getLogger("bot_daemon")

def load_webhook():
    """Load Discord webhook URL from .env files"""
    for env_path in [
        os.path.expanduser("~/.openclaw/workspace/.env"),
        os.path.expanduser("~/.openclaw/.env"),
    ]:
        if os.path.exists(env_path):
            with open(env_path) as f:
                for line in f:
                    if line.startswith("TRADING_DISCORD_WEBHOOK="):
                        return line.strip().split("=", 1)[1].strip('"\'')
    return None

def post_discord(message, urgent=False):
    """Post a message to Discord via webhook — no agent overhead"""
    webhook = DISCORD_WEBHOOK or load_webhook()
    if not webhook:
        log.warning("No Discord webhook configured — skipping notification")
        return False
    try:
        # Discord webhook uses username/content
        payload = {
            "username": "Trading Agent ⚡",
            "content": message[:2000]  # Discord limit
        }
        r = requests.post(webhook, json=payload, timeout=10)
        return r.status_code in (200, 204)
    except Exception as e:
        log.error(f"Discord post failed: {e}")
        return False

def run_bot():
    """Run one iteration of bot.py and return the output"""
    import subprocess
    result = subprocess.run(
        [sys.executable, os.path.join(TRADING_DIR, "bot.py")],
        capture_output=True, text=True, timeout=30,
        cwd=TRADING_DIR
    )
    return result.stdout.strip(), result.returncode

def run_research():
    """Run research.py and return output"""
    import subprocess
    result = subprocess.run(
        [sys.executable, os.path.join(TRADING_DIR, "research.py")],
        capture_output=True, text=True, timeout=60,
        cwd=TRADING_DIR
    )
    return result.stdout.strip(), result.returncode

def has_action(output):
    """Check if bot output contains a trade action worth reporting"""
    keywords = ["STOP-LOSS HIT", "TAKE-PROFIT HIT", "TRAILING EXIT",
                "✅ **BOUGHT", "✅ **SOLD", "✅ BOUGHT", "✅ SOLD",
                "XRP SCALP", "ENTRY SIGNAL", "EXIT TRIGGERED",
                "🚨", "⚡ XRP", "🎯 XRP", "🛑 XRP"]
    return any(k in output for k in keywords)

def is_urgent(output):
    """Check if output requires immediate attention"""
    urgent = ["STOP-LOSS HIT", "🚨", "EXIT TRIGGERED", "CRITICAL"]
    return any(k in output for k in urgent)

def main():
    global DISCORD_WEBHOOK
    DISCORD_WEBHOOK = load_webhook()

    if not DISCORD_WEBHOOK:
        log.error("No TRADING_DISCORD_WEBHOOK found in .env — daemon will run but cannot post to Discord")
        log.error("Add: TRADING_DISCORD_WEBHOOK=https://discord.com/api/webhooks/... to ~/.openclaw/workspace/.env")

    log.info("Trading daemon starting...")
    log.info(f"Check interval: {CHECK_INTERVAL}s | Research interval: {RESEARCH_INTERVAL}s")

    post_discord("🤖 **Trading daemon started** — monitoring every 60s. Reports on trade actions only.")

    last_research = 0
    last_hourly_report = 0
    iteration = 0

    while True:
        try:
            now = time.time()
            iteration += 1

            # ── Run bot check ──
            output, code = run_bot()

            if code != 0:
                log.error(f"Bot exited with code {code}")
            else:
                log.info(f"Bot check #{iteration} complete")

            # ── Post to Discord only on actions ──
            if has_action(output):
                urgent = is_urgent(output)
                prefix = "🚨 **URGENT**\n" if urgent else ""
                post_discord(f"{prefix}{output}")
                log.info(f"Action detected — posted to Discord (urgent={urgent})")

            # ── Hourly status report ──
            if now - last_hourly_report >= HOURLY_REPORT_INTERVAL:
                post_discord(output)
                last_hourly_report = now
                log.info("Hourly status report posted")

            # ── Research run (every hour at :30 offset) ──
            if now - last_research >= RESEARCH_INTERVAL:
                log.info("Running research engine...")
                research_out, rcode = run_research()
                if research_out:
                    post_discord(f"🔬 **Research Update**\n{research_out[:1800]}")
                last_research = now
                log.info("Research complete")

            time.sleep(CHECK_INTERVAL)

        except KeyboardInterrupt:
            log.info("Daemon stopped by user")
            post_discord("🛑 Trading daemon stopped.")
            break
        except Exception as e:
            log.error(f"Daemon error: {e}", exc_info=True)
            time.sleep(30)  # back off on error

if __name__ == "__main__":
    main()
