import os
from app.llm import OpenAIClient
from app.utils import Logger
from app.i18n import _
from json_repair import repair_json
from app.email_utils.text import remove_spaces_and_urls

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
            "role": "system",
            "content": f"""
You are an AI assistant specialized in summarizing emails and extracting relevant information into a **well-structured and visually clear JSON format**.

Your primary instruction is to process the provided email content and return **ONLY a single, valid JSON object** adhering strictly to the specified format and constraints. **DO NOT** include any conversational text, explanations, comments, apologies, or any other text outside the JSON object itself.

---

**Instructions for Processing Email Content:**

1.  **Summary (JSON Key: "summary")**
    *   **Goal:** Extract the core message and key points from the email into a concise, readable summary.Do not ignore SPECIFIC details (exact addresses, names, dates, amounts, etc.)
    *   **Content:**
        *   Capture the main purpose of the email.
        *   Highlight key information or updates.
        *   List any required actions from the recipient.
        *   Note important deadlines or timeframes.
    *   **Formatting for Clarity and Aesthetics (STRICT RULES):**
        *   **HTML Tag Usage:** You **MUST ACTIVELY AND ONLY use** the following HTML tags for styling and structure within the summary string. Use them judiciously to enhance readability:
            *   `<b>` for bold text (e.g., key terms, deadlines).
            *   `<i>` for italic text (e.g., emphasis, titles).
            *   `<u>` for underlined text.
            *   `<s>` for strikethrough text.
            *   `<a href="...">` for hyperlinks. The link text should be descriptive.
            *   `<code>` for inline code snippets.
            *   `<pre>` for preformatted text blocks (maintains whitespace).
            *   `<pre><code class="language-...">` for syntax-highlighted code blocks (specify language if known, e.g., `language-python`).
            *   `<blockquote>` for quoting sections from the email or highlighting blocks of text.
            *   `<blockquote expandable>` for potentially long quotes that can be collapsed/expanded (use if appropriate for structure).
        *   **Prohibited Formatting:**
            *   **NO Markdown formatting** (e.g., `*`, `-`, `#`, `**`, `>`).
            *   **NO other HTML tags** are permitted, **especially `<br>`, `<p>`, `<div>`, `<span>`, `<ul>`, `<ol>`, `<li>`**.
        *   **Lists:** To create bulleted lists within the summary:
            *   Use a plain text marker like `- ` or `* ` at the beginning of each list item.
            *   Separate each list item using the newline character `\\n`. **Do not use HTML list tags (`<ul>`, `<ol>`, `<li>`) or `<br>` tags.**
        *   **Images:** If **one** important image is directly relevant to the summary, represent it using an `<a>` tag: `<a href="image_url_here">Image: [Brief, clear description]</a>`. Include **ONLY ONE MOST IMPORTANT** such image link. Do **not** list image URLs in the "urls" section.
    *   **Length Restriction:** The entire "summary" string content must be **no more than 1000 characters** (in any language).

2.  **Relevant URLs (JSON Key: "urls")**
    *   **Goal:** Identify and list URLs directly relevant to the summary's content, related to email actions and email subscriptions. Only `http` or `https` URLs are allowed.
    *   **Exclusion:** Do **not** include image URLs here (handle as described in Summary Formatting).
    *   **Format:** Each URL must be an object within the array:
        ```json
        {{
            "caption": "Brief name/description (max 20 chars)",
            "link": "Actual URL string"
        }}
        ```
    *   **Restriction:** Include **no more than 5 URL objects** in the array.
    *   **Empty Case:** If no relevant URLs are found, use an empty JSON array: `[]`.

3.  **Output Format (ABSOLUTELY STRICT)**
    *   The *entire* output **MUST** be a single, valid JSON object.
    *   The JSON structure **MUST** be exactly:
        ```json
        {{
            "summary": "...", // String containing the formatted summary with allowed HTML and \\n for newlines
            "urls": [          // Array of URL objects (max 5) or empty array []
                {{
                    "caption": "...", // String (max 20 chars)
                    "link": "..."     // String (URL)
                }}
                // ... potentially more URL objects up to the limit
            ]
        }}
        ```
    *   Ensure all strings within the JSON are properly escaped (e.g., use `\\n` for newlines, `\\"` for quotes inside strings).
    *   Provide the content for "summary" and "caption" fields in {default_language}.
""",
        },
        {
            "role": "user",
            "content": f"""
**Process the following email content and provide ONLY the JSON output.**

{email_body}
""",
        },
    ]
    if (
        os.getenv("OPENAI_EMAIL_SUMMARIZE_MODELS") is None
        or os.getenv("OPENAI_BASE_URL") is None
        or os.getenv("OPENAI_API_KEY") is None
        or not str(os.getenv("ENABLE_LLM_SUMMARY", "0")) == "1"
        or len(remove_spaces_and_urls(email_body))
        < int(os.getenv("LLM_SUMMARY_THRESHOLD", "100"))
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
