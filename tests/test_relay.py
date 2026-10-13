import base64
import contextlib
import copy
import json
import os
import time
import unittest
from unittest.mock import MagicMock, patch
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("ADMIN_PASSWORD", "test-password-not-for-deployment")
os.environ.setdefault("GMAIL_ADDRESS", "debate@example.com")
os.environ.setdefault("BASE_URL", "https://relay.example.com")
os.environ.setdefault("INVOKER_EMAIL", "service@example.com")

import app as server
from relay import Discord, DeliveryError, RateLimited, email_contents, message_units


def b64(data):
    return base64.urlsafe_b64encode(data).decode().rstrip("=")


def sample(mid="mail1", tid="round1"):
    return {"id": mid, "threadId": tid, "labelIds": ["INBOX"], "payload": {
        "mimeType": "multipart/mixed", "headers": [
            {"name": "From", "value": "Alex <alex@example.com>"},
            {"name": "Subject", "value": "Round 3"},
            {"name": "Date", "value": "Thu, 8 Oct 2026 12:00:00 -0500"}],
        "parts": [
            {"mimeType": "text/plain", "body": {"data": b64(b"1AC attached")}},
            {"mimeType": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
             "filename": "1AC.docx", "body": {"attachmentId": "file1"}}]}}


class ParsingTests(unittest.TestCase):
    def test_nested_mime_preserves_original_attachment(self):
        message = sample()
        message["payload"]["parts"] = [{"mimeType": "multipart/alternative", "parts": message["payload"]["parts"]}]
        original = b"PK\x03\x04evidence contents\x00\xff"
        text, files = email_contents(message, lambda _: original)
        self.assertEqual(text, "1AC attached")
        self.assertEqual(files[0][0], "1AC.docx")
        self.assertEqual(files[0][1], original)

    def test_html_only_email_and_script_content(self):
        message = {"payload": {"mimeType": "text/html", "body": {"data": b64(b"<p>1AC &amp; case</p><script>hidden()</script>")}}}
        text, files = email_contents(message, lambda _: b"")
        self.assertEqual(text, "1AC & case")
        self.assertEqual(files, [])

    def test_long_email_keeps_full_body_as_file(self):
        text = "Evidence " * 1000
        units = message_units(sample(), text, [("1AC.docx", b"original", "application/octet-stream")])
        self.assertLessEqual(len(units[0]["content"]), 2000)
        self.assertEqual(units[1]["files"][0][1].decode(), text)
        self.assertEqual(units[2]["files"][0][0], "1AC.docx")

    def test_oversize_is_explicit_without_silently_dropping_other_files(self):
        units = message_units(sample(), "", [("big.docx", b"12345", "text/plain"), ("small.docx", b"1", "text/plain")], max_file_bytes=4)
        self.assertIn("could not be uploaded", units[1]["content"])
        self.assertEqual(units[2]["files"][0][0], "small.docx")

    @patch("relay.requests.post")
    def test_discord_disables_pings_and_waits_for_confirmation(self, post):
        post.return_value.status_code = 200
        post.return_value.json.return_value = {"id": "1", "channel_id": "2"}
        Discord("https://discord.com/api/webhooks/123/secret").send("@everyone", thread_name="R3")
        args = post.call_args.kwargs
        self.assertEqual(args["json"]["allowed_mentions"], {"parse": []})
        self.assertEqual(args["params"]["wait"], "true")

    @patch("relay.requests.post")
    def test_rate_limit_carries_requested_delay(self, post):
        post.return_value.status_code = 429
        post.return_value.json.return_value = {"retry_after": 31.5}
        with self.assertRaises(RateLimited) as caught:
            Discord("https://discord.com/api/webhooks/123/secret").send("hello", thread_id="2")
        self.assertEqual(caught.exception.retry_after, 31.5)

    @patch("relay.requests.post")
    def test_transport_error_does_not_expose_webhook(self, post):
        import requests
        post.side_effect = requests.ConnectionError("https://discord.com/api/webhooks/123/secret")
        with self.assertRaises(DeliveryError) as caught:
            Discord("https://discord.com/api/webhooks/123/secret").send("hello", thread_id="2")
        self.assertNotIn("secret", str(caught.exception))


