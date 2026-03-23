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
# Create location
# -------------------------------
def create_location(row):
    payload = {
        "name": row["Property Name"].strip(),
        "address": row["Street Address"].strip(),
        "city": row["City"].strip(),
        "state": row["State"].strip(),
        "zip": row["Zip"].strip(),
        "notes": row["Property ID"].strip()
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
    name = row["Property Name"].strip()
    city = row["City"].strip()
    state = row["State"].strip()

    needs_update = False

    if existing.get("name") != name:
        needs_update = True
    if existing.get("city") != city:
        needs_update = True
    if existing.get("state") != state:
        needs_update = True

    if not needs_update:
        print(f"⏭️ No change: {name}")
        return False

    payload = {
        "name": name,
        "address": row["Street Address"].strip(),
        "city": city,
        "state": state,
        "zip": row["Zip"].strip(),
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
            prop_id = row["Property ID"].strip()

            if not prop_id:
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
