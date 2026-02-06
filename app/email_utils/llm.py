import os
import re
from app.llm import OpenAIClient
from app.utils import Logger
from app.i18n import _
from app.email_utils.labels import LLM_EMAIL_CATEGORIES_SET, normalize_llm_category
from json_repair import repair_json
from app.email_utils.text import remove_spaces_and_urls
from bs4 import BeautifulSoup, NavigableString, Tag
from html import escape as html_escape

logger = Logger().get_logger(__name__)

_TELEGRAM_ALLOWED_INLINE_TAGS = {"b", "i", "code"}


def _locale_to_language_name(locale_code: str) -> str:
    """
    Convert a locale code (e.g. en_US, zh_CN) into a human-readable language name
    for use in LLM prompts.
    """
    code = (locale_code or "").strip()
    lower = code.lower()
    if lower.startswith("zh"):
        if "tw" in lower or "hk" in lower or "hant" in lower:
            return "繁體中文"
        return "简体中文"
    if lower.startswith("en"):
        return "English"
    if lower.startswith("ja"):
        return "日本語"
    if lower.startswith("ko"):
        return "한국어"
    if lower.startswith("fr"):
        return "Français"
    if lower.startswith("de"):
        return "Deutsch"
    if lower.startswith("es"):
        return "Español"
    return code or "English"


def _sanitize_telegram_limited_html(raw_html: str) -> str:
    """
    Sanitize an untrusted HTML fragment for Telegram HTML parse mode.

    Only keeps <b>, <i>, <code> tags (no attributes). Everything else is stripped
    to plain text and HTML-escaped.
    """
    if not raw_html:
        return ""

    # Normalize common line-break tags to newlines before parsing.
    normalized = (
        raw_html.replace("<br />", "\n")
        .replace("<br/>", "\n")
        .replace("<br>", "\n")
    )

    soup = BeautifulSoup(f"<div>{normalized}</div>", "html.parser")
    root = soup.div

    def render(node) -> str:
        if isinstance(node, NavigableString):
            return html_escape(str(node), quote=False)
        if isinstance(node, Tag):
            name = (node.name or "").lower()
            if name == "br":
                return "\n"
            if name in _TELEGRAM_ALLOWED_INLINE_TAGS:
                inner = "".join(render(child) for child in node.contents)
                return f"<{name}>{inner}</{name}>"
            # Strip tag but keep its children
            return "".join(render(child) for child in node.contents)
        return ""

    return "".join(render(child) for child in root.contents).strip()


def _escape_telegram_html_text(text: str) -> str:
    """Escape untrusted text that will be embedded into Telegram HTML messages."""
    if text is None:
        return ""
    return html_escape(str(text), quote=False)


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
        safe_summary = _sanitize_telegram_limited_html(summary_text)
        parts.append(f"\n{safe_summary}")

    # Action required indicator
    if summary_data.get("action_required", False):
        parts.append(f"\n<b>{_('email_action_required')}</b>")

        # Action items
        action_items = summary_data.get("action_items", [])
        if action_items:
            parts.append(f"\n<b>{_('email_action_items')}:</b>")
            for i, item in enumerate(action_items[:5], 1):  # Limit to 5 items
                parts.append(f"  {i}. {_escape_telegram_html_text(item)}")

    # Deadline information
    deadline = summary_data.get("deadline")
    if deadline:
        parts.append(
            f"\n<b>{_('email_deadline')}:</b> {_escape_telegram_html_text(deadline)}"
        )

    # Key contacts
    key_contacts = summary_data.get("key_contacts", [])
    if key_contacts:
        contacts_str = ", ".join(
            _escape_telegram_html_text(c) for c in key_contacts[:3]
        )  # Limit to 3 contacts
        parts.append(f"\n<b>{_('email_key_contacts')}:</b> {contacts_str}")

    return "\n".join(parts)


