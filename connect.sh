#!/usr/bin/env bash
# Run only after the Branding URLs are saved and Audience says In production.
set -euo pipefail
umask 077
cd "$(dirname "${BASH_SOURCE[0]}")"
if [[ ! -f .deployment.env ]]; then echo "Run deploy.sh first." >&2; exit 1; fi
source .deployment.env
export PROJECT_ID BASE_URL
if [[ -z "${BASE_URL:-}" ]]; then echo "Deployment did not finish. Rerun deploy.sh." >&2; exit 1; fi
OAUTH_FILE="${1:-}"
if [[ -z "$OAUTH_FILE" ]]; then read -r -p "Path to the downloaded Web application OAuth client JSON: " OAUTH_FILE; fi
export OAUTH_FILE
python3 - <<'PY'
import json, os, sys
from pathlib import Path
path = Path(os.environ['OAUTH_FILE']).expanduser()
try:
    data = json.loads(path.read_text())
except (ValueError, OSError):
    sys.exit('Unable to read that JSON file. Upload the downloaded client JSON and check its path.')
web = data.get('web', {})
if not web.get('client_id') or not web.get('client_secret'):
    sys.exit('Create a Web application OAuth client, not a Desktop app client.')
if web.get('project_id') != os.environ['PROJECT_ID']:
    sys.exit('This OAuth client belongs to a different project.')
if os.environ['BASE_URL'] + '/oauth/callback' not in web.get('redirect_uris', []):
    sys.exit('Add the exact OAuth redirect URI printed by deploy.sh to the client, then download its JSON again.')
if web.get('auth_uri') != 'https://accounts.google.com/o/oauth2/auth' or web.get('token_uri') != 'https://oauth2.googleapis.com/token':
    sys.exit('Unexpected authorization endpoints. Use the original Google-downloaded client JSON.')
PY
# Python expands a leading ~ if it was entered at the interactive prompt.
OAUTH_FILE="$(python3 -c 'import os; print(os.path.expanduser(os.environ["OAUTH_FILE"]))')"
gcloud secrets versions add debate-oauth-client --data-file="$OAUTH_FILE" --project="$PROJECT_ID" --quiet >/dev/null
printf '\nOAuth client saved. Open: %s/setup\n' "$BASE_URL"
printf 'Browser login username: admin\n'
printf 'To view your private login password, run: cat .admin-password\n'
printf 'Do not share the password, client JSON, or webhook URL in chat.\n'
printf 'On the setup page, confirm the account and authorize forwarding.\n'
