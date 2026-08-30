import html
import re
import unittest

from app.services.telegram_mail_card import build_safe_mail_card, has_projectable_mail_content
from app.integrations.mail.telegram import format_topic_name


def rendered_length(fragment: str) -> int:
    without_tags = re.sub(r"<[^>]+>", "", fragment)
    return len(html.unescape(without_tags))


class TelegramMailCardTests(unittest.TestCase):
    def test_card_contains_only_subject_sender_and_summary(self):
        card = "".join(build_safe_mail_card(
            subject="Quarterly report",
            sender="boss@example.com",
            received_at="2026-08-14 12:30 UTC",
            body_text="raw body should not replace a summary",
            summary="Please review this report",
            category="task",
            priority="high",
        ))
        self.assertIn("主题", card)
        self.assertIn("发件人", card)
        self.assertIn("摘要", card)
        self.assertNotIn("时间", card)
        self.assertNotIn("分类", card)
        self.assertNotIn("优先级", card)

    def test_topic_name_uses_sender_and_subject_and_is_limited(self):
        name = format_topic_name("sender@example.com", "A" * 200)
        self.assertEqual(name, "sender@example.com · " + "A" * (128 - len("sender@example.com · ")))
        self.assertLessEqual(len(name), 128)

    def test_all_untrusted_fields_are_escaped_and_metadata_is_clear(self):
        cards = build_safe_mail_card(
            subject="<b>Invoice</b>",
            sender='Mallory <img src=x onerror="bad">',
            received_at="2026-08-14 12:30 UTC",
            body_text="<script>alert('not executable')</script>",
            summary="Please <b>review</b> <a href='https://evil.invalid' onclick='bad'>this</a> <i>now</i>",
            category="<urgent>",
            priority="high & immediate",
        )

        card = "".join(cards)
        self.assertIn("<b>主题：</b> &lt;b&gt;Invoice&lt;/b&gt;", card)
        self.assertIn("<b>发件人：</b> Mallory &lt;img src=x onerror=&quot;bad&quot;&gt;", card)
        self.assertNotIn("时间：", card)
        self.assertNotIn("分类：", card)
        self.assertNotIn("优先级：", card)
        self.assertIn("Please <b>review</b> this <i>now</i>", card)
        self.assertNotIn("evil.invalid", card)
        self.assertNotIn("onclick", card)
        self.assertNotIn("<img src=x", card)
        self.assertNotIn("<script>", card)

    def test_long_plain_text_summary_is_balanced_and_limited_per_fragment(self):
        cards = build_safe_mail_card(
            subject="Subject",
            sender="sender@example.test",
            received_at="now",
            body_text="",
            summary=("<b>safe &amp; useful</b> " * 800),
        )

        self.assertEqual(len(cards), 1)
        self.assertTrue(all(rendered_length(card) <= 4096 for card in cards))
        self.assertTrue(all(card.count("<b>") == card.count("</b>") for card in cards))

    def test_plain_body_html_remains_inert_without_a_summary(self):
        card = "".join(build_safe_mail_card(
            subject="Subject", sender="sender", received_at="now",
            body_text="<b>untrusted</b><script>bad()</script>", summary=None,
        ))
        self.assertIn("&lt;b&gt;untrusted&lt;/b&gt;", card)
        self.assertNotIn("<script>", card)

    def test_only_text_inputs_are_supported(self):
        with self.assertRaises(TypeError):
            build_safe_mail_card(
                subject="subject",
                sender="sender",
                received_at="now",
                body_text="text",
                html_body="<b>must not be accepted</b>",
            )

    def test_html_only_mail_can_be_projected_without_passing_html_to_the_card(self):
        self.assertTrue(has_projectable_mail_content(
            body_text="", summary=None, html_body="<p>HTML-only message</p>"
        ))


if __name__ == "__main__":
    unittest.main()
