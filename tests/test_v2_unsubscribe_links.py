import unittest

from app.services.telegram_mail_links import extract_unsubscribe_links, merge_mail_action_links


class UnsubscribeLinkTests(unittest.TestCase):
    def test_html_anchor_label_is_matched(self):
        links = extract_unsubscribe_links(
            '<p>Newsletter</p><a href="https://example.test/u?t=1">Unsubscribe</a>'
        )

        self.assertEqual(links, [{"caption": "退订", "link": "https://example.test/u?t=1"}])

    def test_html_href_is_matched_when_the_label_is_not_descriptive(self):
        links = extract_unsubscribe_links(
            '<a href="https://example.test/optout?x=1">click here</a>'
        )

        self.assertEqual(links, [{"caption": "退订", "link": "https://example.test/optout?x=1"}])

    def test_hidden_and_credential_anchors_are_ignored(self):
        html = (
            "<div style='display: none'>"
            "<a href='https://hidden.test/unsubscribe'>Unsubscribe</a></div>"
            "<a href='https://user:pw@example.test/unsubscribe'>Unsubscribe</a>"
            "<a href='mailto:help@example.test'>Unsubscribe</a>"
            "<a href='https://visible.test/unsubscribe'>Unsubscribe</a>"
        )

        self.assertEqual(
            extract_unsubscribe_links(html),
            [{"caption": "退订", "link": "https://visible.test/unsubscribe"}],
        )

    def test_anchor_hidden_by_its_own_style_is_ignored(self):
        # A link the reader cannot see must not become a visible button, even
        # when the hiding style sits on the anchor itself rather than a parent.
        html = (
            "<a href='https://selfhidden.test/unsubscribe' style='display: none'>Unsubscribe</a>"
            "<a href='https://selfhidden.test/u2' style='visibility:hidden'>Unsubscribe</a>"
            "<a href='https://visible.test/unsubscribe'>Unsubscribe</a>"
        )

        self.assertEqual(
            extract_unsubscribe_links(html),
            [{"caption": "退订", "link": "https://visible.test/unsubscribe"}],
        )

    def test_an_anchor_with_an_unencodable_url_is_ignored(self):
        # Telegram rejects a button URL that still contains a raw space, and a
        # rejected keyboard fails the whole delivery.
        html = (
            "<a href='https://broken.test/unsub?p=1 2'>Unsubscribe</a>"
            "<a href='https://visible.test/unsubscribe'>Unsubscribe</a>"
        )

        self.assertEqual(
            extract_unsubscribe_links(html),
            [{"caption": "退订", "link": "https://visible.test/unsubscribe"}],
        )

    def test_a_wrapped_or_bulleted_next_line_url_is_recovered(self):
        # Angle brackets and list markers are ordinary plain-text ways to write
        # a URL, and the term line usually precedes the URL.
        for text in (
            "Unsubscribe:\n<https://example.test/u?e=1>",
            "Unsubscribe:\n- https://example.test/u?e=1",
            "Unsubscribe:\n* https://example.test/u?e=1",
        ):
            self.assertEqual(
                extract_unsubscribe_links(None, text),
                [{"caption": "退订", "link": "https://example.test/u?e=1"}],
            )

    def test_a_returned_url_never_contains_whitespace(self):
        # Telegram rejects a button URL containing a raw space, so no returned
        # link may carry one however the body was written.
        for text in (
            "Unsubscribe here: https://broken.test/unsub?p=1 2",
            "Unsubscribe\nhttps://broken.test/unsub?p=1 2",
        ):
            for link in extract_unsubscribe_links(None, text):
                self.assertNotIn(" ", link["link"])
                self.assertTrue(link["link"].startswith("https://"))

    def test_plain_text_term_line_supplies_the_following_url_line(self):
        text = (
            "Your receipt is attached.\r\n"
            "\r\n"
            "We respect your privacy, if you no longer wish to receive email, "
            "open the following link in a browser to unsubscribe:\r\n"
            "https://preferences.test/unsub?e=1\r\n"
        )

        self.assertEqual(
            extract_unsubscribe_links(None, text),
            [{"caption": "退订", "link": "https://preferences.test/unsub?e=1"}],
        )

    def test_plain_text_line_with_an_inline_url_is_used(self):
        text = "Unsubscribe here: https://example.test/unsub?e=2 thanks"

        self.assertEqual(
            extract_unsubscribe_links(None, text),
            [{"caption": "退订", "link": "https://example.test/unsub?e=2"}],
        )

    def test_html_pass_wins_over_the_plain_text_pass(self):
        html = '<a href="https://html.test/unsubscribe">Unsubscribe</a>'
        text = "Unsubscribe here: https://text.test/unsubscribe"

        self.assertEqual(
            extract_unsubscribe_links(html, text),
            [{"caption": "退订", "link": "https://html.test/unsubscribe"}],
        )

    def test_no_matching_term_returns_nothing(self):
        self.assertEqual(
            extract_unsubscribe_links(
                '<a href="https://example.test/open">Open in browser</a>',
                "Read https://example.test/guide",
            ),
            [],
        )

    def test_the_result_is_bounded_by_max_links(self):
        html = "".join(
            f"<a href='https://example.test/u{index}'>Unsubscribe</a>" for index in range(3)
        )

        self.assertEqual(1, len(extract_unsubscribe_links(html)))
        self.assertEqual(2, len(extract_unsubscribe_links(html, max_links=2)))


class MergeMailActionLinksTests(unittest.TestCase):
    def test_unsubscribe_link_is_first_and_duplicates_are_removed(self):
        merged = merge_mail_action_links(
            [{"caption": "退订", "link": "https://example.test/u"}],
            [
                {"caption": "Open", "link": "https://example.test/u"},
                {"caption": "Task", "link": "https://example.test/t"},
            ],
        )

        self.assertEqual(
            merged,
            [
                {"caption": "退订", "link": "https://example.test/u"},
                {"caption": "Task", "link": "https://example.test/t"},
            ],
        )

    def test_the_llm_order_is_preserved_after_the_unsubscribe_link(self):
        merged = merge_mail_action_links(
            [{"caption": "退订", "link": "https://example.test/u"}],
            [
                {"caption": "First", "link": "https://example.test/1"},
                {"caption": "Second", "link": "https://example.test/2"},
            ],
        )

        self.assertEqual(
            [item["link"] for item in merged],
            ["https://example.test/u", "https://example.test/1", "https://example.test/2"],
        )

    def test_the_button_cap_is_enforced(self):
        merged = merge_mail_action_links(
            [{"caption": "退订", "link": "https://example.test/u"}],
            [{"caption": str(index), "link": f"https://example.test/{index}"} for index in range(8)],
        )

        self.assertEqual(5, len(merged))
        self.assertEqual("https://example.test/u", merged[0]["link"])

    def test_untrusted_values_are_rejected(self):
        self.assertEqual(
            merge_mail_action_links(
                [{"caption": "退订", "link": "mailto:help@example.test"}],
                [
                    {"caption": "Script", "link": "javascript:alert(1)"},
                    {"caption": "Credentials", "link": "https://user:pw@example.test/x"},
                ],
            ),
            [],
        )

    def test_empty_inputs_are_accepted(self):
        self.assertEqual(merge_mail_action_links(None, None), [])
        self.assertEqual(
            merge_mail_action_links(None, [{"caption": "Task", "link": "https://example.test/t"}]),
            [{"caption": "Task", "link": "https://example.test/t"}],
        )


if __name__ == "__main__":
    unittest.main()
