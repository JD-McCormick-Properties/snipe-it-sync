"""Resolve URLs found in Snipe-IT notes to actual image bytes.

Two-tier strategy:

* **HTTP-only path** (default for most URLs): follow redirects, accept
  direct image responses, otherwise parse HTML for OpenGraph / Twitter /
  itemprop image tags. Fast and dependency-light.
* **Playwright path** (used for known JavaScript-rendered hosts like
  iCloud and Google Photos): launch headless Chromium, render the share
  page, and grab the URL of the largest visible image.

The HTTP path no longer falls back to "first <img> on the page" — that
fallback was prone to grabbing site logos. JavaScript-heavy share pages
must go through Playwright instead.

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

# Hosts whose share pages render the actual photo via JavaScript. For
# these, the initial HTML doesn't carry the real image URL, so we go
# straight to Playwright.
JS_RENDERED_HOSTS = {
    "share.icloud.com",
    "www.icloud.com",
    "icloud.com",
    "photos.app.goo.gl",
    "photos.google.com",
    "www.photos.google.com",
}

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


def _find_image_url_in_html(html: str, base_url: str) -> Tuple[Optional[str], str]:
    """Inspect an HTML document for the most-likely image URL.

    Returns ``(image_url, selector_used)``. ``selector_used`` is a short
    label like ``og:image`` or ``twitter:image`` for diagnostic logging,
    or ``""`` if nothing matched.

    Note: deliberately does NOT fall back to the first <img> tag — that
    fallback was prone to grabbing site logos on JavaScript-heavy share
    pages (iCloud, Google Photos). Those hosts go through Playwright.
    """
    try:
        soup = BeautifulSoup(html, "html.parser")
    except Exception as exc:  # pragma: no cover — defensive
        log.warning("Failed to parse HTML for %s: %s", base_url, exc)
        return None, ""

    for tag_name, attrs, value_attr in META_TAG_PRIORITY:
        tag = soup.find(tag_name, attrs=attrs)
        if tag and tag.get(value_attr):
            candidate = tag.get(value_attr).strip()
            if candidate:
                # Build a label like "og:image" or "twitter:image" or "link[image_src]"
                if tag_name == "link":
                    label = f"link[{attrs.get('rel', '')}]"
                else:
                    key = (
                        attrs.get("property")
                        or attrs.get("name")
                        or attrs.get("itemprop")
                        or ""
                    )
                    label = key
                return urljoin(base_url, candidate), label

    return None, ""


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


def _is_js_rendered_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in JS_RENDERED_HOSTS or any(
        host.endswith("." + h) for h in JS_RENDERED_HOSTS
    )


# ---------------------------------------------------------------------- #
# Playwright path
# ---------------------------------------------------------------------- #
def _resolve_with_playwright(
    url: str, *, timeout: int = 30
) -> Optional[str]:
    """Render the page in headless Chromium and return the largest image URL.

    Returns the ``src`` URL of the largest visible <img> on the page after
    JavaScript has loaded. We pick the largest image because share pages
    typically render the actual photo as the dominant visual element while
    site chrome (logos, icons) is much smaller.

    Returns None if Playwright isn't installed, the page didn't load, or
    no usable image was found.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning(
            "Playwright not installed; cannot resolve JS-rendered URL %s", url
        )
        return None

    js = """
        () => {
            const imgs = Array.from(document.querySelectorAll('img'));
            let best = null;
            let bestArea = 0;
            for (const img of imgs) {
                const rect = img.getBoundingClientRect();
                const area = rect.width * rect.height;
                const src = img.currentSrc || img.src || '';
                if (area > bestArea && src && !src.startsWith('data:')) {
                    bestArea = area;
                    best = src;
                }
            }
            return best;
        }
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(headless=True)
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    viewport={"width": 1280, "height": 1600},
                )
                page = context.new_page()
                page.goto(url, wait_until="networkidle", timeout=timeout * 1000)
                # Give iCloud / Google Photos a moment to actually render
                # the image after networkidle (they sometimes lazy-load).
                try:
                    page.wait_for_selector("img", state="visible", timeout=5_000)
                except Exception:
                    pass
                page.wait_for_timeout(1_500)
                largest = page.evaluate(js)
                return largest
            finally:
                browser.close()
    except Exception as exc:
        log.warning("Playwright resolution failed for %s: %s", url, exc)
        return None


def _download_image(
    image_url: str,
    *,
    referer: Optional[str] = None,
    timeout: int = 30,
    max_bytes: int = 50 * 1024 * 1024,
) -> Optional[Tuple[bytes, str]]:
    """Fetch image bytes from a direct URL. Returns (content, mime) or None."""
    session = _build_session()
    headers = {}
    if referer:
        headers["Referer"] = referer
    try:
        resp = session.get(
            image_url,
            headers=headers,
            timeout=timeout,
            allow_redirects=True,
            stream=True,
        )
    except requests.RequestException as exc:
        log.warning("GET %s (image) failed: %s", image_url, exc)
        return None

    is_image, mime = _is_image_response(resp)
    if not is_image:
        log.info(
            "URL %s did not return an image (content-type: %s)",
            image_url,
            mime or "?",
        )
        resp.close()
        return None

    content = _read_capped(resp, max_bytes)
    if content is None:
        return None
    return content, mime


# ---------------------------------------------------------------------- #
# Main resolver
# ---------------------------------------------------------------------- #
def resolve_url(
    url: str,
    *,
    timeout: int = 30,
    max_bytes: int = 50 * 1024 * 1024,
    use_playwright: bool = True,
) -> Optional[ResolvedImage]:
    """Resolve a URL to image bytes, or return None if it can't be resolved.

    For known JavaScript-rendered hosts (iCloud, Google Photos shares),
    we go through Playwright first since their initial HTML doesn't carry
    the real image URL. For everything else, HTTP-only with an OpenGraph
    fallback handles it cheaply.

    Args:
        url: A URL extracted from a Snipe-IT notes field.
        timeout: Per-request timeout in seconds.
        max_bytes: Hard cap on download size.
        use_playwright: If False, skip the Playwright path entirely.
    """
    js_host = _is_js_rendered_host(url)

    # ------- Path 1: JS-rendered hosts go through Playwright first ------- #
    if js_host and use_playwright:
        log.info("Using Playwright for JS-rendered host: %s", url)
        rendered_image_url = _resolve_with_playwright(url, timeout=timeout)
        if rendered_image_url:
            if _is_google_photos_share(url):
                rendered_image_url = _upgrade_google_photos_url(rendered_image_url)
            result = _download_image(
                rendered_image_url,
                referer=url,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            if result:
                content, mime = result
                log.info(
                    "Resolved %s via Playwright (image: %s, %d bytes)",
                    url,
                    rendered_image_url,
                    len(content),
                )
                return ResolvedImage(
                    source_url=url,
                    final_url=rendered_image_url,
                    content=content,
                    mime_type=mime,
                    extension=_ext_for_mime(mime),
                )
        log.warning("Playwright path yielded no image for %s; falling back to HTTP", url)

    # ------- Path 2: HTTP-only --------- #
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
        log.info("Resolved %s as direct image (%d bytes)", url, len(content))
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

    image_url, selector = _find_image_url_in_html(html, base_url=final_url)
    if not image_url:
        log.info("No image meta tag found in HTML at %s", final_url)
        return None

    if _is_google_photos_share(url):
        image_url = _upgrade_google_photos_url(image_url)

    result = _download_image(
        image_url, referer=final_url, timeout=timeout, max_bytes=max_bytes
    )
    if not result:
        return None
    content, mime = result
    log.info(
        "Resolved %s via HTML %s (image: %s, %d bytes)",
        url,
        selector,
        image_url,
        len(content),
    )
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
