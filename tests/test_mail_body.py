import unittest

from app.email_utils.mail_body import (
    html_to_plain_text,
    is_placeholder_text_body,
    prepare_email_body,
)


class MailBodyTests(unittest.TestCase):
    def test_prepare_email_body_prefers_rendered_html_over_a_placeholder_text_part(self):
        html = """
        <html><body>
          <h1>Receipt</h1>
          <p>Shogun F5228976044</p>
          <p>Total 42.00</p>
          <p>It looks like your email client might not support HTML formatted email.</p>
        </body></html>
        """
        text = (
            "Subject Line: Receipt\r\n\r\n"
            "It looks like your email client might not support HTML formatted email.\r\n"
            "Try opening this email in another email client.\r\n"
        )

        body = prepare_email_body(html, text)

        self.assertIn("Shogun", body)
        self.assertIn("F5228976044", body)
        self.assertNotIn("<p>", body)

    def test_prepare_email_body_keeps_a_real_plain_text_part(self):
        html = "<html><body><p>HTML version of the same message</p></body></html>"
        text = "A real plain text body that is long enough to be the intended content. " * 20

        self.assertEqual(prepare_email_body(html, text), text)

    def test_prepare_email_body_keeps_the_plain_text_part_when_html_is_absent(self):
        text = "Only a plain text body exists for this message."

        self.assertEqual(prepare_email_body(None, text), text)

    def test_prepare_email_body_falls_back_to_rendered_html_without_text(self):
        self.assertEqual(prepare_email_body("<p>Only HTML</p>", ""), "Only HTML")

    def test_prepare_email_body_returns_empty_for_empty_input(self):
        self.assertEqual(prepare_email_body(None, ""), "")
        self.assertEqual(prepare_email_body("   ", None), "")

    def test_placeholder_detection_accepts_a_short_body_with_a_client_marker(self):
        self.assertTrue(
            is_placeholder_text_body(
                "It looks like your email client might not support HTML formatted email."
            )
        )
        self.assertTrue(
            is_placeholder_text_body(
                "Or, open the following link to view this email in a browser:"
            )
        )

    def test_placeholder_detection_ignores_a_long_newsletter_with_a_browser_footer(self):
        text = "Weekly digest with real content. " * 80 + "\nView this email in your browser"

        self.assertFalse(is_placeholder_text_body(text))

    def test_placeholder_detection_ignores_ordinary_bodies(self):
        self.assertFalse(is_placeholder_text_body(""))
        self.assertFalse(is_placeholder_text_body(None))
        self.assertFalse(is_placeholder_text_body("Let's meet tomorrow at 3pm."))

    def test_html_to_plain_text_drops_script_and_style_content(self):
        text = html_to_plain_text(
            "<html><head><style>.x{color:red}</style></head>"
            "<body><script>track()</script><p>Hello Alice</p></body></html>"
        )

        self.assertEqual(text, "Hello Alice")

    def test_html_to_plain_text_keeps_link_targets_after_the_label(self):
        text = html_to_plain_text('<p>See <a href="https://example.test/doc">the doc</a>.</p>')

        self.assertIn("the doc (https://example.test/doc)", text)

    def test_html_to_plain_text_returns_empty_for_empty_input(self):
        self.assertEqual(html_to_plain_text(None), "")
        self.assertEqual(html_to_plain_text("   "), "")


if __name__ == "__main__":
    unittest.main()
