import csv
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

# -------------------------------
# Get ALL locations (pagination)
# -------------------------------
def get_all_locations():
    locations = {}
    offset = 0
    limit = 50

    while True:
        url = f"{SNIPE_URL}/api/v1/locations?limit={limit}&offset={offset}"
        r = requests.get(url, headers=HEADERS)
        r.raise_for_status()

        data = r.json()
        rows = data.get("rows", [])

        for loc in rows:
            prop_id = (loc.get("notes") or "").strip()
            if prop_id:
                locations[prop_id] = loc

        if len(rows) < limit:
            break

        offset += limit

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
        headers=HEADERS
    )

    r.raise_for_status()
    print(f"✅ Created: {payload['name']}")


# -------------------------------
# Update location (if changed)
# -------------------------------
def update_location(existing, row):
    name = (row.get("Property Name") or row.get("Property") or "").strip()
    city = (row.get("Property City") or "").strip()
    state = (row.get("Property State") or "").strip()
    address = build_address(row)
    zip_code = (row.get("Property Zip") or "").strip()

    needs_update = False

    if existing.get("name") != name:
        needs_update = True
    if existing.get("city") != city:
        needs_update = True
    if existing.get("state") != state:
        needs_update = True
    if existing.get("address") != address:
        needs_update = True
    if existing.get("zip") != zip_code:
        needs_update = True

    if not needs_update:
        print(f"⏭️ No change: {name}")
        return False

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
        headers=HEADERS
    )

    r.raise_for_status()
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
        r = requests.get(url, headers=HEADERS)
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
    r = requests.post(f"{SNIPE_URL}/api/v1/locations", json=payload, headers=HEADERS)
    r.raise_for_status()
    print(f"  ✅ Created: {name}")


def sync_units():
    """Create Snipe-IT sublocations for every unit under each property."""
    print("\n--- Unit sync ---")

    if not os.path.exists(UNIT_DIRECTORY_CSV):
        print(f"⚠️  Unit directory CSV not found: {UNIT_DIRECTORY_CSV} — skipping unit sync")
        return

    unit_map = parse_unit_directory(UNIT_DIRECTORY_CSV)
    prop_id_map = load_property_name_to_id("properties.csv")

    # Build a normalized version of prop_id_map for fuzzy matching.
    norm_prop_id_map = {_normalize(k): v for k, v in prop_id_map.items()}

    # Fetch all Snipe-IT locations once.
    print("Fetching all Snipe-IT locations...")
    all_locs = get_all_locations_raw()
    print(f"Found {len(all_locs)} total locations\n")

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
            existing_subs.setdefault(parent_id, {})[loc["name"].strip().lower()] = loc["id"]

    created = skipped = unmatched = 0

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
        new_units = [u for u in units if u.lower() not in prop_subs]
        existing_count = len(units) - len(new_units)

        print(f"{prop_full}")
        print(f"  {existing_count} existing, {len(new_units)} to create")

        for unit_name in new_units:
            create_sublocation(unit_name, parent_snipeit_id)
            created += 1

        skipped += existing_count

    print(f"\nUnit sync summary:")
    print(f"  Created:  {created}")
    print(f"  Skipped:  {skipped}")
    print(f"  Unmatched properties: {unmatched}")


# -------------------------------
# Main sync logic
# -------------------------------
def main():
    print("Fetching existing locations...")
    existing_locations = get_all_locations()
    print(f"Found {len(existing_locations)} existing locations\n")

    created = 0
    updated = 0
    skipped = 0

    with open("properties.csv", newline="", encoding="utf-8-sig") as f:
        reader = csv.DictReader(f)

        print("CSV Columns:", reader.fieldnames, "\n")

        for row in reader:
            prop_id = (row.get("Property ID") or "").strip()

            if not prop_id:
                print(f"⚠️ Skipping row (missing Property ID): {row}")
                continue

            if prop_id not in existing_locations:
                create_location(row)
                created += 1
            else:
                if update_location(existing_locations[prop_id], row):
                    updated += 1
                else:
                    skipped += 1

    print("\nProperty sync summary:")
    print(f"  Created: {created}")
    print(f"  Updated: {updated}")
    print(f"  Skipped: {skipped}")

    sync_units()


if __name__ == "__main__":
    main()
