from aiotdlib import Client
from aiotdlib.api import LinkPreviewOptions, UpdateNewCallbackQuery

from app.bot.utils import answer_callback
from app.database import DBManager
from app.email_utils.account_manager import AccountManager
from app.i18n import _
from app.utils import Logger

logger = Logger().get_logger(__name__)


async def handle_identity_suggestion_callback(
    *, client: Client, update: UpdateNewCallbackQuery, data: str
) -> bool:
    chat_id = update.chat_id
    message_id = update.message_id

    if data.startswith("id_suggest:add:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'id_suggest:add' callback query: {e}")

        try:
            suggestion_id = int(data.split(":", 2)[2])
        except Exception:
            logger.warning(f"Invalid suggestion id in callback data: {data}")
            return True

        db = DBManager()
        suggestion = db.get_identity_suggestion(suggestion_id)
        if not suggestion:
            logger.warning(f"Identity suggestion not found: {suggestion_id}")
            return True

        account_manager = AccountManager()
        account = account_manager.get_account(id=suggestion["account_id"])
        if not account:
            logger.warning(
                f"Account not found for identity suggestion: {suggestion['account_id']}"
            )
            return True

        db.upsert_account_identity(
            account_id=suggestion["account_id"],
            from_email=suggestion["suggested_email"],
            display_name=account.get("alias") or account["email"],
            is_default=False,
        )
        db.mark_identity_suggestion_accepted(suggestion_id=suggestion_id)

        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"✅ {_('identity_added')}: {suggestion['suggested_email']}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception as e:
            logger.error(f"Failed to edit message after identity add: {e}")
        return True

    if data.startswith("id_suggest:ignore:"):
        try:
            await answer_callback(client=client, update=update)
        except Exception as e:
            logger.warning(f"Failed to answer 'id_suggest:ignore' callback query: {e}")

        try:
            suggestion_id = int(data.split(":", 2)[2])
        except Exception:
            logger.warning(f"Invalid suggestion id in callback data: {data}")
            return True

        db = DBManager()
        suggestion = db.get_identity_suggestion(suggestion_id)
        if not suggestion:
            logger.warning(f"Identity suggestion not found: {suggestion_id}")
            return True

        db.mark_identity_suggestion_ignored(suggestion_id=suggestion_id)
        try:
            await client.edit_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"🙈 {_('identity_ignored')}",
                link_preview_options=LinkPreviewOptions(is_disabled=True),
                clear_draft=False,
            )
        except Exception as e:
            logger.error(f"Failed to edit message after identity ignore: {e}")
        return True

    return False

