"""Redaction of public CI logs.

The repository is public, so every Actions run log is readable by anyone.
These lines are what the sync actually printed in October 2026 before
redaction existed: share links anyone could open, employee names, VINs.
"""

from __future__ import annotations

import io
import logging
from types import SimpleNamespace

import pytest

import usage_report
from helpers import log_redaction
from helpers.log_redaction import redact


# --------------------------------------------------------------------- #
# What gets removed
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "line,leaked",
    [
        ("  Resolving Google Photos album: https://photos.app.goo.gl/AbC123xyz",
         "photos.app.goo.gl"),
        ("  Uploaded → https://contoso-my.sharepoint.com/personal/owner_contoso_com/"
         "Documents/AssetPhotos/Vehicle/GMC%20Canyon%202018/x.jpg", "owner"),
        ("  Skipping iCloud link (manual export only): https://share.icloud.com/photos/0aBc",
         "icloud"),
        ("cdn //lh3.googleusercontent.com/pw/AP1Gcz=s0", "googleusercontent"),
        ("Notified ['casey@example.com', 'alex@example.com']", "jdmccormick.com"),
        ("Asset 3 (1ABCD23EFGH456796): 34 URL(s) (34 from history notes)", "1ABCD23EFGH456796"),
        ("Created OneDrive folder /JD McCormick Shared Drive/AssetPhotos/Vehicle/GMC Canyon 2018/"
         "Check Out - Alex Rivera - 2026-10-22 10-47", "Alex Rivera"),
        ("  Using event subfolder: Check In - Jordan Lee - 2026-08-19 15-08", "Jordan"),
        ("    Chevrolet Silverado 2007 - Sam Ortiz - 2026-08-06 13-19-45.jpg  -> Check Out",
         "Sam"),
    ],
)
def test_sensitive_detail_is_removed(line, leaked):
    assert leaked not in redact(line)


def test_redaction_keeps_the_line_readable():
    line = ("Created OneDrive folder /JD McCormick Shared Drive/AssetPhotos/Vehicle/"
            "GMC Canyon 2018/Check Out - Alex Rivera - 2026-10-22 10-47")
    assert redact(line) == (
        "Created OneDrive folder /JD McCormick Shared Drive/AssetPhotos/Vehicle/"
        "GMC Canyon 2018/Check Out - [person] - 2026-10-22 10-47"
    )
    assert redact("Asset 3 (1ABCD23EFGH456796): 5 URL(s)") == "Asset 3 ([VIN]): 5 URL(s)"


@pytest.mark.parametrize(
    "line",
    [
        "Done. assets_seen=21 assets_with_urls=6 uploaded=5 skipped=35 unresolved=0 failed=0",
        "  Uploading GMC Canyon 2018 - 3.jpg (2174500 bytes)",
        "  5 native Snipe-IT upload(s) found",
        "Hash 3f9a1c2b7d already uploaded for asset 3 — skipping",
        "Album quiet, checked within 1 day, 0:00:00 — skipping: [link]",
    ],
)
def test_ordinary_lines_are_untouched(line):
    assert redact(line) == line


def test_only_17_character_tokens_count_as_vins():
    # 16 and 18 characters, and a 17-character token containing I/O/Q
    for token in ["1ABCD23EFGH45679", "1ABCD23EFGH4567961", "1ABCD23EFGH45679O"]:
        assert redact(token) == token


# --------------------------------------------------------------------- #
# When it applies
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "env,expected",
    [
        ({"GITHUB_ACTIONS": "true"}, True),
        ({}, False),
        ({"GITHUB_ACTIONS": "true", "REDACT_LOGS": "false"}, False),
        ({"REDACT_LOGS": "true"}, True),
    ],
)
def test_on_in_actions_off_locally(monkeypatch, env, expected):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("REDACT_LOGS", raising=False)
    for k, v in env.items():
        monkeypatch.setenv(k, v)
    assert log_redaction.should_redact() is expected


@pytest.fixture
def root_stream():
    """A plain handler on the root logger, as basicConfig would add."""
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(logging.Formatter("%(levelname)s | %(message)s"))
    root = logging.getLogger()
    root.addHandler(handler)
    yield stream
    root.removeHandler(handler)


