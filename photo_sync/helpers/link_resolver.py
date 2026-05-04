"""Resolve URLs found in Snipe-IT notes to actual image bytes.

Strategy (HTTP only — no headless browser):

1. Detect URLs in free-text notes.
2. For each URL, follow redirects.
3. If the final response is an image content-type, return its bytes.
4. Otherwise parse the HTML for OpenGraph / Twitter / itemprop image tags
   and download the first viable image we find.
5. Special-case Google Photos and iCloud share pages, both of which embed
   the image URL in og:image (or in some cases a thumbnail URL we have to
   upgrade to a higher resolution).

This module is deliberately conservative: it returns None on any failure
so the orchestrator can log and move on instead of crashing the run.
"""

from __future__ import annotations

import logging
import re
from dataclasses import dataclass
from typing import List, Optional, Tuple
from urllib.parse import urljoin, urlparse

import requests
from bs4 import BeautifulSoup

log = logging.getLogger(__name__)

# Conservative URL extraction — captures http(s) URLs up to whitespace or
# a small set of trailing punctuation that's almost certainly not part of
# the URL itself.
URL_RE = re.compile(r"https?://[^\s<>\"'\)\]\}]+", re.IGNORECASE)

DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0 Safari/537.36"
    ),
    "Accept": (
        "text/html,application/xhtml+xml,application/xml;q=0.9,"
        "image/avif,image/webp,image/apng,*/*;q=0.8"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}

# Trailing characters we'll strip from a URL (common note-formatting noise).
TRAILING_TRIM = ".,;:!?\"')]}"

IMAGE_MIME_PREFIXES = ("image/",)
IMAGE_EXT_BY_MIME = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/gif": "gif",
    "image/webp": "webp",
    "image/heic": "heic",
    "image/heif": "heic",
}


@dataclass
class ResolvedImage:
    """An image successfully resolved from a notes URL."""

    source_url: str       # The URL as written in Snipe-IT notes
    final_url: str        # The actual image URL after redirects/scraping
    content: bytes        # Raw image bytes
    mime_type: str        # e.g. "image/jpeg"
    extension: str        # e.g. "jpg"


# ---------------------------------------------------------------------- #
# URL extraction
# ---------------------------------------------------------------------- #
def extract_urls(notes: str) -> List[str]:
    """Return distinct URLs found in a notes string, order-preserving."""
    if not notes:
        return []
    seen: set[str] = set()
    out: List[str] = []
    for match in URL_RE.finditer(notes):
        url = match.group(0).rstrip(TRAILING_TRIM)
        if url and url not in seen:
            seen.add(url)
            out.append(url)
    return out


# ---------------------------------------------------------------------- #
# HTTP helpers
# ---------------------------------------------------------------------- #
def _build_session() -> requests.Session:
    s = requests.Session()
    s.headers.update(DEFAULT_HEADERS)
    return s


def _is_image_response(resp: requests.Response) -> Tuple[bool, str]:
    ct = (resp.headers.get("Content-Type") or "").split(";")[0].strip().lower()
    return ct.startswith(IMAGE_MIME_PREFIXES), ct


def _ext_for_mime(mime: str, fallback: str = "jpg") -> str:
    return IMAGE_EXT_BY_MIME.get(mime.lower(), fallback)


# ---------------------------------------------------------------------- #
# HTML scraping for image URLs
# ---------------------------------------------------------------------- #
META_TAG_PRIORITY = [
    ("meta", {"property": "og:image:secure_url"}, "content"),
    ("meta", {"property": "og:image"}, "content"),
    ("meta", {"name": "og:image"}, "content"),
    ("meta", {"name": "twitter:image"}, "content"),
    ("meta", {"name": "twitter:image:src"}, "content"),
    ("meta", {"itemprop": "image"}, "content"),
    ("link", {"rel": "image_src"}, "href"),
]


def _find_image_url_in_html(html: str, base_url: str) -> Optional[str]:
    """Inspect an HTML document for the most-likely image URL."""
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("Failed to parse HTML for %s: %s", base_url, exc)
        return None

    for tag_name, attrs, value_attr in META_TAG_PRIORITY:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get(value_attr):
            candidate = tag.get(value_attr).strip()
            if candidate:
                return urljoin(base_url, candidate)

    # Last resort: first <img src>
    img = soup.find("img")
    if img and img.get("src"):
        return urljoin(base_url, img["src"].strip())

    return None


