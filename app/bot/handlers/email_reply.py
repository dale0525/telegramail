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
)

# 配置日志
logger = logging.getLogger(__name__)

# 模块初始化日志记录
logger.info("====== 初始化邮件回复模块 ======")

RECIPIENT_MANAGEMENT_TEXT = "管理收件人"
REMOVE_RECIPIENT_TEXT = "移除 "
CONFIRM_RECIPIENT_TEXT = "✅ 确认收件人"
TO_NEXT_STEP_TEXT = "✅ 继续下一步"

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
    keyboard.append(["❌ 取消"])

    return ReplyKeyboardMarkup(
        keyboard,
        one_time_keyboard=True,
        resize_keyboard=True,
        input_field_placeholder="选择收件人或输入新的收件人",
    )


# 提示信息函数
def get_reply_options_prompt(context):
    """获取回复选项提示消息"""
    email_id = context.user_data.get("reply_email_id")
    if not email_id:
        return "⚠️ 无法获取邮件信息，请重试。"

    # 获取邮件和账户信息
    email = get_email_by_id(email_id)
    account = get_email_account_by_id(email.account_id)
    subject = context.user_data.get("reply_subject", "")

    return (
        f"📤 <b>回复邮件</b>\n\n"
        f"<b>账号:</b> {html.escape(account.email)}\n"
        f"<b>主题:</b> {html.escape(subject)}\n"
        f"<b>收件人:</b> {html.escape(email.sender)}\n\n"
        f"请选择操作以继续邮件回复流程：\n"
        f"• 使用默认收件人 - 直接回复给原邮件发件人\n"
        f"• 管理收件人/抄送/密送列表 - 自定义接收者\n"
        f"• 继续编写正文 - 进入邮件正文编写\n"
        f"• 取消 - 放弃当前回复操作"
    )


def get_body_prompt(context):
    """获取正文输入提示"""
    return "📝 请输入回复邮件正文：\n\n支持Markdown格式，使用 /cancel 取消操作"


def get_body_keyboard(context):
    """获取正文输入键盘"""
    keyboard = [["❌ 取消"]]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True)


def get_manage_cc_prompt(context):
    """获取管理抄送提示"""
    current_cc = context.user_data.get("reply_cc", [])
    cc_text = ", ".join(current_cc) if current_cc else "暂无"

    return (
        f"📋 <b>管理抄送列表</b>\n\n"
        f"当前抄送: {html.escape(cc_text)}\n\n"
        f"您可以:\n"
        f"• 从下方候选列表中选择抄送人\n"
        f"• 直接输入新的抄送邮箱\n"
        f"• 输入多个抄送时用逗号分隔\n"
        f'• 选择完成后点击"确认抄送"'
    )


