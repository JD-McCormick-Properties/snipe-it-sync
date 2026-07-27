"""Remove sublocations whose name repeats their parent, e.g. "X - X".

A qualified-naming fallback briefly created one of these for every
single-unit property, where the property lists itself as its only unit so the
unit name collided with its own parent. The parent location already
represents those units, so the children are redundant.

Only deletes a location that satisfies all of:
  * name is exactly "<something> - <the same something>"
  * the repeated half equals its parent's name
  * it has no assets, users, or child locations attached

Dry run by default; --delete performs the removal.
"""

from __future__ import annotations

import argparse
import html
import os
import sys

import requests

SNIPE_URL = os.environ["SNIPE_URL"].strip().rstrip("/")
API_KEY = os.environ["SNIPE_API_KEY"].strip()
HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json",
}
TIMEOUT = 30


def all_locations():
    out, offset, limit = [], 0, 500
    while True:
        r = requests.get(
            f"{SNIPE_URL}/api/v1/locations?limit={limit}&offset={offset}",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        r.raise_for_status()
        rows = r.json().get("rows", []) or []
        out.extend(rows)
        if len(rows) < limit:
            return out
        offset += limit


def occupancy(loc) -> list:
    """Non-zero attachment counts, so we never delete something in use."""
    fields = (
        "assets_count", "assigned_assets_count", "users_count",
        "children_count", "rtd_assets_count", "accessories_count",
    )
    return [f"{f}={loc[f]}" for f in fields if loc.get(f)]


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--delete", action="store_true", help="actually delete")
    args = ap.parse_args()

    locs = all_locations()
    by_id = {l["id"]: l for l in locs}
    print(f"{len(locs)} locations\n")

    targets, unsafe = [], []
    for loc in locs:
        name = html.unescape(loc.get("name") or "").strip()
        if " - " not in name:
            continue
        left, _, right = name.partition(" - ")
        if left.strip() != right.strip():
            continue

        parent = loc.get("parent") or {}
        parent_id = parent.get("id") if isinstance(parent, dict) else None
        parent_name = html.unescape(
            (by_id.get(parent_id, {}) or {}).get("name") or ""
        ).strip()
        if not parent_name or parent_name.lower() != left.strip().lower():
            continue

        busy = occupancy(loc)
        if busy:
            unsafe.append((loc, busy))
        else:
            targets.append(loc)

    for loc, busy in unsafe:
        print(f"  SKIP (in use)  {loc['name']}  [{', '.join(busy)}]")

    if not targets:
        print("Nothing to remove.")
        return 0

    print(f"{len(targets)} redundant sublocation(s):")
    for loc in targets:
        print(f"  id={loc['id']:<6} {html.unescape(loc['name'])}")

    if not args.delete:
        print("\nDry run — nothing deleted. Re-run with --delete.")
        return 0

    removed = 0
    for loc in targets:
        r = requests.delete(
            f"{SNIPE_URL}/api/v1/locations/{loc['id']}",
            headers=HEADERS,
            timeout=TIMEOUT,
        )
        payload = r.json() if r.content else {}
        if r.status_code >= 400 or payload.get("status") == "error":
            print(f"  failed id={loc['id']}: {payload.get('messages') or r.status_code}")
            continue
        print(f"  deleted id={loc['id']} {html.unescape(loc['name'])}")
        removed += 1

    print(f"\nDeleted {removed} of {len(targets)}.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