# ---------------------------------------------------------------------- #
# Provider-specific tweaks
# ---------------------------------------------------------------------- #
def _upgrade_google_photos_url(url: str) -> str:
    """Google Photos thumbnails come back at low resolution by default.

    The `=w...-h...` query suffix controls dimensions. Strip it (or replace
    it with a larger size) to get the original/high-res variant.
    """
    if "googleusercontent.com" not in url and "ggpht.com" not in url:
        return url
    # Google's CDN uses '=' as a separator for size parameters
    if "=" in url:
        base, _ = url.split("=", 1)
        return f"{base}=s0"  # s0 = original size
    return url


def _is_google_photos_share(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("photos.app.goo.gl") or host.endswith("photos.google.com")


def _is_icloud_share(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return "icloud.com" in host


# ---------------------------------------------------------------------- #
# Main resolver
# ---------------------------------------------------------------------- #
def resolve_url(
    url: str,
    *,
    timeout: int = 30,
    max_bytes: int = 50 * 1024 * 1024,
) -> Optional[ResolvedImage]:
    """Resolve a URL to image bytes, or return None if it can't be resolved.

    Args:
        url: A URL extracted from a Snipe-IT notes field.
        timeout: Per-request timeout in seconds.
        max_bytes: Hard cap on download size.
    """
    session = _build_session()

    try:
        resp = session.get(url, timeout=timeout, allow_redirects=True, stream=True)
    except requests.RequestException as exc:
        log.warning("GET %s failed: %s", url, exc)
        return None

    final_url = resp.url
    is_image, mime = _is_image_response(resp)

    if is_image:
        content = _read_capped(resp, max_bytes)
        if content is None:
            return None
        return ResolvedImage(
            source_url=url,
            final_url=final_url,
            content=content,
            mime_type=mime,
            extension=_ext_for_mime(mime),
        )

    # Otherwise: assume HTML and look for an image inside.
    try:
        html = resp.text
    except Exception as exc:
        log.warning("Could not decode HTML at %s: %s", final_url, exc)
        return None
    finally:
        resp.close()

    image_url = _find_image_url_in_html(html, base_url=final_url)
    if not image_url:
        log.info("No image found in HTML at %s", final_url)
        return None

    if _is_google_photos_share(url):
        image_url = _upgrade_google_photos_url(image_url)

    # iCloud's og:image already points at a CDN-hosted JPG, no rewrite needed.
    _ = _is_icloud_share  # referenced for symmetry / future tweaks

    try:
        img_resp = session.get(
            image_url, timeout=timeout, allow_redirects=True, stream=True
        )
    except requests.RequestException as exc:
        log.warning("GET %s (image) failed: %s", image_url, exc)
        return None

    is_image, mime = _is_image_response(img_resp)
    if not is_image:
        log.info(
            "URL %s pointed to og:image %s but content-type was %s",
            url,
            image_url,
            mime or "?",
        )
        img_resp.close()
        return None

    content = _read_capped(img_resp, max_bytes)
    if content is None:
        return None

    return ResolvedImage(
        source_url=url,
        final_url=image_url,
        content=content,
        mime_type=mime,
        extension=_ext_for_mime(mime),
    )


def _read_capped(resp: requests.Response, max_bytes: int) -> Optional[bytes]:
    """Stream a response into memory, refusing to exceed max_bytes."""
    chunks: List[bytes] = []
    total = 0
    try:
        for chunk in resp.iter_content(chunk_size=64 * 1024):
            if not chunk:
                continue
            total += len(chunk)
            if total > max_bytes:
                log.warning(
                    "Refusing to download %s: exceeded max_bytes (%d)",
                    resp.url,
                    max_bytes,
                )
                return None
            chunks.append(chunk)
    except requests.RequestException as exc:
        log.warning("Stream read failed for %s: %s", resp.url, exc)
        return None
    finally:
        resp.close()
    return b"".join(chunks)