class DeliveryTests(unittest.TestCase):
    def setUp(self):
        self.records = {("relay", "state"): {"connected": True, "history_id": "10", "activated_at": 1}}
        self.getter = patch.object(server, "get_doc", side_effect=lambda c, k: copy.deepcopy(self.records.get((c, k), {})))
        def put(c, k, value):
            self.records.setdefault((c, k), {}).update(copy.deepcopy(value))
        self.setter = patch.object(server, "put_doc", side_effect=put)
        self.getter.start()
        self.setter.start()
        self.addCleanup(self.getter.stop)
        self.addCleanup(self.setter.stop)
        self.service = MagicMock()
        self.service.users().messages().get.return_value.execute.return_value = sample()
        self.service.users().messages().attachments().get.return_value.execute.return_value = {"data": b64(b"original doc")}
        self.discord = MagicMock()
        self.discord.send.return_value = {"id": "discord-message", "channel_id": "discord-round"}

    def test_retry_resumes_at_failed_attachment_without_reposting_header_or_round(self):
        self.discord.send.side_effect = [
            {"id": "round-starter", "channel_id": "discord-round"},
            {"id": "email-header", "channel_id": "discord-round"},
            DeliveryError("temporary failure")]
        with self.assertRaises(DeliveryError):
            server.deliver_message(self.service, self.discord, "mail1", time.monotonic() + 60)
        self.assertEqual(self.records[("messages", "mail1")]["completed_units"], 1)
        self.discord.send.reset_mock(side_effect=True)
        self.discord.send.return_value = {"id": "file", "channel_id": "discord-round"}
        server.deliver_message(self.service, self.discord, "mail1", time.monotonic() + 60)
        self.assertEqual(self.discord.send.call_count, 1)
        self.assertEqual(self.discord.send.call_args.kwargs["files"][0][1], b"original doc")
        self.assertTrue(self.records[("messages", "mail1")]["done"])
        server.deliver_message(self.service, self.discord, "mail1", time.monotonic() + 60)
        self.assertEqual(self.discord.send.call_count, 1)

    def test_separate_rounds_and_same_chain_replies(self):
        for mid, tid in [("a", "round1"), ("b", "round1"), ("c", "round2")]:
            self.service.users().messages().get.return_value.execute.return_value = sample(mid, tid)
            server.deliver_message(self.service, self.discord, mid, time.monotonic() + 60)
        starts = [call for call in self.discord.send.call_args_list if call.kwargs.get("thread_name")]
        self.assertEqual(len(starts), 2)

    def test_sent_messages_are_not_forwarded(self):
        message = sample()
        message["labelIds"] = ["SENT"]
        self.service.users().messages().get.return_value.execute.return_value = message
        server.deliver_message(self.service, self.discord, "mail1", time.monotonic() + 60)
        self.discord.send.assert_not_called()

    def sync_patches(self):
        stack = contextlib.ExitStack()
        stack.enter_context(patch.object(server, "mailbox_lock", contextlib.nullcontext))
        stack.enter_context(patch.object(server, "gmail", return_value=self.service))
        stack.enter_context(patch.object(server, "secret_value", return_value="unused"))
        stack.enter_context(patch.object(server, "Discord", return_value=self.discord))
        return stack

    def test_history_cursor_does_not_skip_failed_addition(self):
        self.service.users().history().list.return_value.execute.return_value = {
            "historyId": "30", "history": [
                {"id": "11", "messagesAdded": [{"message": {"id": "a"}}]},
                {"id": "12", "messagesAdded": [{"message": {"id": "b"}}, {"message": {"id": "c"}}]}]}
        with self.sync_patches(), patch.object(server, "deliver_message", side_effect=[None, None, DeliveryError("failed")]):
            with self.assertRaises(DeliveryError):
                server.sync_mailbox()
        self.assertEqual(server.status()["history_id"], "11")

    def test_history_pagination_and_final_cursor(self):
        self.service.users().history().list.return_value.execute.side_effect = [
            {"historyId": "99", "nextPageToken": "page2", "history": [{"id": "11", "messagesAdded": [{"message": {"id": "a"}}]}]},
            {"historyId": "99", "history": [{"id": "12", "messagesAdded": [{"message": {"id": "b"}}]}]}]
        with self.sync_patches(), patch.object(server, "deliver_message") as deliver:
            server.sync_mailbox()
        self.assertEqual([call.args[2] for call in deliver.call_args_list], ["a", "b"])
        self.assertEqual(server.status()["history_id"], "99")
        self.assertEqual(self.service.users().history().list.call_args.kwargs["startHistoryId"], "10")

    def test_expired_history_scans_from_saved_time_and_keeps_arrival_cursor(self):
        from googleapiclient.errors import HttpError
        from httplib2 import Response
        self.service.users().history().list.return_value.execute.side_effect = HttpError(Response({"status": "404"}), b"{}")
        self.service.users().getProfile.return_value.execute.return_value = {"historyId": "500"}
        self.service.users().messages().list.return_value.execute.return_value = {"messages": [{"id": "newer"}, {"id": "older"}]}
        with self.sync_patches(), patch.object(server, "deliver_message") as deliver:
            server.sync_mailbox()
        self.assertEqual([call.args[2] for call in deliver.call_args_list], ["older", "newer"])
        self.assertEqual(server.status()["history_id"], "500")
        self.assertEqual(self.service.users().messages().list.call_args.kwargs["q"], "after:1")

    def test_pause_preserves_cursor_and_does_not_contact_gmail(self):
        self.records[("relay", "state")]["paused"] = True
        with self.sync_patches(), patch.object(server, "gmail") as gmail:
            server.sync_mailbox()
        gmail.assert_not_called()
        self.assertEqual(server.status()["history_id"], "10")

    def test_renew_preserves_unprocessed_cursor(self):
        self.service.users().watch.return_value.execute.return_value = {"historyId": "900", "expiration": "100000"}
        with patch.object(server, "require_service"), patch.object(server, "mailbox_lock", contextlib.nullcontext), patch.object(server, "gmail", return_value=self.service):
            response = server.app.test_client().post("/renew")
        self.assertEqual(response.status_code, 204)
        self.assertEqual(server.status()["history_id"], "10")


class AuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.client = server.app.test_client()
        self.auth = {"Authorization": "Basic " + base64.b64encode(b"admin:test-password-not-for-deployment").decode()}

    def test_public_pages_do_not_access_cloud_or_disclose_mailbox(self):
        with patch.object(server, "database", side_effect=AssertionError("should not call cloud")):
            for path in ["/", "/privacy"]:
                result = self.client.get(path)
                self.assertEqual(result.status_code, 200)
                self.assertNotIn(b"debate@example.com", result.data)

    def test_admin_and_setup_require_password(self):
        for path in ["/admin", "/setup"]:
            self.assertEqual(self.client.get(path).status_code, 401)

    def test_notification_and_scheduler_endpoints_require_identity(self):
        for path in ["/push", "/sync", "/renew"]:
            self.assertEqual(self.client.post(path).status_code, 401)

    def test_valid_signature_with_wrong_service_account_is_rejected(self):
        with patch.object(server.google.oauth2.id_token, "verify_oauth2_token", return_value={"email": "stranger@example.com", "email_verified": True}):
            result = self.client.post("/sync", headers={"Authorization": "Bearer signed-but-wrong-identity"})
        self.assertEqual(result.status_code, 403)

    def test_csrf_protects_admin_action_and_oauth_start(self):
        for path in ["/connect", "/admin/action"]:
            result = self.client.post(path, headers=self.auth, base_url=server.BASE_URL)
            self.assertEqual(result.status_code, 403)

    def test_callback_rejects_missing_state_without_exchange(self):
        with patch.object(server, "flow") as flow:
            response = self.client.get("/oauth/callback?code=secret-code&state=bad", base_url=server.BASE_URL)
        self.assertEqual(response.status_code, 400)
        flow.assert_not_called()

    def test_oauth_pkce_verifier_survives_browser_round_trip(self):
        config = {"web": {"client_id": "client", "client_secret": "secret", "auth_uri": "https://accounts.google.com/o/oauth2/auth", "token_uri": "https://oauth2.googleapis.com/token", "redirect_uris": [server.BASE_URL + "/oauth/callback"]}}
        with patch.object(server, "secret_value", return_value=json.dumps(config)):
            response = self.client.post("/connect", data={"csrf": server.csrf_token()}, headers=self.auth, base_url=server.BASE_URL)
        self.assertEqual(response.status_code, 302)
        query = parse_qs(urlparse(response.location).query)
        self.assertIn("code_challenge", query)
        # Inspect the signed Flask cookie through the test client's session.
        with self.client.session_transaction(base_url=server.BASE_URL) as sess:
            state = sess["oauth_state"]
            verifier = sess["oauth_verifier"]
        fake_flow = MagicMock()
        fake_flow.fetch_token.side_effect = DeliveryError("stop test before any network exchange")
        with patch.object(server, "flow", return_value=fake_flow) as constructor, patch.object(server, "record_failure"):
            self.client.get(f"/oauth/callback?code=example&state={state}", base_url=server.BASE_URL)
        constructor.assert_called_once_with(state=state, code_verifier=verifier)

    def test_first_connection_sets_baseline_before_watch_and_reconnect_preserves_it(self):
        config_state = {}
        saved_tokens = []
        fake_flow = MagicMock()
        fake_flow.credentials.refresh_token = "example-test-token"
        fake_flow.credentials.to_json.return_value = '{"refresh_token":"example-test-token"}'
        service = MagicMock()
        service.users().getProfile.return_value.execute.return_value = {"emailAddress": server.MAILBOX, "historyId": "100"}
        def watch():
            self.assertIn("history_id", config_state)
            return {"expiration": "9999999", "historyId": "200"}
        service.users().watch.return_value.execute.side_effect = watch
        with patch.object(server, "status", side_effect=lambda: dict(config_state)), \
             patch.object(server, "set_status", side_effect=lambda **kw: config_state.update(kw)), \
             patch.object(server, "flow", return_value=fake_flow), \
             patch.object(server, "gmail_for", return_value=service), \
             patch.object(server, "save_token", side_effect=saved_tokens.append), \
             patch.object(server, "mailbox_lock", contextlib.nullcontext):
            for index in range(2):
                with self.client.session_transaction(base_url=server.BASE_URL) as sess:
                    sess.update(oauth_state="valid", oauth_started=time.time(), oauth_verifier="verifier")
                if index:
                    service.users().getProfile.return_value.execute.return_value["historyId"] = "300"
                response = self.client.get("/oauth/callback?code=example&state=valid", base_url=server.BASE_URL)
                self.assertEqual(response.status_code, 200)
                self.assertEqual(config_state["history_id"], "100")
        self.assertTrue(config_state["connected"])
        self.assertEqual(len(saved_tokens), 2)


if __name__ == "__main__":
    unittest.main()
