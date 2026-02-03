import unittest


class TestEmailContentProcessing(unittest.TestCase):
    def test_clean_html_content_removes_quotes_and_boilerplate(self):
        from app.email_utils.text import clean_html_content

        html = """
        <html>
          <head>
            <style>.x{color:red}</style>
            <script>console.log("tracking")</script>
          </head>
          <body>
            <div>Hi Alice,<br/>Let's meet tomorrow at 3pm.</div>
            <div>See <a href="https://example.com/path?x=1">the doc</a>.</div>
            <div class="gmail_quote">
              <blockquote>
                On Mon, Bob wrote:<br/>
                old message content
              </blockquote>
            </div>
            <div class="footer">
              Unsubscribe here: <a href="https://example.com/unsubscribe">unsubscribe</a>
            </div>
          </body>
        </html>
        """

        result = clean_html_content(html)

        self.assertIn("Hi Alice", result)
        self.assertIn("Let's meet tomorrow at 3pm.", result)
        self.assertIn("the doc (https://example.com/path?x=1)", result)

        # Quoted/boilerplate content should be removed from the effective content.
        self.assertNotIn("On Mon, Bob wrote", result)
        self.assertNotIn("old message content", result)
        self.assertNotIn("Unsubscribe here", result)

    def test_clean_html_content_strips_inline_reply_markers(self):
        from app.email_utils.text import clean_html_content

        html = """
        <html>
          <body>
            <div>Hello Alice,</div>
            <div>Here is the latest update.</div>
            <div>On Mon, Bob wrote:</div>
            <div>old message content</div>
          </body>
        </html>
        """

        result = clean_html_content(html)

        self.assertIn("Hello Alice", result)
        self.assertIn("Here is the latest update.", result)
        self.assertNotIn("On Mon, Bob wrote", result)
        self.assertNotIn("old message content", result)

    def test_extract_unsubscribe_urls_finds_footer_link(self):
        from app.email_utils.text import extract_unsubscribe_urls

        html = """
        <html>
          <body>
            <div>Newsletter content</div>
            <div class="footer">
              Unsubscribe here:
              <a href="https://example.com/unsubscribe?u=1">unsubscribe</a>
            </div>
          </body>
        </html>
        """

        urls = extract_unsubscribe_urls(html, default_language="en_US")

        self.assertEqual(urls[0]["caption"], "Unsubscribe")
        self.assertEqual(urls[0]["link"], "https://example.com/unsubscribe?u=1")

    def test_format_enhanced_email_summary_sanitizes_untrusted_fields(self):
        from app.email_utils.llm import format_enhanced_email_summary

        summary_data = {
            "summary": 'Hello <a href="https://evil.example">click</a> <b>OK</b> <div>bad</div>',
            "priority": "high",
            "category": "task",
            "action_required": True,
            "action_items": [
                "Do <b>this</b>",
                'Visit <a href="https://evil.example">site</a>',
            ],
            "deadline": "<script>alert(1)</script> tomorrow",
            "key_contacts": ["Alice <b>Boss</b>"],
            "urls": [],
        }

        formatted = format_enhanced_email_summary(summary_data)

        # Only <b>, <i>, <code> should remain in the final HTML output.
        self.assertNotIn("<a", formatted)
        self.assertNotIn("<div", formatted)
        self.assertNotIn("<script", formatted)

        # Untrusted fields should be HTML-escaped to avoid breaking Telegram HTML parsing.
        self.assertIn("Do &lt;b&gt;this&lt;/b&gt;", formatted)
