"""Private Gmail-to-Discord relay. Public pages contain no mailbox information."""
import base64
import contextlib
import functools
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from datetime import datetime, timezone

import google.auth.transport.requests
import google.oauth2.id_token
import google_auth_httplib2
import httplib2
from flask import Flask, Response, abort, redirect, render_template, request, session
from google.cloud import firestore, secretmanager
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

from relay import DeliveryError, Discord, RateLimited, decode64, email_contents, header, message_units

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]
PROJECT = os.environ.get("GOOGLE_CLOUD_PROJECT", "your-project-id")
MAILBOX = os.environ.get("GMAIL_ADDRESS", "").lower()
BASE_URL = os.environ.get("BASE_URL", "").rstrip("/")
INVOKER = os.environ.get("INVOKER_EMAIL", "")
TOPIC = f"projects/{PROJECT}/topics/debate-gmail"
ADMIN_PASSWORD = os.environ.get("ADMIN_PASSWORD", "")
app = Flask(__name__)
app.secret_key = ADMIN_PASSWORD or secrets.token_hex(32)
app.config.update(SESSION_COOKIE_SECURE=True, SESSION_COOKIE_HTTPONLY=True,
                  SESSION_COOKIE_SAMESITE="Lax", MAX_CONTENT_LENGTH=1024 * 1024)


@functools.lru_cache
def database():
    return firestore.Client(project=PROJECT)


@functools.lru_cache
def secret_client():
    return secretmanager.SecretManagerServiceClient()


def secret_value(name):
    result = secret_client().access_secret_version(
        request={"name": f"projects/{PROJECT}/secrets/{name}/versions/latest"})
    return result.payload.data.decode("utf-8")


def save_token(value):
    secret_client().add_secret_version(request={
        "parent": f"projects/{PROJECT}/secrets/debate-gmail-token",
        "payload": {"data": json.dumps(value).encode("utf-8")}})


def get_doc(collection, key):
    return database().collection(collection).document(key).get().to_dict() or {}


def put_doc(collection, key, value):
    database().collection(collection).document(key).set(value, merge=True)


def status():
    return get_doc("relay", "state")


def set_status(**values):
    put_doc("relay", "state", values)


class Busy(DeliveryError):
    pass


@contextlib.contextmanager
def mailbox_lock():
    ref = database().collection("relay").document("lock")
    owner = secrets.token_hex(16)

    @firestore.transactional
    def acquire(transaction):
        value = ref.get(transaction=transaction).to_dict() or {}
        if value.get("expires", 0) > time.time():
            raise Busy("Another delivery is in progress; automatic retry will follow.")
        transaction.set(ref, {"owner": owner, "expires": time.time() + 360})

    @firestore.transactional
    def release(transaction):
        value = ref.get(transaction=transaction).to_dict() or {}
        if value.get("owner") == owner:
            transaction.delete(ref)

    acquire(database().transaction())
    try:
        yield
    finally:
        release(database().transaction())


def gmail_for(creds):
    http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=30))
    return build("gmail", "v1", http=http, cache_discovery=False)


def gmail():
    token = json.loads(secret_value("debate-gmail-token"))
    if not token.get("refresh_token"):
        raise DeliveryError("Gmail is not connected yet. Complete the setup page.")
    return gmail_for(Credentials.from_authorized_user_info(token, scopes=SCOPES))


def require_admin(view):
    @functools.wraps(view)
    def wrapper(*args, **kwargs):
        auth = request.authorization
        valid = bool(ADMIN_PASSWORD and auth and auth.type.lower() == "basic"
                     and hmac.compare_digest(auth.username or "", "admin")
                     and hmac.compare_digest(auth.password or "", ADMIN_PASSWORD))
        if not valid:
            return Response("Administrator login required.", 401,
                            {"WWW-Authenticate": 'Basic realm="Debate Documents", charset="UTF-8"'})
        return view(*args, **kwargs)
    return wrapper


def require_service():
    if not BASE_URL or not INVOKER:
        abort(503)
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer "):
        abort(401)
    try:
        claims = google.oauth2.id_token.verify_oauth2_token(
            authorization[7:], google.auth.transport.requests.Request(), audience=BASE_URL)
        if claims.get("email") != INVOKER or claims.get("email_verified") not in (True, "true"):
            abort(403)
    except (ValueError, google.auth.exceptions.GoogleAuthError):
        abort(401)


@app.after_request
def secure_headers(response):
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["X-Frame-Options"] = "DENY"
    response.headers["Content-Security-Policy"] = "default-src 'none'; style-src 'unsafe-inline'; form-action 'self' https://accounts.google.com; base-uri 'none'; frame-ancestors 'none'"
    response.headers["Cache-Control"] = "no-store"
    return response


@app.get("/")
def home():
    return render_template("home.html")


@app.get("/privacy")
def privacy():
    return render_template("privacy.html")


