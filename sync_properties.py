import csv
import html
import os
import requests

SNIPE_URL = os.environ["SNIPE_URL"].strip().rstrip("/")
API_KEY = os.environ["SNIPE_API_KEY"].strip()

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

UNIT_DIRECTORY_CSV = os.environ.get("UNIT_DIRECTORY_CSV", "unit_directory.csv")

# Requests never had a timeout; a hung connection would stall the whole run.
TIMEOUT = 30


class SnipeError(RuntimeError):
    """A write Snipe-IT rejected."""


def check(resp):
    """Return the response payload, raising if Snipe-IT rejected the write.

    Snipe-IT answers validation failures with HTTP 200 and a body of
    {"status": "error", "messages": ...}, so raise_for_status() alone reports
    a failed write as a success.
    """
    resp.raise_for_status()
    try:
        data = resp.json()
    except ValueError:
        return {}
    if isinstance(data, dict) and data.get("status") == "error":
        raise SnipeError(data.get("messages") or data)
    return data

# -------------------------------
# Get ALL locations (pagination)
# -------------------------------
def get_all_locations(all_locs=None):
    """Map Property ID (stored in a location's notes) -> location object."""
    locations = {}
    for loc in all_locs if all_locs is not None else get_all_locations_raw():
        prop_id = (loc.get("notes") or "").strip()
        if prop_id:
            locations[prop_id] = loc
    return locations


# -------------------------------
# Build full address (handles line 2)
# -------------------------------
def build_address(row):
    addr1 = (row.get("Property Street Address 1") or "").strip()
    addr2 = (row.get("Property Street Address 2") or "").strip()

    if addr2:
        return f"{addr1} {addr2}"
    return addr1


