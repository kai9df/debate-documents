"""Email parsing and Discord delivery, with no Google credentials at import time."""
import base64
import email.header
import json
import re
from html.parser import HTMLParser

import requests


class DeliveryError(Exception):
    """A safe, content-free operational error suitable for the admin page."""


class RateLimited(DeliveryError):
    def __init__(self, retry_after):
        super().__init__("Discord rate limit reached; automatic retry will follow.")
        self.retry_after = retry_after


def discord_error_details(body):
    """Only expose known explanations and validation codes, never raw response text."""
    if not isinstance(body, dict):
        return "No structured error code returned."
    code = body.get("code")
    reasons = {
        220003: "The webhook points to a regular channel. Its destination must be the Forum channel.",
        40067: "The forum requires a tag. Turn off the channel's Require tags setting.",
        40066: "No forum tags are available to this webhook.",
        50035: "Discord rejected a request field.",
        50045: "The attachment exceeds this server's upload limit.",
        50013: "The webhook lacks permission to post here.",
        50001: "The webhook cannot access this channel.",
        160005: "The destination forum post is locked.",
        220001: "Discord requires a thread name or thread ID for this forum.",
        220002: "Discord received both a thread name and a thread ID.",
        10015: "The webhook no longer exists.",
        50027: "The saved webhook token is invalid.",
        200000: "Discord automatic moderation blocked the message.",
        200001: "Discord automatic moderation blocked the post title.",
        40333: "Discord's edge protection blocked the request.",
    }
    details = [f"Discord code {code}." if type(code) is int else "No numeric Discord code."]
    if type(code) is int and code in reasons:
        details.append(reasons[code])
    def walk(node, path="request", depth=0):
        if not isinstance(node, dict) or depth > 8 or len(details) >= 12:
            return
        for error in node.get("_errors", [])[:5] if isinstance(node.get("_errors", []), list) else []:
            value = error.get("code", "") if isinstance(error, dict) else ""
            if isinstance(value, str) and re.fullmatch(r"[A-Z0-9_]{1,80}", value):
                details.append(f"{path}: {value}.")
        for key, value in list(node.items())[:20]:
            if key != "_errors" and isinstance(key, str) and re.fullmatch(r"[A-Za-z0-9_]{1,40}", key):
                walk(value, path + "." + key, depth + 1)
    walk(body.get("errors", {}))
    return " ".join(details)[:1500]


def decode64(value):
    return base64.urlsafe_b64decode(value + "=" * (-len(value) % 4))


def header(message, name):
    raw = next((x["value"] for x in message.get("payload", {}).get("headers", [])
                if x["name"].lower() == name.lower()), "")
    try:
        return str(email.header.make_header(email.header.decode_header(raw)))
    except (LookupError, UnicodeError):
        return raw


def parts(payload):
    yield payload
    for child in payload.get("parts", []):
        yield from parts(child)


class PlainHTML(HTMLParser):
    def __init__(self):
        super().__init__()
        self.items = []
        self.hidden = 0

    def handle_starttag(self, tag, attrs):
        if tag in ("script", "style"):
            self.hidden += 1
        if tag in ("p", "br", "div", "li") and not self.hidden:
            self.items.append("\n")

    def handle_endtag(self, tag):
        if tag in ("script", "style"):
            self.hidden = max(0, self.hidden - 1)

    def handle_data(self, data):
        if not self.hidden:
            self.items.append(data)


def filename(raw):
    # Discard directories/control characters, keeping readable evidence filenames.
    return re.sub(r"[\x00-\x1f\x7f]", "_", raw.replace("\\", "/").split("/")[-1])[:180] or "attachment"


