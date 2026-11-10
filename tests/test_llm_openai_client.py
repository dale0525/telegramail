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

    def test_disable_thinking_only_sends_reasoning_effort_when_enabled(self):
        from app.llm.openai import OpenAIClient

        # OpenAIClient is a process-wide singleton, so reset it explicitly:
        # otherwise the toggle set below would leak into later test methods.
        OpenAIClient.reset_instance()
        self.addCleanup(OpenAIClient.reset_instance)

        # Off by default: an untouched configuration must not start sending an
        # unsupported field to every OpenAI-compatible endpoint.
        client = OpenAIClient({"base_url": "http://example.invalid", "api_key": "sk-test"})
        completions = _FakeChatCompletions()
        client.client = _FakeOpenAIClient(completions)
        client.generate_completion("gpt-test", messages=[{"role": "user", "content": "hi"}])
        self.assertNotIn("reasoning_effort", completions.last_params)

        # Enabling it must reach the wire for both call styles.
        client.configure({"base_url": "http://example.invalid", "api_key": "sk-test",
                          "disable_thinking": True})
        client.generate_completion("gpt-test", messages=[{"role": "user", "content": "hi"}],
                                   output_json=True)
        self.assertEqual(completions.last_params.get("reasoning_effort"), "none")
        self.assertEqual(completions.last_params.get("temperature"), 0)

        streamed = _FakeChatCompletions()
        client.client = _FakeOpenAIClient(streamed)
        try:
            client.stream_completion("gpt-test", [{"role": "user", "content": "hi"}], True)
        except Exception:
            # The fake yields no finish_reason; only the request body matters.
            pass
        self.assertEqual(streamed.last_params.get("reasoning_effort"), "none")

    def test_configure_keeps_client_identity_when_only_thinking_changes(self):
        from app.llm.openai import OpenAIClient

        client = OpenAIClient({"base_url": "http://example.invalid", "api_key": "sk-test"})
        original = client.client
        client.configure({"base_url": "http://example.invalid", "api_key": "sk-test",
                          "disable_thinking": True})
        self.assertIs(client.client, original)
        self.assertTrue(client.disable_thinking)
