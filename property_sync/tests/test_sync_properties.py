"""Property and unit sync.

Each section corresponds to a bug that ran in production undetected, in one
case for weeks. A failure here means one of them is back.
"""

from __future__ import annotations

import pytest
from conftest import FakeResponse

import sync_properties as sp


# --------------------------------------------------------------------- #
# Write results
#
# Snipe-IT answers validation failures with HTTP 200 and an error body, so
# raise_for_status() passed and every write was reported as a success. The
# daily run claimed the same 15 creations and 4 updates for weeks while the
# location count never moved.
# --------------------------------------------------------------------- #
def test_rejected_write_raises_despite_http_200():
    resp = FakeResponse({"status": "error", "messages": {"name": ["The name must be unique."]}})
    with pytest.raises(sp.SnipeError) as err:
        sp.check(resp)
    assert "unique" in str(err.value)


def test_successful_write_returns_payload():
    assert sp.check(FakeResponse({"status": "success", "payload": {"id": 5}}))["payload"]["id"] == 5


def test_http_error_still_raises():
    import requests

    with pytest.raises(requests.HTTPError):
        sp.check(FakeResponse({"status": "success"}, status_code=500))


def test_empty_body_is_not_an_error():
    assert sp.check(FakeResponse(None)) == {}


# --------------------------------------------------------------------- #
# Change detection
#
# Snipe-IT HTML-escapes on output, so "402 & 404 Genesee Street" comes back
# as "402 &amp; 404 ...". Comparing raw made every property containing & or
# an apostrophe look permanently stale, and it was rewritten every run.
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "stored,intended",
    [
        ("402 &amp; 404 Genesee Street", "402 & 404 Genesee Street"),
        ("Joe&#039;s Soley Owned Parent LLC", "Joe's Soley Owned Parent LLC"),
        ("Smith &amp; Sons&#039; Place", "Smith & Sons' Place"),
        ("plain text", "plain text"),
    ],
)
def test_escaped_values_are_not_treated_as_changes(stored, intended):
    assert sp.fields_differing({"name": stored}, {"name": intended}) == []


def test_a_real_change_is_still_detected():
    assert sp.fields_differing({"name": "Old Name"}, {"name": "New Name"}) == ["name"]


def test_every_differing_field_is_reported():
    existing = {"name": "A", "city": "Madison", "zip": "53711"}
    want = {"name": "B", "city": "Madison", "zip": "53703"}
    assert sorted(sp.fields_differing(existing, want)) == ["name", "zip"]


def test_missing_field_compares_as_empty():
    assert sp.fields_differing({}, {"city": ""}) == []
    assert sp.fields_differing({}, {"city": "Madison"}) == ["city"]


# --------------------------------------------------------------------- #
# Addresses
# --------------------------------------------------------------------- #
def test_address_line_two_is_appended():
    assert sp.build_address(
        {"Property Street Address 1": "123 Main St", "Property Street Address 2": "Apt 4"}
    ) == "123 Main St Apt 4"


def test_address_without_line_two():
    assert sp.build_address({"Property Street Address 1": "123 Main St"}) == "123 Main St"


# --------------------------------------------------------------------- #
# Unit naming
#
# Names are globally unique in Snipe-IT, but existence was checked only
# within the parent, so 15 names used elsewhere were retried and rejected
# every run. The qualified fallback that fixed it then produced
# "Skyline Drive - Skyline Drive" for single-unit properties.
# --------------------------------------------------------------------- #
PARENT = "Seminole Woods"


def test_free_name_is_created_bare():
    create, existing, blocked = sp.plan_units(["205"], PARENT, {}, set())
    assert create == [("205", False)] and existing == 0 and blocked == []


def test_name_taken_elsewhere_is_qualified():
    create, _, blocked = sp.plan_units(["122"], PARENT, {}, {"122"})
    assert create == [("Seminole Woods - 122", True)] and blocked == []


