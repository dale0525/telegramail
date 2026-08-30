import json
import re
from urllib.parse import urlsplit
from collections.abc import Mapping
from typing import Any
from app.llm import OpenAIClient
from app.utils import Logger
from app.i18n import _
from app.email_utils.labels import LLM_EMAIL_CATEGORIES_SET, normalize_llm_category
from json_repair import repair_json
from app.email_utils.text import remove_spaces_and_urls
from html import escape as html_escape
from app.services.telegram_html import sanitize_telegram_limited_html

logger = Logger().get_logger(__name__)


_MAX_IMPORTANT_LINKS = 5
_MAX_IMPORTANT_LINK_CANDIDATES = 10
_MAX_IMPORTANT_LINK_LENGTH = 4096


def _strip_link_markup(value: Any, *, max_length: int | None = None) -> str:
    """Turn untrusted markup into single-line plain text with an optional cap."""

    text = re.sub(r"<[^>]*>", "", str(value or ""))
    text = re.sub(r"[\x00-\x1f\x7f]+", " ", text)
    text = " ".join(text.split())
    return text[:max_length] if max_length is not None else text


def _normalize_url_item(item: Any) -> dict[str, str] | None:
    """Validate one LLM link without allowing non-browser or credential URLs."""

    if isinstance(item, str):
        raw_link = item
        raw_caption = item
    elif isinstance(item, Mapping):
        raw_link = item.get("link", item.get("url", ""))
        raw_caption = item.get("caption", item.get("title", item.get("text", "")))
    else:
        return None
    link = str(raw_link or "").strip()
    if not link or len(link) > _MAX_IMPORTANT_LINK_LENGTH:
        return None
    if any(ord(char) < 0x20 or ord(char) == 0x7f for char in link):
        return None
    try:
        parsed = urlsplit(link)
        # Accessing these properties validates malformed ports/IPv6 hosts.
        hostname = parsed.hostname
        _ = parsed.port
    except (TypeError, ValueError):
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.netloc or not hostname:
        return None
    # Credentials in model output can leak secrets into later Telegram/API
    # projections.  Browser links do not need userinfo, so reject it outright.
    if parsed.username is not None or parsed.password is not None:
        return None
    if any(char.isspace() for char in parsed.netloc):
        return None
    caption = _strip_link_markup(raw_caption, max_length=25) or link[:25]
    return {"caption": caption, "link": link}


def sanitize_important_links(
    value: Any,
    extra_links: Any = None,
    *,
    max_links: int = _MAX_IMPORTANT_LINKS,
) -> list[dict[str, str]]:
    """Safely normalize canonical ``important_links`` and legacy ``urls``.

    The helper accepts decoded lists as well as JSON text from a durable claim
    path.  Only HTTP(S) links without credentials are retained, duplicates are
    removed, and the bounded result is safe to carry on ``IncomingMail``.
    """

    try:
        limit = max(0, min(int(max_links), _MAX_IMPORTANT_LINKS))
    except (TypeError, ValueError):
        limit = _MAX_IMPORTANT_LINKS

    def candidates(raw: Any) -> list[Any]:
        if raw is None:
            return []
        if isinstance(raw, str):
            try:
                decoded = json.loads(raw)
            except (TypeError, ValueError, json.JSONDecodeError):
                return [raw]
            return candidates(decoded)
        if isinstance(raw, Mapping):
            # A persisted result may be wrapped as {important_links: ...}.
            for key in ("important_links", "urls"):
                if key in raw:
                    return candidates(raw.get(key))
            return [raw]
        if isinstance(raw, (list, tuple)):
            return list(raw)
        return []

    cleaned: list[dict[str, str]] = []
    seen_links: set[str] = set()
    for item in candidates(value)[:_MAX_IMPORTANT_LINK_CANDIDATES]:
        normalized = _normalize_url_item(item)
        if not normalized or normalized["link"] in seen_links:
            continue
        seen_links.add(normalized["link"])
        cleaned.append(normalized)
        if len(cleaned) >= limit:
            break

    # Deterministic links (for example unsubscribe URLs) take precedence over
    # model-picked links while preserving the existing five-link cap.
    extras: list[dict[str, str]] = []
    for item in candidates(extra_links)[:_MAX_IMPORTANT_LINK_CANDIDATES]:
        normalized = _normalize_url_item(item)
        if not normalized or normalized["link"] in seen_links:
            continue
        seen_links.add(normalized["link"])
        extras.append(normalized)
        if len(extras) >= limit:
            break
    if extras:
        return cleaned[: max(0, limit - len(extras))] + extras[:limit]
    return cleaned[:limit]

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
    return sanitize_telegram_limited_html(raw_html)


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


def _setting(settings: Mapping[str, Any] | Any | None, *names: str, default: Any = None) -> Any:
    """Read a setting from a repository result (dict or small value object)."""
    if settings is None:
        return default
    for name in names:
        if isinstance(settings, Mapping) and name in settings:
            value = settings[name]
        else:
            value = getattr(settings, name, None)
        if value is not None:
            return value
    return default


