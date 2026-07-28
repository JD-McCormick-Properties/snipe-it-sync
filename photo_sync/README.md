# Snipe-IT → OneDrive Photo Sync

Pulls every hardware asset from Snipe-IT and collects its photos from two
places: links pasted into the **notes** field or a check-in/check-out note
(Google Photos shares, direct image URLs), and files attached to the asset
through SnipeMobile, which uploads check-in/check-out photos to
`/api/v1/hardware/{id}/files`.

Photos are filed in OneDrive by category, model, and event:

```
{ONEDRIVE_BASE_FOLDER}/
└── Vehicle/
    └── Chevrolet Silverado 2007/
        ├── Check Out - Nick Brown - 2026-07-17 09-01/
        │   ├── Chevrolet Silverado 2007 - 1.jpg
        │   └── Chevrolet Silverado 2007 - 2.jpg
        ├── Check In - Nick Brown - 2026-07-18 16-30/
        │   └── Chevrolet Silverado 2007 - 1.jpg
        └── File Upload/
            └── Chevrolet Silverado 2007 - 2026-07-28 14-30-00.jpg
```

Every check-in and check-out gets its own subfolder, even when it produced a
single photo. Native SnipeMobile attachments are matched to their event by
timestamp so they land in the same folder as any linked photos from that
event.

`File Upload/` holds attachments with no event to match — typically added
through SnipeMobile's Files tab rather than during a check-in or check-out.
Photos from the asset image field or the top-level notes belong to no event
either and stay directly in the model folder.

A small SQLite file tracks what's already been uploaded so the next run is
mostly no-ops. Deduplication is by content, not by URL: sources re-serve the
same photo with slightly different bytes, so each image is also fingerprinted
with a perceptual hash and matched within a small Hamming distance.
Optionally, the resulting OneDrive links can be written back into the asset's
notes.

iCloud share links cannot be resolved — Apple blocks automated export. They
are counted and listed at INFO, then skipped. Attach photos through
SnipeMobile instead.

This subproject lives next to the existing AppFolio location sync
(`sync_properties.py`) and reuses the same repo for CI.

## Layout

```
photo_sync/
├── sync.py                 # entry point
├── cleanup_orphans.py      # maintenance: remove redundant uploads
├── organize_photos.py      # maintenance: file loose photos into event folders
├── requirements.txt
├── requirements-dev.txt    # requirements.txt + pytest
├── .env.example            # copy to .env and fill in
├── README.md               # (this file)
├── helpers/
│   ├── snipeit.py          # paginated Snipe-IT REST client
│   ├── onedrive.py         # Microsoft Graph (client credentials) uploader
│   ├── link_resolver.py    # URL extraction + image resolution
│   ├── image_utils.py      # HEIC→JPG, hashing, filename normalization
│   └── dedupe.py           # SQLite store of already-uploaded photos
├── tests/                  # offline; no credentials or network needed
└── (state)
    └── photo_sync_state.db # created on first run
```

The GitHub Actions workflow lives at the repo root:
`.github/workflows/photo_sync.yml`.

## Setup

### 1. Snipe-IT API token

In Snipe-IT, go to your user menu → **Manage API Keys** → **Create New Token**.
Copy the resulting JWT into `SNIPE_API_KEY`. The token inherits the
permissions of the user who creates it; pick a user with read access to
hardware (and write access if you intend to enable writeback).

### 2. Azure App Registration (Microsoft Graph, app-only)

This sync uses the **client credentials** flow so it can run unattended.

1. In the Azure portal, open **Microsoft Entra ID** → **App registrations**
   → **New registration**.
   - Name: e.g. "Snipe-IT Photo Sync"
   - Supported account types: **Single tenant**
   - Redirect URI: leave blank
2. After creation, copy the **Application (client) ID** and the
   **Directory (tenant) ID** — these become `AZURE_CLIENT_ID` and
   `AZURE_TENANT_ID`.
3. Under **Certificates & secrets** → **New client secret**, generate a
   secret. Copy the **Value** (not the Secret ID) into
   `AZURE_CLIENT_SECRET`. You only see it once.
