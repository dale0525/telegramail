from telegram import ReplyKeyboardMarkup
from app.bot.utils.conversation_chain import ConversationStep

from app.bot.utils.email_utils import EmailUtils


def attachment_step(chain):
    email_utils = EmailUtils(chain=chain)
    return chain.create_step_template(
        name="附件",
        handler_func=email_utils.handle_attachments,
        keyboard_func=email_utils.get_attachment_keyboard,
        prompt_func=email_utils.get_attachment_prompt,
        data_key="attachments",
        filter_type="ALL",
    )


def email_body_step(chain):
    email_utils = EmailUtils(chain=chain)
    return chain.create_step_template(
        name="邮件正文",
        handler_func=email_utils.handle_body,
        keyboard_func=get_cancel_keyboard,
        prompt_func=email_utils.get_body_prompt,
        filter_type="TEXT",
        data_key="body",
    )


def confirm_send_step(chain):
    email_utils = EmailUtils(chain=chain)
    return chain.create_step_template(
        name="确认发送",
        handler_func=email_utils.handle_confirm_send,
        keyboard_func=lambda context: ReplyKeyboardMarkup(
            [["✅ 确认发送", "❌ 取消"]], one_time_keyboard=True, resize_keyboard=True
        ),
        prompt_func=lambda context: "📨 请确认发送邮件，或取消操作：",
        filter_type="TEXT",
        data_key="confirm_send",
    )


def get_cancel_keyboard(context):
    keyboard = [["❌ 取消"]]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
