import os
import unittest
from types import SimpleNamespace
from unittest import mock


class _FakeChatCompletions:
    def __init__(self):
        self.last_params = None

    def create(self, **params):
        self.last_params = params
        return SimpleNamespace(
            choices=[SimpleNamespace(message=SimpleNamespace(content="{}"))]
        )


class _FakeOpenAIClient:
    def __init__(self, chat_completions: _FakeChatCompletions):
        self.chat = SimpleNamespace(completions=chat_completions)


class TestOpenAIClient(unittest.TestCase):
    def test_generate_completion_sets_temperature_for_json(self):
        from app.llm.openai import OpenAIClient

        client = OpenAIClient({"base_url": "http://example.invalid", "api_key": "sk-test"})
        fake_completions = _FakeChatCompletions()
        client.client = _FakeOpenAIClient(fake_completions)

        client.generate_completion(
            "gpt-test", messages=[{"role": "user", "content": "hi"}], output_json=True
        )

        self.assertIsNotNone(fake_completions.last_params)
        self.assertEqual(fake_completions.last_params.get("temperature"), 0)

    def test_generate_completion_does_not_debug_log_completion_contents(self):
        from app.llm import openai
        from app.llm.openai import OpenAIClient

        client = OpenAIClient({"base_url": "http://example.invalid", "api_key": "sk-test"})
        client.client = _FakeOpenAIClient(_FakeChatCompletions())
        with mock.patch.object(openai.logger, "debug") as debug:
            client.generate_completion(
                "gpt-test", messages=[{"role": "user", "content": "secret completion material"}]
            )

        self.assertEqual(debug.call_count, 1)
        self.assertNotIn("secret completion material", str(debug.call_args_list))
