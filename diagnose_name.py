"""One-off: find out what is holding a location name Snipe-IT calls taken.

Usage: python diagnose_name.py "2898-202"
"""

from __future__ import annotations

import html
import os
import sys
import time

import requests

SNIPE_URL = os.environ["SNIPE_URL"].strip().rstrip("/")
HEADERS = {
    "Authorization": f"Bearer {os.environ['SNIPE_API_KEY'].strip()}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def get(path, **params):
    """GET with backoff — the instance refuses connections under load."""
    last = None
    for attempt in range(5):
        try:
            r = requests.get(
                f"{SNIPE_URL}{path}", headers=HEADERS, params=params, timeout=30
            )
            if r.status_code in (429, 500, 502, 503, 504):
                last = f"HTTP {r.status_code}"
                time.sleep(2 ** attempt)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            last = str(exc)[:90]
            time.sleep(2 ** attempt)
    raise RuntimeError(f"giving up after 5 tries: {last}")


def main() -> int:
    target = sys.argv[1] if len(sys.argv) > 1 else "2898-202"
    print(f"looking for a location named {target!r}\n")

    for label, params in [
        ("search, default (live only)", {"search": target, "limit": 50}),
        ("search, deleted=true", {"search": target, "limit": 50, "deleted": "true"}),
        ("search, all", {"search": target, "limit": 50, "all": "true"}),
    ]:
        try:
            data = get("/api/v1/locations", **params)
        except Exception as exc:
            print(f"{label}: request failed — {exc}")
            continue
        rows = data.get("rows") or []
        exact = [
            r for r in rows
            if html.unescape(r.get("name") or "").strip().lower() == target.lower()
        ]
        print(f"{label}: total={data.get('total')} returned={len(rows)} exact={len(exact)}")
        for r in exact:
            parent = (r.get("parent") or {}).get("name") if isinstance(r.get("parent"), dict) else None
            print(f"    id={r.get('id')} name={html.unescape(r.get('name') or '')!r}")
            print(f"      parent={parent!r} deleted_at={r.get('deleted_at')!r}")
            print(f"      assets={r.get('assets_count')} children={r.get('children_count')}")
        print()

    # Is it perhaps not a location at all? Names collide within locations only,
    # but check the full unpaged list one more time for near-matches.
    # Does the paged fetch the sync relies on actually return this record?
    print("paginating exactly the way sync_properties does:")
    out, offset, pages = [], 0, []
    while True:
        data = get("/api/v1/locations", limit=500, offset=offset)
        rows = data.get("rows") or []
        pages.append((offset, len(rows), data.get("total")))
        out.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
    for off, n, total in pages:
        print(f"    offset={off:<6} rows={n:<5} total_reported={total}")

    ids = [r["id"] for r in out]
    print(f"\n    fetched {len(out)} rows, {len(set(ids))} unique ids")
    print(f"    id 925 present in paged fetch: {925 in set(ids)}")
    names = {html.unescape(r.get("name") or "").strip().lower() for r in out}
    print(f"    {target!r} present in paged names: {target.lower() in names}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