def email_contents(message, attachment_loader):
    texts, html_texts, attachments = [], [], []
    for part in parts(message.get("payload", {})):
        body = part.get("body", {})
        name = part.get("filename", "")
        mime = part.get("mimeType", "application/octet-stream")
        if not name and mime not in ("text/plain", "text/html"):
            continue
        if "data" in body:
            data = decode64(body["data"])
        elif body.get("attachmentId"):
            data = attachment_loader(body["attachmentId"])
        else:
            continue
        if name:
            attachments.append((filename(name), data, mime))
        else:
            charset = "utf-8"
            for h in part.get("headers", []):
                if h["name"].lower() == "content-type":
                    found = re.search(r'charset=["\']?([^;"\'\s]+)', h["value"], re.I)
                    if found:
                        charset = found.group(1)
            try:
                decoded = data.decode(charset, errors="replace")
            except LookupError:
                decoded = data.decode("utf-8", errors="replace")
            (texts if mime == "text/plain" else html_texts).append(decoded)
    if texts:
        text = "\n".join(texts)
    else:
        parser = PlainHTML()
        parser.feed("\n".join(html_texts))
        text = "".join(parser.items)
    text = re.sub(r"\n{4,}", "\n\n\n", text.replace("\r\n", "\n")).strip()
    return text, attachments


def message_units(message, text, attachments, max_file_bytes=20 * 1024 * 1024):
    """Stable units allow resuming after a later attachment fails."""
    sender = header(message, "From")[:300]
    subject = header(message, "Subject")[:300]
    sent = header(message, "Date")[:150]
    # Disable link previews/mentions in the webhook itself, preserving original text.
    prefix = f"From: {sender}\nSubject: {subject}\nSent: {sent}\nEmail ID: {message['id']}"
    units = [{"content": prefix, "files": []}]
    # Keep the full body available without flooding the round with quoted replies.
    if text:
        units[0]["content"] += "\n\n" + text[:900]
        if len(text) > 900:
            units[0]["content"] += "\n\n[Full email text attached below.]"
            attachments = [("email-body.txt", text.encode("utf-8"), "text/plain")] + attachments
    # One original file per message makes retries and oversize reporting unambiguous.
    for name, data, mime in attachments:
        if len(data) > max_file_bytes:
            units.append({"content": f"Attachment could not be uploaded: {name}\n"
                          f"Size: {len(data) / 1048576:.1f} MiB; configured limit: {max_file_bytes / 1048576:g} MiB.\n"
                          "The original remains in the round email. Ask the sender for a smaller file or a shared link.", "files": []})
        else:
            units.append({"content": f"Attachment: {name}", "files": [(name, data, mime)]})
    return units


class Discord:
    def __init__(self, webhook):
        if not re.fullmatch(r"https://discord\.com/api(?:/v\d+)?/webhooks/\d+/[A-Za-z0-9._-]+", webhook):
            raise DeliveryError("Discord webhook URL is not a supported discord.com webhook URL.")
        self.webhook = webhook

    def send(self, content, thread_id=None, thread_name=None, files=()):
        payload = {"content": content[:2000], "allowed_mentions": {"parse": []}, "flags": 4}
        if thread_name:
            payload["thread_name"] = thread_name[:100]
        params = {"wait": "true"}
        if thread_id:
            params["thread_id"] = str(thread_id)
        try:
            if files:
                payload["attachments"] = [{"id": i, "filename": f[0]} for i, f in enumerate(files)]
                response = requests.post(self.webhook, params=params,
                    data={"payload_json": json.dumps(payload)},
                    files={f"files[{i}]": f for i, f in enumerate(files)}, timeout=(10, 45))
            else:
                response = requests.post(self.webhook, params=params, json=payload, timeout=(10, 45))
        except requests.RequestException:
            # Never include requests exceptions: they contain the secret webhook URL.
            raise DeliveryError("Discord connection failed; delivery will be retried (a duplicate is possible after an uncertain response).") from None
        if response.status_code == 429:
            try:
                delay = max(1, min(3600, float(response.json().get("retry_after", 60))))
            except (ValueError, TypeError, AttributeError):
                delay = 60
            raise RateLimited(delay)
        if response.status_code == 413:
            raise DeliveryError("Discord rejected an attachment as too large. Check the upload limit.")
        if response.status_code >= 400:
            try:
                explanation = discord_error_details(response.json())
            except ValueError:
                explanation = "No structured error code returned."
            raise DeliveryError(f"Discord returned HTTP {response.status_code}. {explanation}")
        try:
            result = response.json()
            if not result.get("id") or not result.get("channel_id"):
                raise ValueError()
            return result
        except (ValueError, AttributeError):
            raise DeliveryError("Discord did not return a delivery receipt; a duplicate is possible on retry.") from None
