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
    import re

    base_url = os.environ.get("APPFOLIO_BASE_URL")
    session_cookie = os.environ.get("APPFOLIO_SESSION")

    print("Fetching AppFolio report...")

    cookies = {
        "_appfolio_session": session_cookie
    }

    # Step 1: request report page
    report_url = f"{base_url}/buffered_reports/unit_directory"
    r = requests.get(report_url, cookies=cookies)
    r.raise_for_status()

    # Step 2: extract S3 download link
    match = re.search(r'https://appfolio-reports-app-prod[^"]+', r.text)

    if not match:
        raise Exception("Could not find download link in AppFolio response")

    download_url = match.group(0)

    print("Downloading CSV...")

    # Step 3: download CSV
    r = requests.get(download_url)
    r.raise_for_status()

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
