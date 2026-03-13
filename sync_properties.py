import csv
import os
import requests

SNIPE_URL = os.environ["SNIPE_URL"]
API_KEY = os.environ["SNIPE_API_KEY"]

print("API key length:", len(API_KEY))
print("Headers:", f"Bearer {API_KEY[:6]}...")

HEADERS = {
    "Authorization": f"Bearer {API_KEY}",
    "Accept": "application/json",
    "Content-Type": "application/json"
}

def get_snipe_locations():
    r = requests.get(f"{SNIPE_URL}/api/v1/locations", headers=HEADERS)
    r.raise_for_status()
    locations = r.json()["rows"]

    return {loc.get("notes"): loc for loc in locations}


def create_location(name, address, city, state, zip_code, property_id):
    payload = {
        "name": name,
        "address": address,
        "city": city,
        "state": state,
        "zip": zip_code,
        "notes": property_id
    }

    r = requests.post(
        f"{SNIPE_URL}/api/v1/locations",
        json=payload,
        headers=HEADERS
    )

    r.raise_for_status()
    print(f"Created {name}")


def main():
    existing_locations = get_snipe_locations()

    with open("properties.csv") as f:
        reader = csv.DictReader(f)

        for row in reader:
            property_id = row["property_id"]

            if property_id not in existing_locations:
                create_location(
                    row["property_name"],
                    row["address"],
                    row["city"],
                    row["state"],
                    row["zip"],
                    property_id
                )
            else:
                print(f"{row['property_name']} already exists")


if __name__ == "__main__":
    main()
