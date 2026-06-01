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


def is_google_photos_share(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host.endswith("photos.app.goo.gl") or host.endswith("photos.google.com")


def is_icloud_share(url: str) -> bool:
    """Return True for any share.icloud.com / icloud.com photo-share URL.

    Used by the orchestrator to short-circuit iCloud URLs before attempting
    resolution, since Apple actively blocks headless browsers and the newer
    "iCloud Link" format (icloudlinks/…) is not accessible via the legacy
    sharedstreams API either.  These URLs require the manual WSL export
    workaround described in the README.
    """
    host = urlparse(url).netloc.lower()
    return "icloud.com" in host


def _is_js_rendered_host(url: str) -> bool:
    host = urlparse(url).netloc.lower()
    return host in JS_RENDERED_HOSTS or any(
        host.endswith("." + h) for h in JS_RENDERED_HOSTS
    )


# ---------------------------------------------------------------------- #
# iCloud sharedstreams API
# ---------------------------------------------------------------------- #
# Apple's web app calls this API to enumerate and download photos from a
# share. Talking to it directly is faster and more reliable than rendering
# their share page in a headless browser (which they actively try to block).
#
# Flow:
#   1. Parse the share token from the URL.
#   2. Compute the API base URL from the token's first character.
#   3. POST {"streamCtag": null} to /webstream → list of photos with
#      derivatives (different size variants and their checksums).
#   4. POST {"photoGuids": [...]} to /webasseturls → actual download URLs.
#   5. GET the download URL with a normal HTTP request.
#
# A 330 response carries an X-Apple-MMe-Host header; we follow it to the
# correct partition and retry the call.
ICLOUD_BASE62 = (
    "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789"
)
ICLOUD_TOKEN_RE = re.compile(
    r"https?://(?:www\.)?share\.icloud\.com/photos/([A-Za-z0-9_\-]+)",
    re.IGNORECASE,
)


def _icloud_token_from_url(url: str) -> Optional[str]:
    m = ICLOUD_TOKEN_RE.match(url)
    return m.group(1) if m else None


def _icloud_base_api_url(token: str) -> Optional[str]:
    """Compute the iCloud sharedstreams API base URL from a share token.

    The first character of the token, base62-decoded, is the server
    partition number. Apple zero-pads partitions below 40.
    """
    if not token or token[0] not in ICLOUD_BASE62:
        return None
    partition = ICLOUD_BASE62.index(token[0])
    server_num = partition + 1
    host = (
        f"p{server_num:02d}-sharedstreams.icloud.com"
        if server_num < 40
        else f"p{server_num}-sharedstreams.icloud.com"
    )
    return f"https://{host}/{token}/sharedstreams"


def _icloud_post(
    api_url: str, body: dict, *, timeout: int = 30, max_redirects: int = 3
) -> Optional[dict]:
    """POST JSON to an iCloud sharedstreams endpoint, following 330 redirects.

    iCloud returns 330 when we hit the wrong partition, with a
    ``X-Apple-MMe-Host`` header (or a body field) telling us where to go.
    """
    headers = {
        "Origin": "https://www.icloud.com",
        "Referer": "https://www.icloud.com/",
        "User-Agent": DEFAULT_HEADERS["User-Agent"],
        "Content-Type": "text/plain",
    }
    for _ in range(max_redirects):
        try:
            r = requests.post(
                api_url,
                headers=headers,
                json=body,
                timeout=timeout,
                allow_redirects=False,
            )
        except requests.RequestException as exc:
            log.warning("iCloud POST %s failed: %s", api_url, exc)
            return None

        if r.status_code == 330:
            new_host = r.headers.get("X-Apple-MMe-Host")
            if not new_host:
                try:
                    new_host = (r.json() or {}).get("X-Apple-MMe-Host")
                except Exception:
                    new_host = None
            if not new_host:
                log.warning(
                    "iCloud %s returned 330 without redirect host", api_url
                )
                return None
            api_url = re.sub(
                r"https?://[^/]+", f"https://{new_host}", api_url, count=1
            )
            log.info("iCloud redirected to %s", new_host)
            continue

        if r.status_code == 200:
            try:
                return r.json()
            except Exception as exc:
                log.warning("iCloud %s returned non-JSON: %s", api_url, exc)
                return None

        log.warning(
            "iCloud %s returned status %d (body: %s)",
            api_url,
            r.status_code,
            r.text[:200],
        )
        return None

    log.warning("iCloud %s exceeded %d redirects", api_url, max_redirects)
    return None


def _resolve_icloud_share(
    url: str, *, timeout: int = 30
) -> Optional[Tuple[str, str]]:
    """Resolve an iCloud share URL via Apple's sharedstreams API.

    Returns ``(image_url, mime_type)`` for the largest derivative of the
    first photo in the share, or None on failure. iCloud shares can hold
    multiple photos; for now we take the first one (and log a warning if
    there are more).
    """
    token = _icloud_token_from_url(url)
    if not token:
        log.warning("Could not extract token from iCloud URL: %s", url)
        return None

    base = _icloud_base_api_url(token)
    if not base:
        log.warning("Could not derive API URL for iCloud token %r", token)
        return None

    log.info("Calling iCloud sharedstreams API for token %s…", token[:8])

    stream = _icloud_post(
        f"{base}/webstream", {"streamCtag": None}, timeout=timeout
    )
    if not stream:
        return None

    photos = stream.get("photos") or []
    if not photos:
        log.warning("iCloud webstream returned no photos for %s", url)
        return None

    if len(photos) > 1:
        log.info(
            "iCloud share contains %d photos; only the first will be uploaded "
            "(album mode not yet supported)",
            len(photos),
        )

    photo = photos[0]
    derivatives = photo.get("derivatives") or {}
    if not derivatives:
        log.warning("iCloud photo has no derivatives")
        return None

    # Pick the derivative with the largest fileSize (highest resolution).
    best_key = None
    best_size = -1
    for key, deriv in derivatives.items():
        try:
            size = int(deriv.get("fileSize") or 0)
        except (TypeError, ValueError):
            size = 0
        if size > best_size:
            best_size = size
            best_key = key

    if not best_key:
        log.warning("iCloud derivatives had no usable fileSize")
        return None

    photo_guid = photo.get("photoGuid")
    if not photo_guid:
        log.warning("iCloud photo missing photoGuid")
        return None

    asset_resp = _icloud_post(
        f"{base}/webasseturls",
        {"photoGuids": [photo_guid]},
        timeout=timeout,
    )
    if not asset_resp:
        return None

    items = asset_resp.get("items") or {}
    locations = asset_resp.get("locations") or {}
    if not items or not locations:
        log.warning("iCloud webasseturls response missing items/locations")
        return None

    # The 'items' dict is keyed by the derivative checksum. Find the one
    # matching our chosen size; fall back to the first item.
    target_checksum = derivatives[best_key].get("checksum")
    item = items.get(target_checksum) if target_checksum else None
    if not item and items:
        target_checksum = next(iter(items))
        item = items[target_checksum]
    if not item:
        return None

    url_path = item.get("url_path")
    url_loc_id = item.get("url_location")
    if not url_path or not url_loc_id:
        log.warning("iCloud item missing url_path/url_location")
        return None

    location = locations.get(url_loc_id) or {}
    scheme = location.get("scheme") or "https"
    hosts = location.get("hosts") or []
    if not hosts:
        log.warning("iCloud location has no hosts")
        return None

    download_url = f"{scheme}://{hosts[0]}{url_path}"
    return download_url, "image/jpeg"


def _dump_icloud_debug(page, url: str, *, reason: str) -> None:
    """Dump page state to ``_debug/`` when iCloud resolution fails.

    Writes a PNG screenshot and a JSON file listing all visible clickable
    elements. The workflow uploads ``_debug/`` as an artifact so we can
    inspect what iCloud actually rendered to headless Chromium.
    """
    import json
    import os
    import time as _time

    try:
        debug_dir = os.path.join(os.getcwd(), "_debug")
        os.makedirs(debug_dir, exist_ok=True)
        stamp = _time.strftime("%Y%m%d-%H%M%S")
        png_path = os.path.join(debug_dir, f"icloud-{stamp}.png")
        json_path = os.path.join(debug_dir, f"icloud-{stamp}.json")

        # Screenshot — useful even if the rest fails.
        try:
            page.screenshot(path=png_path, full_page=True)
        except Exception as exc:
            log.warning("debug screenshot failed: %s", exc)

        # Enumerate clickable / interesting elements with their visible text.
        info = {}
        try:
            info["clickables"] = page.evaluate(
                """
                () => {
                    const out = [];
                    const els = document.querySelectorAll(
                        'button, [role="button"], a, input[type="submit"]'
                    );
                    for (const el of Array.from(els).slice(0, 60)) {
                        const r = el.getBoundingClientRect();
                        out.push({
                            tag: el.tagName,
                            role: el.getAttribute('role'),
                            text: (el.innerText || el.textContent || '').trim().slice(0, 80),
                            href: el.getAttribute('href') || '',
                            visible: r.width > 0 && r.height > 0,
                            rect: { w: Math.round(r.width), h: Math.round(r.height) }
                        });
                    }
                    return out;
                }
                """
            )
        except Exception as exc:
            info["clickables_error"] = str(exc)

        try:
            info["title"] = page.title()
            info["final_url"] = page.url
        except Exception:
            pass

        info["reason"] = reason
        info["source_url"] = url

        with open(json_path, "w", encoding="utf-8") as f:
            json.dump(info, f, indent=2)

        log.warning(
            "iCloud resolution failed (%s); wrote diagnostics to %s and %s",
            reason,
            png_path,
            json_path,
        )
    except Exception as exc:
        log.warning("Could not write iCloud debug dump: %s", exc)


def _resolve_icloud_link_download(
    url: str, *, timeout: int = 90
) -> Optional[Tuple[bytes, str, str]]:
    """Resolve a newer-format iCloud Link share by clicking the Download button.

    Apple's newer "Copy iCloud Link" feature (URLs that redirect to
    /photos/#/icloudlinks/...) uses an API we can't talk to directly, but
    the share page has a visible Download button. We drive that button
    via Playwright and capture the resulting download.

    Returns ``(bytes, mime_type, extension)`` or None on failure.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning(
            "Playwright not installed; cannot drive iCloud download for %s", url
        )
        return None

    # Stealth init script — iCloud actively detects headless browsers. The
    # main signal is navigator.webdriver === true; we hide that plus a few
    # related fingerprinting traces. Doesn't make us undetectable, but is
    # enough to get past iCloud's first-pass checks in most cases.
    stealth_init = """
        Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
        Object.defineProperty(navigator, 'languages', {
            get: () => ['en-US', 'en']
        });
        Object.defineProperty(navigator, 'plugins', {
            get: () => [1, 2, 3, 4, 5]
        });
        window.chrome = window.chrome || { runtime: {} };
        const origQuery = window.navigator.permissions && window.navigator.permissions.query;
        if (origQuery) {
            window.navigator.permissions.query = (params) => (
                params && params.name === 'notifications'
                    ? Promise.resolve({ state: Notification.permission })
                    : origQuery(params)
            );
        }
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=[
                    "--disable-blink-features=AutomationControlled",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                    "--disable-gpu",
                ],
            )
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    viewport={"width": 1440, "height": 900},
                    locale="en-US",
                    timezone_id="America/Chicago",
                    accept_downloads=True,
                )
                context.add_init_script(stealth_init)
                page = context.new_page()
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout * 1000,
                )
                # Let the SPA render. iCloud's web app builds the page
                # asynchronously after load.
                page.wait_for_timeout(7_000)

                # Find the Download button. iCloud renders it as a styled
                # <button> with text "Download" in English. We use a
                # text-based selector to stay resilient to class changes.
                try:
                    download_btn = page.wait_for_selector(
                        'button:has-text("Download"), '
                        'a:has-text("Download"), '
                        '[role="button"]:has-text("Download"), '
                        'button:has-text("Save"), '
                        '[role="button"]:has-text("Save")',
                        timeout=25_000,
                        state="visible",
                    )
                except Exception as exc:
                    _dump_icloud_debug(page, url, reason=f"button-timeout: {exc}")
                    return None

                if download_btn is None:
                    _dump_icloud_debug(page, url, reason="button-selector-missed")
                    return None

                # Click and wait for the download event.
                try:
                    with page.expect_download(timeout=timeout * 1000) as dl_info:
                        download_btn.click()
                    download = dl_info.value
                except Exception as exc:
                    log.warning(
                        "Clicking Download on %s did not produce a download: %s",
                        url,
                        exc,
                    )
                    return None

                # Persist to a temp path and read bytes. (Playwright won't
                # hand us the bytes directly — it streams to disk.)
                import os
                import tempfile

                suggested = download.suggested_filename or "photo.jpg"
                with tempfile.NamedTemporaryFile(
                    delete=False, suffix=f"_{suggested}"
                ) as tmp:
                    tmp_path = tmp.name
                try:
                    download.save_as(tmp_path)
                    with open(tmp_path, "rb") as f:
                        content = f.read()
                finally:
                    try:
                        os.unlink(tmp_path)
                    except OSError:
                        pass

                ext = (
                    suggested.rsplit(".", 1)[-1].lower() if "." in suggested else "jpg"
                )
                mime = {
                    "jpg": "image/jpeg",
                    "jpeg": "image/jpeg",
                    "png": "image/png",
                    "heic": "image/heic",
                    "heif": "image/heic",
                    "gif": "image/gif",
                    "webp": "image/webp",
                    "zip": "application/zip",
                }.get(ext, "image/jpeg")

                log.info(
                    "Downloaded iCloud link %s as %s (%d bytes)",
                    url,
                    suggested,
                    len(content),
                )

                # We don't currently unpack ZIPs (multi-photo shares). If
                # the user shares more than one photo, the Download button
                # gives us a ZIP — for now we skip those with a clear log.
                if mime == "application/zip":
                    log.warning(
                        "iCloud share %s contains multiple photos; ZIP archive "
                        "downloads aren't supported yet, skipping",
                        url,
                    )
                    return None

                return content, mime, ext
            finally:
                browser.close()
    except Exception as exc:
        log.warning(
            "Playwright-driven iCloud download failed for %s: %s", url, exc
        )
        return None


