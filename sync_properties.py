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
# Download AppFolio CSV
# -------------------------------
def download_properties_csv():
    base_url = os.environ.get("APPFOLIO_BASE_URL")
    session_cookie = os.environ.get("APPFOLIO_SESSION")

    print("Downloading AppFolio CSV export...")

    cookies = {
        "_property_session": session_cookie
    }

    export_url = f"{base_url}/reporting/unit_directory_3d34c027-1db8-4d6f-92df-0d0d4ce16dc8/csv"

    r = requests.get(export_url, cookies=cookies)
    r.raise_for_status()

    # safety check
    if b"<html" in r.content[:500]:
        raise Exception("Got HTML instead of CSV — session may be invalid")

    with open("properties.csv", "wb") as f:
        f.write(r.content)

    print("Downloaded properties.csv")


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
            prop_id = loc.get("notes")
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
        "name": row["property_name"],
        "address": row["address"],
        "city": row["city"],
        "state": row["state"],
        "zip": row["zip"],
        "notes": row["property_id"]
    }

    r = requests.post(
        f"{SNIPE_URL}/api/v1/locations",
        json=payload,
        headers=HEADERS
    )

    r.raise_for_status()
    print(f"✅ Created: {row['property_name']}")


# -------------------------------
# Update location (if changed)
# -------------------------------
def update_location(existing, row):
    needs_update = False

    if existing.get("name") != row["property_name"]:
        needs_update = True
    if existing.get("city") != row["city"]:
        needs_update = True
    if existing.get("state") != row["state"]:
        needs_update = True

    if not needs_update:
        print(f"⏭️ No change: {row['property_name']}")
        return

    payload = {
        "name": row["property_name"],
        "address": row["address"],
        "city": row["city"],
        "state": row["state"],
        "zip": row["zip"],
    }

    loc_id = existing["id"]

    r = requests.put(
        f"{SNIPE_URL}/api/v1/locations/{loc_id}",
        json=payload,
        headers=HEADERS
    )

    r.raise_for_status()
    print(f"🔄 Updated: {row['property_name']}")


# -------------------------------
# Main sync logic
# -------------------------------
def main():
    # Step 0: download latest AppFolio data
    download_properties_csv()

    print("Fetching existing locations...")
    existing_locations = get_all_locations()
    print(f"Found {len(existing_locations)} existing locations")

    with open("properties.csv") as f:
        reader = csv.DictReader(f)

        for row in reader:
            prop_id = row["property_id"]

            if prop_id not in existing_locations:
                create_location(row)
            else:
                update_location(existing_locations[prop_id], row)


if __name__ == "__main__":
    main()
