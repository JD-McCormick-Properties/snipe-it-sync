# AC Unit Checkout Notifier

Emails when an asset in the **AC Units** category is checked out. Runs every
90 minutes via `.github/workflows/ac_unit_notify.yml`.

Units are retired once checked out, which can happen before the next run. The watch list
therefore includes archived and deleted units as well as active ones —
Snipe-IT's asset list omits those by default, which would drop the email for
any unit retired before the notifier saw its checkout.

Mail goes out through the Microsoft Graph API using the same Azure app
registration as the photo sync, which needs the `Mail.Send` application
permission with admin consent.

## Configuration

Repository variables:

| Variable | Default | Purpose |
| --- | --- | --- |
| `NOTIFY_FROM_EMAIL` | — | Sending mailbox |
| `NOTIFY_TO_EMAILS` | — | Comma-separated recipients |
| `NOTIFY_CATEGORY` | `AC Units` | Snipe-IT category to watch |
| `NOTIFY_STATE_PATH` | `ac_notify_state.json` | Where already-notified checkouts are recorded (set by the workflow) |

Restricting the app to a single mailbox is worth doing:

```powershell
New-ApplicationAccessPolicy -AppId <AZURE_CLIENT_ID> `
  -PolicyScopeGroupId info@example.com `
  -AccessRight RestrictAccess
```

## The email

Subject names the asset tag and model. The body carries asset tag, model name
and number, manufacturer, who checked it out and who performed it, any note,
and recent history for that asset.

Values arrive from Snipe-IT already HTML-escaped, so they are interpolated as
received rather than escaped again.

## State

Notified activity-entry ids live in `ac_notify_state.json`, cached between
Actions runs. Three properties matter, each covered by tests:

- **A missing state file primes rather than sends.** The cache can miss or be
  evicted, and 200 recent checkouts are fetched per run — treating that as
  "all new" would email a notification for every one at once. A run with no
  prior state records what it finds and sends nothing.
- **An id is recorded only after its email succeeds.** A Graph outage leaves
  the entry unseen so the next run retries, rather than dropping it silently.
- **State is saved even if a later notification fails.** The workflow uses
  separate restore/save actions and runs the save step unconditionally, so a
  successful email is not forgotten merely because another item failed.

## Failure alerts

`alert.py` emails when a scheduled workflow does not succeed, driven by
`.github/workflows/alert_on_failure.yml` on a `workflow_run` completion. It
reuses the Graph sender above.

Recipients come from `ALERT_TO_EMAILS`, deliberately separate from
`NOTIFY_TO_EMAILS`. Unset, the alert is skipped rather than mailed to the AC
unit recipients.

It fires for failures and timeouts. GitHub reports a job that hit its
`timeout-minutes` as *cancelled*, so cancelled runs are checked for GitHub's
"exceeded the maximum execution time" annotation and alerted as `timed_out`
when it is there. Other cancellations — infrastructure trouble, or a queued run
superseded by a newer one — are ignored: the application may never have
started and a later scheduled run will recover.
All three syncs return a non-zero exit status when a write is rejected, so a run
that would previously have reported success while writing nothing now fails
and triggers this.

## Tests

```bash
pip install -r requirements.txt pytest
python -m pytest tests/
```

Offline; no credentials or network needed.
