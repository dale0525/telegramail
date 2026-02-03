import os
import unittest
from unittest import mock


class TestEmailUrlsMerge(unittest.TestCase):
    def test_summarize_email_merges_extra_unsubscribe_urls(self):
        os.environ["ENABLE_LLM_SUMMARY"] = "1"
        os.environ["OPENAI_BASE_URL"] = "http://example.invalid"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_EMAIL_SUMMARIZE_MODELS"] = "gpt-test"
        os.environ["LLM_SUMMARY_THRESHOLD"] = "0"
        os.environ["DEFAULT_LANGUAGE"] = "en_US"

        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                return object()

            def extract_response_text(self, completion):
                return (
                    '{"summary":"Hello","priority":"medium","action_required":false,'
                    '"action_items":[],"deadline":null,"key_contacts":[],'
                    '"category":"other","urls":[]}'
                )

        from app.email_utils.llm import summarize_email

        extra = [{"caption": "Unsubscribe", "link": "https://example.com/unsubscribe"}]

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            result = summarize_email("hello", extra_urls=extra)

        self.assertIsNotNone(result)
        self.assertIn("urls", result)
        self.assertEqual(result["urls"][0]["link"], "https://example.com/unsubscribe")

    def test_summarize_email_keeps_extra_urls_when_llm_already_full(self):
        os.environ["ENABLE_LLM_SUMMARY"] = "1"
        os.environ["OPENAI_BASE_URL"] = "http://example.invalid"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_EMAIL_SUMMARIZE_MODELS"] = "gpt-test"
        os.environ["LLM_SUMMARY_THRESHOLD"] = "0"
        os.environ["DEFAULT_LANGUAGE"] = "en_US"

        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                return object()

            def extract_response_text(self, completion):
                return (
                    '{"summary":"Hello","priority":"medium","action_required":false,'
                    '"action_items":[],"deadline":null,"key_contacts":[],'
                    '"category":"other","urls":['
                    '{"caption":"A","link":"https://example.com/a"},'
                    '{"caption":"B","link":"https://example.com/b"},'
                    '{"caption":"C","link":"https://example.com/c"},'
                    '{"caption":"D","link":"https://example.com/d"},'
                    '{"caption":"E","link":"https://example.com/e"}'
                    ']}'
                )

        from app.email_utils.llm import summarize_email

        extra = [{"caption": "Unsubscribe", "link": "https://example.com/unsubscribe"}]

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            result = summarize_email("hello", extra_urls=extra)

        urls = result.get("urls", [])
        self.assertLessEqual(len(urls), 5)
        links = [u["link"] for u in urls[:5]]
        self.assertIn("https://example.com/unsubscribe", links)
