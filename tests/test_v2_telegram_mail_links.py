import unittest

from app.services.telegram_mail_links import extract_email_action_links, sanitize_important_links


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

    def test_promotes_unsubscribe_and_browser_links_then_keeps_visible_ctas(self):
        links = extract_email_action_links(
            """
            <a href='https://store.example.test/deal'>Shop the sale</a>
            <a href='https://mail.example.test/open'>View in browser</a>
            <a href='https://mail.example.test/unsubscribe?token=x'>Unsubscribe</a>
            <a href='javascript:alert(1)'>bad</a>
            <a href='https://tracker.example.test/pixel'><img src='https://tracker.example.test/p.gif'></a>
            """
        )

        self.assertEqual(
            links,
            [
                {"caption": "退订", "link": "https://mail.example.test/unsubscribe?token=x"},
                {"caption": "在浏览器中查看", "link": "https://mail.example.test/open"},
                {"caption": "Shop the sale", "link": "https://store.example.test/deal"},
            ],
        )

    def test_plain_text_urls_are_a_fallback_and_only_http_urls_are_returned(self):
        links = extract_email_action_links(
            None,
            "Read https://example.test/guide and ignore mailto:help@example.test or javascript:alert(1)",
        )

        self.assertEqual(links, [{"caption": "打开链接", "link": "https://example.test/guide"}])

    def test_links_with_credentials_or_hidden_anchors_are_rejected(self):
        links = extract_email_action_links(
            """
            <a href='https://user:secret@example.test/'>private</a>
            <div style='display: none'><a href='https://hidden.example.test/'>open</a></div>
            <a href='https://visible.example.test/'>Visible</a>
            """
        )

        self.assertEqual(links, [{"caption": "Visible", "link": "https://visible.example.test/"}])


if __name__ == "__main__":
    unittest.main()
