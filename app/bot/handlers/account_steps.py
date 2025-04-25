import re

# Remove Client and asyncio imports if no longer needed elsewhere in the file
import asyncio

# from aiotdlib import Client
from app.i18n import _
from app.bot.common_components import create_yes_no_keyboard, create_providers_keyboard
from app.email_utils.common_providers import COMMON_PROVIDERS
from app.email_utils.verification import verify_account_credentials
from app.utils import Logger
from app.bot.handlers.check_email import fetch_emails_action

# Import the new utility functions
from app.bot.utils import get_email_folder_id, get_group_id

logger = Logger().get_logger(__name__)

# --- Helper function ---


def check_common_provider(context: dict, email: str):
    """Checks if the email domain matches a common provider and updates context."""
    domain = email.split("@")[-1].lower()
    matched_provider = None
    for provider in COMMON_PROVIDERS:
        provider_name_lower = provider["name"].lower()
        # Basic matching logic (can be refined)
        if (
            provider_name_lower in domain
            or any(d in domain for d in provider.get("domains", []))
            or provider["smtp_server"].split(".")[-2]
            in domain  # Check common part of server names
            or provider["imap_server"].split(".")[-2] in domain
        ):
            matched_provider = provider
            break

    if matched_provider:
        provider_name = matched_provider["name"]
        logger.info(
            f"Common provider found for {email}: {provider_name}. Applying settings."
        )
        context["use_common_provider"] = True
        context["common_provider_name"] = provider_name
        # Pre-fill context based on common provider
        context["smtp_server"] = matched_provider["smtp_server"]
        context["smtp_port"] = matched_provider["smtp_port"]
        context["smtp_ssl"] = matched_provider["smtp_ssl"]
        context["imap_server"] = matched_provider["imap_server"]
        context["imap_port"] = matched_provider["imap_port"]
        context["imap_ssl"] = matched_provider["imap_ssl"]
    else:
        logger.info(
            f"No common provider found for {email}. Proceeding with manual input."
        )
        context["use_common_provider"] = False
    # Return value isn't strictly needed if context is modified directly, but good practice
    return context


def handle_provider_selection(context: dict, selection: str):
    """
    Handles the selection of an email provider template or custom option.

    Args:
        context: The conversation context dictionary
        selection: The provider name selected by the user

    Returns:
        Updated context dictionary
    """
    # Check if user selected "Custom"
    if selection.lower() == _("add_addcount_provider_custom").lower():
        logger.info("User selected custom email provider configuration.")
        context["use_common_provider"] = False
        return context

    # Find the matching provider in COMMON_PROVIDERS
    matched_provider = None
    for provider in COMMON_PROVIDERS:
        if provider["name"] == selection:
            matched_provider = provider
            break

    if matched_provider:
        provider_name = matched_provider["name"]
        logger.info(f"User selected predefined provider: {provider_name}.")
        context["use_common_provider"] = True
        context["common_provider_name"] = provider_name
        context["selected_provider"] = True

        # Pre-fill context based on selected provider
        context["smtp_server"] = matched_provider["smtp_server"]
        context["smtp_port"] = matched_provider["smtp_port"]
        context["smtp_ssl"] = matched_provider["smtp_ssl"]
        context["imap_server"] = matched_provider["imap_server"]
        context["imap_port"] = matched_provider["imap_port"]
        context["imap_ssl"] = matched_provider["imap_ssl"]
    else:
        logger.warning(f"Provider '{selection}' not found in common providers list.")
        context["use_common_provider"] = False

    return context


# --- Conversation Step Definitions ---

# Note: The 'optional' flag and skip logic depends on the Conversation class implementation.
# We assume here that sending an empty message or a specific command like /skip
# will trigger skipping the step if 'optional' is True.

# Verification step definition using the new generic action step configuration
VERIFICATION_STEP = {
    "action": verify_account_credentials,  # Use the original sync function
    "pre_action_message_key": "verifying_account_wait",  # Message before action
    "success_message_key": "account_verification_success",  # Optional: Message on success
    "terminate_on_fail": True,
    "fail_message_key": "account_verification_failed_message",
}

