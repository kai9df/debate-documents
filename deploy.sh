#!/usr/bin/env bash
# Run in Google Cloud Shell. No credentials are embedded in this package.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
trap 'echo "Setup stopped at line $LINENO. Keep the error above; rerunning this script reuses existing resources." >&2' ERR

PROJECT_ID="${1:?Usage: bash deploy.sh YOUR_GOOGLE_CLOUD_PROJECT_ID}"
REGION="us-central1"
SERVICE="debate-documents"
RUNTIME="debate-relay@${PROJECT_ID}.iam.gserviceaccount.com"
BUILDER="debate-builder@${PROJECT_ID}.iam.gserviceaccount.com"
INVOKER="debate-invoker@${PROJECT_ID}.iam.gserviceaccount.com"
gc() { gcloud "$@" --project="$PROJECT_ID" --quiet; }
retry() {
  local attempt delay=10
  for attempt in 1 2 3 4 5 6 7; do
    if "$@"; then return 0; fi
    if [[ "$attempt" == 7 ]]; then return 1; fi
    echo "Waiting briefly for Google permissions to take effect..."
    sleep "$delay"
    delay=$((delay < 30 ? delay * 2 : 60))
  done
}

echo "Setting up Debate Documents in project: $PROJECT_ID"
echo "This creates a small Cloud Run service (minimum 0, maximum 1 instance),"
echo "a Firestore database, notification delivery, secrets, and two scheduled jobs."
echo "It does not authorize Gmail or begin forwarding yet."
gc projects describe "$PROJECT_ID" --format='value(projectId)'
BILLING_ENABLED="$(gc billing projects describe "$PROJECT_ID" --format='value(billingEnabled)')"
if [[ "${BILLING_ENABLED,,}" != "true" ]]; then
  echo "Billing must be enabled for this project before deployment." >&2
  exit 1
fi

if [[ -f .deployment.env ]]; then
  PREVIOUS_MAILBOX="$(bash -c 'source .deployment.env; printf "%s" "$GMAIL_ADDRESS"')"
else
  PREVIOUS_MAILBOX=""
fi
read -r -p "Dedicated debate Gmail address${PREVIOUS_MAILBOX:+ [$PREVIOUS_MAILBOX]}: " GMAIL_ADDRESS
GMAIL_ADDRESS="${GMAIL_ADDRESS:-$PREVIOUS_MAILBOX}"
export GMAIL_ADDRESS PROJECT_ID REGION RUNTIME INVOKER
python3 - <<'PY'
import os, re, sys
if not re.fullmatch(r'[A-Za-z0-9.!#$%&\x27*+/=?^_`{|}~-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}', os.environ['GMAIL_ADDRESS']):
    sys.exit('Enter the complete Gmail address, not a display name.')
PY

echo "Enabling the service and deployment APIs..."
gc services enable gmail.googleapis.com pubsub.googleapis.com run.googleapis.com \
  firestore.googleapis.com secretmanager.googleapis.com cloudscheduler.googleapis.com \
  cloudbuild.googleapis.com artifactregistry.googleapis.com iam.googleapis.com \
  iamcredentials.googleapis.com cloudresourcemanager.googleapis.com

for name in debate-relay debate-builder debate-invoker; do
  if ! gc iam service-accounts describe "${name}@${PROJECT_ID}.iam.gserviceaccount.com" >/dev/null 2>&1; then
    gc iam service-accounts create "$name" --display-name="$name"
  fi
done
retry gc projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$BUILDER" --role=roles/run.builder --condition=None >/dev/null
retry gc projects add-iam-policy-binding "$PROJECT_ID" --member="serviceAccount:$RUNTIME" --role=roles/datastore.user --condition=None >/dev/null

if ! gc firestore databases describe --database='(default)' >/dev/null 2>&1; then
  gc firestore databases create --database='(default)' --location="$REGION" --type=firestore-native --edition=standard
fi

ensure_secret() {
  local name="$1"
  if ! gc secrets describe "$name" >/dev/null 2>&1; then
    gc secrets create "$name" --replication-policy=automatic >/dev/null
  fi
  retry gc secrets add-iam-policy-binding "$name" --member="serviceAccount:$RUNTIME" --role=roles/secretmanager.secretAccessor >/dev/null
}
for name in debate-discord-webhook debate-oauth-client debate-gmail-token debate-admin-password; do
  ensure_secret "$name"
done
retry gc secrets add-iam-policy-binding debate-gmail-token --member="serviceAccount:$RUNTIME" --role=roles/secretmanager.secretVersionAdder >/dev/null

if ! gc secrets versions describe latest --secret=debate-admin-password >/dev/null 2>&1; then
  python3 -c 'import secrets,sys; sys.stdout.write(secrets.token_urlsafe(32))' > .admin-password
  gc secrets versions add debate-admin-password --data-file=.admin-password >/dev/null
else
  gc secrets versions access latest --secret=debate-admin-password > .admin-password
fi

