from telegram import ReplyKeyboardMarkup, ReplyKeyboardRemove
from app.bot.utils.conversation_step import ConversationStep

from app.bot.utils.email_utils import EmailUtils


def attachment_step(chain):
    email_utils = EmailUtils(chain=chain)
    return chain.create_step_template(
        name="附件",
        handler_func=email_utils.handle_attachments,
        keyboard_func=email_utils.get_attachment_keyboard,
        prompt_func=email_utils.get_attachment_prompt,
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
    )


def auto_process_step(chain, name, handler_func, prompt_text):
    """创建一个自动处理的步骤，不需要用户输入，立即执行处理函数"""

    # 创建一个包装处理函数，在执行前将自动执行标志添加到context中
    async def wrapped_handler(update, context, user_input):
        # 设置标记，指示这是一个自动执行的步骤
        context.user_data["is_auto_execute"] = True

        # 调用原始处理函数
        result = await handler_func(update, context, user_input)

        # 移除标记
        if "is_auto_execute" in context.user_data:
            del context.user_data["is_auto_execute"]

        return result

    return chain.create_step_template(
        name=name,
        handler_func=wrapped_handler,  # 使用包装后的处理函数
        keyboard_func=lambda context: ReplyKeyboardRemove(),  # 清理键盘
        prompt_func=lambda context: prompt_text,
        # 使用特殊过滤器，这样任何输入都会匹配
        filter_type="ALL",
        auto_execute=True,  # 设置为自动执行步骤
    )


def fetch_sent_email_step(chain):
    """创建获取发送邮件的步骤"""
    email_utils = EmailUtils(chain=chain)
    # 使用auto_process_step，自动处理步骤
    return auto_process_step(
        chain=chain,
        name="获取发送邮件",
        handler_func=email_utils.fetch_sent_email,
        prompt_text="📤 邮件发送成功，正在获取发送邮件详情...",
    )


def get_cancel_keyboard(context):
    keyboard = [["❌ 取消"]]
    return ReplyKeyboardMarkup(keyboard, one_time_keyboard=True, resize_keyboard=True)
