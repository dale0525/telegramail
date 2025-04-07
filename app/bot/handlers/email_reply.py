"""
Email reply handlers for TelegramMail Bot using ConversationChain and SubConversationChain.
这个模块实现了使用 ConversationChain 和 SubConversationChain 的邮件回复功能，使代码更加模块化和易于维护。
重构后使用子链来管理收件人、抄送和密送列表。
"""

import logging
import html
from typing import List, Dict
from telegram import Update, ReplyKeyboardMarkup
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
)

from app.database.operations import get_email_by_id, get_email_account_by_id
from app.bot.utils.conversation_chain import ConversationChain
from app.bot.utils.email_utils import EmailUtils
from app.bot.utils.common_steps import (
    attachment_step,
    confirm_send_step,
    email_body_step,
    fetch_sent_email_step,
    get_cancel_keyboard,
)
from app.i18n import _  # 导入国际化翻译函数

# 配置日志
logger = logging.getLogger(__name__)

# 模块初始化日志记录
logger.info("====== 初始化邮件回复模块 ======")

# 使用i18n翻译常量文本
RECIPIENT_MANAGEMENT_TEXT = _("recipient_management")
REMOVE_RECIPIENT_TEXT = _("remove_recipient")
CONFIRM_RECIPIENT_TEXT = _("confirm_recipient")
CONFIRM_CC_TEXT = _("confirm_cc")
TO_NEXT_STEP_TEXT = _("to_next_step")

# 创建邮件回复的主会话链条
reply_chain = ConversationChain(
    name="reply",
    description="回复邮件",
    clean_messages=True,
    clean_delay=1,
)

# 创建收件人管理子链
recipients_chain = ConversationChain(
    name="manage_recipients",
    description="管理收件人",
    clean_messages=True,
    clean_delay=1,
)

# 创建抄送管理子链
cc_chain = ConversationChain(
    name="manage_cc",
    description="管理抄送列表",
    clean_messages=True,
    clean_delay=1,
)

# 创建密送管理子链
bcc_chain = ConversationChain(
    name="manage_bcc",
    description="管理密送列表",
    clean_messages=True,
    clean_delay=1,
)

# 记录链条初始化完成
logger.info("邮件回复链条和子链初始化完成")

# 创建邮件工具类实例
email_utils = EmailUtils(chain=reply_chain)


# 辅助函数
def get_recipients_keyboard(candidates: Dict[str, List[str]]):
    """获取收件人选择键盘"""
    keyboard = []

    # 获取所有候选人
    all_candidates = set()

    # 添加默认收件人（原邮件发件人）
    if "sender" in candidates:
        all_candidates.add(candidates["sender"])

    # 添加所有接收者
    for recipient in candidates.get("recipients", []):
        all_candidates.add(recipient)

    # 添加抄送和密送
    for cc in candidates.get("cc", []):
        all_candidates.add(cc)

    for bcc in candidates.get("bcc", []):
        all_candidates.add(bcc)

    # 创建键盘布局 - 每行放置一个收件人
    for candidate in all_candidates:
        keyboard.append([candidate])

    # 添加确认和取消按钮
    keyboard.append([CONFIRM_RECIPIENT_TEXT])
    keyboard.append([_("cancel")])

    return ReplyKeyboardMarkup(
        keyboard,
        one_time_keyboard=True,
        resize_keyboard=True,
        input_field_placeholder=_("select_recipients"),
    )


# 提示信息函数
def get_reply_options_prompt(context):
    """获取回复选项提示消息"""
    email_id = context.user_data.get("compose_email_id")
    if not email_id:
        return _("warning_email_info_not_available")

    # 获取邮件和账户信息
    email = get_email_by_id(email_id)
    account = get_email_account_by_id(email.account_id)
    subject = context.user_data.get("compose_subject", "")

    return (
        f"{_('reply_email')}\n\n"
        f"<b>{_('account')}:</b> {html.escape(account.email)}\n"
        f"<b>{_('subject')}:</b> {html.escape(subject)}\n"
        f"<b>{_('recipient')}:</b> {html.escape(email.sender)}\n\n"
        f"{_('please_select_action')}\n"
        f"{_('use_default_recipient')}\n"
        f"{_('manage_recipients_cc_bcc')}\n"
        f"{_('continue_compose_body')}\n"
        f"{_('cancel_action')}"
    )