# -------------------------------
# Create location
# -------------------------------
def create_location(row):
    payload = {
        "name": (row.get("Property Name") or row.get("Property") or "").strip(),
        "address": build_address(row),
        "city": (row.get("Property City") or "").strip(),
        "state": (row.get("Property State") or "").strip(),
        "zip": (row.get("Property Zip") or "").strip(),
        "notes": (row.get("Property ID") or "").strip()
    }

    r = requests.post(
        f"{SNIPE_URL}/api/v1/locations",
        json=payload,
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    try:
        check(r)
    except SnipeError as exc:
        print(f"❌ Create failed: {payload['name']} — {exc}")
        return False
    print(f"✅ Created: {payload['name']}")
    return True


# -------------------------------
# Update location (if changed)
# -------------------------------
def update_location(existing, row):
    name = (row.get("Property Name") or row.get("Property") or "").strip()
    city = (row.get("Property City") or "").strip()
    state = (row.get("Property State") or "").strip()
    address = build_address(row)
    zip_code = (row.get("Property Zip") or "").strip()

    # Snipe-IT HTML-escapes text on the way out, so "402 & 404 Genesee Street"
    # comes back as "402 &amp; 404 ..." and an apostrophe as &#039;. Comparing
    # raw made any property containing & or ' look permanently out of date, so
    # it was rewritten on every run.
    differing = [
        field
        for field, want in (
            ("name", name), ("city", city), ("state", state),
            ("address", address), ("zip", zip_code),
        )
        if html.unescape((existing.get(field) or "").strip()) != want
    ]

    if not differing:
        print(f"⏭️ No change: {name}")
        return False

    print(f"   {name}: differs on {', '.join(differing)}")

    payload = {
        "name": name,
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
    }

    loc_id = existing["id"]

    r = requests.put(
        f"{SNIPE_URL}/api/v1/locations/{loc_id}",
        json=payload,
        headers=HEADERS,
        timeout=TIMEOUT,
    )

    try:
        check(r)
    except SnipeError as exc:
        print(f"❌ Update failed: {name} — {exc}")
        return None
    print(f"🔄 Updated: {name}")
    return True


# -------------------------------
# Unit sync helpers
# -------------------------------
def get_all_locations_raw():
    """Return every location object from Snipe-IT (no filtering)."""
    locations = []
    offset = 0
    limit = 500
    while True:
        url = f"{SNIPE_URL}/api/v1/locations?limit={limit}&offset={offset}"
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        r.raise_for_status()
        rows = r.json().get("rows", []) or []
        locations.extend(rows)
        if len(rows) < limit:
            break
        offset += limit
    return locations


def parse_unit_directory(csv_path):
    """Parse unit_directory CSV. Returns {property_full_name: [unit_name, ...]}.

    Property header rows start with '-> ' in the Unit Name field.
    Summary rows (empty Unit Name) are skipped.
    """
    properties = {}
    current_property = None

    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            name = (row.get("Unit Name") or "").strip()
            if not name:
                continue
            if name.startswith("-> "):
                current_property = name[3:].strip()
                properties.setdefault(current_property, [])
            elif current_property is not None:
                properties[current_property].append(name)

    return properties


def load_property_name_to_id(csv_path):
    """Parse properties.csv. Returns {property_full_name: property_id_string}."""
    mapping = {}
    with open(csv_path, newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)
        for row in reader:
            prop_full = (row.get("Property") or "").strip()
            prop_id = (row.get("Property ID") or "").strip()
            if prop_full and prop_id:
                mapping[prop_full] = prop_id
    return mapping


def _normalize(s):
    """Collapse internal whitespace for fuzzy matching."""
    return " ".join(s.split())


def create_sublocation(name, parent_id):
    payload = {"name": name, "parent_id": parent_id}
    r = requests.post(
        f"{SNIPE_URL}/api/v1/locations", json=payload, headers=HEADERS, timeout=TIMEOUT
    )
    try:
        check(r)
    except SnipeError as exc:
        print(f"  ❌ Create failed: {name} — {exc}")
        return False
    print(f"  ✅ Created: {name}")
    return True


def sync_units(all_locs=None):
    """Create Snipe-IT sublocations for every unit under each property."""
    print("\n--- Unit sync ---")

    if not os.path.exists(UNIT_DIRECTORY_CSV):
        print(f"⚠️  Unit directory CSV not found: {UNIT_DIRECTORY_CSV} — skipping unit sync")
        return

    unit_map = parse_unit_directory(UNIT_DIRECTORY_CSV)
    prop_id_map = load_property_name_to_id("properties.csv")

    # Build a normalized version of prop_id_map for fuzzy matching.
    norm_prop_id_map = {_normalize(k): v for k, v in prop_id_map.items()}

    if all_locs is None:
        all_locs = get_all_locations_raw()
    print(f"{len(all_locs)} total locations\n")

    # property_id string (from notes) -> Snipe-IT numeric location id
    snipeit_id_by_prop = {}
    for loc in all_locs:
        notes = (loc.get("notes") or "").strip()
        if notes:
            snipeit_id_by_prop[notes] = loc["id"]

    # parent_id -> {unit_name_lower: sublocation_id}
    existing_subs = {}
    for loc in all_locs:
        parent = loc.get("parent") or {}
        parent_id = parent.get("id") if isinstance(parent, dict) else None
        if parent_id:
            key = html.unescape(loc["name"]).strip().lower()
            existing_subs.setdefault(parent_id, {})[key] = loc["id"]

    # Snipe-IT enforces globally unique location names, so a unit name already
    # used anywhere — including as a top-level property — can never be created
    # under another parent. Checking only within the parent meant 15 names were
    # retried and rejected on every run.
    all_names = {html.unescape(loc["name"]).strip().lower() for loc in all_locs}

    # Snipe-IT location id -> its display name, for qualifying unit names.
    name_by_id = {
        loc["id"]: html.unescape(loc["name"]).strip() for loc in all_locs
    }

    created = skipped = unmatched = failed = blocked = 0

    for prop_full, units in sorted(unit_map.items()):
        # Match property full name to a Property ID.
        prop_id_str = prop_id_map.get(prop_full) or norm_prop_id_map.get(_normalize(prop_full))
        if not prop_id_str:
            print(f"⚠️  No Property ID match for: {prop_full}")
            unmatched += 1
            continue

        parent_snipeit_id = snipeit_id_by_prop.get(prop_id_str)
        if not parent_snipeit_id:
            print(f"⚠️  Not in Snipe-IT yet: {prop_full} (Property ID: {prop_id_str})")
            unmatched += 1
            continue

        prop_subs = existing_subs.get(parent_snipeit_id, {})
        parent_name = name_by_id.get(parent_snipeit_id, "")

        # Names are globally unique in Snipe-IT, so a bare unit like "122" can
        # only exist once across every property. When it's already taken, fall
        # back to qualifying it with the parent — "Seminole Woods - 122".
        # Both spellings are checked for existence so a qualified unit isn't
        # recreated under its bare name on the next run.
        to_create = []          # (display_name, was_qualified)
        existing_count = 0
        for u in units:
            bare = u.strip()
            # A single-unit property lists the property itself as its only
            # unit, so the name collides with its own parent. Qualifying that
            # yields "Skyline Drive - Skyline Drive"; the parent location
            # already represents it, so there is nothing to add.
            if parent_name and bare.lower() == parent_name.lower():
                existing_count += 1
                continue
            qualified = f"{parent_name} - {bare}" if parent_name else bare
            if bare.lower() in prop_subs or qualified.lower() in prop_subs:
                existing_count += 1
            elif bare.lower() not in all_names:
                to_create.append((bare, False))
            elif qualified.lower() not in all_names:
                to_create.append((qualified, True))
            else:
                blocked += 1
                print(f"  ⚠️  {bare!r} and {qualified!r} are both taken — skipping")

        qualified_count = sum(1 for _, q in to_create if q)
        print(f"{prop_full}")
        note = f", {qualified_count} qualified with the property name" if qualified_count else ""
        print(f"  {existing_count} existing, {len(to_create)} to create{note}")
        new_units = to_create

        for unit_name, was_qualified in new_units:
            if create_sublocation(unit_name, parent_snipeit_id):
                created += 1
                # Keep the in-run view current so later properties don't
                # collide with a name we just took.
                all_names.add(unit_name.lower())
                prop_subs[unit_name.lower()] = None
            else:
                failed += 1

        skipped += existing_count

    print(f"\nUnit sync summary:")
    print(f"  Created:  {created}")
    print(f"  Failed:   {failed}")
    print(f"  Blocked (bare and qualified both taken): {blocked}")
    print(f"  Skipped:  {skipped}")
    print(f"  Unmatched properties: {unmatched}")


# -------------------------------
# Main sync logic
# -------------------------------
def main():
    print("Fetching all Snipe-IT locations...")
    all_locs = get_all_locations_raw()
    existing_locations = get_all_locations(all_locs)
    print(f"Found {len(all_locs)} locations, {len(existing_locations)} with a Property ID\n")

    created = 0
    updated = 0
    skipped = 0
    failed = 0

    with open("properties.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        print("CSV Columns:", reader.fieldnames, "\n")

        for row in reader:
            prop_id = (row.get("Property ID") or "").strip()

            if not prop_id:
                print(f"⚠️ Skipping row (missing Property ID): {row}")
                continue

            if prop_id not in existing_locations:
                if create_location(row):
                    created += 1
                else:
                    failed += 1
            else:
                result = update_location(existing_locations[prop_id], row)
                if result is True:
                    updated += 1
                elif result is None:
                    failed += 1
                else:
                    skipped += 1

    print("\nProperty sync summary:")
    print(f"  Created: {created}")
    print(f"  Updated: {updated}")
    print(f"  Failed:  {failed}")
    print(f"  Skipped: {skipped}")

    sync_units(all_locs)


if __name__ == "__main__":
    main()