def summarize_email(
    email_body: str,
    extra_urls: list[dict] | None = None,
    *,
    llm_settings: Mapping[str, Any] | Any | None = None,
    client: Any = None,
    stream: bool = False,
    strict_stream: bool = False,
) -> dict | None:
    """
    Use OpenAI's large language models to summarize an email with enhanced structure.

    Given an email body, send a prompt to the LLM to analyze and summarize the email into
    a structured format that includes summary, priority, action items, deadlines, and other
    key information. The summary should capture the main purpose of the email, urgency level,
    required actions, deadlines, and essential content for quick understanding and decision making.
    The provider, credential, model, threshold, and language are supplied by
    the Mini App's persisted settings.  No environment variables are consulted;
    a missing/disabled setting is a deliberate no-op.

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
        - important_links: Relevant browser links from the email
        - urls: Legacy alias for important_links
        Returns None if all LLM requests failed.
    """
    # The persisted Mini App setting controls all human-readable output.
    default_language = str(_setting(llm_settings, "default_language", "language", "llm_language", default="en_US"))
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
- important_links: array of {{"caption": string, "link": string}} (max 5; link must be http/https; choose only links that materially help the recipient complete the email's main task; normally exclude tracking pixels, logos, decorative links, unsubscribe/preferences and generic footer links unless the email's main purpose is managing that subscription)
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
    setting_models = _setting(llm_settings, "models", "summary_models", "email_summarize_models", "model", "llm_models", "llm_model")
    if isinstance(setting_models, str):
        setting_models = [part.strip() for part in setting_models.split(",") if part.strip()]
    models = list(setting_models or ())
    base_url = _setting(llm_settings, "base_url", "openai_base_url", "llm_base_url", default="")
    api_key = _setting(llm_settings, "api_key", "openai_api_key", "llm_api_key", default="")
    enabled = bool(_setting(llm_settings, "enabled", "enable", "llm_enabled", default=False))
    threshold = _setting(llm_settings, "threshold", "summary_threshold", default=120)
    try:
        threshold = int(threshold)
    except (TypeError, ValueError):
        threshold = 120
    if (
        not models
        or not base_url
        or not api_key
        or not enabled
        or len(remove_spaces_and_urls(email_body)) < threshold
    ):
        return None
    if client is None:
        try:
            openai_client = OpenAIClient(llm_settings)
        except TypeError:
            # Small test doubles may expose a zero-argument constructor.  They
            # still receive the explicit settings through ``configure`` below.
            openai_client = OpenAIClient()
    else:
        openai_client = client
    if llm_settings is not None and callable(getattr(openai_client, "configure", None)):
        openai_client.configure(llm_settings)
    last_error: Exception | None = None
    for model in models:
        try:
            if stream and callable(getattr(openai_client, "stream_completion", None)):
                json_str = openai_client.stream_completion(model, messages, True)
            else:
                completion = openai_client.generate_completion(model, messages, True)
                json_str = openai_client.extract_response_text(completion)
            if not json_str:
                raise ValueError("empty LLM completion")
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
            ]

            if all(field in real_result for field in required_fields) and (
                "important_links" in real_result or "urls" in real_result
            ):
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
                # ``important_links`` is canonical.  Providers using the old
                # ``urls`` key remain accepted during rolling upgrades.
                raw_links = (
                    real_result["important_links"]
                    if real_result.get("important_links") is not None
                    else real_result.get("urls", [])
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

                real_result["action_items"] = [
                    _strip_link_markup(item, max_length=100)
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
                        real_result["deadline"] = _strip_link_markup(deadline_text, max_length=120)

                real_result["key_contacts"] = [
                    _strip_link_markup(name, max_length=50)
                    for name in real_result.get("key_contacts", [])
                    if str(name).strip()
                ][:3]

                cleaned_links = sanitize_important_links(raw_links, extra_urls)
                real_result["important_links"] = cleaned_links
                # Keep the legacy key for callers that still read ``urls``.
                real_result["urls"] = list(cleaned_links)

                return real_result
            else:
                # Fallback: check for old format compatibility.  Older models
                # may omit the structured label fields but still provide links.
                if "summary" in real_result and (
                    "important_links" in real_result or "urls" in real_result
                ):
                    # Convert old format to new format
                    raw_links = (
                        real_result["important_links"]
                        if real_result.get("important_links") is not None
                        else real_result.get("urls", [])
                    )
                    cleaned_links = sanitize_important_links(raw_links, extra_urls)
                    return {
                        "summary": real_result.get("summary", ""),
                        "priority": "medium",
                        "action_required": False,
                        "action_items": [],
                        "deadline": None,
                        "key_contacts": [],
                        "category": "other",
                        "category_confidence": None,
                        "important_links": cleaned_links,
                        "urls": list(cleaned_links),
                    }
                else:
                    raise ValueError("Invalid response format")
        except Exception as e:
            logger.error(f"failed to summarize email content: {e}")
            last_error = e
            continue
    if stream and strict_stream and last_error is not None:
        raise last_error
    return None
