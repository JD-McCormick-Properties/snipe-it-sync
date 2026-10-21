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
shared between them except credentials (see below).

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
│   ├── usage_report.py               # read-only: who attaches photos, and how
│   ├── helpers/                      # Snipe-IT, OneDrive, image, dedupe
│   └── tests/
├── notifications/
│   ├── notifier.py                   # entry point
│   ├── alert.py                      # failure alert email
│   └── tests/
└── .github/
    ├── workflows/
    └── dependabot.yml                # weekly dependency update PRs
```

## Where photos come from

Techs attach photos through SnipeMobile during a check-in or check-out. Those
upload to Snipe-IT as asset files, and `photo_sync` mirrors them to OneDrive
grouped by event:

```
AssetPhotos/Vehicle/Chevrolet Silverado 2007/
├── Check Out - Alex Rivera - 2026-07-17 09-01/
├── Check In - Alex Rivera - 2026-07-18 16-30/
└── File Upload/          # attached outside a check-in/check-out
```

Photo links pasted into an asset's notes or a check-in/check-out note are also
picked up — Google Photos shares and direct image URLs. iCloud links can't be
resolved (Apple blocks automated export) and are skipped.

Google Photos albums are re-opened to catch photos added after the link was
posted: every run while the check-in/out is under 3 days old or the album is
still growing, then once a day. A photo added to an old album lands within a
day rather than within two hours.

## Workflows

| Workflow | Trigger | Purpose |
| --- | --- | --- |
| `sync.yml` | daily + manual | Property and unit sync |
| `photo_sync.yml` | 2-hourly + manual | Photo sync |
| `ac_unit_notify.yml` | 90-minutely + manual | AC unit checkout emails |
| `tests.yml` | push / PR | All three test suites, plus actionlint on the workflows |
| `codeql.yml` | push / PR / weekly | CodeQL security scan |
| `alert_on_failure.yml` | on a failed or timed-out run | Emails when a scheduled job doesn't succeed |
| `cleanup_locations.yml` | manual | Remove redundant sublocations |
| `cleanup_orphans.yml` | manual | Remove duplicate photo uploads |
| `organize_photos.yml` | manual | File loose photos into event folders |
| `usage_report.yml` | manual | Who attaches photos, and through which route |

Every maintenance workflow reports by default and requires an explicit input
to make changes.

Each sync exits non-zero when a write is rejected, not only when it crashes,
so the alert covers silent failure too. That matters: the property sync once
spent weeks reporting success while writing nothing, and no crash-based alert
would have caught it.

GitHub reports a job that hit its time limit as *cancelled*, not failed, so the
alert also checks cancelled runs and emails when one actually timed out.
Ordinary cancellations — a superseded queued run, or GitHub infrastructure
trouble — stay quiet.

The photo sync, its cleanup tools, and the organizer share a concurrency group
so they can never run against OneDrive at the same time.

## Keeping it current

Dependabot opens grouped pull requests weekly for the GitHub Actions and each
subproject's Python requirements. Merge them once Tests and CodeQL pass. After
any Pillow or pillow-heif update, check that the next photo sync reports
`uploaded=0`: a change in image decoding could shift the perceptual hashes
and re-upload photos that are already in OneDrive.

Secret scanning with push protection is enabled, so a commit containing a
credential is blocked before it reaches this public repo. Merged pull-request
branches are deleted automatically.

## Credentials

Set as repository secrets. All three jobs use the same Snipe-IT token.

| Secret | Used by |
| --- | --- |
| `SNIPE_URL`, `SNIPE_API_KEY` | all |
| `AZURE_TENANT_ID`, `AZURE_CLIENT_ID`, `AZURE_CLIENT_SECRET` | photo sync, notifier |
| `ONEDRIVE_USER_ID` or `ONEDRIVE_DRIVE_ID` | photo sync |

Repository variables: `ONEDRIVE_BASE_FOLDER`, `NOTIFY_FROM_EMAIL`,
`NOTIFY_TO_EMAILS`, `NOTIFY_CATEGORY`, `ALERT_TO_EMAILS`.

`ALERT_TO_EMAILS` receives workflow failure alerts and is separate from
`NOTIFY_TO_EMAILS` on purpose — an AC unit checkout goes to the office, a sync
failure goes to whoever maintains this. Leave it unset and alerts are skipped.

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
- The asset list **omits archived and deleted assets** unless a `status` is
  requested. AC units are retired after checkout, so the notifier asks for
  both explicitly.
- Photo sources re-serve the same image with slightly different bytes, so
  deduplication is by perceptual hash within a small Hamming distance rather
  than by URL or byte hash.