def summarize_email(email_body: str, extra_urls: list[dict] | None = None) -> dict | None:
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
    language_name = _locale_to_language_name(default_language)
    messages = [
        {
            "role": "system",
            "content": f"""
You analyze one email and return a STRICT JSON object for quick reading in mobile chat.

Return ONLY a single JSON object. Do NOT return markdown, code fences, or any extra text.

Language requirement:
- All human-readable strings MUST be in {language_name} (locale: {default_language}).

Do not hallucinate. If information is missing, use null / [] and keep text concise.

 JSON schema (MUST include ALL keys):
- summary: string (may include ONLY <b>, <i>, <code>; no attributes; use \\n for line breaks; keep <= 800 chars)
- priority: "high" | "medium" | "low"
- action_required: boolean
- action_items: string[] (max 5, each <= 100 chars, plain text only, no HTML)
- deadline: string | null (plain text, no HTML)
- key_contacts: string[] (max 3, names only, plain text)
- category: "task" | "meeting" | "financial" | "travel" | "newsletter" | "system" | "social" | "other"
- category_confidence: number | null (0.0 - 1.0)
- urls: array of {{"caption": string, "link": string}} (max 5; link must be http/https; include unsubscribe link when present; avoid obviously irrelevant tracking-only links)
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

                allowed_priorities = {"high", "medium", "low"}
                priority = str(real_result.get("priority", "medium")).lower().strip()
                if priority not in allowed_priorities:
                    priority = "medium"
                real_result["priority"] = priority

                category = normalize_llm_category(real_result.get("category", "other"))
                if category not in LLM_EMAIL_CATEGORIES_SET:
                    category = "other"
                real_result["category"] = category

                confidence_raw = real_result.get("category_confidence", None)
                confidence: float | None = None
                if confidence_raw is not None:
                    try:
                        confidence = float(confidence_raw)
                        if confidence < 0:
                            confidence = 0.0
                        elif confidence > 1:
                            confidence = 1.0
                    except Exception:
                        confidence = None
                real_result["category_confidence"] = confidence

                summary = real_result.get("summary", "")
                if not isinstance(summary, str):
                    summary = str(summary)
                summary = _sanitize_telegram_limited_html(summary)
                real_result["summary"] = summary[:800]

                def _strip_tags(value: str) -> str:
                    return re.sub(r"<[^>]+>", "", value or "").strip()

                real_result["action_items"] = [
                    _strip_tags(str(item))[:100]
                    for item in real_result.get("action_items", [])
                    if str(item).strip()
                ][:5]

                deadline = real_result.get("deadline", None)
                if deadline is None:
                    real_result["deadline"] = None
                else:
                    deadline_text = str(deadline).strip()
                    if deadline_text.lower() in {"null", "none", ""}:
                        real_result["deadline"] = None
                    else:
                        real_result["deadline"] = _strip_tags(deadline_text)[:120]

                real_result["key_contacts"] = [
                    _strip_tags(str(name))[:50]
                    for name in real_result.get("key_contacts", [])
                    if str(name).strip()
                ][:3]

                def _normalize_url_item(item: dict) -> dict | None:
                    if not isinstance(item, dict):
                        return None
                    caption_raw = str(item.get("caption", "")).strip()
                    link = str(item.get("link", "")).strip()
                    if not link.startswith(("http://", "https://")):
                        return None
                    caption = _strip_tags(caption_raw)[:25]
                    if not caption:
                        caption = link[:25]
                    return {"caption": caption, "link": link}

                max_urls = 5
                cleaned_urls: list[dict] = []
                seen_links = set()
                for url in real_result.get("urls", [])[:10]:
                    normalized = _normalize_url_item(url)
                    if not normalized:
                        continue
                    link = normalized["link"]
                    if link in seen_links:
                        continue
                    seen_links.add(link)
                    cleaned_urls.append(normalized)
                    if len(cleaned_urls) >= max_urls:
                        break

                # Merge extra URLs (e.g., deterministic unsubscribe links), keep unique and capped.
                merged_extra: list[dict] = []
                for url in (extra_urls or [])[:10]:
                    normalized = _normalize_url_item(url)
                    if not normalized:
                        continue
                    link = normalized["link"]
                    if link in seen_links:
                        continue
                    seen_links.add(link)
                    merged_extra.append(normalized)

                if merged_extra:
                    keep_llm = max(0, max_urls - len(merged_extra))
                    real_result["urls"] = (
                        cleaned_urls[:keep_llm] + merged_extra[:max_urls]
                    )
                else:
                    real_result["urls"] = cleaned_urls

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
                        "category_confidence": None,
                        "urls": real_result.get("urls", []),
                    }
                else:
                    raise ValueError("Invalid response format")
        except Exception as e:
            logger.error(f"failed to summarize email content: {e}")
            continue
    return None
