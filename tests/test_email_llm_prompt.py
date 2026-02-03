import os
import unittest
from unittest import mock


class TestEmailLlmPrompt(unittest.TestCase):
    def test_prompt_mentions_language_name_for_locale(self):
        os.environ["ENABLE_LLM_SUMMARY"] = "1"
        os.environ["OPENAI_BASE_URL"] = "http://example.invalid"
        os.environ["OPENAI_API_KEY"] = "sk-test"
        os.environ["OPENAI_EMAIL_SUMMARIZE_MODELS"] = "gpt-test"
        os.environ["LLM_SUMMARY_THRESHOLD"] = "0"
        os.environ["DEFAULT_LANGUAGE"] = "en_US"

        captured = {}

        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                captured["messages"] = messages
                raise RuntimeError("stop")

        from app.email_utils.llm import summarize_email

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            summarize_email("hello")

        system_content = captured["messages"][0]["content"]
        self.assertIn("English", system_content)
        self.assertIn("en_US", system_content)