@app.get("/health")
def health():
    return "ok", 200


def flow(state=None, code_verifier=None):
    config = json.loads(secret_value("debate-oauth-client"))
    if "web" not in config:
        raise DeliveryError("Upload the Web application OAuth client JSON using connect.sh first.")
    return Flow.from_client_config(config, scopes=SCOPES, state=state, code_verifier=code_verifier,
                                  redirect_uri=BASE_URL + "/oauth/callback")


def csrf_token():
    return hmac.new(ADMIN_PASSWORD.encode(), b"admin-action", hashlib.sha256).hexdigest()


@app.get("/admin")
@require_admin
def admin():
    return render_template("admin.html", state=status(), csrf=csrf_token(), mailbox=MAILBOX)


@app.get("/setup")
@require_admin
def setup():
    return render_template("setup.html", mailbox=MAILBOX, csrf=csrf_token())


@app.post("/connect")
@require_admin
def connect():
    if not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
        abort(403)
    oauth = flow()
    url, state = oauth.authorization_url(access_type="offline", prompt="consent",
                                         login_hint=MAILBOX, include_granted_scopes="false")
    session["oauth_state"] = state
    session["oauth_started"] = time.time()
    session["oauth_verifier"] = oauth.code_verifier
    return redirect(url)


@app.get("/oauth/callback")
def callback():
    expected = session.pop("oauth_state", "")
    started = session.pop("oauth_started", 0)
    verifier = session.pop("oauth_verifier", None)
    if not expected or time.time() - started > 600 or not hmac.compare_digest(expected, request.args.get("state", "")):
        abort(400, "The connection session expired or is invalid. Return to /setup and try again.")
    if request.args.get("error"):
        return render_template("result.html", title="Connection cancelled", detail="No new Gmail authorization was saved. Return to setup when ready."), 400
    oauth = flow(state=expected, code_verifier=verifier)
    oauth.fetch_token(code=request.args.get("code", ""))
    service = gmail_for(oauth.credentials)
    profile = service.users().getProfile(userId="me").execute()
    if profile["emailAddress"].lower() != MAILBOX:
        raise DeliveryError("The authorized account does not match the dedicated debate inbox. Try again with the correct account.")
    if not oauth.credentials.refresh_token:
        raise DeliveryError("Google did not issue ongoing access. Reconnect and grant consent again.")
    with mailbox_lock():
        previous = status()
        save_token(json.loads(oauth.credentials.to_json()))
        # Baseline is saved before watch, so messages arriving during watch aren't lost.
        if not previous.get("history_id"):
            set_status(history_id=profile["historyId"], activated_at=int(time.time()),
                       last_success_epoch=int(time.time()))
        set_status(connected=True)
        watch = service.users().watch(userId="me", body={"topicName": TOPIC}).execute()
        set_status(watch_expiration=watch["expiration"], last_error="")
    return render_template("result.html", title="Gmail is connected", detail="New incoming emails will now be forwarded to your Discord forum. Existing inbox history was not imported. Send a practice round email with a Word document to test delivery.")


def deliver_message(service, discord, message_id, deadline):
    saved = get_doc("messages", message_id)
    if saved.get("done"):
        return
    if time.monotonic() > deadline:
        raise DeliveryError("Delivery batch paused at its time limit; the next attempt will continue.")
    try:
        message = service.users().messages().get(userId="me", id=message_id, format="full").execute()
    except HttpError as error:
        if error.resp.status == 404:
            put_doc("messages", message_id, {"done": True, "skipped": "message no longer available"})
            return
        raise
    if set(message.get("labelIds", [])) & {"SENT", "DRAFT", "SPAM", "TRASH"}:
        put_doc("messages", message_id, {"done": True, "skipped": "outgoing, draft, spam or trash"})
        return
    thread_key = message["threadId"]
    thread = get_doc("rounds", thread_key)
    if not thread.get("discord_thread"):
        subject = header(message, "Subject") or "Round documents"
        title = f"{subject[:82]} · {thread_key[-8:]}"
        receipt = discord.send("Round email chain: " + subject[:1500], thread_name=title)
        thread = {"discord_thread": receipt["channel_id"], "created_at": int(time.time())}
        put_doc("rounds", thread_key, thread)

    def load_attachment(attachment_id):
        result = service.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id).execute()
        return decode64(result["data"])

    text, attachments = email_contents(message, load_attachment)
    units = message_units(message, text, attachments,
                          max_file_bytes=int(os.environ.get("MAX_FILE_BYTES", str(20 * 1024 * 1024))))
    completed = saved.get("completed_units", 0)
    for index, unit in enumerate(units):
        if index < completed:
            continue
        if time.monotonic() > deadline:
            raise DeliveryError("Delivery batch paused at its time limit; the next attempt will continue.")
        receipt = discord.send(unit["content"], thread_id=thread["discord_thread"], files=unit["files"])
        # Checkpoint only after Discord confirms delivery. There is no cross-service
        # transaction; a crash between send and checkpoint can cause a duplicate.
        put_doc("messages", message_id, {"completed_units": index + 1,
                "last_discord_message": receipt["id"], "thread_id": thread_key})
    put_doc("messages", message_id, {"done": True, "delivered_at": int(time.time())})


