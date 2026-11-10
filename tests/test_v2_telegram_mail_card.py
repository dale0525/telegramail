import html
import re
import unittest

from app.services.telegram_mail_card import (
    build_rich_mail_card,
    build_safe_mail_card,
    has_projectable_mail_content,
)
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
        self.assertIn("Please <b>review</b>", card)
        self.assertIn("<i>now</i>", card)
        # A summary link is an allowed inline entity, but its event handler is not.
        self.assertIn('<a href="https://evil.invalid">this</a>', card)
        self.assertNotIn("onclick", card)
        self.assertNotIn("<img src=x", card)
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


class RichMailCardTests(unittest.TestCase):
    """The rich card must stay inert for untrusted header/body content."""

    def _card(self, **overrides):
        arguments = dict(
            subject="Quarterly report",
            sender="boss@example.com",
            summary="Please review this report",
            body_text="",
            received_at="2026-08-14 12:30 UTC",
        )
        arguments.update(overrides)
        return build_rich_mail_card(**arguments)

    def test_card_uses_rich_structure_for_subject_sender_and_summary(self):
        card = self._card(summary="Please <b>review</b> this report")
        self.assertIn("<h3>", card)
        self.assertIn("Quarterly report", card)
        self.assertIn("boss@example.com", card)
        self.assertIn("Please <b>review</b> this report", card)
        self.assertIn("</h3>", card)

    def test_untrusted_fields_cannot_inject_rich_tags(self):
        card = self._card(
            subject="<h1>Injected</h1><tg-button type=\"url\" url=\"https://evil.invalid\">x</tg-button>",
            sender='Mallory <img src=x onerror="bad">',
            body_text="<script>alert(1)</script>",
        )
        self.assertNotIn("<h1>Injected</h1>", card)
        self.assertNotIn("<tg-button type=", card)
        self.assertNotIn("<img src=x", card)
        self.assertNotIn("<script>", card)
        self.assertIn("&lt;h1&gt;Injected&lt;/h1&gt;", card)

    def test_summary_keeps_only_the_whitelisted_inline_tags(self):
        card = self._card(summary="<b>keep</b> <script>drop</script> <table>drop</table> <i>keep</i>")
        self.assertIn("<b>keep</b>", card)
        self.assertIn("<i>keep</i>", card)
        self.assertNotIn("<script>", card)
        self.assertNotIn("<table>", card)

    def test_card_length_stays_inside_the_message_contract(self):
        card = self._card(summary="<b>safe</b> " * 4000)
        self.assertLessEqual(rendered_length(card), 4096)
        self.assertEqual(card.count("<b>"), card.count("</b>"))

    def test_card_escapes_body_text_when_there_is_no_summary(self):
        card = self._card(summary=None, body_text="<b>untrusted</b><script>bad()</script>")
        self.assertIn("&lt;b&gt;untrusted&lt;/b&gt;", card)
        self.assertNotIn("<script>", card)

    def test_empty_content_falls_back_to_a_visible_placeholder(self):
        card = self._card(summary=None, body_text="")
        self.assertTrue(card.strip())
        self.assertIn("无可用内容", card)


class ProjectionMessageKindTests(unittest.TestCase):
    def test_repository_accepts_only_the_supported_message_kinds(self):
        import tempfile

        from app.db.repository import V2Repository

        with tempfile.TemporaryDirectory() as path:
            repository = V2Repository(f"{path}/test.db")
            for kind in ("html", "text", "rich"):
                # A missing row returns None; the kind must still pass validation
                # instead of raising, which is what a rejected kind does.
                self.assertIsNone(
                    repository.complete_projection(
                        999999, lease_token="t", success=True, message_kind=kind
                    )
                )
            with self.assertRaises(ValueError):
                repository.complete_projection(
                    999999, lease_token="t", success=True, message_kind="bogus"
                )


class SummarySanitizerTests(unittest.TestCase):
    def test_summary_links_and_extra_inline_styles_survive(self):
        from app.services.telegram_html import sanitize_telegram_limited_html

        rendered = sanitize_telegram_limited_html(
            '<a href="https://example.test/confirm">Confirm</a> <u>u</u> <s>s</s> '
            '<tg-spoiler>hidden</tg-spoiler> <mark>m</mark>'
        )
        self.assertIn('<a href="https://example.test/confirm">Confirm</a>', rendered)
        self.assertIn("<u>u</u>", rendered)
        self.assertIn("<s>s</s>", rendered)
        self.assertIn("<tg-spoiler>hidden</tg-spoiler>", rendered)
        self.assertIn("<mark>m</mark>", rendered)

    def test_sanitizer_still_drops_dangerous_markup(self):
        from app.services.telegram_html import sanitize_telegram_limited_html

        rendered = sanitize_telegram_limited_html(
            '<a href="javascript:alert(1)" onclick="bad()">x</a><script>bad()</script>'
            '<img src=x onerror="bad">'
        )
        self.assertNotIn("javascript:", rendered)
        self.assertNotIn("onclick", rendered)
        self.assertNotIn("<script>", rendered)
        self.assertNotIn("<img", rendered)


if __name__ == "__main__":
    unittest.main()
