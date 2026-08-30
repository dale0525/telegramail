"""V2 connection verification and local draft-attachment contracts."""

from __future__ import annotations

import asyncio
import os
import stat
import tempfile
import unittest
from pathlib import Path

import httpx

from app.api import create_app
from app.core.config import Settings
from app.db import V2Repository
from app.integrations.mail.types import Attachment
from app.services.v2_repository_adapter import V2MailRepositoryAdapter
from app.services.account_verification import AccountDeleteWorker, AccountVerificationWorker
from tests.test_v2_api_auth import BOT_TOKEN, signed_init_data


ACCOUNT_CONFIG = {
    "imap_server": "imap.example.test", "imap_port": 993, "imap_ssl": True,
    "smtp_server": "smtp.example.test", "smtp_port": 465, "smtp_ssl": True,
}


class _Verifier:
    def __init__(self, account, error: Exception | None = None):
        self.account, self.error = account, error

    async def verify(self):
        if self.error:
            raise self.error
        await asyncio.sleep(0)
        return True


class _TelegramTopics:
    def __init__(self):
        self.deleted = []

    async def delete_forum_topic(self, chat_id, thread_id):
        self.deleted.append((chat_id, thread_id))
        return True


class AccountVerificationTests(unittest.IsolatedAsyncioTestCase):
    async def _client(self, *, imap_error: Exception | None = None, smtp_error: Exception | None = None):
        directory = tempfile.TemporaryDirectory()
        repo = V2Repository(Path(directory.name) / "api.db", master_key=b"k" * 32)
        app = create_app(
            Settings(session_secret="s" * 32, telegram_bot_token=BOT_TOKEN, setup_code="one-time", secure_cookies=False, web_dist=None),
            db=repo,
            imap_transport_factory=lambda account: _Verifier(account, imap_error),
            smtp_transport_factory=lambda account: _Verifier(account, smtp_error),
        )
        client = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://test")
        setup = await client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})
        csrf = setup.json()["csrf_token"]
        account = await client.post("/api/v1/accounts", headers={"X-CSRF-Token": csrf}, json={**ACCOUNT_CONFIG, "email": "a@example.test", "password": "secret", "signature": "Regards"})
        return directory, client, csrf, account

    async def test_verifies_both_transports_and_hides_secret(self):
        directory, client, csrf, account = await self._client()
        try:
            self.assertEqual(account.status_code, 201)
            self.assertEqual("Regards", account.json()["signature"])
            self.assertFalse(account.json()["enabled"])
            self.assertNotIn("tg_group_id", account.json())
            self.assertEqual(422, (await client.post("/api/v1/accounts", headers={"X-CSRF-Token": csrf}, json={**ACCOUNT_CONFIG, "email": "legacy@example.test", "password": "secret", "tg_group_id": 9})).status_code)
            patched = await client.patch(
                f"/api/v1/accounts/{account.json()['id']}", headers={"X-CSRF-Token": csrf}, json={"signature": "Updated"}
            )
            self.assertEqual("Updated", patched.json()["signature"])
            response = await client.post(f"/api/v1/accounts/{account.json()['id']}/verify", headers={"X-CSRF-Token": csrf})
            self.assertEqual(response.status_code, 200)
            self.assertEqual({"imap": {"ok": True, "error": None}, "smtp": {"ok": True, "error": None}}, response.json())
            self.assertTrue(client._transport.app.state.db.get_account(account.json()["id"])["enabled"])
            changed_transport = await client.patch(
                f"/api/v1/accounts/{account.json()['id']}", headers={"X-CSRF-Token": csrf},
                json={"smtp_server": "smtp.changed.example.test"},
            )
            self.assertEqual(200, changed_transport.status_code)
            self.assertFalse(changed_transport.json()["enabled"])
            self.assertNotIn("secret", response.text)
        finally:
            await client.aclose()
            directory.cleanup()


    async def test_reports_imap_and_smtp_failures_without_transport_text(self):
        directory, client, csrf, account = await self._client(imap_error=PermissionError("secret"), smtp_error=TimeoutError("secret"))
        try:
            response = await client.post(f"/api/v1/accounts/{account.json()['id']}/verify", headers={"X-CSRF-Token": csrf})
            self.assertEqual(200, response.status_code)
            self.assertEqual("authentication", response.json()["imap"]["error"])
            self.assertEqual("timeout", response.json()["smtp"]["error"])
            self.assertFalse(client._transport.app.state.db.get_account(account.json()["id"])["enabled"])
            self.assertNotIn("secret", response.text)
        finally:
            await client.aclose()
            directory.cleanup()

    async def test_rejects_incomplete_unsafe_or_invalid_transport_settings(self):
        directory, client, csrf, _account = await self._client()
        try:
            for overrides in (
                {"imap_server": ""}, {"smtp_server": "smtp example.test"},
                {"imap_port": 0}, {"smtp_port": 65536}, {"imap_ssl": False}, {"smtp_ssl": False},
            ):
                response = await client.post(
                    "/api/v1/accounts", headers={"X-CSRF-Token": csrf},
                    json={**ACCOUNT_CONFIG, **overrides, "email": f"{len(overrides)}@example.test", "password": "secret"},
                )
                self.assertEqual(422, response.status_code)
            starttls = await client.post(
                "/api/v1/accounts", headers={"X-CSRF-Token": csrf},
                json={**ACCOUNT_CONFIG, "smtp_server": "smtp.office365.com", "smtp_port": 587,
                      "smtp_ssl": False, "email": "starttls@example.test", "password": "secret"},
            )
            self.assertEqual(201, starttls.status_code)
        finally:
            await client.aclose()
            directory.cleanup()

    async def test_verify_requires_session_and_csrf(self):
        directory, client, _csrf, account = await self._client()
        try:
            self.assertEqual(403, (await client.post(f"/api/v1/accounts/{account.json()['id']}/verify")).status_code)
            anonymous = httpx.AsyncClient(transport=httpx.ASGITransport(app=client._transport.app), base_url="http://test")
            try:
                self.assertEqual(401, (await anonymous.post(f"/api/v1/accounts/{account.json()['id']}/verify")).status_code)
            finally:
                await anonymous.aclose()
        finally:
            await client.aclose()
            directory.cleanup()


