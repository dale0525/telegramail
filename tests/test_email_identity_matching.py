import unittest
from email.message import EmailMessage


class TestEmailIdentityMatching(unittest.TestCase):
    def test_extract_delivered_to_candidates_priority_and_dedup(self):
        from app.email_utils.identity import extract_delivered_to_candidates

        msg = EmailMessage()
        msg["Delivered-To"] = "B+Tag@Example.com"
        msg["X-Original-To"] = "other@example.com"
        msg["Delivered-To"] = "b+tag@example.com"  # duplicate, different case
        msg["Envelope-To"] = "b@example.com"

        candidates = extract_delivered_to_candidates(msg)

        # Priority: Delivered-To values first, then X-Original-To, then Envelope-To.
        self.assertEqual(candidates[0], "b+tag@example.com")
        self.assertIn("other@example.com", candidates)
        self.assertIn("b@example.com", candidates)
        # Dedup should remove duplicates while preserving first occurrence.
        self.assertEqual(candidates.count("b+tag@example.com"), 1)

    def test_choose_recommended_from_prefers_base_for_plus_address(self):
        from app.email_utils.identity import choose_recommended_from

        candidates = ["b+tag@example.com"]
        identities = {"b@example.com"}

        recommended = choose_recommended_from(
            candidates=candidates,
            identity_emails=identities,
            default_email="a@example.com",
        )

        self.assertEqual(recommended, "b@example.com")

    def test_choose_recommended_from_uses_raw_if_raw_identity_exists(self):
        from app.email_utils.identity import choose_recommended_from

        candidates = ["b+tag@example.com"]
        identities = {"b+tag@example.com"}

        recommended = choose_recommended_from(
            candidates=candidates,
            identity_emails=identities,
            default_email="a@example.com",
        )

        self.assertEqual(recommended, "b+tag@example.com")

    def test_choose_recommended_from_falls_back_to_default(self):
        from app.email_utils.identity import choose_recommended_from

        candidates = ["c@example.com"]
        identities = {"b@example.com"}

        recommended = choose_recommended_from(
            candidates=candidates,
            identity_emails=identities,
            default_email="a@example.com",
        )

        self.assertEqual(recommended, "a@example.com")

    def test_suggest_identity_returns_base_for_plus_address(self):
        from app.email_utils.identity import suggest_identity

        candidates = ["b+tag@example.com"]
        identities = {"a@example.com"}

        suggestion = suggest_identity(candidates=candidates, identity_emails=identities)

        self.assertEqual(suggestion, "b@example.com")

    def test_suggest_identity_none_when_already_exists(self):
        from app.email_utils.identity import suggest_identity

        candidates = ["b+tag@example.com"]
        identities = {"b@example.com"}

        suggestion = suggest_identity(candidates=candidates, identity_emails=identities)

        self.assertIsNone(suggestion)

