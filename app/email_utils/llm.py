import os
from app.llm import OpenAIClient
from app.utils import Logger
from app.i18n import _
from json_repair import repair_json

logger = Logger().get_logger(__name__)


def summarize_email(email_body: str) -> str | None:
    """
    Use OpenAI's large language models to summarize an email.

    Given an email body, send a prompt to the LLM to summarize the email into
    a concise bullet-point summary. The summary should capture the main purpose
    of the email, key information, actions required, any deadlines, and essential
    content for quick understanding. The summary should be in the language
    specified by the DEFAULT_LANGUAGE environment variable.

    If the request fails for any reason, try other models in the list specified
    by the OPENAI_EMAIL_SUMMARIZE_MODELS environment variable. If all models fail,
    return None.

    Parameters
    ----------
    email_body : str
        The text content of the email to summarize.

    Returns
    -------
    str | None
        The summarized text of the email, or None if all LLM requests failed.
    """
    default_language = os.getenv("DEFAULT_LANGUAGE", "en_US")
    messages = [
        {
            "role": "user",
            "content": f"""
You are an AI assistant specialized in summarizing emails and extracting relevant information into a structured JSON format.

Your task is to read the following email content and generate a JSON object containing a summary and a list of relevant URLs based on the specific instructions below.

**ONLY return the final JSON object. Do NOT include any other text before or after the JSON.**

---

**Instructions:**

1.  **Summary (JSON Key: "summary")**
    *   Extract the core message of the email.
    *   Present the key points (Main purpose, Key information, Actions required, Deadlines) as a concise list within the summary content. Use a list marker (e.g., `- ` or `* `) followed by the point's content, with each point on a new line (`\n`).
    *   Specifically capture:
        *   Main purpose of the email.
        *   Key information or highlights.
        *   Any required actions from the recipient (you).
        *   Important deadlines or timeframes.
    *   **Formatting:**
        *   You **MUST ONLY** use the following HTML tags for styling within the summary text: `<b>`, `<i>`, `<u>`, `<s>`, `<a href="...">`, `<code>`, `<pre>`, `<pre><code class="language-...">`, `<blockquote>`, `<blockquote expandable>`.
        *   **DO NOT** use *any* other HTML tags, **especially `<br>`**.
        *   If there is **one** important image strongly related to the summary, represent it using an `<a>` tag with the image URL in the `href` attribute and a concise caption as the link text. Example: `<a href="image_url_here">[Brief Image Description]</a>`. **Do not include more than one image** using this method. Do NOT include image URLs in the "urls" section.
    *   **Length Restriction:** The summary content must be **no more than 1000 characters** (in any language).

2.  **Relevant URLs (JSON Key: "urls")**
    *   Identify and list URLs that are strongly related to the summary content or are email subscription-related.
    *   **Exclusion:** Do NOT include image URLs in this list (handle images as described in Summary Formatting).
    *   **Format:** Each URL should be an object with two keys:
        *   `caption`: A brief name or description for the URL (max 20 characters).
        *   `link`: The actual URL string.
    *   **Restriction:** Include **no more than 5 URLs** in total.
    *   If no relevant URLs are found, the value for the "urls" key should be an empty JSON array `[]`.

3.  **Output Format**
    *   The final output **MUST** be a single JSON object.
    *   The structure should be exactly:
        ```json
        {{
            "summary": "...",
            "urls": [
                {{
                    "caption": "...",
                    "link": "..."
                }},
                ...
            ]
        }}
        ```
    *   Provide the content in `{default_language}`.

---

**Email Content:**
{email_body}
""",
        }
    ]
    if (
        os.getenv("OPENAI_EMAIL_SUMMARIZE_MODELS") is None
        or os.getenv("OPENAI_BASE_URL") is None
        or os.getenv("OPENAI_API_KEY") is None
        or not str(os.getenv("ENABLE_LLM_SUMMARY", "0")) == "1"
    ):
        return None
    models = os.getenv("OPENAI_EMAIL_SUMMARIZE_MODELS").split(",")
    openai_client = OpenAIClient()
    for model in models:
        try:
            completion = openai_client.generate_completion(model, messages, True)
            json_str = openai_client.extract_response_text(completion)
            result = repair_json(
                json_str=json_str, ensure_ascii=False, return_objects=True
            )
            if len(result.keys()) == 1:
                real_result = result[list(result.keys())[0]]
                if "summary" in real_result and "urls" in real_result:
                    return real_result
                else:
                    raise
            elif "summary" in result and "urls" in result:
                return result
            else:
                raise
        except Exception as e:
            logger.error(f"failed to summarize email content: {e}")
            continue
    return None
