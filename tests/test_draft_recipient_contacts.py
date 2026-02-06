import os
import re
import tempfile
import unittest


class _FakeSenderId:
    def __init__(self, user_id: int):
        self.user_id = user_id


class _FakeMessageText:
    ID = "messageText"

    def __init__(self, text: str):
        self.text = type("_T", (), {"text": text})()


class _FakeMessage:
    def __init__(self, *, chat_id: int, thread_id: int, user_id: int, text: str):
        self.chat_id = chat_id
        self.message_thread_id = thread_id
        self.sender_id = _FakeSenderId(user_id)
        self.content = _FakeMessageText(text)
        self.id = 111


class _FakeUpdate:
    def __init__(self, message: _FakeMessage):
        self.message = message


class _FakeCallbackPayload:
    def __init__(self, data: str):
        self.data = data.encode("utf-8")


class _FakeCallbackUpdate:
    def __init__(self, *, chat_id: int, user_id: int, message_id: int, data: str):
        self.chat_id = chat_id
        self.sender_user_id = user_id
        self.message_id = message_id
        self.payload = _FakeCallbackPayload(data)
        self.id = 1


class _FakeClient:
    def __init__(self):
        class _Api:
            def __init__(self, outer):
                self._outer = outer

            async def send_message(self, **kwargs):
                self._outer.sent_messages.append(kwargs)
                return type("_Msg", (), {"id": 888})()

            async def answer_callback_query(
                self, callback_query_id: int, text: str, url: str, cache_time: int
            ):
                return None

        self.edits = []
        self.sent_messages = []
        self.api = _Api(self)

    async def edit_text(self, **kwargs):
        self.edits.append(kwargs)