if ! gc secrets versions describe latest --secret=debate-discord-webhook >/dev/null 2>&1; then
  read -r -s -p "Paste the Discord forum webhook URL (input is hidden), then press Enter: " WEBHOOK
  printf '\n'
  printf '%s' "$WEBHOOK" | python3 -c 'import re,sys; value=sys.stdin.read(); sys.exit(0 if re.fullmatch(r"https://discord\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+",value) else "Invalid webhook URL. Copy it again from Discord channel Integrations.")'
  printf '%s' "$WEBHOOK" | gc secrets versions add debate-discord-webhook --data-file=- >/dev/null
  unset WEBHOOK
else
  echo "Reusing the stored Discord webhook."
fi
for name in debate-oauth-client debate-gmail-token; do
  if ! gc secrets versions describe latest --secret="$name" >/dev/null 2>&1; then
    printf '{}' | gc secrets versions add "$name" --data-file=- >/dev/null
  fi
done

if ! gc pubsub topics describe debate-gmail >/dev/null 2>&1; then
  gc pubsub topics create debate-gmail >/dev/null
fi
retry gc pubsub topics add-iam-policy-binding debate-gmail --member=serviceAccount:gmail-api-push@system.gserviceaccount.com --role=roles/pubsub.publisher >/dev/null
gc beta services identity create --service=pubsub.googleapis.com >/dev/null
gc beta services identity create --service=cloudscheduler.googleapis.com >/dev/null
PROJECT_NUMBER="$(gc projects describe "$PROJECT_ID" --format='value(projectNumber)')"
retry gc iam service-accounts add-iam-policy-binding "$INVOKER" \
  --member="serviceAccount:service-${PROJECT_NUMBER}@gcp-sa-pubsub.iam.gserviceaccount.com" \
  --role=roles/iam.serviceAccountTokenCreator >/dev/null

python3 - <<'PY'
import json, os
with open('.runtime-env.yaml', 'w') as f:
    json.dump({'GOOGLE_CLOUD_PROJECT': os.environ['PROJECT_ID'],
               'GMAIL_ADDRESS': os.environ['GMAIL_ADDRESS'].lower(),
               'INVOKER_EMAIL': os.environ['INVOKER']}, f)
PY
# Save progress before the build so a failed build can be resumed.
printf 'PROJECT_ID=%q\nREGION=%q\nSERVICE=%q\nGMAIL_ADDRESS=%q\n' \
  "$PROJECT_ID" "$REGION" "$SERVICE" "$GMAIL_ADDRESS" > .deployment.env
echo "Building and deploying. This step can take several minutes..."
retry gc run deploy "$SERVICE" --source=. --region="$REGION" \
  --build-service-account="projects/$PROJECT_ID/serviceAccounts/$BUILDER" \
  --service-account="$RUNTIME" --allow-unauthenticated \
  --cpu=1 --memory=512Mi --cpu-throttling --min=0 --max=1 \
  --min-instances=0 --max-instances=1 --concurrency=8 --timeout=300 --port=8080 \
  --env-vars-file=.runtime-env.yaml --set-secrets=ADMIN_PASSWORD=debate-admin-password:latest
BASE_URL="$(gc run services describe "$SERVICE" --region="$REGION" --format='value(status.url)')"
gc run services update "$SERVICE" --region="$REGION" --update-env-vars="BASE_URL=$BASE_URL" >/dev/null
printf 'BASE_URL=%q\n' "$BASE_URL" >> .deployment.env

if gc pubsub subscriptions describe debate-gmail-push >/dev/null 2>&1; then
  gc pubsub subscriptions modify-push-config debate-gmail-push --push-endpoint="$BASE_URL/push" \
    --push-auth-service-account="$INVOKER" --push-auth-token-audience="$BASE_URL" >/dev/null
else
  gc pubsub subscriptions create debate-gmail-push --topic=debate-gmail \
    --push-endpoint="$BASE_URL/push" --push-auth-service-account="$INVOKER" \
    --push-auth-token-audience="$BASE_URL" --ack-deadline=300 \
    --min-retry-delay=10s --max-retry-delay=300s --message-retention-duration=7d >/dev/null
fi

schedule_job() {
  local name="$1" path="$2" schedule="$3" operation=create
  if gc scheduler jobs describe "$name" --location="$REGION" >/dev/null 2>&1; then operation=update; fi
  gc scheduler jobs "$operation" http "$name" --location="$REGION" --schedule="$schedule" \
    --time-zone=America/Chicago --uri="$BASE_URL/$path" --http-method=POST \
    --oidc-service-account-email="$INVOKER" --oidc-token-audience="$BASE_URL" \
    --attempt-deadline=300s >/dev/null
}
schedule_job debate-watch-renew renew '17 4 * * *'
schedule_job debate-mail-recovery sync '*/5 * * * *'
retry curl --fail --silent --show-error "$BASE_URL/health"
printf '\n\nDEPLOYMENT READY -- forwarding is not connected yet.\n'
printf 'Homepage URL:       %s/\n' "$BASE_URL"
printf 'Privacy policy URL: %s/privacy\n' "$BASE_URL"
printf 'OAuth redirect URI: %s/oauth/callback\n' "$BASE_URL"
printf '\nNext: add the first two URLs to Google Branding, then publish the authorization app.\n'
printf 'Keep .admin-password and all credential files private.\n'
