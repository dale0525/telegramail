import os
from app.llm import OpenAIClient
from app.utils import Logger
from app.i18n import _
from json_repair import repair_json
from app.email_utils.text import remove_spaces_and_urls

logger = Logger().get_logger(__name__)


def format_enhanced_email_summary(summary_data: dict) -> str:
    """
    Format the enhanced email summary data for Telegram display.

    Args:
        summary_data: Dictionary containing structured email analysis

    Returns:
        str: Formatted HTML string for Telegram display
    """
    if not summary_data:
        return ""

    # Build the formatted message
    parts = []

    # Priority indicator with emoji
    priority = summary_data.get("priority", "medium").lower()
    priority_text = _(f"email_priority_{priority}")
    parts.append(f"<b>{priority_text}</b>")

    # Category with emoji
    category = summary_data.get("category", "other")
    category_text = _(f"email_category_{category}")
    parts.append(f"<b>{_('email_category')}:</b> {category_text}")

    # Main summary
    summary_text = summary_data.get("summary", "")
    if summary_text:
        parts.append(f"\n{summary_text}")

    # Action required indicator
    if summary_data.get("action_required", False):
        parts.append(f"\n<b>{_('email_action_required')}</b>")

        # Action items
        action_items = summary_data.get("action_items", [])
        if action_items:
            parts.append(f"\n<b>{_('email_action_items')}:</b>")
            for i, item in enumerate(action_items[:5], 1):  # Limit to 5 items
                parts.append(f"  {i}. {item}")

    # Deadline information
    deadline = summary_data.get("deadline")
    if deadline:
        parts.append(f"\n<b>{_('email_deadline')}:</b> {deadline}")

    # Key contacts
    key_contacts = summary_data.get("key_contacts", [])
    if key_contacts:
        contacts_str = ", ".join(key_contacts[:3])  # Limit to 3 contacts
        parts.append(f"\n<b>{_('email_key_contacts')}:</b> {contacts_str}")

    return "\n".join(parts)