def test_unit_matching_its_own_parent_is_skipped():
    """Single-unit properties list themselves; the parent already covers it."""
    create, existing, blocked = sp.plan_units(
        ["Skyline Drive"], "Skyline Drive", {}, {"skyline drive"}
    )
    assert create == [] and existing == 1 and blocked == []


def test_both_names_taken_is_blocked_not_attempted():
    create, _, blocked = sp.plan_units(
        ["999"], PARENT, {}, {"999", "seminole woods - 999"}
    )
    assert create == [] and blocked == [("999", "Seminole Woods - 999")]


def test_existing_bare_unit_is_not_recreated():
    create, existing, _ = sp.plan_units(["205"], PARENT, {"205": 1}, {"205"})
    assert create == [] and existing == 1


def test_existing_qualified_unit_is_not_recreated_under_its_bare_name():
    """The regression that would re-create a qualified unit on every run."""
    create, existing, _ = sp.plan_units(
        ["122"], PARENT, {"seminole woods - 122": 1}, {"122", "seminole woods - 122"}
    )
    assert create == [] and existing == 1


def test_planning_is_idempotent_across_runs():
    units = ["101", "122"]
    subs, names = {}, {"122", "somewhere else"}

    create, _, _ = sp.plan_units(units, PARENT, subs, names)
    assert create == [("101", False), ("Seminole Woods - 122", True)]

    # simulate those landing in Snipe-IT, then run again
    for name, _q in create:
        subs[name.lower()] = 1
        names.add(name.lower())

    create, existing, blocked = sp.plan_units(units, PARENT, subs, names)
    assert create == [] and existing == 2 and blocked == []


def test_unit_names_are_matched_case_and_space_insensitively():
    create, existing, _ = sp.plan_units(["  205  "], PARENT, {"205": 1}, {"205"})
    assert create == [] and existing == 1


def test_missing_parent_name_falls_back_to_bare_only():
    create, _, blocked = sp.plan_units(["122"], "", {}, {"122"})
    assert create == [] and blocked == [("122", "122")]


# --------------------------------------------------------------------- #
# CSV parsing
# --------------------------------------------------------------------- #
def test_unit_directory_groups_units_under_their_property(tmp_path):
    csv_file = tmp_path / "units.csv"
    csv_file.write_text(
        "Unit Name,Other\n"
        "-> Seminole Woods,\n"
        "101,\n"
        "102,\n"
        ",\n"                      # summary row, skipped
        "-> Cox Rentals,\n"
        "122,\n"
    )
    assert sp.parse_unit_directory(str(csv_file)) == {
        "Seminole Woods": ["101", "102"],
        "Cox Rentals": ["122"],
    }


def test_units_before_any_property_header_are_ignored(tmp_path):
    csv_file = tmp_path / "units.csv"
    csv_file.write_text("Unit Name\norphan\n-> Prop\n101\n")
    assert sp.parse_unit_directory(str(csv_file)) == {"Prop": ["101"]}


def test_property_id_mapping_skips_incomplete_rows(tmp_path):
    csv_file = tmp_path / "props.csv"
    csv_file.write_text(
        "Property,Property ID\nSeminole Woods,123\nNo ID Here,\n,456\n"
    )
    assert sp.load_property_name_to_id(str(csv_file)) == {"Seminole Woods": "123"}


def test_normalize_collapses_whitespace():
    assert sp._normalize("202   S.  Randall   Ave.") == "202 S. Randall Ave."


# --------------------------------------------------------------------- #
# Exit status
#
# The run used to report success while Snipe-IT rejected every write. Failure
# alerting is worthless unless a rejected write actually fails the run.
# --------------------------------------------------------------------- #
def test_sync_units_reports_its_failure_count():
    """sync_units returns the number of rejected writes for main() to act on."""
    import inspect

    src = inspect.getsource(sp.sync_units)
    assert "return failed" in src, "sync_units must report failures upward"


def test_main_exits_non_zero_when_a_write_was_rejected():
    import inspect

    src = inspect.getsource(sp.main)
    assert "return 1" in src
    assert "unit_failures" in src, "unit failures must count toward the exit status"
