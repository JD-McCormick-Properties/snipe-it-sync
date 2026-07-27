"""One-off: find out what is holding a location name Snipe-IT calls taken.

Usage: python diagnose_name.py "2898-202"
"""

from __future__ import annotations

import html
import os
import sys

import requests

SNIPE_URL = os.environ["SNIPE_URL"].strip().rstrip("/")
HEADERS = {
    "Authorization": f"Bearer {os.environ['SNIPE_API_KEY'].strip()}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}


def get(path, **params):
    r = requests.get(f"{SNIPE_URL}{path}", headers=HEADERS, params=params, timeout=30)
    r.raise_for_status()
    return r.json()


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
    print("near-matches across all live locations:")
    out, offset = [], 0
    while True:
        data = get("/api/v1/locations", limit=500, offset=offset)
        rows = data.get("rows") or []
        out.extend(rows)
        if len(rows) < 500:
            break
        offset += 500
    t = target.lower().replace(" ", "")
    for r in out:
        n = html.unescape(r.get("name") or "").strip()
        if t in n.lower().replace(" ", "") or n.lower().replace(" ", "") in t:
            print(f"    id={r['id']} {n!r}")
    print(f"\n({len(out)} live locations scanned)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
