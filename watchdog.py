#!/usr/bin/env python3
"""Service Watchdog — checks critical services and alerts on state changes.

Designed for no_agent=True cron mode: output nothing when healthy,
output alert + send email when state changes.

Checks:
  - Agent Bus (9901)
  - Quota API (9899)
  - Agent Telemetry (9900)
  - Claude Usage (8080)
"""

import json
import os
import smtplib
import ssl
import subprocess
import sys
import time
import urllib.request
import urllib.error
from pathlib import Path

# ── Config ──────────────────────────────────────────────────────────────
SERVICES = {
    "agent-bus": {
        "url": "http://127.0.0.1:9901/bus",
        "doc": "Broker messaggi bidirezionale",
    },
    "quota-api": {
        "url": "http://127.0.0.1:9899/usage",
        "doc": "Monitoraggio crediti AI",
    },
    "agent-telemetry": {
        "url": "http://127.0.0.1:9900/agents",
        "doc": "Lettura log agenti CLI",
    },
    "claude-usage": {
        "url": "http://127.0.0.1:8080/api/data",
        "doc": "Dashboard Claude Code (third-party)",
    },
}

STATE_FILE = Path.home() / ".hermes" / "service-monitor-state.json"
CHECK_INTERVAL = 300  # 5 min, used for recovery debounce

# Email config
SMTP_HOST = "smtp.virgilio.it"
SMTP_PORT = 465
SMTP_USER = "fausto.lelli@virgilio.it"
SMTP_PASS = os.environ.get("SMTP_PASSWORD", "")
ALERT_EMAIL_TO = "fausto.lelli@gmail.com"
ALERT_EMAIL_FROM = SMTP_USER


def check_service(url: str, timeout: int = 5) -> tuple[bool, str]:
    """Check if a service responds. Returns (ok, detail)."""
    try:
        req = urllib.request.Request(url, method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as r:
            body = r.read()[:200]
            return True, f"HTTP {r.status}"
    except urllib.error.HTTPError as e:
        return False, f"HTTP {e.code}"
    except urllib.error.URLError as e:
        return False, f"Connection refused: {e.reason}"
    except TimeoutError:
        return False, "Timeout"
    except Exception as e:
        return False, str(e)[:60]


def load_state() -> dict:
    """Load previous service states from disk."""
    if STATE_FILE.exists():
        try:
            with open(STATE_FILE) as f:
                return json.load(f)
        except (json.JSONDecodeError, OSError):
            pass
    # Fresh start — all services unknown
    return {name: {"status": "unknown", "detail": "", "last_change": 0} for name in SERVICES}


def save_state(state: dict):
    """Persist service states."""
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(state, f, indent=2)


def send_email(subject: str, body: str):
    """Send alert email via SMTP."""
    if not SMTP_PASS:
        # Try virgilio.pass file
        pass_file = Path.home() / ".config" / "himalaya" / "virgilio.pass"
        if pass_file.exists():
            try:
                globals()["SMTP_PASS"] = pass_file.read_text().strip()
            except OSError:
                pass

    if not SMTP_PASS:
        print(f"  ⚠️  Email non inviata: SMTP_PASSWORD non impostata")
        return

    message_lines = [
        f"From: {ALERT_EMAIL_FROM}",
        f"To: {ALERT_EMAIL_TO}",
        f"Subject: {subject}",
        "Content-Type: text/plain; charset=utf-8",
        "",
        body,
    ]
    message = "\r\n".join(message_lines)

    try:
        context = ssl.create_default_context()
        with smtplib.SMTP_SSL(SMTP_HOST, SMTP_PORT, context=context) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(ALERT_EMAIL_FROM, [ALERT_EMAIL_TO], message.encode("utf-8"))
        print(f"  ✅ Email inviata a {ALERT_EMAIL_TO}")
    except Exception as e:
        print(f"  ⚠️  Email fallita: {e}")
        # Fallback to himalaya
        try:
            raw_msg = f"Subject: {subject}\n\n{body}"
            result = subprocess.run(
                ["himalaya", "message", "send", raw_msg],
                capture_output=True, text=True, timeout=15,
            )
            if result.returncode == 0:
                print(f"  ✅ Email via himalaya inviata")
            else:
                print(f"  ⚠️  Himalaya fallito: {result.stderr[:100]}")
        except Exception as e2:
            print(f"  ⚠️  Himalaya errore: {e2}")


def main():
    now = time.time()
    state = load_state()

    alerts_stdout = []
    email_body_parts = []

    for name, cfg in SERVICES.items():
        ok, detail = check_service(cfg["url"])
        prev = state.get(name, {"status": "unknown"})
        prev_status = prev.get("status", "unknown")
        last_change = prev.get("last_change", 0)

        # Determine new status
        new_status = "up" if ok else "down"

        # Update state
        state[name] = {
            "status": new_status,
            "detail": detail,
            "last_change": prev["last_change"] if new_status == prev_status else now,
        }

        # Alert on transition
        if new_status != prev_status and prev_status != "unknown":
            ts = time.strftime("%Y-%m-%d %H:%M:%S")
            emoji = "✅" if new_status == "up" else "🔴"
            action = "RIPRISTINATO" if new_status == "up" else "DOWN"

            msg = f"{emoji} {action}: {name}"
            detail_str = f"  {cfg['doc']} — {detail}"

            alerts_stdout.append(msg)
            alerts_stdout.append(detail_str)
            email_body_parts.append(f"{emoji} {action}: {name}")
            email_body_parts.append(f"   Servizio: {cfg['doc']}")
            email_body_parts.append(f"   Stato: {detail}")
            email_body_parts.append(f"   Ora: {ts}")
            email_body_parts.append("")

    # Also check for services that were up but are now stuck (no change in > 1h)
    for name, cfg in SERVICES.items():
        s = state.get(name, {})
        if s.get("status") == "up":
            last_change = s.get("last_change", 0)
            if last_change > 0 and (now - last_change) > 7200:  # 2h since last restart
                # Up for > 2h — normal, no alert needed (but log for info)
                pass

    # Save state
    save_state(state)

    # Output results
    if alerts_stdout:
        print("╔══════════════════════════════════════════╗")
        print("║     SERVIZI WATCHDOG — ALERT             ║")
        print("╚══════════════════════════════════════════╝")
        print()
        for line in alerts_stdout:
            print(line)
        print()

        # Send email
        if email_body_parts:
            email_body = "\n".join(email_body_parts)
            subject = "🔴 Servizio DOWN" if any("DOWN" in p for p in email_body_parts) else "✅ Servizi ripristinati"
            send_email(subject, email_body)
    else:
        # Healthy — silent (no_agent=True: empty stdout = silent)
        pass


if __name__ == "__main__":
    main()
