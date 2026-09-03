# 14 — Gmail Push Setup

`make poller` (IMAP, 15–30s polling) works with zero setup and is the default —
`GMAIL_INBOUND_MODE=poll`. This page is for switching to push (`GMAIL_INBOUND_MODE=push`,
`make poller-push`): sub-second latency instead of a fixed poll interval, at the
cost of a one-time Google Cloud setup. See `docs/13_PRODUCTION_READINESS_GAP.md`
for why this was deferred past v1.

Every step below needs your Google account and a browser — none of it can be done
from a terminal on your behalf. Steps 1–4 are one-time infrastructure; step 5 is
also one-time but per machine; step 6–7 is how you turn it on.

## How it works

```
Gmail inbox  --watch()-->  Pub/Sub topic  --pull-->  this listener  --POST-->  API  --> pipeline
```

Gmail doesn't push to you directly — it publishes a `{emailAddress, historyId}`
notification to a Pub/Sub topic you own, and this listener **pulls** from a
subscription on that topic. That's deliberate: a pull subscription needs no public
HTTPS endpoint, so nothing about this project's "no servers" posture changes — the
listener only ever makes outbound connections, exactly like the IMAP poller does.

On every notification the listener re-derives what's new from `users.history.list`
against a durable local cursor (`data/gmail_push_state.json`), not from the
notification payload itself — Pub/Sub delivers at-least-once and doesn't guarantee
order, and this is the pattern Gmail's own docs recommend to stay correct under
that.

## Setup

### 1. Create or select a GCP project

[console.cloud.google.com](https://console.cloud.google.com) → create a project (or
pick an existing one). Note its **project ID** (not its display name) — you'll set
it as `GCP_PROJECT_ID`.

### 2. Enable the Gmail API and the Cloud Pub/Sub API

APIs & Services → Library → enable both **Gmail API** and **Cloud Pub/Sub API** for
the project.

### 3. Create an OAuth 2.0 Client ID

APIs & Services → Credentials → Create Credentials → OAuth client ID → application
type **Desktop app**. Download the JSON and save it as `secrets/credentials.json`
(the path `GMAIL_CREDENTIALS_PATH` already points at — `secrets/` is git-ignored).

If this project has never configured an OAuth consent screen, you'll be prompted to
do that first — "External" user type is fine for a single-account demo; you don't
need to submit it for Google's verification review, but in that unverified state
the refresh token Google issues expires after 7 days, so you'll re-run step 5
weekly. `docs/11_RUNBOOK.md`'s "Gmail 401" row is this.

### 4. Create a Pub/Sub topic, a pull subscription, and grant Gmail publish rights

```bash
gcloud config set project <GCP_PROJECT_ID>
gcloud pubsub topics create taskflow-gmail-inbound
gcloud pubsub subscriptions create taskflow-gmail-inbound-sub \
  --topic=taskflow-gmail-inbound

# The step people miss — Gmail's watch() call fails with a permissions error
# without this, silently as far as the listener is concerned:
gcloud pubsub topics add-iam-policy-binding taskflow-gmail-inbound \
  --member="serviceAccount:gmail-api-push@system.gserviceaccount.com" \
  --role="roles/pubsub.publisher"
```

(No `gcloud` installed? The Cloud Console's Pub/Sub page does all three — create
topic, create subscription, then Permissions → Add Principal on the topic with that
exact service account and the Pub/Sub Publisher role.)

### 5. Run the one-time OAuth consent

Set `GCP_PROJECT_ID`, `PUBSUB_TOPIC=taskflow-gmail-inbound`,
`PUBSUB_SUBSCRIPTION=taskflow-gmail-inbound-sub` in `.env` first, then:

```bash
uv run python scripts/gmail_watch_setup.py
```

This opens a browser for consent (scopes: `gmail.readonly` and `pubsub` — the same
credential authenticates both the Gmail API and the Pub/Sub subscriber, so there's
no separate service-account key to manage), writes `secrets/token.json`, and then
registers the watch once to confirm step 4's permission grant actually took.
"Watch registered. Baseline historyId=..." means it worked. A `PERMISSION_DENIED`
here means step 4's IAM binding is missing or on the wrong topic.

### 6. Switch modes and start it

```bash
# .env
GMAIL_INBOUND_MODE=push
```

```bash
make api            # terminal 1 — must be running first, see docs/11_RUNBOOK.md
make poller-push     # terminal 2
```

### 7. Verify

Send a real email to the Gmail address in `GMAIL_USER`. It should reach the API
(check its logs for `gmail_push_processed_inbound`) well under the old 15–30s poll
interval. `GET /traces` on the API should show a new trace.

## Troubleshooting

| Symptom | Likely cause |
|---|---|
| `secrets/token.json not found` | run step 5 |
| `PERMISSION_DENIED` registering the watch | step 4's IAM binding missing, or on the wrong topic/project |
| Listener runs but nothing arrives | check the subscription is on the same topic `watch()` targets (`PUBSUB_TOPIC`), and that the topic is in the same `GCP_PROJECT_ID` |
| Gmail 401 after ~7 days | refresh token expired (unverified OAuth consent screen limit) — delete `secrets/token.json`, re-run step 5 |
| Listener silently stops receiving after a week | watch registration expired; `_renew_watch_loop` should have caught this — check the listener's logs for `gmail_watch_renewal_failed` |
