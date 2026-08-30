__all__ = [
    "decode_email_subject",
    "decode_email_address",
    "clean_html_content",
    "extract_unsubscribe_urls",
    "V2IMAPClient",
    "ConnectionFactory",
    "COMMON_PROVIDERS",
    "summarize_email",
    "format_enhanced_email_summary",
]


def __getattr__(name: str):
    """Keep legacy package exports lazy so v2 IMAP imports stay persistence-free."""
    if name in {"decode_email_subject", "decode_email_address", "clean_html_content", "extract_unsubscribe_urls"}:
        from app.email_utils import text as text_module
        return getattr(text_module, name)
    if name == "V2IMAPClient":
        from app.email_utils.imap_connection import V2IMAPClient
        return V2IMAPClient
    if name == "ConnectionFactory":
        from app.email_utils.connection_factory import ConnectionFactory
        return ConnectionFactory
    if name == "COMMON_PROVIDERS":
        from app.email_utils.common_providers import COMMON_PROVIDERS
        return COMMON_PROVIDERS
    if name in {"summarize_email", "format_enhanced_email_summary"}:
        from app.email_utils import llm as llm_module
        return getattr(llm_module, name)
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