class AccountLifecycleTests(unittest.IsolatedAsyncioTestCase):
    def _repo(self):
        directory = tempfile.TemporaryDirectory()
        return directory, V2Repository(Path(directory.name) / "api.db", master_key=b"k" * 32)

    async def test_soft_delete_keeps_history_and_reactivation_reuses_account(self):
        directory, repo = self._repo()
        try:
            account = repo.create_account({**ACCOUNT_CONFIG, "email": "keep@example.test"}, "secret")
            email = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="1", subject="history", body_text="body")
            repo.assign_thread(email["id"], account_id=account["id"], root_message_id="m", subject="history", telegram_chat_id=1, telegram_message_thread_id=2)
            self.assertTrue(repo.soft_delete_account(account["id"]))
            self.assertEqual([], repo.list_accounts())
            self.assertIsNotNone(repo.get_email(email["id"]))
            restored = repo.create_account({**ACCOUNT_CONFIG, "email": "keep@example.test"}, "new-secret")
            self.assertEqual(account["id"], restored["id"])
            self.assertEqual(1, len(repo.list_accounts()))
        finally:
            directory.cleanup()

    async def test_pending_purge_cannot_be_reactivated_under_the_same_account_id(self):
        directory, repo = self._repo()
        try:
            account = repo.create_account({**ACCOUNT_CONFIG, "email": "pending-purge@example.test"}, "secret")
            self.assertTrue(repo.soft_delete_account(account["id"]))
            repo.create_account_delete_operation(account["id"], purge_data=True)
            with self.assertRaisesRegex(RuntimeError, "cleanup is still in progress"):
                repo.create_account({**ACCOUNT_CONFIG, "email": "pending-purge@example.test"}, "new-secret")
            self.assertEqual("deleting", repo.claim_next_account_delete(lease_token="test-worker")["status"])
            self.assertIsNone(repo.claim_next_account_delete(lease_token="second-worker"))
            self.assertIsNone(repo.get_account(account["id"]))
            self.assertIsNotNone(repo.get_account(account["id"], include_deleted=True))
        finally:
            directory.cleanup()

    async def test_purge_worker_deletes_topics_and_cascades_local_data(self):
        directory, repo = self._repo()
        telegram = _TelegramTopics()
        previous_data_dir = os.environ.get("TELEGRAMAIL_DATA_DIR")
        os.environ["TELEGRAMAIL_DATA_DIR"] = directory.name
        try:
            account = repo.create_account({**ACCOUNT_CONFIG, "email": "purge@example.test"}, "secret")
            email = repo.insert_incoming_if_absent(account["id"], mailbox="INBOX", uid="1", subject="purge", body_text="body")
            repo.assign_thread(email["id"], account_id=account["id"], root_message_id="m", subject="purge", telegram_chat_id=9, telegram_message_thread_id=10)
            draft = repo.create_draft(account["id"], body_markdown="draft")
            attachment_dir = Path(directory.name) / "attachments" / str(draft["id"])
            attachment_dir.mkdir(parents=True)
            attachment_path = attachment_dir / "draft.txt"
            attachment_path.write_text("draft attachment")
            repo.add_draft_attachment(draft["id"], file_name="draft.txt", local_path=f"attachments/{draft['id']}/draft.txt")
            operation = repo.create_account_delete_operation(account["id"], purge_data=True)
            result = await AccountDeleteWorker(repo, telegram).run_once()
            self.assertEqual("deleted", result["status"])
            self.assertEqual([(9, 10)], telegram.deleted)
            self.assertIsNone(repo.get_account(account["id"]))
            self.assertIsNone(repo.get_email(email["id"]))
            self.assertFalse(attachment_path.exists())
            self.assertEqual(operation["id"], result["id"])
        finally:
            if previous_data_dir is None:
                os.environ.pop("TELEGRAMAIL_DATA_DIR", None)
            else:
                os.environ["TELEGRAMAIL_DATA_DIR"] = previous_data_dir
            directory.cleanup()

    async def test_verification_worker_records_non_sensitive_failure_and_retry(self):
        directory, repo = self._repo()
        try:
            account = repo.create_account({**ACCOUNT_CONFIG, "email": "verify@example.test", "enabled": False}, "secret")
            class Failing:
                async def verify(self):
                    raise PermissionError("password should not leak")
            worker = AccountVerificationWorker(repo, lambda _account: Failing(), lambda _account: Failing())
            await worker.run_once()
            saved = repo.get_account(account["id"])
            self.assertEqual("failed", saved["connection_status"])
            self.assertEqual("authentication", saved["connection_error"])
            self.assertNotIn("password", saved["connection_error"])
            self.assertFalse(saved["enabled"])
        finally:
            directory.cleanup()


class DraftAttachmentTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.previous_data_dir = os.environ.get("TELEGRAMAIL_DATA_DIR")
        os.environ["TELEGRAMAIL_DATA_DIR"] = self.directory.name
        self.repo = V2Repository(Path(self.directory.name) / "api.db", master_key=b"k" * 32)
        self.app = create_app(Settings(session_secret="s" * 32, telegram_bot_token=BOT_TOKEN, setup_code="one-time", secure_cookies=False, web_dist=None), db=self.repo)
        self.client = httpx.AsyncClient(transport=httpx.ASGITransport(app=self.app), base_url="http://test")
        setup = await self.client.post("/api/v1/auth/setup", json={"initData": signed_init_data(), "code": "one-time"})
        self.headers = {"X-CSRF-Token": setup.json()["csrf_token"]}
        account = await self.client.post("/api/v1/accounts", headers=self.headers, json={**ACCOUNT_CONFIG, "email": "a@example.test", "password": "secret"})
        self.account_id = account.json()["id"]

    async def asyncTearDown(self):
        await self.client.aclose()
        if self.previous_data_dir is None:
            os.environ.pop("TELEGRAMAIL_DATA_DIR", None)
        else:
            os.environ["TELEGRAMAIL_DATA_DIR"] = self.previous_data_dir
        self.directory.cleanup()

    async def _draft(self):
        response = await self.client.post("/api/v1/drafts", headers=self.headers, json={"account_id": self.account_id, "to_addrs": "to@example.test"})
        self.assertEqual(201, response.status_code)
        return response.json()

    async def test_upload_is_private_normalized_and_loaded_for_smtp(self):
        draft = await self._draft()
        self.repo.update_draft(
            draft_id=draft["id"],
            updates={"body_markdown": "**Rendered safely** <script>alert(1)</script>"},
        )
        uploaded = await self.client.post(
            f"/api/v1/drafts/{draft['id']}/attachments", headers={**self.headers, "If-Match": f'"{draft["version"]}"'},
            files={"file": ("../../report.txt", b"attachment data", "text/plain")},
        )
        self.assertEqual(201, uploaded.status_code)
        body = uploaded.json()
        self.assertEqual("report.txt", body["file_name"])
        self.assertNotIn(self.directory.name, uploaded.text)
        row = self.repo.get_draft_attachment(draft["id"], body["id"])
        path = Path(self.directory.name, row["local_path"])
        self.assertTrue(path.is_file())
        self.assertEqual(0o600, stat.S_IMODE(path.stat().st_mode))
        operation = self.repo.create_send_operation(self.account_id, "send-attachment", draft_id=draft["id"])
        send = V2MailRepositoryAdapter(self.repo).get_send(operation["id"])
        self.assertEqual((Attachment("report.txt", b"attachment data", "text/plain"),), send.draft.attachments)
        self.assertIn("<strong>Rendered safely</strong>", send.draft.html_body)
        self.assertNotIn("<script>", send.draft.html_body)

    async def test_oversize_and_cross_draft_delete_are_rejected(self):
        first, second = await self._draft(), await self._draft()
        oversize = await self.client.post(
            f"/api/v1/drafts/{first['id']}/attachments", headers={**self.headers, "If-Match": f'"{first["version"]}"'},
            files={"file": ("big.bin", b"x" * (25 * 1024 * 1024 + 1), "application/octet-stream")},
        )
        self.assertEqual(413, oversize.status_code)
        uploaded = await self.client.post(
            f"/api/v1/drafts/{second['id']}/attachments", headers={**self.headers, "If-Match": f'"{second["version"]}"'},
            files={"file": ("second.txt", b"second", "text/plain")},
        )
        row = self.repo.get_draft_attachment(second["id"], uploaded.json()["id"])
        path = Path(self.directory.name, row["local_path"])
        rejected = await self.client.delete(
            f"/api/v1/drafts/{first['id']}/attachments/{uploaded.json()['id']}", headers={**self.headers, "If-Match": f'"{first["version"]}"'},
        )
        self.assertEqual(404, rejected.status_code)
        self.assertTrue(path.exists())

    async def test_delete_removes_only_its_own_file(self):
        draft = await self._draft()
        uploaded = await self.client.post(
            f"/api/v1/drafts/{draft['id']}/attachments", headers={**self.headers, "If-Match": f'"{draft["version"]}"'},
            files={"file": ("remove.txt", b"remove", "text/plain")},
        )
        row = self.repo.get_draft_attachment(draft["id"], uploaded.json()["id"])
        path = Path(self.directory.name, row["local_path"])
        deleted = await self.client.delete(
            f"/api/v1/drafts/{draft['id']}/attachments/{uploaded.json()['id']}",
            headers={**self.headers, "If-Match": f'"{uploaded.json()["draft_version"]}"'},
        )
        self.assertEqual(200, deleted.status_code)
        self.assertFalse(path.exists())
        self.assertIsNone(self.repo.get_draft_attachment(draft["id"], uploaded.json()["id"]))

    async def test_repository_draft_delete_cleans_its_attachment_directory(self):
        draft = await self._draft()
        uploaded = await self.client.post(
            f"/api/v1/drafts/{draft['id']}/attachments", headers={**self.headers, "If-Match": f'"{draft["version"]}"'},
            files={"file": ("draft-delete.txt", b"delete", "text/plain")},
        )
        row = self.repo.get_draft_attachment(draft["id"], uploaded.json()["id"])
        path = Path(self.directory.name, row["local_path"])
        self.assertTrue(self.repo.delete_draft(draft["id"]))
        self.assertFalse(path.exists())
        self.assertFalse(path.parent.exists())
