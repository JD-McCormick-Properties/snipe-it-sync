"""Hashing, date parsing and filename construction.

Each test here corresponds to a bug that reached production, so a failure
means one of those has come back.
"""

from __future__ import annotations

from datetime import datetime, timezone

import pytest
from conftest import encode, make_photo

from helpers.image_utils import (
    build_filename,
    hash_bytes,
    parse_dt,
    perceptual_hash,
    safe_asset_tag,
    safe_name,
)


# --------------------------------------------------------------------- #
# Perceptual hashing
#
# Google re-serves the same photo with slightly different compressed bytes,
# so SHA-256 reported every re-fetch as a new image and the sync re-uploaded
# whole albums on every run.
# --------------------------------------------------------------------- #
def test_sha256_is_unstable_across_reencoding(photo_a):
    """The reason a byte hash can't be the only dedupe key."""
    assert hash_bytes(encode(photo_a, 92)) != hash_bytes(encode(photo_a, 92, optimize=True))


def test_perceptual_hash_survives_reencoding(photo_a):
    a = perceptual_hash(encode(photo_a, 92))
    b = perceptual_hash(encode(photo_a, 92, optimize=True))
    assert a == b


def test_perceptual_hash_survives_lossier_reencoding(photo_a):
    """Production drift was ~1 KB; this is a far harsher 15%+ size change."""
    original = encode(photo_a, 92)
    degraded = encode(photo_a, 80)
    assert len(degraded) < len(original) * 0.9
    a, b = perceptual_hash(original), perceptual_hash(degraded)
    assert _distance(a, b) <= 20


def test_perceptual_hash_separates_different_photos(photo_a, photo_b):
    a, b = perceptual_hash(encode(photo_a)), perceptual_hash(encode(photo_b))
    assert a != b
    # Comfortably beyond the 20-bit match threshold.
    assert _distance(a, b) > 40


def test_perceptual_hash_is_256_bits(photo_a):
    assert len(perceptual_hash(encode(photo_a))) == 64


def test_perceptual_hash_returns_none_for_non_image():
    assert perceptual_hash(b"this is not an image") is None
    assert perceptual_hash(b"") is None


def test_perceptual_hash_is_deterministic(photo_a):
    data = encode(photo_a)
    assert perceptual_hash(data) == perceptual_hash(data)


def _distance(a: str, b: str) -> int:
    return bin(int(a, 16) ^ int(b, 16)).count("1")


# --------------------------------------------------------------------- #
# Date parsing
#
# parse_dt used to fall back to datetime.now() for Snipe-IT's lowercase
# "9:01am", so filenames got stamped with the run time and every run wrote
# the same photo under a new name.
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "text,expected",
    [
        ("2026-07-17 09:01:45", datetime(2026, 7, 17, 9, 1, 45)),
        ("2026-07-17T09:01:45Z", datetime(2026, 7, 17, 9, 1, 45)),
        ("July 17, 2026 09:01 AM", datetime(2026, 7, 17, 9, 1)),
        ("July 17, 2026 09:01 am", datetime(2026, 7, 17, 9, 1)),
        ("July 17, 2026 09:01pm", datetime(2026, 7, 17, 21, 1)),
    ],
)
def test_parse_dt_handles_snipeit_formats(text, expected):
    assert parse_dt(text).replace(tzinfo=None) == expected


def test_parse_dt_falls_back_to_now_for_junk():
    """Unparseable input must not raise; callers rely on getting a datetime."""
    result = parse_dt("not a date at all")
    assert abs((datetime.now(timezone.utc) - result).total_seconds()) < 10


# --------------------------------------------------------------------- #
# Filenames
# --------------------------------------------------------------------- #
def test_build_filename_includes_uploader_and_stamp():
    assert build_filename("Silverado 2007", "Nick Brown", "2026-07-17 09:01:45", "jpg") == (
        "Silverado 2007 - Nick Brown - 2026-07-17 09-01-45.jpg"
    )


def test_build_filename_omits_empty_uploader():
    assert build_filename("Silverado 2007", "", "2026-07-17 09:01:45", "jpg") == (
        "Silverado 2007 - 2026-07-17 09-01-45.jpg"
    )


def test_build_filename_is_stable_for_the_same_event():
    """Two runs processing one event must produce identical filenames."""
    args = ("Silverado 2007", "Nick Brown", "2026-07-17 09:01:45", "jpg")
    assert build_filename(*args) == build_filename(*args)


def test_safe_name_strips_path_separators():
    assert "/" not in safe_name("Check Out / In")
    assert "\\" not in safe_name("a\\b")


def test_safe_asset_tag_never_returns_empty():
    """Callers use the result as a name, so it must never be blank."""
    assert safe_asset_tag("") == "untagged"
    assert safe_asset_tag("   ") == "untagged"
    # All-punctuation collapses to a placeholder rather than the empty string.
    assert safe_asset_tag("///") != ""


def test_safe_asset_tag_keeps_ordinary_tags_intact():
    assert safe_asset_tag("AT-1042") == "AT-1042"
    assert safe_asset_tag(" AT-1042 ") == "AT-1042"
