# AppFolio → Snipe-IT Property Sync

Mirrors the AppFolio property list into Snipe-IT as locations, and the unit
list as sublocations under each property. Runs daily via
`.github/workflows/sync.yml`.

A property is matched to its Snipe-IT location by **Property ID**, stored in
the location's `notes` field. That is the join key — not the name, which can
change.

```
property_sync/
├── sync_properties.py            # entry point
├── fetch_data.py                 # downloads the AppFolio exports from OneDrive
├── cleanup_stutter_locations.py  # maintenance, see below
├── data/                         # filled at run time; not committed
└── tests/
```

## Where the AppFolio data lives

The two exports, `properties.csv` and `unit_directory.csv`, carry addresses,
rents, deposits and revenue, and this repository is public, so they live in
OneDrive rather than in git: by default
`JD McCormick Shared Drive/AppFolio Exports` on the same drive as the photo
sync (override with the `PROPERTY_DATA_FOLDER` variable).

Before each sync, `fetch_data.py` downloads them into `data/` using the photo
sync's app registration. To refresh the data, replace the two files in that
OneDrive folder; the next run picks them up.

`sync_properties.py` reads `data/` relative to the script, so it runs from any
working directory. Override with `PROPERTIES_CSV` / `UNIT_DIRECTORY_CSV` to
point a local run at other files.

Under Actions the run log is public, so property and unit names print as
`[redacted]`; a local run shows them.

## What it does

1. Every row in `properties.csv` is created or updated as a Snipe-IT location.
2. Every unit in `unit_directory.csv` is created as a sublocation of its
   property. Property header rows in that export start with `-> `.

## Behaviour worth knowing

**Write failures are silent unless checked.** Snipe-IT answers a validation
failure with HTTP 200 and `{"status": "error", ...}`, so `raise_for_status()`
passes. Every write goes through `check()`, which inspects the payload. This
mattered: the sync reported the same 15 creations and 4 updates every day for
weeks while writing nothing at all.

**Comparisons unescape first.** Snipe-IT HTML-escapes on output, so
`100 & 102 Example Street` returns as `100 &amp; 102 ...`. Comparing raw made
every property containing `&` or an apostrophe look permanently out of date.

**Location names are globally unique.** A unit named `122` can exist only once
across the whole instance. When the bare name is taken, `plan_units` falls
back to `<Property> - <Unit>`. A single-unit property lists itself as its only
unit, so that case is skipped rather than qualified — otherwise you get
`Skyline Drive - Skyline Drive`.

**Pagination is sorted by id.** Without an explicit sort, Snipe-IT's ordering
shifts between requests and a record can be returned twice while another is
skipped entirely.

## Maintenance

```bash
python cleanup_stutter_locations.py            # report only
python cleanup_stutter_locations.py --delete   # remove
```

Removes sublocations named `X - X` whose parent is also `X`. Only touches a
location with no assets, users, or children. Also available as
`.github/workflows/cleanup_locations.yml`.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/
```

Offline; no credentials or network needed.