def summarize_email(email_body: str) -> dict | None:
    """
    Use OpenAI's large language models to summarize an email with enhanced structure.

    Given an email body, send a prompt to the LLM to analyze and summarize the email into
    a structured format that includes summary, priority, action items, deadlines, and other
    key information. The summary should capture the main purpose of the email, urgency level,
    required actions, deadlines, and essential content for quick understanding and decision making.
    The summary should be in the language specified by the DEFAULT_LANGUAGE environment variable.

    If the request fails for any reason, try other models in the list specified
    by the OPENAI_EMAIL_SUMMARIZE_MODELS environment variable. If all models fail,
    return None.

    Parameters
    ----------
    email_body : str
        The text content of the email to summarize.

    Returns
    -------
    dict | None
        A dictionary containing structured email analysis with keys:
        - summary: Main content summary with HTML formatting
        - priority: Urgency level (high/medium/low)
        - action_required: Boolean indicating if action is needed
        - action_items: List of specific actions required
        - deadline: Any mentioned deadlines or time constraints
        - key_contacts: Important people mentioned
        - urls: Relevant URLs from the email
        Returns None if all LLM requests failed.
    """
    default_language = os.getenv("DEFAULT_LANGUAGE", "en_US")
    messages = [
        {
            "role": "system",
            "content": f"""
You are an AI assistant specialized in analyzing emails and extracting actionable insights into a **well-structured JSON format** optimized for mobile messaging display.

Your primary instruction is to process the provided email content and return **ONLY a single, valid JSON object** adhering strictly to the specified format. **DO NOT** include any conversational text, explanations, comments, or any other text outside the JSON object itself.

---

**Instructions for Processing Email Content:**

1. **Summary (JSON Key: "summary")**
   * **Goal:** Create a concise, scannable summary that helps users quickly understand the email's purpose and importance.
   * **Content Requirements:**
     - Start with the main purpose/topic in one clear sentence
     - Include specific details (names, dates, amounts, locations)
     - Highlight any urgent or time-sensitive information
     - Keep total length under 800 characters for mobile readability
   * **HTML Formatting (Use strategically for emphasis):**
     - `<b>` for critical information (deadlines, amounts, urgent items)
     - `<i>` for context or secondary information
     - `<code>` for specific codes, IDs, or technical terms
     - Use `\\n` for line breaks between key points
     - **NO** other HTML tags allowed

2. **Priority Level (JSON Key: "priority")**
   * Analyze urgency and importance, return one of: "high", "medium", "low"
   * **High:** Urgent deadlines, critical issues, immediate action required
   * **Medium:** Important but not urgent, scheduled items, FYI with some relevance
   * **Low:** General information, newsletters, non-urgent updates

3. **Action Required (JSON Key: "action_required")**
   * Boolean: true if the recipient needs to take any action, false otherwise

4. **Action Items (JSON Key: "action_items")**
   * Array of specific actions the recipient should take
   * Each item should be a clear, actionable statement (max 100 chars each)
   * Maximum 5 items, prioritize by importance
   * Use empty array [] if no actions required

5. **Deadline Information (JSON Key: "deadline")**
   * Extract any time constraints, deadlines, or scheduled events
   * Format as clear, specific text (e.g., "Reply by Dec 15, 2024" or "Meeting at 2 PM tomorrow")
   * Use null if no deadline mentioned

6. **Key Contacts (JSON Key: "key_contacts")**
   * Array of important people mentioned (names only, max 3)
   * Focus on decision makers, meeting participants, or people requiring follow-up
   * Use empty array [] if no key contacts identified

7. **Category (JSON Key: "category")**
   * Classify email type, return one of: "meeting", "task", "information", "urgent", "financial", "travel", "social", "newsletter", "system", "other"

8. **Relevant URLs (JSON Key: "urls")**
   * Important links for actions or reference (max 3)
   * Format: {{"caption": "Brief description (max 25 chars)", "link": "URL"}}
   * Exclude unsubscribe links and tracking URLs

**Output Format (STRICT):**
```json
{{
    "summary": "...",
    "priority": "high|medium|low",
    "action_required": true|false,
    "action_items": ["action1", "action2", ...],
    "deadline": "deadline text or null",
    "key_contacts": ["name1", "name2", ...],
    "category": "category_name",
    "urls": [{{"caption": "...", "link": "..."}}]
}}
```

* Provide all text content in {default_language}.
* Ensure JSON is valid and properly escaped.
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

            # Handle nested result structure
            if len(result.keys()) == 1:
                real_result = result[list(result.keys())[0]]
            else:
                real_result = result

            # Validate required fields for new structure
            required_fields = [
                "summary",
                "priority",
                "action_required",
                "action_items",
                "deadline",
                "key_contacts",
                "category",
                "urls",
            ]

            if all(field in real_result for field in required_fields):
                # Ensure proper data types
                real_result["action_required"] = bool(
                    real_result.get("action_required", False)
                )
                real_result["action_items"] = (
                    real_result.get("action_items", [])
                    if isinstance(real_result.get("action_items"), list)
                    else []
                )
                real_result["key_contacts"] = (
                    real_result.get("key_contacts", [])
                    if isinstance(real_result.get("key_contacts"), list)
                    else []
                )
                real_result["urls"] = (
                    real_result.get("urls", [])
                    if isinstance(real_result.get("urls"), list)
                    else []
                )
                real_result["priority"] = real_result.get("priority", "medium")
                real_result["category"] = real_result.get("category", "other")
                return real_result
            else:
                # Fallback: check for old format compatibility
                if "summary" in real_result and "urls" in real_result:
                    # Convert old format to new format
                    return {
                        "summary": real_result.get("summary", ""),
                        "priority": "medium",
                        "action_required": False,
                        "action_items": [],
                        "deadline": None,
                        "key_contacts": [],
                        "category": "other",
                        "urls": real_result.get("urls", []),
                    }
                else:
                    raise ValueError("Invalid response format")
        except Exception as e:
            logger.error(f"failed to summarize email content: {e}")
            continue
    return None
