"""Failure alerting.

The point of this alert is the silent-failure case: a run that reports
success while writing nothing. That only works if the syncs exit non-zero on
a rejected write, which is tested alongside the message itself.
"""

from __future__ import annotations

import os

import pytest

from alert import build_alert, main


def test_subject_names_the_repo_and_workflow():
    subject, _ = build_alert("Property Sync", "failure",
                             "https://gh/run/1", "JD-McCormick-Properties/snipe-it-sync")
    assert "Property Sync" in subject
    assert "failure" in subject
    assert "JD-McCormick-Properties/snipe-it-sync" in subject


def test_body_links_to_the_run():
    _, body = build_alert("Photo Sync", "failure", "https://gh/run/42", "repo")
    assert "https://gh/run/42" in body


def test_body_reports_the_actual_conclusion():
    for conclusion in ("failure", "cancelled", "timed_out"):
        subject, body = build_alert("Property Sync", conclusion, "u", "r")
        assert conclusion in subject and conclusion in body


def test_run_number_is_optional():
    _, without = build_alert("W", "failure", "u", "r")
    _, with_num = build_alert("W", "failure", "u", "r", run_number="77")
    assert "#77" in with_num and "#77" not in without


def test_no_recipients_configured_is_a_skip_not_an_error(monkeypatch, caplog):
    """Unset ALERT_TO_EMAILS must not mail the AC unit recipients by mistake."""
    monkeypatch.setenv("ALERT_TO_EMAILS", "")
    assert main() == 0


def test_blank_recipient_entries_are_ignored(monkeypatch):
    monkeypatch.setenv("ALERT_TO_EMAILS", " , ,  ")
    assert main() == 0


def test_alert_never_falls_back_to_the_notifier_recipients(monkeypatch):
    """Sync failures must not reach the people who get AC unit checkouts.

    NOTIFY_TO_EMAILS is set here and ALERT_TO_EMAILS is not; the alert must
    skip rather than borrow the notifier's recipient list.
    """
    monkeypatch.setenv("NOTIFY_TO_EMAILS", "jill@example.com,office@example.com")
    monkeypatch.delenv("ALERT_TO_EMAILS", raising=False)

    sent = []
    monkeypatch.setattr(
        "alert.GraphMailSender",
        lambda *a, **k: type("M", (), {"send": lambda self, to, s, b: sent.append(to)})(),
    )
    assert main() == 0
    assert sent == [], "the alert must not mail the notifier's recipients"


def test_alert_recipients_are_read_only_from_alert_to_emails(monkeypatch):
    monkeypatch.setenv("NOTIFY_TO_EMAILS", "jill@example.com")
    monkeypatch.setenv("ALERT_TO_EMAILS", "maintainer@example.com")

    sent = []
    monkeypatch.setattr(
        "alert.GraphMailSender",
        lambda *a, **k: type("M", (), {"send": lambda self, to, s, b: sent.append(to)})(),
    )
    assert main() == 0
    assert sent == [["maintainer@example.com"]]
    assert "jill@example.com" not in sent[0]


def test_alert_source_does_not_reference_the_notifier_recipient_list():
    """Belt and braces: the string must not appear in the module at all."""
    import inspect

    import alert

    body = inspect.getsource(alert)
    code = "\n".join(
        line for line in body.splitlines()
        if not line.strip().startswith("#")
    )
    # It appears in the module docstring explaining the separation, but must
    # never be read as an environment variable.
    assert 'environ.get("NOTIFY_TO_EMAILS"' not in code
    assert 'environ["NOTIFY_TO_EMAILS"]' not in code
