import unittest


class TestEmailContentProcessing(unittest.TestCase):
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


if __name__ == "__main__":
    unittest.main()