def get_body_prompt(context):
    """获取正文输入提示"""
    return f"{_('please_enter_reply_body')}\n\n{_('markdown_support')}"


def get_body_keyboard(context):
    """获取正文输入键盘"""
    keyboard = [[_("cancel")]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_cc_prompt(context):
    """获取抄送管理提示消息"""
    cc_list = context.user_data.get("compose_cc", [])
    cc_text = ", ".join(cc_list) if cc_list else _("none")

    # 获取邮件主题和账户信息
    subject = context.user_data.get("compose_subject", _("no_subject"))
    email_account = context.user_data.get("compose_account_email", _("unknown_account"))

    return (
        f"{_('cc_management')}\n\n"
        f"<b>{_('account')}:</b> {html.escape(email_account)}\n"
        f"<b>{_('subject')}:</b> {html.escape(subject)}\n"
        f"<b>{_('current_cc')}:</b> {html.escape(cc_text)}\n\n"
        f"{_('please_select_cc_action')}\n"
        f"{_('manage_cc_list')}\n"
        f"{_('continue_to_next')}\n"
        f"{_('cancel_current_reply')}"
    )


def get_bcc_prompt(context):
    """获取密送管理提示消息"""
    bcc_list = context.user_data.get("compose_bcc", [])
    bcc_text = ", ".join(bcc_list) if bcc_list else _("none")

    # 获取邮件主题和账户信息
    subject = context.user_data.get("compose_subject", _("no_subject"))
    email_account = context.user_data.get("compose_account_email", _("unknown_account"))

    return (
        f"{_('bcc_management')}\n\n"
        f"<b>{_('account')}:</b> {html.escape(email_account)}\n"
        f"<b>{_('subject')}:</b> {html.escape(subject)}\n"
        f"<b>{_('current_bcc')}:</b> {html.escape(bcc_text)}\n\n"
        f"{_('please_enter_bcc')}\n"
        f"{_('multiple_emails_comma')}\n"
        f"{_('if_no_bcc_needed')}\n"
        f"{_('cancel_operation')}"
    )


async def handle_recipients(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str
):
    """处理主链中的收件人管理步骤"""
    logger.debug(f"收件人管理主步骤收到输入: {user_input}")

    if user_input == TO_NEXT_STEP_TEXT:
        # 确保收件人列表非空
        recipients = _get_current_recipients(context)
        if not recipients:
            alert_msg = await update.message.reply_text(
                _("warning_at_least_one_recipient")
            )
            await reply_chain._record_message(context, alert_msg)
            return ConversationHandler.END
        return None  # 进入下一步

    # 返回 None 以保持在当前步骤
    return None


# 提示信息函数
def get_recipients_prompt(context):
    """获取收件人管理提示消息"""
    try:
        # 添加调试日志
        logger.debug("生成收件人管理提示消息")

        # 安全获取收件人列表
        recipients = context.user_data.get("compose_recipients", [])
        recipients_text = ", ".join(recipients) if recipients else _("none")

        # 获取邮件主题用于显示
        subject = context.user_data.get("compose_subject", _("no_subject"))
        email_account = context.user_data.get("compose_account_email", _("unknown_account"))

        # 构建完整提示消息
        prompt = (
            f"{_('recipients_management')}\n\n"
            f"<b>{_('account')}:</b> {html.escape(email_account)}\n"
            f"<b>{_('subject')}:</b> {html.escape(subject)}\n"
            f"<b>{_('current_recipients')}:</b> {html.escape(recipients_text)}\n\n"
            f"{_('please_select_action')}:\n"
            f"{_('manage_recipients_list')}\n"
            f"{_('continue_to_next')}\n"
            f"{_('cancel_operation')}"
        )

        logger.debug(f"生成的提示消息: {prompt[:100]}...")
        return prompt
    except Exception as e:
        logger.error(f"生成收件人提示消息出错: {e}")
        # 返回一个基本提示，避免整个流程因为错误中断
        return _("basic_recipient_management_prompt")


def get_bcc_keyboard(context):
    """密送管理键盘"""
    keyboard = [
        [TO_NEXT_STEP_TEXT],
        [_("cancel")],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


async def start_reply(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
    button_id: str,
):
    """处理回复邮件的入口函数"""
    try:
        # 记录初始调试信息和步骤状态
        logger.debug(f"开始回复邮件流程: {button_id}")
        email_id = int(button_id.split("_")[2])
        logger.debug(f"解析的邮件ID: {email_id}")

        # 从数据库获取邮件
        email = get_email_by_id(email_id)
        if not email:
            logger.warning(f"找不到邮件ID: {email_id}")
            await update.callback_query.answer(
                _("error_email_not_found"), show_alert=True
            )
            return ConversationHandler.END

        # 获取账户信息
        account = get_email_account_by_id(email.account_id)
        if not account:
            logger.warning(f"找不到对应的邮箱账户ID: {email.account_id}")
            await update.callback_query.answer(
                _("error_account_not_found"), show_alert=True
            )
            return ConversationHandler.END

        # 存储邮件和账户信息
        context.user_data["compose_email_id"] = email_id
        context.user_data["compose_account_id"] = email.account_id
        context.user_data["compose_account_email"] = account.email
        logger.debug(f"设置回复账户: {account.email}")

        # 处理回复主题(添加Re:前缀)
        subject = email.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        context.user_data["compose_subject"] = subject
        logger.debug(f"设置回复主题: {subject}")

        # 存储原始消息ID以便回复时引用
        if hasattr(update.callback_query.message, "message_id"):
            context.user_data["reply_original_message_id"] = (
                update.callback_query.message.message_id
            )
            logger.debug(f"存储原始消息ID: {update.callback_query.message.message_id}")

        # 准备候选收件人名单
        candidates = {}

        # 将原始发件人作为默认收件人
        candidates["sender"] = email.sender
        logger.debug(f"原邮件发件人: {email.sender}")

        # 保存原始格式的发件人
        context.user_data["compose_default_recipient"] = email.sender

        # 提取纯邮件地址格式
        clean_sender = email_utils.extract_email_from_complex_format(email.sender)
        if clean_sender != email.sender:
            logger.debug(f"清理后的发件人地址: {clean_sender}")

        # 确保收件人列表是有效的列表类型
        recipients_list = [clean_sender]  # 使用清理后的邮件地址
        context.user_data["compose_recipients"] = recipients_list
        logger.debug(f"设置默认收件人(清理后): {clean_sender}")
        logger.debug(f"初始收件人列表: {recipients_list}")

        # 记录数据类型，辅助调试
        logger.debug(
            f"收件人列表类型: {type(context.user_data.get('compose_recipients'))}"
        )

        # 解析其他收件人
        try:
            if email.recipients:
                # 创建包含属性的字典
                email_dict = {
                    "recipients": email.recipients,
                    "cc": email.cc if hasattr(email, "cc") else "",
                    "bcc": email.bcc if hasattr(email, "bcc") else "",
                }

                candidates["recipients"] = email_utils.parse_email_addresses(
                    email_dict, "recipients"
                )
                logger.debug(f"解析收件人列表: {candidates['recipients']}")

                # 从 CC 列表中排除自己的邮箱和触发命令
                if hasattr(email, "cc") and email.cc:
                    all_cc = email_utils.parse_email_addresses(email_dict, "cc")
                    # 过滤掉自己的邮箱和触发命令
                    cc_list = [cc for cc in all_cc if cc != account.email]
                    candidates["cc"] = cc_list
                    context.user_data["compose_cc"] = cc_list
                    logger.debug(f"设置抄送列表: {cc_list}")
                else:
                    context.user_data["compose_cc"] = []

                # 默认密送为空
                context.user_data["compose_bcc"] = []
                logger.debug("初始化密送列表为空")

        except Exception as e:
            logger.error(f"解析收件人时出错: {e}")
            logger.exception(e)  # 记录完整异常信息

        # 存储候选人列表
        context.user_data["compose_candidates"] = candidates
        logger.debug(f"候选人列表: {candidates}")

        # 初始化附件列表
        context.user_data["compose_attachments"] = []

        # 回复callback query
        await update.callback_query.answer()
        logger.debug("已回复callback query")

        # 检查回调查询消息是否存在
        if not update.callback_query.message:
            logger.error("错误: update.callback_query.message不存在")
            return ConversationHandler.END

        # 尝试记录当前会话状态
        try:
            # 显示临时状态消息以确认bot正在处理
            logger.debug("准备发送临时状态消息...")
            temp_message = await update.callback_query.message.reply_text(
                _("preparing_email_reply"), disable_notification=True
            )
            logger.debug(f"临时状态消息已发送，消息ID: {temp_message.message_id}")

            await reply_chain._record_message(context, temp_message)
            logger.debug("已记录临时状态消息")
        except Exception as e:
            logger.error(f"发送临时状态消息失败: {e}")
            logger.exception(e)

        return None
    except Exception as e:
        logger.error(f"处理回复邮件出错: {e}")
        logger.exception(e)  # 记录完整异常栈
        try:
            # 尝试通知用户
            await update.callback_query.answer(
                _("error_processing_reply"), show_alert=True
            )
        except Exception:
            pass  # 忽略二次错误
        return ConversationHandler.END


def get_reply_handler():
    """获取回复邮件的处理器"""
    try:
        # 配置按钮入口点
        reply_chain.add_button_entry_point(start_reply, "^reply_email_")

        # 配置主链步骤
        # 第一步：收件人管理（包含子链入口）
        reply_chain.add_step(
            name="recipients",
            handler_func=handle_recipients,
            prompt_func=get_recipients_prompt,
            keyboard_func=lambda context: ReplyKeyboardMarkup(
                [[RECIPIENT_MANAGEMENT_TEXT], [TO_NEXT_STEP_TEXT, "❌ 取消"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
            filter_type="TEXT",
            sub_chain=recipients_chain,
            trigger_keywords=[RECIPIENT_MANAGEMENT_TEXT],
        )

        recipients_chain.add_step(
            name="manage_recipients",
            handler_func=handle_sub_recipients,
            prompt_func=get_sub_recipients_prompt,
            keyboard_func=get_sub_recipients_keyboard,
            validator=validate_sub_recipients,
            filter_type="TEXT",
            end_keywords=[CONFIRM_RECIPIENT_TEXT],
        )

        # 第二步：抄送人管理（包含子链入口）
        reply_chain.add_step(
            name="cc",
            handler_func=handle_cc,
            prompt_func=get_cc_prompt,
            keyboard_func=lambda context: ReplyKeyboardMarkup(
                [["管理抄送列表"], [TO_NEXT_STEP_TEXT, "❌ 取消"]],
                resize_keyboard=True,
                one_time_keyboard=True,
            ),
            filter_type="TEXT",
            sub_chain=cc_chain,
            trigger_keywords=["管理抄送列表"],
        )

        # 抄送子链配置
        cc_chain.add_step(
            name="manage_cc",
            handler_func=handle_sub_cc,
            prompt_func=get_sub_cc_prompt,
            keyboard_func=get_sub_cc_keyboard,
            validator=validate_sub_cc,
            filter_type="TEXT",
            end_keywords=[CONFIRM_CC_TEXT],
        )

        # 第三步：密送人管理
        reply_chain.add_step(
            name="bcc",
            handler_func=handle_bcc,
            prompt_func=get_bcc_prompt,
            keyboard_func=get_bcc_keyboard,
            validator=validate_bcc,
            filter_type="TEXT",
        )

        # 第四步：正文编写（使用common_steps中的模板）
        reply_chain.add_step_from_template(email_body_step(reply_chain))

        # 第五步：处理附件（使用common_steps中的模板）
        reply_chain.add_step_from_template(attachment_step(reply_chain))

        # 第六步：确认发送（使用common_steps中的模板）
        reply_chain.add_step_from_template(confirm_send_step(reply_chain))

        # 第七步：获取发送结果（使用common_steps中的模板）
        reply_chain.add_step_from_template(fetch_sent_email_step(reply_chain))

        # 构建并返回处理器
        conversation_handler = reply_chain.build()
        return conversation_handler
    except Exception as e:
        logger.error(f"构建回复邮件处理器时出错: {e}")
        logger.exception(e)
        raise


# 收件人子链
def _get_current_recipients(context):
    """安全获取当前收件人列表"""
    current_recipients = context.user_data.get("compose_recipients", [])

    # 类型安全检查
    if not isinstance(current_recipients, list):
        logger.warning(f"收件人列表类型错误，强制转换: {type(current_recipients)}")
        # 尝试转换为列表
        try:
            if isinstance(current_recipients, str):
                if "," in current_recipients:
                    current_recipients = [
                        r.strip() for r in current_recipients.split(",")
                    ]
                else:
                    current_recipients = [current_recipients]
            else:
                current_recipients = []
        except Exception as e:
            logger.error(f"转换收件人列表失败: {e}")
            current_recipients = []

        # 更新上下文数据
        context.user_data["compose_recipients"] = current_recipients

    return current_recipients


def get_sub_recipients_prompt(context):
    """获取收件人列表管理提示"""
    try:
        # 获取当前收件人列表，记录详细信息 - 使用安全获取函数
        recipients = _get_current_recipients(context)
        logger.debug(f"当前收件人列表(从context.user_data获取): {recipients}")

        recipients_text = ", ".join(recipients) if recipients else "暂无"

        # 获取邮件信息 - 需确保这些数据在主链和子链之间共享
        subject = context.user_data.get("compose_subject", "无主题")
        email_account = context.user_data.get("compose_account_email", "未知账户")
        logger.debug(f"邮件信息 - 账户: {email_account}, 主题: {subject}")

        # 构建更详细的提示信息
        return (
            f"👥 <b>管理收件人列表</b>\n\n"
            f"<b>账户:</b> {html.escape(email_account)}\n"
            f"<b>主题:</b> {html.escape(subject)}\n"
            f"<b>当前收件人:</b> {html.escape(recipients_text)}\n\n"
            f"您可以:\n"
            f"• 选择下方的【现有收件人】进行移除\n"
            f"• 从【可添加的收件人】中选择添加\n"
            f"• 直接输入要添加的收件人邮箱，多个邮箱用英文逗号分隔\n"
            f'• 完成后点击 "{CONFIRM_RECIPIENT_TEXT}"'
        )
    except Exception as e:
        logger.error(f"生成子链收件人提示消息出错: {e}")
        logger.exception(e)  # 记录完整异常栈

        # 返回一个基本提示，避免整个流程因为错误中断
        return "👥 <b>管理收件人列表</b>"


def get_sub_recipients_keyboard(context):
    """获取候选收件人键盘"""
    try:
        # 添加调试日志
        logger.debug("生成子链收件人管理键盘")

        # 获取候选人列表和当前收件人列表
        candidates = context.user_data.get("compose_candidates", {})
        logger.debug(f"候选收件人列表: {candidates}")

        # 使用安全获取函数
        current_recipients = _get_current_recipients(context)
        logger.debug(f"当前收件人列表(从context.user_data获取): {current_recipients}")

        # 初始化键盘
        keyboard = []

        # 分类：当前收件人区域
        if current_recipients:
            keyboard.append(["--- 当前收件人 ---"])
            for recipient in current_recipients:
                keyboard.append([f"{REMOVE_RECIPIENT_TEXT}{recipient}"])
        else:
            keyboard.append(["--- 当前没有收件人 ---"])

        # 分类：候选收件人区域
        all_candidates = set()

        # 添加默认收件人（原邮件发件人）
        if "sender" in candidates:
            all_candidates.add(candidates["sender"])
            logger.debug(f"添加发件人到候选列表: {candidates['sender']}")

        # 添加所有接收者
        for recipient in candidates.get("recipients", []):
            all_candidates.add(recipient)

        # 添加抄送和密送
        for cc in candidates.get("cc", []):
            all_candidates.add(cc)

        for bcc in candidates.get("bcc", []):
            all_candidates.add(bcc)  # 修复：使用bcc变量而不是cc

        # 过滤掉已经在当前收件人列表中的邮箱
        available_candidates = [
            c for c in all_candidates if c not in current_recipients
        ]
        logger.debug(f"可添加的候选收件人: {available_candidates}")

        if available_candidates:
            keyboard.append(["--- 可添加的收件人 ---"])
            for candidate in available_candidates:
                keyboard.append([candidate])
        else:
            keyboard.append(["--- 没有可添加的收件人 ---"])

        # 添加确认和取消按钮
        keyboard.append([CONFIRM_RECIPIENT_TEXT])
        keyboard.append(["❌ 取消"])

        logger.debug(f"生成的键盘布局: {keyboard}")

        return ReplyKeyboardMarkup(
            keyboard,
            one_time_keyboard=True,
            resize_keyboard=True,
            input_field_placeholder="你也可以在此直接输入要添加的收件人，多个收件人用英文逗号分隔",
        )
    except Exception as e:
        logger.error(f"生成子链收件人键盘出错: {e}")
        logger.exception(e)  # 记录完整异常栈

        # 返回一个简单的应急键盘，避免整个流程因错误中断
        emergency_keyboard = [[CONFIRM_RECIPIENT_TEXT], ["❌ 取消"]]
        return ReplyKeyboardMarkup(
            emergency_keyboard,
            one_time_keyboard=True,
            resize_keyboard=True,
            input_field_placeholder="在此直接输入要添加的收件人，多个收件人用英文逗号分隔",
        )


def validate_recipients(user_input, context):
    """验证收件人列表"""
    is_valid, error_msg, email_list = EmailUtils.validate_email_list(
        user_input, is_optional=False
    )
    if is_valid:
        current_recipients = _get_current_recipients(context)
        context.user_data["compose_recipients"] = [*current_recipients, *email_list]
    return is_valid, error_msg


def validate_sub_recipients(user_input, context):
    """验证子链收件人"""
    if user_input == "❌ 取消" or user_input == CONFIRM_RECIPIENT_TEXT:
        return True, None
    # 对于"移除"操作的特殊处理
    if user_input.startswith(REMOVE_RECIPIENT_TEXT):
        recipient_to_remove = user_input.replace(REMOVE_RECIPIENT_TEXT, "")
        current_recipients = _get_current_recipients(context)

        if recipient_to_remove in current_recipients:
            current_recipients.remove(recipient_to_remove)
            context.user_data["compose_recipients"] = current_recipients
            logger.debug(
                f"已移除收件人 {recipient_to_remove}, 当前列表: {current_recipients}"
            )
            return True, None
        else:
            return False, f"⚠️ 收件人列表中没有 {recipient_to_remove}"

    # 使用通用验证函数
    is_valid, error_msg = validate_recipients(user_input, context)

    return is_valid, error_msg


async def handle_sub_recipients(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str
) -> int:
    """
    收件人子链的处理函数 - 专注于收件人管理的流程和UI交互

    Args:
        update: Telegram更新对象
        context: 上下文对象
        user_input: 用户输入

    Returns:
        int: 下一步状态ID或特殊标记
    """
    # 记录详细的输入信息
    logger.debug(f"收件人管理处理函数收到输入: '{user_input}'")

    # 获取当前收件人列表供日志和界面显示使用
    current_recipients = _get_current_recipients(context)
    logger.debug(f"当前收件人列表: {current_recipients}")

    # 处理不同的用户输入场景
    if user_input == "❌ 取消":
        # 用户选择取消，退出对话
        await update.message.reply_text("已取消操作")
        return ConversationHandler.END

    elif user_input == CONFIRM_RECIPIENT_TEXT:
        # 用户确认收件人列表，返回None让ConversationChain处理子链结束
        logger.debug(f"用户确认收件人列表: {current_recipients}")
        return None

    elif user_input.startswith(REMOVE_RECIPIENT_TEXT):
        # 已在validate_sub_recipients中处理移除操作，这里只是记录结果
        recipient_to_remove = user_input.replace(REMOVE_RECIPIENT_TEXT, "")
        logger.debug(f"已处理移除收件人: {recipient_to_remove}")

    else:
        # 用户输入了新的收件人，已在validate_sub_recipients中验证和添加
        logger.debug(f"已处理新增收件人，当前列表: {current_recipients}")

    # 返回None，让ConversationChain继续当前子链
    return None


# 抄送人子链
def _get_current_cc(context):
    """安全获取当前抄送列表"""
    current_cc = context.user_data.get("compose_cc", [])

    # 类型安全检查
    if not isinstance(current_cc, list):
        logger.warning(f"抄送列表类型错误，强制转换: {type(current_cc)}")
        # 尝试转换为列表
        try:
            if isinstance(current_cc, str):
                if "," in current_cc:
                    current_cc = [r.strip() for r in current_cc.split(",")]
                else:
                    current_cc = [current_cc]
            else:
                current_cc = []
        except Exception as e:
            logger.error(f"转换抄送列表失败: {e}")
            current_cc = []

        # 更新上下文数据
        context.user_data["compose_cc"] = current_cc

    return current_cc


def get_sub_cc_prompt(context):
    """获取抄送列表管理提示"""
    try:
        # 获取当前抄送列表
        cc_list = _get_current_cc(context)
        logger.debug(f"当前抄送列表(从context.user_data获取): {cc_list}")

        cc_text = ", ".join(cc_list) if cc_list else _("none")

        # 获取邮件信息
        subject = context.user_data.get("compose_subject", _("no_subject"))
        email_account = context.user_data.get("compose_account_email", _("unknown_account"))
        logger.debug(f"邮件信息 - 账户: {email_account}, 主题: {subject}")

        # 构建提示信息
        return (
            f"📋 <b>管理抄送列表</b>\n\n"
            f"<b>账户:</b> {html.escape(email_account)}\n"
            f"<b>主题:</b> {html.escape(subject)}\n"
            f"<b>当前抄送:</b> {html.escape(cc_text)}\n\n"
            f"您可以:\n"
            f"• 选择下方的【现有抄送人】进行移除\n"
            f"• 从【可添加的抄送人】中选择添加\n"
            f"• 直接输入要添加的抄送邮箱，多个邮箱用英文逗号分隔\n"
            f'• 完成后点击 "{CONFIRM_CC_TEXT}"'
        )
    except Exception as e:
        logger.error(f"生成子链抄送提示消息出错: {e}")
        logger.exception(e)  # 记录完整异常栈

        # 返回一个基本提示，避免整个流程因为错误中断
        return "📋 <b>管理抄送列表</b>"


def get_sub_cc_keyboard(context):
    """获取候选抄送人键盘"""
    try:
        # 添加调试日志
        logger.debug("生成子链抄送管理键盘")

        # 获取候选人列表和当前抄送列表
        candidates = context.user_data.get("compose_candidates", {})
        logger.debug(f"候选抄送列表: {candidates}")

        # 使用安全获取函数
        current_cc = _get_current_cc(context)
        logger.debug(f"当前抄送列表(从context.user_data获取): {current_cc}")

        # 初始化键盘
        keyboard = []

        # 分类：当前抄送区域
        if current_cc:
            keyboard.append(["--- 当前抄送 ---"])
            for cc in current_cc:
                keyboard.append([f"{REMOVE_RECIPIENT_TEXT}{cc}"])
        else:
            keyboard.append(["--- 当前没有抄送 ---"])

        # 分类：候选抄送区域
        all_candidates = set()

        # 添加所有候选人
        for recipient in candidates.get("recipients", []):
            all_candidates.add(recipient)

        if "sender" in candidates:
            all_candidates.add(candidates["sender"])

        for cc in candidates.get("cc", []):
            all_candidates.add(cc)

        for bcc in candidates.get("bcc", []):
            all_candidates.add(bcc)

        # 过滤掉已经在当前抄送列表中的邮箱
        available_candidates = [c for c in all_candidates if c not in current_cc]
        logger.debug(f"可添加的候选抄送人: {available_candidates}")

        if available_candidates:
            keyboard.append(["--- 可添加的抄送人 ---"])
            for candidate in available_candidates:
                keyboard.append([candidate])
        else:
            keyboard.append(["--- 没有可添加的抄送人 ---"])

        # 添加确认和取消按钮
        keyboard.append([CONFIRM_CC_TEXT])
        keyboard.append(["❌ 取消"])

        logger.debug(f"生成的键盘布局: {keyboard}")

        return ReplyKeyboardMarkup(
            keyboard,
            one_time_keyboard=True,
            resize_keyboard=True,
            input_field_placeholder="你也可以在此直接输入要添加的抄送邮箱，多个邮箱用英文逗号分隔",
        )
    except Exception as e:
        logger.error(f"生成子链抄送键盘出错: {e}")
        logger.exception(e)  # 记录完整异常栈

        # 返回一个简单的应急键盘，避免整个流程因错误中断
        emergency_keyboard = [[CONFIRM_RECIPIENT_TEXT], ["❌ 取消"]]
        return ReplyKeyboardMarkup(
            emergency_keyboard,
            one_time_keyboard=True,
            resize_keyboard=True,
            input_field_placeholder="在此直接输入要添加的抄送邮箱，多个邮箱用英文逗号分隔",
        )


def validate_cc(user_input, context):
    """验证抄送列表"""
    is_valid, error_msg, email_list = EmailUtils.validate_email_list(
        user_input, is_optional=True
    )
    if is_valid:
        current_cc = _get_current_cc(context)
        context.user_data["compose_cc"] = [*current_cc, *email_list]
    return is_valid, error_msg


def validate_sub_cc(user_input, context):
    """验证子链抄送"""
    if user_input == "❌ 取消" or user_input == CONFIRM_CC_TEXT:
        return True, None
    # 对于"移除"操作的特殊处理
    if user_input.startswith(REMOVE_RECIPIENT_TEXT):
        cc_to_remove = user_input.replace(REMOVE_RECIPIENT_TEXT, "")
        current_cc = _get_current_cc(context)

        if cc_to_remove in current_cc:
            current_cc.remove(cc_to_remove)
            context.user_data["compose_cc"] = current_cc
            logger.debug(f"已移除抄送人 {cc_to_remove}, 当前列表: {current_cc}")
            return True, None
        else:
            return False, f"⚠️ 抄送列表中没有 {cc_to_remove}"

    # 使用通用验证函数
    is_valid, error_msg = validate_cc(user_input, context)

    return is_valid, error_msg


async def handle_cc(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str
):
    """处理主链中的抄送管理步骤"""
    logger.debug(f"抄送管理主步骤收到输入: {user_input}")

    if user_input == TO_NEXT_STEP_TEXT:
        # 抄送是可选的，无需验证是否为空
        return None  # 进入下一步

    # 返回 None 以保持在当前步骤
    return None


async def handle_sub_cc(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str
) -> int:
    """
    抄送子链的处理函数 - 专注于抄送管理的流程和UI交互

    Args:
        update: Telegram更新对象
        context: 上下文对象
        user_input: 用户输入

    Returns:
        int: 下一步状态ID或特殊标记
    """
    # 记录详细的输入信息
    logger.debug(f"抄送管理处理函数收到输入: '{user_input}'")

    # 获取当前抄送列表供日志和界面显示使用
    current_cc = _get_current_cc(context)
    logger.debug(f"当前抄送列表: {current_cc}")

    # 处理不同的用户输入场景
    if user_input == "❌ 取消":
        # 用户选择取消，退出对话
        await update.message.reply_text("已取消操作")
        return ConversationHandler.END

    elif user_input == CONFIRM_RECIPIENT_TEXT:
        # 用户确认抄送列表，返回None让ConversationChain处理子链结束
        logger.debug(f"用户确认抄送列表: {current_cc}")
        return None

    elif user_input.startswith(REMOVE_RECIPIENT_TEXT):
        # 已在validate_sub_cc中处理移除操作，这里只是记录结果
        cc_to_remove = user_input.replace(REMOVE_RECIPIENT_TEXT, "")
        logger.debug(f"已处理移除抄送人: {cc_to_remove}")

    else:
        # 用户输入了新的抄送，已在validate_sub_cc中验证和添加
        logger.debug(f"已处理新增抄送，当前列表: {current_cc}")

    # 返回None，让ConversationChain继续当前子链
    return None


def validate_bcc(user_input, context):
    """验证密送列表"""
    # 添加日志记录输入参数
    logger.debug(f"验证密送人输入: '{user_input}'")
    logger.debug(f"验证前密送列表: {context.user_data.get('compose_bcc', [])}")
    logger.debug(f"验证前收件人列表: {context.user_data.get('compose_recipients', [])}")

    if user_input == TO_NEXT_STEP_TEXT:
        return True, None

    is_valid, error_msg, email_list = EmailUtils.validate_email_list(
        user_input, is_optional=True
    )

    if is_valid:
        # 记录验证结果
        logger.debug(f"密送验证结果 - 有效: True, 邮箱列表: {email_list}")

        # 确保即使用户输入了 "-" 或 "无"，也会存储为空列表
        context.user_data["compose_bcc"] = email_list

        # 记录更新后的密送列表
        logger.debug(f"验证后密送列表: {context.user_data.get('compose_bcc', [])}")
    else:
        # 记录验证失败的情况
        logger.warning(f"密送验证失败: {error_msg}")

    return is_valid, error_msg


async def handle_bcc(
    update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: str
):
    """处理密送步骤"""
    logger.debug(f"密送管理步骤收到输入: {user_input}")

    # 验证器已经处理了存储密送列表的逻辑
    return None  # 进入下一步
