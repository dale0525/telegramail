from telegram import ReplyKeyboardMarkup
from app.bot.utils.conversation_chain import ConversationStep
from telegram.ext import (
    filters,
)

from app.bot.utils.email_utils import EmailUtils


def attachment_step(chain):
    email_utils = EmailUtils(chain=chain)
    return ConversationStep(
        name="附件",
        handler_func=email_utils.handle_attachments,
        keyboard_func=email_utils.get_attachment_keyboard,
        prompt_func=email_utils.get_attachment_prompt,
        data_key="attachments",
        filter_type="CUSTOM",
        filter_handlers=[
            (filters.TEXT & ~filters.COMMAND, email_utils.handle_attachments),
            (filters.Document.ALL, email_utils.handle_attachments),
            (filters.PHOTO, email_utils.handle_attachments),
        ],
    )


def email_body_step(chain):
    email_utils = EmailUtils(chain=chain)
    return ConversationStep(
        name="邮件正文",
        handler_func=email_utils.handle_body,
        keyboard_func=get_cancel_keyboard,
        prompt_func=email_utils.get_body_prompt,
        filter_type="TEXT",
        data_key="body",
    )


def get_cancel_keyboard(context):
    keyboard = [["❌ 取消"]]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
