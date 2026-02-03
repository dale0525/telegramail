import os
import unittest
from types import SimpleNamespace


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
        os.environ.setdefault("OPENAI_BASE_URL", "http://example.invalid")
        os.environ.setdefault("OPENAI_API_KEY", "sk-test")

        from app.llm.openai import OpenAIClient

        client = OpenAIClient()
        fake_completions = _FakeChatCompletions()
        client.client = _FakeOpenAIClient(fake_completions)

        client.generate_completion(
            "gpt-test", messages=[{"role": "user", "content": "hi"}], output_json=True
        )

        self.assertIsNotNone(fake_completions.last_params)
        self.assertEqual(fake_completions.last_params.get("temperature"), 0)

