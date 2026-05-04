# Snipe-IT → OneDrive Photo Sync

Pulls every hardware asset from Snipe-IT, scans the **notes** field for
photo links (Google Photos shares, iCloud shares, direct image URLs),
downloads each image, and uploads it to OneDrive under
`{ONEDRIVE_BASE_FOLDER}/{asset_tag}/`.

A small SQLite file tracks what's already been uploaded so the next run
is mostly no-ops. Optionally, the resulting OneDrive links can be written
back into the asset's notes.

This subproject lives next to the existing AppFolio location sync
(`sync_properties.py`) and reuses the same repo for CI.

## Layout

```
photo_sync/
├── sync.py                 # entry point
├── requirements.txt
├── .env.example            # copy to .env and fill in
├── README.md               # (this file)
├── helpers/
│   ├── snipeit.py          # paginated Snipe-IT REST client
│   ├── onedrive.py         # Microsoft Graph (client credentials) uploader
│   ├── link_resolver.py    # URL extraction + image resolution
│   ├── image_utils.py      # HEIC→JPG, hashing, filename normalization
│   └── dedupe.py           # SQLite store of already-uploaded URLs
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

`--force` bypasses both checks.

The DB is cached between GitHub Actions runs (see workflow). For a clean
slate, delete the file or pass `--force`.

## Shared link resolution

`helpers/link_resolver.py` is the most failure-prone component because
Google Photos and iCloud shape their share pages aggressively. The
current strategy is HTTP-only:

1. `GET` the URL with a real-browser User-Agent and follow redirects.
2. If the response is `image/*`, we have the image — return its bytes.
3. Otherwise parse the HTML and look for, in order:
   `og:image:secure_url`, `og:image`, `twitter:image`,
   `itemprop=image`, `<link rel="image_src">`, then any `<img src>`.
4. For Google Photos hosts (`googleusercontent.com`, `ggpht.com`),
   strip the `=w...-h...` size suffix and replace with `=s0` to fetch
   the original-resolution variant.

If a specific link type repeatedly fails, the easiest next step is to
add Playwright as a fallback: render the share page in a real browser,
wait for the `<img>` to appear, and pass its `src` back into the same
`_read_capped` flow.

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
- Re-upload as a Snipe-IT attachment (in addition to OneDrive)
- Discord/Slack notification on run summary
- Per-category folder layout (`AssetPhotos/Laptops/AT-001/...`)
