import unittest
from unittest import mock


class TestEmailLlmPrompt(unittest.TestCase):
    def test_prompt_mentions_language_name_for_locale(self):
        captured = {}

        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                captured["messages"] = messages
                raise RuntimeError("stop")

        from app.email_utils.llm import summarize_email

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            summarize_email(
                "hello",
                llm_settings={"enabled": True, "base_url": "http://example.invalid",
                               "api_key": "sk-test", "model": "gpt-test", "summary_threshold": 0,
                               "default_language": "en_US"},
            )

        system_content = captured["messages"][0]["content"]
        self.assertIn("English", system_content)
        self.assertIn("en_US", system_content)
        self.assertIn("important", system_content.casefold())
        self.assertIn("tracking", system_content.casefold())

    def test_prompt_uses_persisted_chinese_language(self):
        captured = {}

        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                captured["messages"] = messages
                raise RuntimeError("stop")

        from app.email_utils.llm import summarize_email

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            summarize_email(
                "测试邮件",
                llm_settings={"enabled": True, "base_url": "http://example.invalid",
                              "api_key": "sk-test", "model": "gpt-test", "summary_threshold": 0,
                              "default_language": "zh_CN"},
            )

        system_content = captured["messages"][0]["content"]
        self.assertIn("简体中文", system_content)
        self.assertIn("zh_CN", system_content)

    def test_prompt_requires_llm_selected_important_links(self):
        captured = {}

        class _FakeOpenAIClient:
            def generate_completion(self, model, messages, output_json=False):
                captured["messages"] = messages
                raise RuntimeError("stop")

        from app.email_utils.llm import summarize_email

        with mock.patch("app.email_utils.llm.OpenAIClient", return_value=_FakeOpenAIClient()):
            summarize_email(
                "A message with a useful call to action",
                llm_settings={"enabled": True, "base_url": "http://example.invalid",
                               "api_key": "sk-test", "model": "gpt-test", "summary_threshold": 0},
            )

        system_content = captured["messages"][0]["content"].casefold()
        self.assertIn("important_links", system_content)
        self.assertIn("tracking", system_content)
        self.assertIn("logo", system_content)