ADD_ACCOUNT_STEPS = [
    {
        "text": _("add_account_select_provider"),
        "key": "provider_selection",
        "reply_markup": create_providers_keyboard(),
        "validate": lambda x: (
            x == _("add_addcount_provider_custom")
            or any(p["name"] == x for p in COMMON_PROVIDERS),
            _("add_account_invalid_provider"),
        ),
        "post_process": handle_provider_selection,
    },
    {
        "text": f"📧 {_('add_account_input_email')}",
        "key": "email",
        "validate": lambda x: (
            bool(re.match(r"^[\w\.-]+@[\w\.-]+\.\w+$", x)),
            _("add_account_invalid_email"),
        ),
        # Only check common provider if not already selected
        "post_process": lambda context, email: (
            check_common_provider(context, email)
            if not context.get("selected_provider", False)
            else context
        ),
    },
    {
        "text": _("add_account_input_password"),
        "key": "password",
        "is_sensitive": True,  # Mark password as sensitive for potential masking/deletion
    },
    {
        "text": _("add_account_input_smtp_server"),
        "key": "smtp_server",
        "skip": lambda context: context.get("use_common_provider", False),
    },
    {
        "text": _("add_account_input_smtp_port"),
        "key": "smtp_port",
        "validate": lambda x: (
            x.isdigit() and 0 < int(x) < 65536,
            _("add_account_invalid_port"),
        ),
        "process": int,
        "skip": lambda context: context.get("use_common_provider", False),
    },
    {
        "text": _("add_account_smtp_ssl"),
        "key": "smtp_ssl",
        "reply_markup": create_yes_no_keyboard(),
        "validate": lambda x: (
            x.lower() in [_("yes").lower(), _("no").lower()],
            _("invalid_yes_no"),
        ),
        "process": lambda data: data.lower() == _("yes").lower(),
        "skip": lambda context: context.get("use_common_provider", False),
    },
    {
        "text": _("add_account_input_imap_server"),
        "key": "imap_server",
        "skip": lambda context: context.get("use_common_provider", False),
    },
    {
        "text": _("add_account_input_imap_port"),
        "key": "imap_port",
        "validate": lambda x: (
            x.isdigit() and 0 < int(x) < 65536,
            _("add_account_invalid_port"),
        ),
        "process": int,
        "skip": lambda context: context.get("use_common_provider", False),
    },
    {
        "text": _("add_account_imap_ssl"),
        "key": "imap_ssl",
        "reply_markup": create_yes_no_keyboard(),
        "validate": lambda x: (
            x.lower() in [_("yes").lower(), _("no").lower()],
            _("invalid_yes_no"),
        ),
        "process": lambda data: data.lower() == _("yes").lower(),
        "skip": lambda context: context.get("use_common_provider", False),
    },
    {
        "text": _("add_account_input_alias"),
        "key": "alias",
    },
    # Add verification step at the end
    VERIFICATION_STEP,
    {
        # Step to create the supergroup
        "action": lambda ctx: get_group_id(
            email=ctx["email"],
            alias=ctx["alias"],
            provider_name=ctx.get("common_provider_name"),
        ),
        "pre_action_message_key": "group_creating",
        "success_message_key": "group_create_success",
        "fail_message_key": "group_create_fail",
        "terminate_on_fail": True,
    },
    {
        # Step to create the supergroup
        "action": lambda ctx: get_email_folder_id(),
        "pre_action_message_key": "group_creating",
        "success_message_key": "group_create_success",
        "fail_message_key": "group_create_fail",
        "terminate_on_fail": False,
    },
    {
        # Step to check for new emails
        "action": lambda ctx: fetch_emails_action(ctx, ctx["email"]),
        "pre_action_message_key": "fetching_emails_wait",
        "success_message_key": "initial_email_check_success",
        "fail_message_key": "initial_email_check_fail",
        "terminate_on_fail": False,
    },
]


# Steps for editing an existing account
# We use lambdas for text to access the current context (ctx) which holds existing values
EDIT_ACCOUNT_STEPS = [
    {
        # Password: Show placeholder, always ask if they want to update.
        "text": lambda ctx: f"{_('edit_account_password')} ({_('current')}: ******).\n{_('send_new_or_skip')}",
        "key": "password",
        "optional": True,  # Allow skipping to keep the old password
        "is_sensitive": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_smtp_server')} ({_('current')}: {ctx.get('smtp_server', 'N/A')}).\n{_('send_new_or_skip')}",
        "key": "smtp_server",
        "optional": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_smtp_port')} ({_('current')}: {ctx.get('smtp_port', 'N/A')}).\n{_('send_new_or_skip')}",
        "key": "smtp_port",
        "validate": lambda x: (
            x.isdigit() and 0 < int(x) < 65536,
            _("add_account_invalid_port"),
        ),
        "process": int,
        "optional": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_smtp_ssl')} ({_('current')}: {ctx.get('smtp_ssl', 'N/A')}).\n{_('select_yes_no_or_skip')}",
        "key": "smtp_ssl",
        "reply_markup": create_yes_no_keyboard(),
        "validate": lambda x: (
            x.lower() in [_("yes").lower(), _("no").lower()],
            _("invalid_yes_no"),
        ),
        "process": lambda data: data.lower() == _("yes").lower(),
        "optional": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_imap_server')} ({_('current')}: {ctx.get('imap_server', 'N/A')}).\n{_('send_new_or_skip')}",
        "key": "imap_server",
        "optional": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_imap_port')} ({_('current')}: {ctx.get('imap_port', 'N/A')}).\n{_('send_new_or_skip')}",
        "key": "imap_port",
        "validate": lambda x: (
            x.isdigit() and 0 < int(x) < 65536,
            _("add_account_invalid_port"),
        ),
        "process": int,
        "optional": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_imap_ssl')} ({_('current')}: {ctx.get('imap_ssl', 'N/A')}).\n{_('select_yes_no_or_skip')}",
        "key": "imap_ssl",
        "reply_markup": create_yes_no_keyboard(),
        "validate": lambda x: (
            x.lower() in [_("yes").lower(), _("no").lower()],
            _("invalid_yes_no"),
        ),
        "process": lambda data: data.lower() == _("yes").lower(),
        "optional": True,
    },
    {
        "text": lambda ctx: f"{_('edit_account_alias')} ({_('current')}: {ctx.get('alias', 'N/A')}).\n{_('send_new_or_skip')}",
        "key": "alias",
        "optional": True,
    },
    # Add verification step at the end
    VERIFICATION_STEP,
]
