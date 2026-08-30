import time
import json
from typing import Any, Mapping
from openai import OpenAI
from app.utils import Logger
from app.i18n import _
from app.utils.decorators import Singleton, retry_on_fail

logger = Logger().get_logger(__name__)


class IncompleteStreamError(RuntimeError):
    """Raised when an LLM stream ends before ``finish_reason`` and ``[DONE]``."""


PartialStreamError = IncompleteStreamError


@Singleton
class OpenAIClient:
    def __init__(
        self,
        settings: Mapping[str, Any] | None = None,
    ):
        """
        Initialize OpenAI client.

        Create an OpenAI-compatible client from explicitly supplied settings.
        """
        settings = settings or {}
        self._settings_signature: tuple[Any, Any] | None = None
        self._configure(settings)

    def _configure(self, settings: Mapping[str, Any] | Any | None = None) -> None:
        settings = settings or {}
        get = settings.get if isinstance(settings, Mapping) else lambda key: getattr(settings, key, None)
        base_url = get("base_url") or get("openai_base_url") or get("llm_base_url")
        api_key = get("api_key") or get("openai_api_key") or get("llm_api_key")
        signature = (base_url, api_key)
        if self._settings_signature != signature or not hasattr(self, "client"):
            self.base_url, self.api_key = base_url, api_key
            # Keep construction side-effect free when settings are absent.  The
            # summary service treats that state as a no-op; an actual request
            # still fails clearly instead of consulting process environment.
            self.client = OpenAI(base_url=self.base_url, api_key=self.api_key) if base_url and api_key else None
            self._settings_signature = signature

    def configure(self, settings: Mapping[str, Any] | Any | None = None) -> "OpenAIClient":
        """Apply a repository-provided endpoint/key to this singleton."""
        self._configure(settings)
        return self

    @staticmethod
    def _content_and_finish(choice: Any) -> tuple[str, bool]:
        """Read only ``choices[].delta.content`` from an SDK chunk."""
        if isinstance(choice, Mapping):
            delta = choice.get("delta") or {}
            content = delta.get("content") if isinstance(delta, Mapping) else getattr(delta, "content", None)
            finish = choice.get("finish_reason")
        else:
            delta = getattr(choice, "delta", None)
            content = (delta.get("content") if isinstance(delta, Mapping) else getattr(delta, "content", None)) if delta is not None else None
            finish = getattr(choice, "finish_reason", None)
        return (content if isinstance(content, str) else "", finish is not None)

    @staticmethod
    def _sse_data(value: Any) -> tuple[Any | None, bool]:
        """Decode one SSE ``data:`` value.

        Returns ``(payload, done_marker)``.  Non-SSE SDK chunks are returned as
        payloads unchanged, while malformed JSON is rejected by the caller.
        """
        if isinstance(value, bytes):
            value = value.decode("utf-8", "replace")
        if not isinstance(value, str):
            return value, False
        line = value.strip()
        if line.startswith("data:"):
            line = line[5:].strip()
            if line == "[DONE]":
                return None, True
            if not line:
                return None, False
            return json.loads(line), False
        return value, False

    def stream_completion(
        self,
        model: str,
        messages: list[dict],
        output_json: bool = False,
        *,
        settings: Mapping[str, Any] | None = None,
    ) -> str:
        """Collect an OpenAI-compatible chat completion SSE stream.

        Only ``choices[].delta.content`` is appended.  A stream is successful
        only after a choice carries ``finish_reason`` *and* an SSE ``[DONE]``
        marker.  SDK iterators do not expose the wire marker, so exhaustion is
        treated as the equivalent marker only for non-text SDK chunks.
        """
        params: dict[str, Any] = {"model": model, "messages": messages, "stream": True}
        if output_json:
            params["response_format"] = {"type": "json_object"}
            params["temperature"] = 0
        if settings is not None:
            self.configure(settings)
        if self.client is None:
            raise ValueError("LLM settings require base_url and api_key")
        logger.info("using %s for streaming llm", model)
        started = time.time()
        stream = self.client.chat.completions.create(**params)
        content: list[str] = []
        finished = False
        done_marker = False
        saw_text_sse = False
        for raw in stream:
            # A raw HTTP transport may yield one buffer containing several SSE
            # lines, while the OpenAI SDK yields one object per event.
            if isinstance(raw, bytes):
                raw_parts = raw.decode("utf-8", "replace").splitlines() or [""]
            elif isinstance(raw, str):
                raw_parts = raw.splitlines() or [raw]
            else:
                raw_parts = [raw]
            for part in raw_parts:
                payload, done = self._sse_data(part)
                if done:
                    done_marker = True
                    continue
                if isinstance(part, str) and part.lstrip().startswith("data:"):
                    saw_text_sse = True
                if payload is None:
                    continue
                if isinstance(payload, Mapping):
                    choices = payload.get("choices") or []
                else:
                    choices = getattr(payload, "choices", None) or []
                for choice in choices:
                    piece, has_finish = self._content_and_finish(choice)
                    if piece:
                        content.append(piece)
                    finished = finished or has_finish
        # The OpenAI Python SDK consumes [DONE] internally.  Raw SSE callers
        # must provide it explicitly; SDK object streams get an implicit marker.
        if not saw_text_sse:
            done_marker = True
        elapsed = time.time() - started
        logger.debug("streaming llm call completed after %.2fs", elapsed)
        if not finished or not done_marker:
            raise IncompleteStreamError("LLM stream ended before finish_reason/[DONE]")
        return "".join(content)

    # Names used by integrations predating the worker API.
    generate_completion_stream = stream_completion
    stream_chat_completion = stream_completion
    generate_stream = stream_completion
    chat_completion_stream = stream_completion

    @retry_on_fail(
        max_retries=2,
        retry_delay=1,
    )
    def generate_completion(
        self, model: str, messages: list[dict], output_json: bool = False, *, stream: bool = False
    ):
        """
        Generate an OpenAI completion for the given model and messages.

        This method will make a call to the OpenAI API to generate a completion
        for the given model and messages. The output will be returned as is from
        the API.

        Args:
            model (str): The model to use for the completion.
            messages (list[dict]): A list of messages to use as input for the completion.
            output_json (bool): If True, the response will be returned as a JSON object.
                Otherwise, the response will be returned as a string.

        Returns:
            The generated completion from the OpenAI API.

        Raises:
            Exception: If the call to the OpenAI API fails.
        """
        if stream:
            return self.stream_completion(model, messages, output_json)
        try:
            if self.client is None:
                raise ValueError("LLM settings require base_url and api_key")
            logger.info(f"using {model} for llm...")

            params = {
                "model": model,
                "messages": messages,
            }

            if output_json:
                params["response_format"] = {"type": "json_object"}
                # Lower temperature improves JSON stability and reduces hallucinated structure.
                params["temperature"] = 0

            start_time = time.time()
            completion = self.client.chat.completions.create(**params)

            elapsed = time.time() - start_time
            logger.debug(f"llm call completed after {elapsed:.2f}s")

            return completion
        except Exception as e:
            logger.error(f"failed to generate llm completion: {e}")
            raise

    def extract_response_text(self, completion):
        """
        Extracts the response text from the given completion object.

        Args:
            completion: The completion object returned by the OpenAI API.

        Returns:
            The response text as a string, or None if the completion is invalid.
        """
        if completion and completion.choices and len(completion.choices) > 0:
            return completion.choices[0].message.content
        return None