# ---------------------------------------------------------------------- #
# Playwright path (generic — for non-iCloud JS-rendered hosts)
# ---------------------------------------------------------------------- #
def _resolve_with_playwright(
    url: str, *, timeout: int = 60
) -> Optional[str]:
    """Render the page in headless Chromium and return the largest image URL.

    Returns the URL of the largest visible image on the page after the SPA
    has had a chance to render. We look at both <img> tags and CSS
    ``background-image`` declarations, because some hosts (notably iCloud
    shared streams) render photos as background images on <div>s rather
    than as <img> tags.

    Important: we do NOT use ``networkidle`` to detect "page is ready" —
    iCloud and similar hosts hold open long-polling / streaming requests
    that prevent networkidle from ever firing. Instead we wait for the
    DOM to be parsed and then settle for a fixed delay.

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

    # JS that scans the rendered DOM and returns the URL of the largest
    # visible image. Handles both <img> elements and CSS background-image.
    extract_js = """
        () => {
            const seen = new Set();
            const candidates = [];

            // Real <img> tags
            for (const img of document.querySelectorAll('img')) {
                const rect = img.getBoundingClientRect();
                const area = rect.width * rect.height;
                const src = img.currentSrc || img.src || '';
                if (src && !src.startsWith('data:') && !seen.has(src) && area > 100) {
                    seen.add(src);
                    candidates.push({ src, area });
                }
            }

            // CSS background-image (iCloud renders photos this way)
            for (const el of document.querySelectorAll('div, span, section, a, figure, picture')) {
                try {
                    const bg = window.getComputedStyle(el).backgroundImage;
                    const m = bg && bg.match(/url\\(["']?(.+?)["']?\\)/);
                    if (m && m[1] && !m[1].startsWith('data:') && !seen.has(m[1])) {
                        const rect = el.getBoundingClientRect();
                        const area = rect.width * rect.height;
                        if (area > 100) {
                            seen.add(m[1]);
                            candidates.push({ src: m[1], area });
                        }
                    }
                } catch (e) {}
            }

            candidates.sort((a, b) => b.area - a.area);
            return candidates.length ? candidates[0].src : null;
        }
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                # 'domcontentloaded' fires as soon as the HTML is parsed,
                # without waiting for ongoing network activity. iCloud's
                # streaming requests would otherwise prevent networkidle.
                page.goto(
                    url,
                    wait_until="domcontentloaded",
                    timeout=timeout * 1000,
                )
                # Let the SPA render. iCloud needs a generous window
                # because it makes its photo API call after page load.
                page.wait_for_timeout(10_000)
                largest = page.evaluate(extract_js)
                return largest
            finally:
                browser.close()
    except Exception as exc:
        log.warning("Playwright resolution failed for %s: %s", url, exc)
        return None


def resolve_google_photos_album(
    url: str,
    *,
    timeout: int = 90,
    max_photos: int = 50,
    max_bytes: int = 50 * 1024 * 1024,
) -> List["ResolvedImage"]:
    """Resolve a Google Photos share link and return every photo in the album.

    Uses Playwright to render the album grid, scrolls to trigger lazy loading,
    collects all thumbnail URLs, upgrades them to full resolution, then
    downloads each one.  Returns an empty list on any failure.

    For single-photo shares this returns a one-item list, which lets callers
    use this function uniformly for all Google Photos URLs.
    """
    try:
        from playwright.sync_api import sync_playwright  # type: ignore
    except ImportError:
        log.warning("Playwright not installed; cannot resolve Google Photos album %s", url)
        return []

    # JS that collects all distinct Google CDN image URLs visible in the grid.
    extract_js = """
        () => {
            const seen = new Set();
            const urls = [];
            for (const img of document.querySelectorAll('img')) {
                const src = img.currentSrc || img.src || '';
                if (!src || src.startsWith('data:') || seen.has(src)) continue;
                if (!src.includes('googleusercontent.com') && !src.includes('ggpht.com')) continue;
                const rect = img.getBoundingClientRect();
                if (rect.width < 50 || rect.height < 50) continue;
                seen.add(src);
                urls.push(src);
            }
            return urls;
        }
    """

    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--disable-blink-features=AutomationControlled"],
            )
            try:
                context = browser.new_context(
                    user_agent=DEFAULT_HEADERS["User-Agent"],
                    viewport={"width": 1440, "height": 900},
                )
                page = context.new_page()
                page.goto(url, wait_until="domcontentloaded", timeout=timeout * 1000)
                page.wait_for_timeout(5_000)

                # Scroll to trigger lazy loading of album thumbnails.
                for _ in range(5):
                    page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                    page.wait_for_timeout(1_500)

                raw_urls = page.evaluate(extract_js) or []
            finally:
                browser.close()
    except Exception as exc:
        log.warning("Playwright album resolution failed for %s: %s", url, exc)
        return []

    # Upgrade thumbnails to full resolution and deduplicate.
    full_res_urls: List[str] = []
    seen: set = set()
    for raw in raw_urls[:max_photos]:
        upgraded = _upgrade_google_photos_url(raw)
        if upgraded not in seen:
            seen.add(upgraded)
            full_res_urls.append(upgraded)

    if not full_res_urls:
        log.warning("No images found in Google Photos album: %s", url)
        return []

    log.info("Found %d image(s) in Google Photos album %s", len(full_res_urls), url)

    min_photo_bytes = 50 * 1024
    results: List[ResolvedImage] = []
    for img_url in full_res_urls:
        dl = _download_image(img_url, referer=url, max_bytes=max_bytes)
        if dl:
            content, mime = dl
            if len(content) >= min_photo_bytes:
                results.append(
                    ResolvedImage(
                        source_url=url,
                        final_url=img_url,
                        content=content,
                        mime_type=mime,
                        extension=_ext_for_mime(mime),
                    )
                )

    log.info(
        "Downloaded %d/%d photo(s) from Google Photos album %s",
        len(results),
        len(full_res_urls),
        url,
    )
    return results


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

    # Photos are typically much bigger than UI chrome / logos. If
    # Playwright (or any path) returns something tiny, treat it as a
    # logo grab and reject. 50 KB is a generous floor — even a small
    # phone photo at modest quality is several hundred KB.
    min_real_photo_bytes = 50 * 1024

    # ------- Path 0: iCloud shares — manual export required ------------ #
    # Apple actively blocks headless browsers on share.icloud.com, and the
    # newer "Copy iCloud Link" format (/photos/#/icloudlinks/…) is not
    # reachable via the legacy sharedstreams API either.  The orchestrator
    # is expected to catch these via is_icloud_share() *before* calling
    # resolve_url(), but we keep this guard as a safety net so a stray
    # iCloud URL never silently falls through to the HTTP path (which would
    # download Apple's branding logo instead of the photo).
    if is_icloud_share(url):
        log.warning(
            "iCloud share URL requires manual export and cannot be resolved "
            "automatically — skipping: %s",
            url,
        )
        return None

    # ------- Path 1: JS-rendered hosts go through Playwright (only) ------ #
    if js_host and use_playwright:
        log.info("Using Playwright for JS-rendered host: %s", url)
        rendered_image_url = _resolve_with_playwright(url, timeout=60)
        if rendered_image_url:
            if is_google_photos_share(url):
                rendered_image_url = _upgrade_google_photos_url(rendered_image_url)
            result = _download_image(
                rendered_image_url,
                referer=url,
                timeout=timeout,
                max_bytes=max_bytes,
            )
            if result:
                content, mime = result
                if len(content) < min_real_photo_bytes:
                    log.warning(
                        "Playwright returned %s for %s but it's only %d bytes "
                        "(likely a logo / UI asset); rejecting",
                        rendered_image_url,
                        url,
                        len(content),
                    )
                else:
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
        # JS-rendered hosts (iCloud, Google Photos) deliberately serve
        # branding-only og:image meta tags, so an HTTP fallback would
        # download the wrong thing. Fail cleanly instead.
        log.warning(
            "Could not resolve %s — Playwright did not yield a usable photo and "
            "HTTP fallback is disabled for this host (its og:image points to a "
            "logo). The share may be private, expired, or require sign-in.",
            url,
        )
        return None

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

    if is_google_photos_share(url):
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
