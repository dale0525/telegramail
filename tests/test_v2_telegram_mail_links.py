import unittest

from app.services.telegram_mail_links import sanitize_important_links


class TelegramMailLinkTests(unittest.TestCase):
    def test_sanitize_important_links_accepts_only_structured_llm_values(self):
        links = sanitize_important_links([
            {"caption": "Open", "link": "https://example.test/open"},
            {"caption": "Credentials", "link": "https://user:secret@example.test/private"},
            {"caption": "Mail", "link": "mailto:help@example.test"},
            {"caption": "Duplicate", "link": "https://example.test/open"},
        ])

        self.assertEqual(links, [{"caption": "Open", "link": "https://example.test/open"}])
        self.assertEqual([], sanitize_important_links("Read https://example.test/open"))

    def test_sanitize_important_links_preserves_llm_order_and_caps_buttons(self):
        links = sanitize_important_links([
            {"caption": str(index), "link": f"https://example.test/{index}"}
            for index in range(8)
        ])

        self.assertEqual(5, len(links))
        self.assertEqual([str(index) for index in range(5)], [item["caption"] for item in links])


if __name__ == "__main__":
    unittest.main()
