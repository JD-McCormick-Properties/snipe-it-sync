"""URL classification and the Google Photos resolution size fix."""

from __future__ import annotations

import pytest

from helpers.link_resolver import (
    _upgrade_google_photos_url,
    extract_urls,
    is_google_photos_share,
    is_icloud_share,
)


# --------------------------------------------------------------------- #
# Full-resolution upgrade
#
# This only forced =s0 when the scraped URL already carried a size suffix.
# The album grid is scraped mid-render, so the same photo arrives with or
# without one depending on timing, and the un-suffixed form downloaded a
# ~90 KB thumbnail that then overwrote the full-resolution copy.
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "raw",
    [
        "https://lh3.googleusercontent.com/pw/AP1GczABC",              # no suffix
        "https://lh3.googleusercontent.com/pw/AP1GczABC=w400-h300",    # grid thumbnail
        "https://lh3.googleusercontent.com/pw/AP1GczABC=w400-h300-c",  # cropped
        "https://lh3.googleusercontent.com/pw/AP1GczABC=s0",           # already full
    ],
)
def test_every_google_url_shape_becomes_full_resolution(raw):
    assert _upgrade_google_photos_url(raw) == (
        "https://lh3.googleusercontent.com/pw/AP1GczABC=s0"
    )


def test_upgrade_is_idempotent():
    once = _upgrade_google_photos_url("https://lh3.googleusercontent.com/pw/X=w100")
    assert _upgrade_google_photos_url(once) == once


def test_non_google_urls_pass_through_untouched():
    for url in [
        "https://example.com/photo.jpg",
        "https://example.com/photo.jpg?size=w400",
        "https://share.icloud.com/photos/abc",
    ]:
        assert _upgrade_google_photos_url(url) == url


# --------------------------------------------------------------------- #
# Host classification
# --------------------------------------------------------------------- #
@pytest.mark.parametrize(
    "url,is_gphotos",
    [
        ("https://photos.app.goo.gl/abc123", True),
        ("https://photos.google.com/share/abc", True),
        ("https://example.com/photos.app.goo.gl", False),
        ("https://share.icloud.com/photos/abc", False),
    ],
)
def test_google_photos_detection(url, is_gphotos):
    assert is_google_photos_share(url) is is_gphotos


@pytest.mark.parametrize(
    "url,is_icloud",
    [
        ("https://share.icloud.com/photos/abc", True),
        ("https://www.icloud.com/photos/abc", True),
        ("https://photos.app.goo.gl/abc", False),
    ],
)
def test_icloud_detection(url, is_icloud):
    assert is_icloud_share(url) is is_icloud


# --------------------------------------------------------------------- #
# URL extraction from note text
# --------------------------------------------------------------------- #
def test_extracts_multiple_urls_from_a_note():
    note = (
        "Checked out for the Maple St job.\n"
        "Photos: https://photos.app.goo.gl/aaa and https://photos.app.goo.gl/bbb"
    )
    found = extract_urls(note)
    assert "https://photos.app.goo.gl/aaa" in found
    assert "https://photos.app.goo.gl/bbb" in found


def test_note_without_urls_yields_nothing():
    assert extract_urls("Returned with a cracked mirror, no photos taken") == []


def test_empty_note_yields_nothing():
    assert extract_urls("") == []
