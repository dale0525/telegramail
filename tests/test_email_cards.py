import unittest

from app.i18n import _
from app.telegram_ui.email_cards import build_incoming_email_card


class TestEmailCards(unittest.TestCase):
    def test_incoming_card_displays_mailbox_when_provided(self):
        card = build_incoming_email_card(
            subject="Hello",
            sender="Alice <alice@example.com>",
            recipient="bob@example.com",
            mailbox="Archive",
            body_text="Hi there",
        )

        self.assertIn(f"📁 {_('email_mailbox')}: Archive", card)