def get_manage_bcc_prompt(context):
    """获取管理密送提示"""
    current_bcc = context.user_data.get("reply_bcc", [])
    bcc_text = ", ".join(current_bcc) if current_bcc else "暂无"

    return (
        f"🕶 <b>管理密送列表</b>\n\n"
        f"当前密送: {html.escape(bcc_text)}\n\n"
        f"您可以:\n"
        f"• 从下方候选列表中选择密送人\n"
        f"• 直接输入新的密送邮箱\n"
        f"• 输入多个密送时用逗号分隔\n"
        f'• 选择完成后点击"确认密送"'
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
                "⚠️ 请至少添加一个收件人后再继续"
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
        recipients = context.user_data.get("reply_recipients", [])
        recipients_text = ", ".join(recipients) if recipients else "无"

        # 获取邮件主题用于显示
        subject = context.user_data.get("reply_subject", "无主题")
        email_account = context.user_data.get("reply_account_email", "未知账户")

        # 构建完整提示消息
        prompt = (
            f"👥 <b>回复邮件 - 收件人管理</b>\n\n"
            f"<b>账户:</b> {html.escape(email_account)}\n"
            f"<b>主题:</b> {html.escape(subject)}\n"
            f"<b>当前收件人:</b> {html.escape(recipients_text)}\n\n"
            f"请选择操作:\n"
            f"• 管理收件人列表 - 添加或删除收件人\n"
            f"• 继续下一步 - 进入抄送管理\n"
            f"• 取消 - 放弃当前回复操作"
        )

        logger.debug(f"生成的提示消息: {prompt[:100]}...")
        return prompt
    except Exception as e:
        logger.error(f"生成收件人提示消息出错: {e}")
        # 返回一个基本提示，避免整个流程因为错误中断
        return "👥 <b>收件人管理</b>\n\n请选择是管理收件人，继续下一步，还是取消操作。"


def get_cc_prompt(context):
    """获取抄送管理提示消息"""
    cc_list = context.user_data.get("reply_cc", [])
    cc_text = ", ".join(cc_list) if cc_list else "无"

    return (
        f"📋 <b>抄送管理</b>\n\n"
        f"当前抄送: {html.escape(cc_text)}\n\n"
        f"请选择操作："
    )


def get_cc_keyboard_func(context):
    """抄送管理键盘"""
    keyboard = [
        ["管理抄送列表"],
        [TO_NEXT_STEP_TEXT, "❌ 取消"],
    ]
    return ReplyKeyboardMarkup(keyboard, resize_keyboard=True, one_time_keyboard=True)


def get_bcc_prompt(context):
    """获取密送管理提示消息"""
    bcc_list = context.user_data.get("reply_bcc", [])
    bcc_text = ", ".join(bcc_list) if bcc_list else "无"

    return (
        f"🕶 <b>密送管理</b>\n\n"
        f"当前密送: {html.escape(bcc_text)}\n\n"
        f"请选择操作："
    )


def get_bcc_keyboard_func(context):
    """密送管理键盘"""
    keyboard = [
        ["管理密送列表"],
        [TO_NEXT_STEP_TEXT, "❌ 取消"],
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
                "抱歉，找不到该邮件或已被删除。", show_alert=True
            )
            return ConversationHandler.END

        # 获取账户信息
        account = get_email_account_by_id(email.account_id)
        if not account:
            logger.warning(f"找不到对应的邮箱账户ID: {email.account_id}")
            await update.callback_query.answer(
                "抱歉，找不到对应的邮箱账户或账户已被删除。", show_alert=True
            )
            return ConversationHandler.END

        # 存储邮件和账户信息
        context.user_data["reply_email_id"] = email_id
        context.user_data["reply_account_id"] = email.account_id
        context.user_data["reply_account_email"] = account.email
        logger.debug(f"设置回复账户: {account.email}")

        # 处理回复主题(添加Re:前缀)
        subject = email.subject
        if not subject.lower().startswith("re:"):
            subject = f"Re: {subject}"
        context.user_data["reply_subject"] = subject
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

        context.user_data["reply_default_recipient"] = email.sender
        # 确保收件人列表是有效的列表类型
        recipients_list = [email.sender]  # 默认收件人
        context.user_data["reply_recipients"] = recipients_list
        logger.debug(f"设置默认收件人: {email.sender}")
        logger.debug(f"初始收件人列表: {recipients_list}")

        # 记录数据类型，辅助调试
        logger.debug(
            f"收件人列表类型: {type(context.user_data.get('reply_recipients'))}"
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
                    context.user_data["reply_cc"] = cc_list
                    logger.debug(f"设置抄送列表: {cc_list}")
                else:
                    context.user_data["reply_cc"] = []

                # 默认密送为空
                context.user_data["reply_bcc"] = []

        except Exception as e:
            logger.error(f"解析收件人时出错: {e}")
            logger.exception(e)  # 记录完整异常信息

        # 存储候选人列表
        context.user_data["reply_candidates"] = candidates
        logger.debug(f"候选人列表: {candidates}")

        # 初始化附件列表
        context.user_data["reply_attachments"] = []

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
                "正在准备邮件回复，请稍候...", disable_notification=True
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
                "处理回复邮件时出错，请稍后重试。", show_alert=True
            )
        except Exception:
            pass  # 忽略二次错误
        return ConversationHandler.END


def get_reply_handler():
    """获取回复邮件的处理器"""
    try:
        # 配置按钮入口点
        logger.debug("添加按钮入口点: ^reply_email_")
        reply_chain.add_button_entry_point(start_reply, "^reply_email_")

        logger.debug("构建回复邮件处理器 - 开始添加步骤")

        # 配置主链步骤

        # 第一步：收件人管理（包含子链入口）
        logger.debug("添加第一步：收件人管理")

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

        logger.debug("配置收件人管理子链步骤")
        recipients_chain.add_step(
            name="manage_recipients",
            handler_func=handle_sub_recipients,
            prompt_func=get_sub_recipients_prompt,
            keyboard_func=get_sub_recipients_keyboard,
            validator=validate_sub_recipients,
            filter_type="TEXT",
            end_keywords=[CONFIRM_RECIPIENT_TEXT],
        )

        # 第四步：正文编写（使用common_steps中的模板）
        logger.debug("添加第四步：正文编写")
        reply_chain.add_step_from_template(email_body_step(reply_chain))

        # 第五步：处理附件（使用common_steps中的模板）
        logger.debug("添加第五步：附件处理")
        reply_chain.add_step_from_template(attachment_step(reply_chain))

        # 第六步：确认发送（使用common_steps中的模板）
        logger.debug("添加第六步：确认发送")
        reply_chain.add_step_from_template(confirm_send_step(reply_chain))

        # 第七步：获取发送结果（使用common_steps中的模板）
        logger.debug("添加第七步：获取发送结果")
        reply_chain.add_step_from_template(fetch_sent_email_step(reply_chain))

        # 配置子链步骤
        # 收件人子链

        # 构建并返回处理器
        conversation_handler = reply_chain.build()

        if conversation_handler:
            logger.info("会话处理器构建成功！")

            # 检查处理器的关键属性
            logger.debug(f"处理器入口点数量: {len(conversation_handler.entry_points)}")
            logger.debug(f"处理器状态数量: {len(conversation_handler.states)}")
            logger.debug(f"处理器回退处理器数量: {len(conversation_handler.fallbacks)}")

            # 验证是否设置了回复按钮处理
            has_reply_handler = False
            for entry_point in conversation_handler.entry_points:
                if hasattr(entry_point, "pattern") and "reply_email" in str(
                    entry_point.pattern
                ):
                    has_reply_handler = True
                    logger.debug(f"找到回复邮件处理器: {entry_point.pattern}")
                    break

            if not has_reply_handler:
                logger.error("严重错误: 未找到reply_email处理入口点!")
        else:
            logger.error("严重错误: 会话处理器构建失败，返回None!")

        logger.info("========== 回复邮件处理器构建完成 ==========")
        return conversation_handler
    except Exception as e:
        logger.error(f"构建回复邮件处理器时出错: {e}")
        logger.exception(e)
        raise


# 收件人子链
def _get_current_recipients(context):
    """安全获取当前收件人列表"""
    current_recipients = context.user_data.get("reply_recipients", [])

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
        context.user_data["reply_recipients"] = current_recipients

    return current_recipients


def get_sub_recipients_prompt(context):
    """获取收件人列表管理提示"""
    try:
        # 获取当前收件人列表，记录详细信息 - 使用安全获取函数
        recipients = _get_current_recipients(context)
        logger.debug(f"当前收件人列表(从context.user_data获取): {recipients}")

        recipients_text = ", ".join(recipients) if recipients else "暂无"

        # 获取邮件信息 - 需确保这些数据在主链和子链之间共享
        subject = context.user_data.get("reply_subject", "无主题")
        email_account = context.user_data.get("reply_account_email", "未知账户")
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
        candidates = context.user_data.get("reply_candidates", {})
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
        context.user_data["reply_recipients"] = [*current_recipients, *email_list]
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
            context.user_data["reply_recipients"] = current_recipients
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