def test_installed_formatter_redacts_messages_and_tracebacks(monkeypatch, root_stream):
    monkeypatch.setenv("REDACT_LOGS", "true")
    assert log_redaction.install() is True
    log = logging.getLogger("redaction-test")
    log.warning("Could not resolve %s", "https://photos.app.goo.gl/AbC123xyz")
    try:
        raise RuntimeError("GET https://photos.app.goo.gl/AbC123xyz failed")
    except RuntimeError:
        log.exception("Upload failed for asset 1ABCD23EFGH456796")
    out = root_stream.getvalue()
    assert "goo.gl" not in out and "1ABCD23EFGH456796" not in out
    assert "WARNING | Could not resolve [link]" in out
    assert "Traceback" in out


def test_install_is_a_no_op_locally(monkeypatch, root_stream):
    monkeypatch.delenv("GITHUB_ACTIONS", raising=False)
    monkeypatch.delenv("REDACT_LOGS", raising=False)
    assert log_redaction.install() is False
    logging.getLogger("redaction-test").warning("https://photos.app.goo.gl/AbC123xyz")
    assert "goo.gl" in root_stream.getvalue()


# --------------------------------------------------------------------- #
# The usage report is a per-person table: emailed in Actions, never logged
# --------------------------------------------------------------------- #
class _FakeDrive:
    def __init__(self, **kw):
        pass

    def iter_files(self, base):
        return [{"path": f"{base}/Vehicle/GMC Canyon 2018/Check Out - Alex Rivera - 2026-10-22 10-47/x.jpg"}]

    def _headers(self, content_type=None):
        return {"Authorization": "Bearer test"}


class _FakeSnipe:
    def __init__(self, *a):
        pass

    def iter_hardware(self):
        return []


def _setup_report(monkeypatch, *, in_actions):
    """Fakes for OneDrive, Snipe-IT and config; returns the list emails land in."""
    monkeypatch.setattr(usage_report, "OneDriveClient", _FakeDrive)
    monkeypatch.setattr(usage_report, "SnipeITClient", _FakeSnipe)
    monkeypatch.setattr(usage_report, "_configure_logging", lambda level: None)
    monkeypatch.setattr(usage_report, "load_config", lambda: SimpleNamespace(
        log_level="INFO", azure_tenant="t", azure_client_id="c", azure_client_secret="s",
        onedrive_user_id="u", onedrive_drive_id=None, onedrive_base_folder="AssetPhotos",
        snipe_url="https://snipe.invalid", snipe_token="x",
    ))
    monkeypatch.setenv("REDACT_LOGS", "true" if in_actions else "false")
    monkeypatch.setenv("ALERT_TO_EMAILS", "maintainer@example.com")
    monkeypatch.setenv("NOTIFY_FROM_EMAIL", "info@example.com")
    sent = []

    def fake_post(url, **kw):
        sent.append((url, kw["json"]))
        return SimpleNamespace(raise_for_status=lambda: None)

    monkeypatch.setattr(usage_report.requests, "post", fake_post)
    return sent


def test_report_is_emailed_not_logged_in_actions(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = _setup_report(monkeypatch, in_actions=True)
    assert usage_report.main() == 0
    assert len(sent) == 1
    url, payload = sent[0]
    assert url.endswith("/users/info@example.com/sendMail")
    assert "Alex Rivera" in payload["message"]["body"]["content"]
    assert payload["message"]["toRecipients"] == [
        {"emailAddress": {"address": "maintainer@example.com"}}
    ]
    assert "Alex Rivera" not in caplog.text
    assert "emailed to 1 recipient" in caplog.text


def test_report_prints_locally(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = _setup_report(monkeypatch, in_actions=False)
    assert usage_report.main() == 0
    assert sent == []
    assert "Alex Rivera" in caplog.text


def test_report_fails_loudly_rather_than_logging_without_recipients(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    sent = _setup_report(monkeypatch, in_actions=True)
    monkeypatch.delenv("ALERT_TO_EMAILS")
    assert usage_report.main() == 1
    assert sent == []
    assert "Alex Rivera" not in caplog.text


def test_every_photo_script_gets_redaction_through_configure_logging(monkeypatch, root_stream):
    """sync, cleanup_orphans, organize_photos and usage_report all set up
    logging through sync._configure_logging; dropping the install() call
    there would quietly un-redact every one of them."""
    import sync

    monkeypatch.setenv("REDACT_LOGS", "true")
    sync._configure_logging("INFO")
    logging.getLogger("photo_sync").warning("Resolving https://photos.app.goo.gl/AbC123xyz")
    assert "goo.gl" not in root_stream.getvalue()