class TestDraftRecipientContacts(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path

        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        DBManager.reset_instance()
        AccountManager.reset_instance()

        from app.email_utils.account_manager import AccountManager as AM

        self.account_mgr = AM()
        self.assertTrue(
            self.account_mgr.add_account(
                {
                    "email": "a@example.com",
                    "password": "pw",
                    "imap_server": "imap.example.com",
                    "imap_port": 993,
                    "imap_ssl": True,
                    "smtp_server": "smtp.example.com",
                    "smtp_port": 465,
                    "smtp_ssl": True,
                    "alias": "Work",
                    "tg_group_id": 123,
                }
            )
        )
        self.account = self.account_mgr.get_account(
            email="a@example.com", smtp_server="smtp.example.com"
        )

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    def _seed_contact_history(self):
        from app.database import DBManager

        db = DBManager()
        conn = db._get_connection()
        cur = conn.cursor()
        rows = [
            (
                int(self.account["id"]),
                "<msg-1@example.com>",
                "Alice <alice@example.com>",
                "a@example.com",
                "",
                "",
                "hello",
                "2025-01-01",
                "body",
                "",
                "1",
                "INBOX",
            ),
            (
                int(self.account["id"]),
                "<msg-2@example.com>",
                "a@example.com",
                "Bob <bob@example.com>",
                "Carol <carol@example.com>",
                "",
                "hello2",
                "2025-01-02",
                "body",
                "",
                "2",
                "OUTGOING",
            ),
        ]
        cur.executemany(
            """
            INSERT INTO emails
              (email_account, message_id, sender, recipient, cc, bcc, subject, email_date,
               body_text, body_html, uid, mailbox)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            rows,
        )
        conn.commit()
        conn.close()

    async def test_to_without_arg_shows_contact_selector(self):
        from app.database import DBManager
        from unittest import mock

        db = DBManager()
        self._seed_contact_history()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/to"))

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        selector_markup = client.sent_messages[-1].get("reply_markup")
        self.assertIsNotNone(selector_markup)
        labels = [
            button.text
            for row in getattr(selector_markup, "rows", [])
            for button in row
            if hasattr(button, "text")
        ]
        joined = "\n".join(labels).lower()
        self.assertIn("alice@example.com", joined)
        self.assertIn("bob@example.com", joined)
        self.assertNotIn("a@example.com", joined)

    async def test_to_keyword_filters_contacts(self):
        from app.database import DBManager
        from unittest import mock

        db = DBManager()
        self._seed_contact_history()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        update = _FakeUpdate(
            _FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/to bob")
        )

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        selector_markup = client.sent_messages[-1].get("reply_markup")
        self.assertIsNotNone(selector_markup)
        labels = [
            button.text
            for row in getattr(selector_markup, "rows", [])
            for button in row
            if hasattr(button, "text")
        ]
        joined = "\n".join(labels).lower()
        self.assertIn("bob@example.com", joined)
        self.assertNotIn("alice@example.com", joined)

    async def test_to_direct_email_still_updates_draft(self):
        from app.database import DBManager
        from unittest import mock

        db = DBManager()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(draft_id=draft_id, updates={"card_message_id": 99})

        client = _FakeClient()
        update = _FakeUpdate(
            _FakeMessage(
                chat_id=123,
                thread_id=456,
                user_id=1,
                text="/to direct@example.com",
            )
        )

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        draft = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertEqual(draft["to_addrs"], "direct@example.com")
        self.assertFalse(client.sent_messages)

    async def test_callback_select_contact_requires_save_to_apply(self):
        from app.database import DBManager
        from unittest import mock

        db = DBManager()
        self._seed_contact_history()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(
            draft_id=draft_id,
            updates={"card_message_id": 99, "to_addrs": "old@example.com"},
        )

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/to"))

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        selector_markup = client.sent_messages[-1].get("reply_markup")
        self.assertIsNotNone(selector_markup)
        first_button = next(
            (
                button
                for row in getattr(selector_markup, "rows", [])
                for button in row
                if hasattr(button, "type_")
            ),
            None,
        )
        self.assertIsNotNone(first_button)
        callback_data = (
            getattr(getattr(first_button, "type_", None), "data", b"") or b""
        ).decode("utf-8")
        self.assertTrue(callback_data.startswith("draft:rcpt_pick:toggle:"))
        selected_from_label = (getattr(first_button, "text", "") or "").lower()
        email_match = re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", selected_from_label)
        self.assertIsNotNone(email_match)
        selected_email = email_match.group(0)

        callback_update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=888,
            data=callback_data,
        )
        with mock.patch(
            "app.bot.handlers.callback.Conversation.get_instance",
            lambda *_args, **_kwargs: None,
        ):
            from app.bot.handlers.callback import callback_handler

            await callback_handler(client, callback_update)

        # Toggle only updates picker UI; draft field is updated on explicit save.
        refreshed = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertEqual((refreshed.get("to_addrs") or "").lower(), "old@example.com")

        save_update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=888,
            data=f"draft:rcpt_pick:save:{draft_id}:to",
        )
        with mock.patch(
            "app.bot.handlers.callback.Conversation.get_instance",
            lambda *_args, **_kwargs: None,
        ):
            from app.bot.handlers.callback import callback_handler

            await callback_handler(client, save_update)

        refreshed = db.get_active_draft(chat_id=123, thread_id=456)
        to_addrs = (refreshed.get("to_addrs") or "").lower()
        self.assertIn("old@example.com", to_addrs)
        self.assertIn(selected_email, to_addrs)
        self.assertTrue(any(int(edit.get("message_id") or 0) == 99 for edit in client.edits))

    async def test_callback_multi_select_can_add_multiple_contacts(self):
        from app.database import DBManager
        from unittest import mock

        db = DBManager()
        self._seed_contact_history()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(
            draft_id=draft_id,
            updates={"card_message_id": 99, "to_addrs": "old@example.com"},
        )

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/to"))

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        self.assertTrue(client.sent_messages)
        selector_markup = client.sent_messages[-1].get("reply_markup")
        self.assertIsNotNone(selector_markup)
        contact_buttons = [
            button
            for row in getattr(selector_markup, "rows", [])
            for button in row
            if hasattr(button, "type_")
            and "draft:rcpt_pick:toggle:" in (
                (getattr(getattr(button, "type_", None), "data", b"") or b"").decode(
                    "utf-8"
                )
            )
        ]
        self.assertGreaterEqual(len(contact_buttons), 2)

        picked_emails = []
        for button in contact_buttons[:2]:
            label = (getattr(button, "text", "") or "").lower()
            match = re.search(r"[a-z0-9._%+\-]+@[a-z0-9.\-]+\.[a-z]{2,}", label)
            self.assertIsNotNone(match)
            picked_emails.append(match.group(0))

            callback_update = _FakeCallbackUpdate(
                chat_id=123,
                user_id=1,
                message_id=888,
                data=(
                    getattr(getattr(button, "type_", None), "data", b"") or b""
                ).decode("utf-8"),
            )
            with mock.patch(
                "app.bot.handlers.callback.Conversation.get_instance",
                lambda *_args, **_kwargs: None,
            ):
                from app.bot.handlers.callback import callback_handler

                await callback_handler(client, callback_update)

        # Before save, only existing addresses remain.
        refreshed = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertEqual((refreshed.get("to_addrs") or "").lower(), "old@example.com")

        save_update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=888,
            data=f"draft:rcpt_pick:save:{draft_id}:to",
        )
        with mock.patch(
            "app.bot.handlers.callback.Conversation.get_instance",
            lambda *_args, **_kwargs: None,
        ):
            from app.bot.handlers.callback import callback_handler

            await callback_handler(client, save_update)

        refreshed = db.get_active_draft(chat_id=123, thread_id=456)
        to_addrs = (refreshed.get("to_addrs") or "").lower()
        self.assertIn("old@example.com", to_addrs)
        for email_addr in picked_emails:
            self.assertIn(email_addr, to_addrs)

    async def test_callback_save_without_change_does_not_edit_draft_card(self):
        from app.database import DBManager
        from unittest import mock

        db = DBManager()
        self._seed_contact_history()
        draft_id = db.create_draft(
            account_id=self.account["id"],
            chat_id=123,
            thread_id=456,
            draft_type="compose",
            from_identity_email="a@example.com",
        )
        db.update_draft(
            draft_id=draft_id,
            updates={"card_message_id": 99, "to_addrs": "old@example.com"},
        )

        client = _FakeClient()
        update = _FakeUpdate(_FakeMessage(chat_id=123, thread_id=456, user_id=1, text="/to"))

        with mock.patch("app.bot.handlers.message.validate_admin", lambda _u: True), mock.patch(
            "app.bot.handlers.message.Conversation.get_instance", lambda *_args, **_kwargs: None
        ):
            from app.bot.handlers.message import message_handler

            await message_handler(client, update)

        save_update = _FakeCallbackUpdate(
            chat_id=123,
            user_id=1,
            message_id=888,
            data=f"draft:rcpt_pick:save:{draft_id}:to",
        )
        with mock.patch(
            "app.bot.handlers.callback.Conversation.get_instance",
            lambda *_args, **_kwargs: None,
        ):
            from app.bot.handlers.callback import callback_handler

            await callback_handler(client, save_update)

        refreshed = db.get_active_draft(chat_id=123, thread_id=456)
        self.assertEqual((refreshed.get("to_addrs") or "").lower(), "old@example.com")
        self.assertFalse(any(int(edit.get("message_id") or 0) == 99 for edit in client.edits))
