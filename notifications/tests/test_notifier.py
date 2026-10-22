"""AC unit checkout notifier.

The state machine is the risky part: getting it wrong either floods the
recipients or silently drops an alert nobody knows was missed.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from conftest import checkout

import notifier as n


# --------------------------------------------------------------------- #
# Snipe-IT retry safety
#
# Only idempotent reads may retry.  In particular, this policy must never be
# broadened to POST, because an ambiguous Microsoft Graph sendMail timeout can
# otherwise produce a duplicate notification.
# --------------------------------------------------------------------- #
def test_snipe_client_retries_only_get_requests():
    client = n.SnipeClient("https://snipe.invalid", "token")
    retries = client.session.get_adapter("https://").max_retries

    assert retries.total == n.SNIPE_RETRY_COUNT
    assert retries.connect == n.SNIPE_RETRY_COUNT
    assert retries.read == n.SNIPE_RETRY_COUNT
    assert retries.allowed_methods == frozenset({"GET"})


def test_snipe_client_retries_only_transient_http_statuses():
    client = n.SnipeClient("https://snipe.invalid", "token")
    retries = client.session.get_adapter("https://").max_retries

    assert set(retries.status_forcelist) == set(n.SNIPE_RETRYABLE_STATUSES)
    assert not ({400, 401, 403, 404} & set(retries.status_forcelist))
    assert retries.respect_retry_after_header is True


# --------------------------------------------------------------------- #
# State persistence
#
# The state file lives in the Actions cache, which can miss or be evicted.
# Treating "no file" as "nothing seen" would email every one of the 200
# fetched checkouts at once.
# --------------------------------------------------------------------- #
def test_missing_state_file_is_flagged_not_silently_empty(tmp_path):
    state = n.NotifyState.load(str(tmp_path / "absent.json"))
    assert state.seen_ids == set()
    assert state.loaded_from_disk is False


def test_corrupt_state_file_is_flagged_not_fatal(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{ this is not valid json")
    state = n.NotifyState.load(str(path))
    assert state.loaded_from_disk is False


def test_saved_state_round_trips_and_is_flagged_as_loaded(tmp_path):
    path = str(tmp_path / "state.json")
    n.NotifyState(seen_ids={1, 2, 3}).save(path)
    state = n.NotifyState.load(path)
    assert state.seen_ids == {1, 2, 3}
    assert state.loaded_from_disk is True


def test_state_is_capped_keeping_the_newest_ids(tmp_path):
    path = str(tmp_path / "state.json")
    n.NotifyState(seen_ids=set(range(12_000))).save(path)
    kept = json.loads(Path(path).read_text())["seen_ids"]
    assert len(kept) == 10_000
    assert max(kept) == 11_999          # newest retained
    assert min(kept) == 2_000           # oldest dropped


# --------------------------------------------------------------------- #
# Send ordering
#
# Ids were previously marked seen before the send, so a Graph outage dropped
# that notification permanently.
# --------------------------------------------------------------------- #
def _state(*seen):
    return n.NotifyState(seen_ids=set(seen), loaded_from_disk=True)


def test_watched_checkout_is_sent_and_recorded():
    state = _state()
    sent = []
    ok, failed = n.process_checkouts(
        [checkout(1, asset_id=7)], {7}, state, lambda e, a: sent.append(e["id"])
    )
    assert (ok, failed) == (1, 0)
    assert sent == [1] and state.seen_ids == {1}


def test_failed_send_is_not_recorded_so_it_retries():
    state = _state()

    def boom(entry, asset_id):
        raise RuntimeError("graph unavailable")

    ok, failed = n.process_checkouts([checkout(1, asset_id=7)], {7}, state, boom)
    assert (ok, failed) == (0, 1)
    assert state.seen_ids == set(), "a failed send must stay unseen"


def test_a_previously_failed_send_goes_out_on_the_next_run():
    state = _state()
    n.process_checkouts([checkout(1, 7)], {7}, state,
                        lambda e, a: (_ for _ in ()).throw(RuntimeError("down")))
    sent = []
    ok, _ = n.process_checkouts([checkout(1, 7)], {7}, state, lambda e, a: sent.append(e["id"]))
    assert ok == 1 and sent == [1] and state.seen_ids == {1}


def test_one_failure_does_not_stop_the_others():
    state = _state()
    sent = []

    def send(entry, asset_id):
        if entry["id"] == 2:
            raise RuntimeError("transient")
        sent.append(entry["id"])

    ok, failed = n.process_checkouts(
        [checkout(1, 7), checkout(2, 7), checkout(3, 7)], {7}, state, send
    )
    assert (ok, failed) == (2, 1)
    assert sent == [1, 3] and state.seen_ids == {1, 3}


def test_already_seen_entries_are_not_resent():
    state = _state(1)
    sent = []
    ok, _ = n.process_checkouts([checkout(1, 7)], {7}, state, lambda e, a: sent.append(e))
    assert ok == 0 and sent == []


def test_unwatched_asset_is_recorded_without_sending():
    state = _state()
    sent = []
    ok, _ = n.process_checkouts([checkout(1, asset_id=99)], {7}, state,
                                lambda e, a: sent.append(e))
    assert ok == 0 and sent == []
    assert state.seen_ids == {1}, "recording it avoids re-checking next run"


def test_entry_without_an_id_is_ignored():
    state = _state()
    ok, failed = n.process_checkouts([{"item": {"id": 7}}], {7}, state,
                                     lambda e, a: None)
    assert (ok, failed) == (0, 0)


# --------------------------------------------------------------------- #
# Retired units
#
# AC units are retired once checked out, and Snipe-IT's asset list omits
# archived and deleted assets by default. A unit retired before the next run
# fell off the watch list, so its checkout counted as "unwatched" and was
# recorded without an email.
# --------------------------------------------------------------------- #
class _ListingSnipe(n.SnipeClient):
    """Answers /hardware listings from a {status: [asset ids]} table."""

    def __init__(self, by_status):
        super().__init__("https://snipe.invalid", "token")
        self.by_status = by_status
        self.requests = []

    def _get(self, path, params=None):
        params = dict(params or {})
        self.requests.append(params)
        rows = [{"id": i} for i in self.by_status.get(params.get("status"), [])]
        return {"rows": rows[params["offset"]:], "total": len(rows)}


def test_watch_list_includes_archived_and_deleted_units():
    snipe = _ListingSnipe({None: [1, 2], "Archived": [3], "Deleted": [4]})
    assert snipe.get_asset_ids_in_category(11) == {1, 2, 3, 4}


def test_active_listing_is_still_requested_unfiltered():
    snipe = _ListingSnipe({None: [1]})
    snipe.get_asset_ids_in_category(11)
    assert [r.get("status") for r in snipe.requests] == [None, "Archived", "Deleted"]
    assert all(r["category_id"] == 11 for r in snipe.requests)


def test_unit_retired_before_the_run_still_gets_its_email():
    snipe = _ListingSnipe({None: [1], "Archived": [7]})
    sent = []
    ok, failed = n.process_checkouts(
        [checkout(500, asset_id=7)], snipe.get_asset_ids_in_category(11),
        _state(), lambda e, a: sent.append(a),
    )
    assert (ok, failed) == (1, 0) and sent == [7]


def test_retired_unit_already_notified_is_not_emailed_again():
    """Its earlier checkout is in the seen state from when it was active."""
    snipe = _ListingSnipe({"Archived": [7]})
    sent = []
    ok, _ = n.process_checkouts(
        [checkout(500, asset_id=7)], snipe.get_asset_ids_in_category(11),
        _state(500), lambda e, a: sent.append(a),
    )
    assert ok == 0 and sent == []


# --------------------------------------------------------------------- #
# Email content
#
# Snipe-IT HTML-escapes its output, so pre-escaped values must pass through
# untouched rather than being escaped a second time.
# --------------------------------------------------------------------- #
ASSET = {
    "id": 7, "asset_tag": "AC-0042",
    "model": {"name": "Friedrich Chill Premier", "model_number": "CCF12B10A"},
    "manufacturer": {"name": "Friedrich"},
}


def test_subject_names_the_asset_and_model():
    subject, _ = n.build_email(ASSET, checkout(1, 7), [], "https://snipe.invalid")
    assert "AC-0042" in subject and "Friedrich Chill Premier" in subject


def test_body_carries_the_asset_details_and_people():
    _, body = n.build_email(ASSET, checkout(1, 7), [], "https://snipe.invalid")
    for expected in ("AC-0042", "CCF12B10A", "Friedrich", "Alex Rivera", "Casey Morgan"):
        assert expected in body


def test_note_row_appears_only_when_a_note_exists():
    # ">Note</td>" is the detail-table row; the history table always has a
    # ">Note</th>" column header, so match the cell specifically.
    entry = checkout(1, 7)
    _, without = n.build_email(ASSET, entry, [], "https://x")
    assert ">Note</td>" not in without

    entry["note"] = "Unit 4B, tenant reported no cooling"
    _, with_note = n.build_email(ASSET, entry, [], "https://x")
    assert ">Note</td>" in with_note and "tenant reported no cooling" in with_note


def test_pre_escaped_text_is_not_escaped_again():
    entry = checkout(1, 7)
    entry["note"] = "Units 4B &amp; 4C"
    _, body = n.build_email(ASSET, entry, [], "https://x")
    assert "Units 4B &amp; 4C" in body
    assert "&amp;amp;" not in body


def test_history_rows_render_and_empty_history_says_so():
    _, empty = n.build_email(ASSET, checkout(1, 7), [], "https://x")
    assert "No history found" in empty

    _, filled = n.build_email(ASSET, checkout(1, 7), [checkout(2, 7)], "https://x")
    assert "No history found" not in filled
    assert "Check Out" in filled


def test_asset_link_points_at_the_instance():
    _, body = n.build_email(ASSET, checkout(1, 7), [], "https://snipe.invalid")
    assert "https://snipe.invalid/hardware/7" in body


# --------------------------------------------------------------------- #
# Field extraction
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "action,label",
    [("checkout", "Check Out"), ("checkin", "Check In"),
     ("checkin from", "Check In"), ("update", "Update"), ("", "—")],
)
def test_action_labels(action, label):
    assert n._action_label(action) == label


@pytest.mark.parametrize(
    "entry,expected",
    [
        ({"admin": {"name": "Alex Rivera"}}, "Alex Rivera"),
        ({"admin": {"first_name": "Sam", "last_name": "Ortiz"}}, "Sam Ortiz"),
        ({"admin": {"username": "jlee"}}, "jlee"),
        ({"admin": "Plain String"}, "Plain String"),
        ({}, ""),
    ],
)
def test_person_name_extraction(entry, expected):
    assert n._person_name(entry, "admin", "created_by", "user") == expected


def test_date_formatting_prefers_the_human_readable_form():
    assert n._fmt_date({"formatted": "July 27, 2026 9:00am",
                        "datetime": "2026-07-27 09:00:00"}) == "July 27, 2026 9:00am"
    assert n._fmt_date({"datetime": "2026-07-27 09:00:00"}) == "2026-07-27 09:00:00"
    assert n._fmt_date(None) == ""
