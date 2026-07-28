# snipe-it-sync

Automation around JD McCormick Properties' Snipe-IT asset tracking, used by
the maintenance team for vehicles and tools.

Three independent jobs, each scheduled through GitHub Actions:

| | What it does | Schedule |
| --- | --- | --- |
| [`property_sync/`](property_sync/) | Mirrors AppFolio properties and units into Snipe-IT as locations and sublocations | daily, 06:00 UTC |
| [`photo_sync/`](photo_sync/) | Copies asset photos from Snipe-IT into OneDrive, filed by check-in / check-out | every 2 hours |
| [`notifications/`](notifications/) | Emails when an AC unit is checked out | every 90 minutes |

Each is self-contained: its own dependencies, tests, and README. Nothing is
shared between them except the Snipe-IT credentials.

## Layout

```
├── property_sync/
│   ├── sync_properties.py            # entry point
│   ├── cleanup_stutter_locations.py  # maintenance
│   ├── data/                         # AppFolio exports (properties, units)
│   └── tests/
├── photo_sync/
│   ├── sync.py                       # entry point
│   ├── cleanup_orphans.py            # maintenance
│   ├── organize_photos.py            # maintenance
│   ├── helpers/                      # Snipe-IT, OneDrive, image, dedupe
│   └── tests/
├── notifications/
│   ├── notifier.py                   # entry point
│   └── tests/
└── .github/workflows/
```

## Where photos come from

Techs attach photos through SnipeMobile during a check-in or check-out. Those
upload to Snipe-IT as asset files, and `photo_sync` mirrors them to OneDrive
grouped by event:

```
AssetPhotos/Vehicle/Chevrolet Silverado 2007/
├── Check Out - Nick Brown - 2026-07-17 09-01/
├── Check In - Nick Brown - 2026-07-18 16-30/
└── File Upload/          # attached outside a check-in/check-out
```

Photo links pasted into an asset's notes or a check-in/check-out note are also
picked up — Google Photos shares and direct image URLs. iCloud links can't be
resolved (Apple blocks automated export) and are skipped.

## Workflows

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `sync.yml` | daily + manual | Property and unit sync |
| `photo_sync.yml` | 2-hourly + manual | Photo sync |
| `ac_unit_notify.yml` | 90-minutely + manual | AC unit checkout emails |
| `tests.yml` | push / PR | All three test suites |
| `cleanup_locations.yml` | manual | Remove redundant sublocations |
| `cleanup_orphans.yml` | manual | Remove duplicate photo uploads |
| `organize_photos.yml` | manual | File loose photos into event folders |

Every maintenance workflow reports by default and requires an explicit input
to make changes.

The photo sync, its cleanup tools, and the organizer share a concurrency group
so they can never run against OneDrive at the same time.

## Credentials

Set as repository secrets. All three jobs use the same Snipe-IT token.

| Secret | Used by |
| --- | --- |
| `SNIPE_URL`, `SNIPE_API_KEY` | all |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | photo sync, notifier |
| `ONEDRIVE_USER_ID` or `ONEDRIVE_DRIVE_ID` | photo sync |

Repository variables: `ONEDRIVE_BASE_FOLDER`, `NOTIFY_FROM_EMAIL`,
`NOTIFY_TO_EMAILS`, `NOTIFY_CATEGORY`.

The Azure app needs `Files.ReadWrite.All` for OneDrive and `Mail.Send` for the
notifier, both as application permissions with admin consent. Setup details
are in [photo_sync/README.md](photo_sync/README.md).

## Tests

Offline — no credentials, no network, no Playwright browsers.

```bash
cd photo_sync    && pip install -r requirements-dev.txt && python -m pytest tests/
cd property_sync && pip install -r requirements.txt pytest && python -m pytest tests/
cd notifications && pip install -r requirements.txt pytest && python -m pytest tests/
```

Tests are anchored to bugs that reached production, so each documents a real
failure rather than a hypothetical one. Worth knowing before changing any of
this code:

- Snipe-IT returns **HTTP 200 with an error body** for validation failures, so
  a bare `raise_for_status()` reports failed writes as successes.
- Snipe-IT **HTML-escapes text on output**, so comparing a stored value
  against what you intend to write needs an unescape first — otherwise records
  containing `&` or an apostrophe look permanently stale and rewrite forever.
- Location names are **globally unique**, not unique per parent.
- Offset pagination **needs an explicit sort**. Without one, records shift
  between pages and some are silently skipped.
- Photo sources re-serve the same image with slightly different bytes, so
  deduplication is by perceptual hash within a small Hamming distance rather
  than by URL or byte hash.