def recover_history(service, discord, current, deadline):
    # Capture a cursor before scanning, preserving any email arriving during recovery.
    baseline = service.users().getProfile(userId="me").execute()["historyId"]
    since = max(current["activated_at"], current.get("last_success_epoch", current["activated_at"]) - 86400)
    page = None
    while True:
        response = service.users().messages().list(userId="me", q=f"after:{since}",
            maxResults=100, pageToken=page, includeSpamTrash=False).execute()
        # Reverse each page to make the common single-page recovery chronological.
        for message in reversed(response.get("messages", [])):
            deliver_message(service, discord, message["id"], deadline)
        page = response.get("nextPageToken")
        if not page:
            break
        if time.monotonic() > deadline:
            raise DeliveryError("History recovery is still running; the next attempt will continue.")
    set_status(history_id=baseline)


def sync_mailbox():
    initial = status()
    if not initial.get("connected"):
        return
    if initial.get("discord_retry_at", 0) > time.time():
        raise Busy("Waiting for Discord's requested retry interval.")
    with mailbox_lock():
        current = status()
        if current.get("paused"):
            return
        set_status(last_attempt=datetime.now(timezone.utc).isoformat())
        service = gmail()
        discord = Discord(secret_value("debate-discord-webhook"))
        deadline = time.monotonic() + 210
        start_id = current["history_id"]
        page = None
        while True:
            try:
                response = service.users().history().list(userId="me", startHistoryId=start_id,
                    historyTypes=["messageAdded"], maxResults=100, pageToken=page).execute()
            except HttpError as error:
                if error.resp.status != 404:
                    raise
                recover_history(service, discord, current, deadline)
                break
            for event in response.get("history", []):
                for item in event.get("messagesAdded", []):
                    deliver_message(service, discord, item["message"]["id"], deadline)
                # Safe to advance only after ALL additions in this event succeeded.
                set_status(history_id=event["id"])
            page = response.get("nextPageToken")
            if not page:
                set_status(history_id=response.get("historyId", start_id))
                break
            if time.monotonic() > deadline:
                raise DeliveryError("Delivery batch paused at its time limit; the next attempt will continue.")
        set_status(last_success=datetime.now(timezone.utc).isoformat(),
                   last_success_epoch=int(time.time()), last_error="")


@app.post("/push")
def push():
    require_service()
    body = request.get_json(silent=True) or {}
    try:
        notification = json.loads(decode64(body["message"]["data"]))
    except (KeyError, ValueError, TypeError):
        abort(400)
    if notification.get("emailAddress", "").lower() != MAILBOX:
        abort(403)
    sync_mailbox()
    return "", 204


@app.post("/sync")
def sync_endpoint():
    require_service()
    sync_mailbox()
    return "", 204


@app.post("/renew")
def renew():
    require_service()
    if status().get("connected"):
        with mailbox_lock():
            watch = gmail().users().watch(userId="me", body={"topicName": TOPIC}).execute()
            # Renewing MUST NOT replace the last processed history cursor.
            set_status(watch_expiration=watch["expiration"])
    return "", 204


@app.post("/admin/action")
@require_admin
def admin_action():
    if not hmac.compare_digest(request.form.get("csrf", ""), csrf_token()):
        abort(403)
    action = request.form.get("action")
    if action in ("pause", "resume"):
        with mailbox_lock():
            set_status(paused=action == "pause")
    elif action == "sync":
        sync_mailbox()
    else:
        abort(400)
    return redirect("/admin")


def record_failure(message):
    try:
        set_status(last_error=message, last_error_at=datetime.now(timezone.utc).isoformat())
    except Exception:
        pass


@app.errorhandler(DeliveryError)
def delivery_error(error):
    if isinstance(error, RateLimited):
        set_status(discord_retry_at=time.time() + error.retry_after)
    if not isinstance(error, Busy):
        record_failure(str(error))
    return render_template("result.html", title="Action needs another attempt", detail=str(error)), 503


@app.errorhandler(Exception)
def unexpected_error(error):
    from werkzeug.exceptions import HTTPException
    if isinstance(error, HTTPException):
        return error
    message = f"An operation failed ({type(error).__name__}). Check account authorization and cloud configuration. Automatic deliveries will retry."
    # Do not log exception strings/tracebacks: these may contain secret URLs/content.
    logging.error("Relay operation failed: %s", type(error).__name__)
    record_failure(message)
    return render_template("result.html", title="Operation failed", detail=message), 503
