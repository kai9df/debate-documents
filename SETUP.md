# Setup and troubleshooting

A private, single-inbox Gmail-to-Discord relay. Each Gmail conversation becomes a
Discord forum post. New incoming emails and their original attachments are posted
as replies. Gmail push notifications drive normal delivery; a five-minute check
recovers missed notifications. A daily job renews the Gmail watch.

## Stage 1: Deploy in Google Cloud Shell

Before deployment, create a dedicated Gmail inbox and a private Discord **Forum**
channel. A regular text channel will not work, even if its name matches this app.
Create a webhook whose destination is the forum, and keep required tags disabled.
Create your own Google Cloud project and enable billing. Review the public pages
in `templates/` so their description and privacy policy match your use.

1. Clone this repository in Google Cloud Shell, or download its ZIP and upload
   and extract it in Cloud Shell.
2. From the directory containing `deploy.sh`, run:

   ```bash
   bash deploy.sh YOUR_GOOGLE_CLOUD_PROJECT_ID
   ```

3. Enter the dedicated Gmail address. Paste the Discord forum webhook when the
   hidden prompt asks for it. The webhook is stored in Secret Manager.
4. Wait for `DEPLOYMENT READY`. No Gmail forwarding starts at this stage.

The script creates project-specific service accounts, a Firestore database,
Secret Manager entries, a Pub/Sub topic/subscription, two Scheduler jobs, and a
Cloud Run service in `us-central1`. Minimum instances are zero, maximum one,
with request-based billing. Public pages are `/` and `/privacy`. Notification
handlers validate Google-signed identity tokens; administration requires a
generated password. Your source upload excludes credentials and local files.

Replace `YOUR_GOOGLE_CLOUD_PROJECT_ID` with your actual project ID. The repository
contains source code and fictional test credentials only; it does not include a
running deployment, Gmail access, a Discord webhook, or speech documents. Cloning
or pushing this repository does not redeploy an existing service automatically.

## Stage 2: Finish Google Branding

Paste the printed Homepage URL and Privacy policy URL into Google Auth Platform
> Branding and save. Return to Audience and publish to In production. Keep the
Gmail scope `https://www.googleapis.com/auth/gmail.readonly`.

Google publishing and Google verification are different. Qualifying personal-use
apps may use Google's verification exception. If Google requires domain ownership
or verification that you cannot complete, preserve the exact message and resolve
it before connecting. Do not invent website URLs or claim ownership of `run.app`.

## Stage 3: Create and upload the Google OAuth client

In Google Auth Platform > Clients > Create client:

- Application type: **Web application**
- Name: **Debate Documents relay**
- Authorized JavaScript origins: leave blank
- Authorized redirect URIs: the exact `OAuth redirect URI` printed by deployment

Create the client and download its JSON. Upload that JSON to Cloud Shell (outside
the application folder is fine), then run:

```bash
bash connect.sh ~/YOUR_DOWNLOADED_CLIENT_FILE.json
```

Open the printed setup URL. Username is `admin`; get the password privately with
`cat .admin-password`. The page explains what is forwarded. Authorize only the
dedicated debate Gmail account. This starts forwarding new mail. The initial
connection does not import old emails. Test from a different email account.

## Test a practice round

Send a new email to the dedicated address with a harmless Word file. Reply-all
twice with additional documents. Send another new chain to check separate rounds.
Confirm files open and the original filenames remain. Check `/admin` for errors.
Use the real round email chain; matching is by Gmail conversation ID, not subject
alone. A new chain gets a new forum post. Avoid making Discord forum tags mandatory.

## Costs and limitations

- Target: roughly $0-$3/month at modest volume with available Google free tiers;
  this is not a guarantee. Billing must be enabled. Billing alerts do not cap costs.
- Compute, outbound file transfers, build/image storage, metadata, and logs can
  incur charges. Free allowances are shared by the billing account. Periodic
  checks run even between tournaments. Pausing Scheduler jobs can reduce activity.
- Gmail push is usually prompt but not guaranteed instant. A cold service instance
  and file uploads add delay. Cloud downtime or expired authorization delays delivery.
- One file per Discord message preserves originals and makes retries granular.
  Email text longer than 900 characters is attached in full as `email-body.txt`.
- The default per-file limit is 20 MiB, based on current Discord API documentation.
  Larger files produce a visible warning; their originals remain in Gmail. Actual
  server limits may differ. Rejected uploads remain pending and appear on `/admin`.
- Delivery checkpoints prevent ordinary replays from reposting files. Discord
  webhooks cannot provide a transaction with Firestore: an uncertain response or
  a crash after Discord accepts a file can produce a duplicate on retry.
- Sent mail, drafts, spam, and trash are skipped. Everyone allowed to view the
  forum can read all forwarded emails. Keep account/recovery emails out of this
  dedicated inbox. No AI service is used to process mail.
- Recovery after an expired Gmail history cursor scans messages since the last
  successful check, with a one-day overlap and no pre-activation import. Very
  large recovery backlogs may require manual intervention. Recovery ordering
  across multiple pages can differ from normal arrival order.
- Pause/resume is available at `/admin`. A pause retains the cursor for catch-up.
  Deleting or locking a mapped Discord post causes delivery failures until repaired.
  This first version does not provide automatic round merging or post remapping.
- Error status is available in the protected admin page. There is no independent
  email/SMS outage alert in this version. Check a practice message before rounds.

## Credentials, retention, and maintenance

Never share `.admin-password`, OAuth JSON, refresh tokens, or the webhook URL.
Keep Google Secret Manager as the authoritative credential store. The service
account can add Gmail-token versions, but cannot modify other secret values.
Reauthorizing creates a new token version; old versions can be destroyed manually
after successful reconnection. Google can revoke tokens independently of this app.

Metadata stays in Firestore until the project owner deletes it. Discord posts and
original Gmail messages have independent retention. Revoking Gmail authorization
does not erase already forwarded files. To stop the system fully, pause both
Scheduler jobs, revoke Gmail access in Google Account connections, and remove the
Cloud Run service/subscription and other dedicated resources when no longer needed.
Do not delete the whole cloud project if you have added unrelated resources.

Update dependencies and review cloud spending periodically. `requirements.txt`
pins the top-level versions tested for this release. Rebuilding may resolve newer
transitive dependencies. Test changes in a practice round before tournament use.

## Validation

Local tests mock Google and Discord; they do not prove that your cloud permissions,
Google publishing status, OAuth client, or Discord channel are correct. Run:

```bash
python3 -m pip install -r requirements.txt
python3 -m unittest discover -s tests -v
```

Deployment and end-to-end delivery must still be tested in your own accounts.
