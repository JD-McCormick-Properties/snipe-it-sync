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

    print("\nSummary:")
    print(f"Created: {created}")
    print(f"Updated: {updated}")
    print(f"Skipped: {skipped}")


if __name__ == "__main__":
    main()
