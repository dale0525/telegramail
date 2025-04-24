import os
import time
from openai import OpenAI
from app.utils import Logger
from app.i18n import _
from app.utils.decorators import Singleton, retry_on_fail

logger = Logger().get_logger(__name__)


@Singleton
class OpenAIClient:
    def __init__(
        self,
    ):
        """
        Initialize OpenAI client.

        This method will create an OpenAI client based on base_url and api_key from environment variables.
        """
        self.base_url = os.getenv("OPENAI_BASE_URL")
        self.api_key = os.getenv("OPENAI_API_KEY")
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    @retry_on_fail(
        max_retries=2,
        retry_delay=1,
    )
    def generate_completion(
        self, model: str, messages: list[dict], output_json: bool = False
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
        try:
            logger.info(f"using {model} for llm...")

            params = {
                "model": model,
                "messages": messages,
            }

            if output_json:
                params["response_format"] = {"type": "json_object"}

            start_time = time.time()
            completion = self.client.chat.completions.create(**params)

            elapsed = time.time() - start_time
            logger.debug(f"llm call completed after {elapsed:.2f}s")
            logger.debug(f"llm response: {completion}")

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
