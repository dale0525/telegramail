import os
import tempfile
import unittest


class TestSignatureStore(unittest.TestCase):
    def test_legacy_markdown_is_supported(self):
        from app.email_utils.signatures import list_account_signatures

        items, default_id = list_account_signatures("Best regards,\nTeam")
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["markdown"], "Best regards,\nTeam")
        self.assertEqual(default_id, items[0]["id"])

    def test_add_and_switch_default_signature(self):
        from app.email_utils.signatures import (
            add_account_signature,
            list_account_signatures,
            set_default_account_signature,
        )

        raw, first_id = add_account_signature(
            None,
            name="Work",
            markdown="Work signature",
        )
        raw, second_id = add_account_signature(
            raw,
            name="Personal",
            markdown="Personal signature",
        )
        raw = set_default_account_signature(raw, second_id)

        items, default_id = list_account_signatures(raw)
        self.assertEqual(len(items), 2)
        self.assertEqual(default_id, second_id)
        self.assertEqual(first_id != second_id, True)

    def test_resolve_signature_can_choose_specific_or_none(self):
        from app.email_utils.signatures import (
            CHOICE_NONE,
            add_account_signature,
            resolve_signature_for_send,
            set_default_account_signature,
        )

        raw, first_id = add_account_signature(
            None,
            name="Default",
            markdown="Default signature",
        )
        raw, second_id = add_account_signature(
            raw,
            name="Alt",
            markdown="Alt signature",
        )
        raw = set_default_account_signature(raw, first_id)

        selected, _label = resolve_signature_for_send(raw, second_id)
        self.assertEqual(selected, "Alt signature")

        selected_none, _label_none = resolve_signature_for_send(raw, CHOICE_NONE)
        self.assertIsNone(selected_none)


class TestSignatureStatePersistence(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self._tmp.name, "telegramail-test.db")
        os.environ["TELEGRAMAIL_DB_PATH"] = self.db_path
        from app.database import DBManager
        from app.email_utils.account_manager import AccountManager

        DBManager.reset_instance()
        AccountManager.reset_instance()

    def tearDown(self):
        try:
            self._tmp.cleanup()
        finally:
            os.environ.pop("TELEGRAMAIL_DB_PATH", None)

    def test_draft_signature_choice_is_persisted(self):
        from app.database import DBManager
        from app.email_utils.signatures import (
            CHOICE_DEFAULT,
            get_draft_signature_choice,
            set_draft_signature_choice,
        )

        set_draft_signature_choice(draft_id=123, choice="sigA")
        DBManager.reset_instance()
        self.assertEqual(get_draft_signature_choice(draft_id=123), "sigA")

        set_draft_signature_choice(draft_id=123, choice=CHOICE_DEFAULT)
        DBManager.reset_instance()
        self.assertEqual(get_draft_signature_choice(draft_id=123), CHOICE_DEFAULT)

    def test_account_last_choice_is_persisted(self):
        from app.database import DBManager
        from app.email_utils.signatures import (
            CHOICE_DEFAULT,
            get_account_last_signature_choice,
            set_account_last_signature_choice,
        )

        set_account_last_signature_choice(account_id=1, choice="sigB")
        DBManager.reset_instance()
        self.assertEqual(get_account_last_signature_choice(account_id=1), "sigB")

        set_account_last_signature_choice(account_id=1, choice=CHOICE_DEFAULT)
        DBManager.reset_instance()
        self.assertEqual(
            get_account_last_signature_choice(account_id=1),
            CHOICE_DEFAULT,
        )
