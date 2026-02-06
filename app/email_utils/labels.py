from __future__ import annotations

LLM_EMAIL_CATEGORIES: tuple[str, ...] = (
    "task",
    "meeting",
    "financial",
    "travel",
    "newsletter",
    "system",
    "social",
    "other",
)

LLM_EMAIL_CATEGORIES_SET: set[str] = set(LLM_EMAIL_CATEGORIES)

_CATEGORY_ALIASES: dict[str, set[str]] = {
    "task": {"task", "tasks", "todo", "to-do", "to_do", "任务", "待办", "待办事项"},
    "meeting": {"meeting", "meetings", "会议", "开会", "会务"},
    "financial": {"financial", "finance", "billing", "invoice", "财务", "账单", "发票", "报销"},
    "travel": {"travel", "trip", "itinerary", "出行", "旅行", "差旅", "行程"},
    "newsletter": {"newsletter", "newsletters", "订阅", "订阅邮件", "简报"},
    "system": {"system", "systems", "security", "notification", "系统", "系统通知", "安全", "告警"},
    "social": {"social", "社交", "人脉", "活动"},
    "other": {"other", "others", "其他", "其它"},
}


def _alias_key(raw: str | None) -> str:
    value = str(raw or "").strip().lower()
    if not value:
        return ""
    for ch in (" ", "-", "_"):
        value = value.replace(ch, "")
    return value


_ALIAS_TO_CANONICAL: dict[str, str] = {}
for _canonical, _aliases in _CATEGORY_ALIASES.items():
    _ALIAS_TO_CANONICAL[_alias_key(_canonical)] = _canonical
    for _alias in _aliases:
        _ALIAS_TO_CANONICAL[_alias_key(_alias)] = _canonical


def normalize_llm_category(raw: str | None) -> str:
    key = _alias_key(raw)
    if not key:
        return "other"
    normalized = _ALIAS_TO_CANONICAL.get(key)
    if normalized in LLM_EMAIL_CATEGORIES_SET:
        return normalized
    return "other"
