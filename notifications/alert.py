"""Email an alert when a scheduled workflow fails.

Driven by .github/workflows/alert_on_failure.yml, which fires on a
workflow_run completion whose conclusion was not success. Reuses the Graph
sender the AC unit notifier already uses.

Recipients come from ALERT_TO_EMAILS. This is deliberately separate from
NOTIFY_TO_EMAILS: an AC unit checkout is for the office, a sync failure is
for whoever maintains the repo. With ALERT_TO_EMAILS unset the alert is
skipped rather than mailed to the wrong people.
"""

from __future__ import annotations

import logging
import os
import sys

from notifier import GraphMailSender

log = logging.getLogger("alert")


def build_alert(workflow: str, conclusion: str, run_url: str, repo: str,
                run_number: str = "") -> tuple:
    """Return (subject, html_body) for a failed workflow run."""
    run_label = f" #{run_number}" if run_number else ""
    subject = f"[{repo}] {workflow} {conclusion}"

    html = f"""<!DOCTYPE html>
<html>
<body style="font-family:Arial,sans-serif;color:#222;max-width:640px;">
  <h2 style="color:#a32d2d;margin-bottom:4px;">Workflow {conclusion}</h2>
  <p style="color:#888;margin-top:0;font-size:14px;">{repo}</p>

  <table style="width:100%;border-collapse:collapse;margin-bottom:24px;
                border:1px solid #e0e0e0;font-size:14px;">
    <tr><td style="padding:8px 16px;font-weight:bold;width:140px;">Workflow</td>
        <td style="padding:8px 16px;">{workflow}{run_label}</td></tr>
    <tr style="background:#f4f6fa;"><td style="padding:8px 16px;font-weight:bold;">Result</td>
        <td style="padding:8px 16px;">{conclusion}</td></tr>
  </table>

  <p style="font-size:14px;">
    <a href="{run_url}" style="color:#1a56a0;">View the run log</a>
  </p>

  <p style="margin-top:28px;font-size:12px;color:#aaa;">
    The syncs exit non-zero when a write is rejected, so this covers silent
    failures as well as crashes. Nothing is retried automatically except
    unsent notifications, which go out on the next run.
  </p>
</body>
</html>"""
    return subject, html


def main() -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)-7s | %(message)s")

    recipients = [
        e.strip() for e in os.environ.get("ALERT_TO_EMAILS", "").split(",") if e.strip()
    ]
    if not recipients:
        log.warning("ALERT_TO_EMAILS is not set — no alert sent")
        return 0

    workflow = os.environ.get("ALERT_WORKFLOW", "unknown workflow")
    conclusion = os.environ.get("ALERT_CONCLUSION", "failed")
    run_url = os.environ.get("ALERT_RUN_URL", "")
    repo = os.environ.get("ALERT_REPO", "snipe-it-sync")
    run_number = os.environ.get("ALERT_RUN_NUMBER", "")

    subject, body = build_alert(workflow, conclusion, run_url, repo, run_number)

    try:
        mailer = GraphMailSender(
            os.environ["AZURE_TENANT_ID"],
            os.environ["AZURE_CLIENT_ID"],
            os.environ["AZURE_CLIENT_SECRET"],
            os.environ["NOTIFY_FROM_EMAIL"],
        )
        mailer.send(recipients, subject, body)
    except Exception:
        log.exception("Could not send the failure alert")
        return 1

    log.info("Alerted %s: %s", recipients, subject)
    return 0


if __name__ == "__main__":
    sys.exit(main())
