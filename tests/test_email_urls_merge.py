import unittest
from unittest import mock


class TestEmailUrlsMerge(unittest.TestCase):
    def test_summarize_email_accepts_canonical_important_links_and_keeps_compat_alias(self):
        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                return object()

            def extract_response_text(self, completion):
                return (
                    '{"summary":"Hello","priority":"medium","action_required":false,'
                    '"action_items":[],"deadline":null,"key_contacts":[],'
                    '"category":"other","important_links":['
                    '{"caption":"Open invoice","link":"https://example.com/invoice"},'
                    '{"caption":"Tracking pixel","link":"https://example.com/pixel"}]}'
                )

        from app.email_utils.llm import summarize_email

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            result = summarize_email(
                "hello", llm_settings={"enabled": True, "base_url": "http://example.invalid",
                                       "api_key": "sk-test", "model": "gpt-test", "summary_threshold": 0},
            )

        self.assertEqual(result["important_links"], [{"caption": "Open invoice", "link": "https://example.com/invoice"},
                                                      {"caption": "Tracking pixel", "link": "https://example.com/pixel"}])
        self.assertEqual(result["urls"], result["important_links"])


if __name__ == "__main__":
    unittest.main()