4. Under **API permissions** → **Add a permission** → **Microsoft Graph**
   → **Application permissions**, add:
   - `Files.ReadWrite.All`
   - `User.Read.All` *(only needed if uploading via a user UPN; skip if
     you're targeting a drive id directly)*
5. Click **Grant admin consent** for the tenant (a tenant admin must do
   this once).

### 3. Pick an upload target

App-only auth has no signed-in user, so you must specify whose drive
files go into. Two options:

- **`ONEDRIVE_USER_ID`** — a user's UPN (e.g. `svc-assets@yourco.com`)
  or object id. The simplest setup is a dedicated service account whose
  OneDrive holds asset photos.
- **`ONEDRIVE_DRIVE_ID`** — an explicit drive id. Useful if you want
  uploads to land in a SharePoint document library; grab the drive id
  with a Graph call like
  `GET /sites/{site-id}/drives`.

Set one of these in your `.env`. Leave the other blank.

### 4. Local install

```bash
cd photo_sync
python3.11 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# fill in .env
```

### 5. First run

Start with a dry run so you can confirm URL extraction works on your
real notes content:

```bash
python sync.py --dry-run
```

Then a real run, capped to a handful of assets the first time:

```bash
python sync.py --limit 5
```

Once you're happy with the output, drop `--limit` for a full pass.

## Behavior flags

All set in `.env` (and overridable via the GitHub Actions workflow vars):

| Variable | Default | Purpose |
| --- | --- | --- |
| `WRITE_BACK_TO_SNIPEIT` | `false` | Append a `OneDrive Backup:` block to each asset's notes after upload |
| `FORCE_RESYNC` | `false` | Re-upload every URL even if it's in the dedupe DB |
| `DEDUPE_DB_PATH` | `photo_sync_state.db` | Path to the SQLite state file |
| `ONEDRIVE_BASE_FOLDER` | `AssetPhotos` | Top-level folder name under the drive root |
| `LOG_LEVEL` | `INFO` | `DEBUG` to see per-URL details |

CLI flags on `sync.py`:

- `--force` — same as `FORCE_RESYNC=true` for one run
- `--limit N` — stop after N assets (useful for first-run validation)
- `--dry-run` — extract URLs and log them, but don't fetch images or
  touch OneDrive/Snipe-IT

## Dedupe model

A SQLite table `uploads` records every successful upload keyed by
`(asset_id, source_url)`. Before doing any network work, the orchestrator
checks:

1. Has this exact `(asset_id, source_url)` been uploaded? → skip
2. After resolving and normalizing image bytes, has this `content_hash`
   already been uploaded for this asset? → skip (catches the same image
   shared under two different URLs)
3. Does an existing `perceptual_hash` for this asset sit within
   `PERCEPTUAL_MAX_DISTANCE` bits? → skip

Step 3 is what makes the sync settle. Google Photos rotates its CDN URLs on
every scrape, so step 1 never fires for them, and it re-serves the same photo
with slightly different bytes, so step 2 misses too — measured drift shifts a
256-bit perceptual hash by about 5 bits, while genuinely different photos sit
50+ bits apart. Without step 3 every album re-uploaded on every run.

A photo skipped at step 2 has its perceptual hash backfilled onto the
existing row, so rows written before the column existed become protected
after one pass rather than re-uploading once each.

`--force` bypasses all three checks.

The DB is cached between GitHub Actions runs (see workflow). For a clean
slate, delete the file or pass `--force`.

## Shared link resolution

`helpers/link_resolver.py` is the most failure-prone component because
Google Photos shapes its share pages aggressively.

Plain URLs are resolved over HTTP:

1. `GET` the URL with a real-browser User-Agent and follow redirects.
2. If the response is `image/*`, we have the image — return its bytes.
3. Otherwise parse the HTML and look for, in order:
   `og:image:secure_url`, `og:image`, `twitter:image`,
   `itemprop=image`, `<link rel="image_src">`, then any `<img src>`.

Google Photos links go through `resolve_google_photos_album`, which renders
the page in headless Chromium (Playwright), because the album grid is built
by JavaScript and the thumbnails are CSS `background-image` rather than
`<img>`. It polls until the grid appears, then scrolls to trigger lazy
loading, stopping once the image count stops growing — a fixed wait made
every album cost the same 20 seconds regardless of size. Set
`USE_PLAYWRIGHT=false` to skip these entirely.

Every scraped Google CDN URL (`googleusercontent.com`, `ggpht.com`) is
rewritten to `=s0` for the original-resolution variant, whether or not it
arrived with a size suffix. This matters: the grid is scraped mid-render, so
the same photo can appear with or without a suffix depending on timing, and
an un-suffixed URL serves a ~90 KB thumbnail.

iCloud links are detected by `is_icloud_share()` and skipped before any
network work. Apple blocks headless browsers, and a direct sharedstreams API
client was tried and removed — it couldn't reach the newer `icloudlinks/…`
share format. Attach photos through SnipeMobile instead.

## Tests

```
pip install -r requirements-dev.txt
python -m pytest tests/ -v
```

Every test is offline — no credentials, no network, no Playwright browsers —
so the suite runs in seconds and on every push via `.github/workflows/tests.yml`.

Coverage is deliberately anchored to bugs that reached production: perceptual
hash stability across re-encoding, Hamming matching rather than equality,
forcing `=s0` on every Google CDN URL shape, preferring the machine-readable
`datetime` field over `formatted`, batch position staying stable as dedupe
filters siblings out, subfolder filenames not overwriting existing files, and
the dedupe schema migration preserving rows. A failure there means a fixed bug
has come back.

## Production notes

- All long-running operations (Graph token, Snipe-IT pages, image
  downloads) have explicit timeouts. The Snipe-IT client retries on
  429/5xx with exponential backoff.
- The OneDrive uploader switches automatically from the simple `PUT`
  endpoint to chunked upload sessions for files larger than 4 MB.
- Nothing is hardcoded — every credential or path comes from `.env` or
  GitHub secrets.
- Failures on a single asset never abort the whole run; they're logged
  and counted in the final summary.

## Adding to GitHub Actions

The workflow `/.github/workflows/photo_sync.yml` runs nightly. In the
repo's **Settings → Secrets and variables → Actions**, add these
**Secrets**:

- `SNIPE_URL`
- `SNIPE_API_KEY`
- `AZURE_TENANT_ID`
- `AZURE_CLIENT_ID`
- `AZURE_CLIENT_SECRET`
- `ONEDRIVE_USER_ID` *or* `ONEDRIVE_DRIVE_ID`

And these **Variables** (optional):

- `ONEDRIVE_BASE_FOLDER`
- `WRITE_BACK_TO_SNIPEIT`

You can also trigger the workflow on demand from the Actions tab; the
manual trigger exposes a `force` input that maps to `FORCE_RESYNC`.

## Stretch ideas

- OCR serial-number detection on uploaded images (Tesseract + Pillow)
- QR code reading for asset re-tagging
- Discord/Slack notification on run summary

Already shipped, previously listed here: per-category folder layout, and
Snipe-IT attachments — SnipeMobile now uploads check-in/check-out photos as
asset files and this sync mirrors them to OneDrive.
