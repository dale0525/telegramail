"""
命令链条抽象模块 - 用于创建一系列问答交互的会话流程
此模块提供了一种灵活的方式来创建 Telegram 机器人中的多步骤会话交互
"""

import asyncio
import logging
from typing import Dict, List, Any, Callable, Optional, Union, Tuple
from telegram import (
    Update,
    Message,
    ReplyKeyboardMarkup,
    ReplyKeyboardRemove,
    ForceReply,
)
from telegram.ext import (
    ContextTypes,
    ConversationHandler,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    filters,
)
import traceback

# 配置日志
logger = logging.getLogger(__name__)


class ConversationStep:
    """
    会话步骤类，封装了会话中单个步骤的所有属性和行为

    这个类使得每个步骤的定义更加清晰和结构化，同时提供了更好的类型提示。
    """

    def __init__(
        self,
        name: str,
        handler_func: Callable,
        validator: Optional[Callable] = None,
        keyboard_func: Optional[Callable] = None,
        prompt_func: Optional[Callable] = None,
        data_key: Optional[str] = None,
        filter_type: str = "TEXT",
        filter_handlers: Optional[List[Tuple]] = None,
    ):
        """
        初始化会话步骤

        Args:
            name: 步骤名称
            handler_func: 处理用户输入的函数
            validator: 验证用户输入的函数，若未提供则不做验证
            keyboard_func: 生成回复键盘的函数，若未提供则不使用键盘
            prompt_func: 生成提示消息的函数
            data_key: 存储用户响应的数据键，若未提供则使用步骤名称
            filter_type: 消息过滤器类型，可选 "TEXT", "PHOTO", "DOCUMENT", "ALL", "MEDIA", "CUSTOM"
            filter_handlers: 自定义过滤器和处理函数列表，仅当 filter_type="CUSTOM" 时使用
        """
        self.id = None  # 将在添加到ConversationChain时设置
        self.name = name
        self.handler_func = handler_func
        self.validator = validator
        self.keyboard_func = keyboard_func
        self.prompt_func = prompt_func
        self.data_key = data_key or name
        self.filter_type = filter_type
        self.filter_handlers = (
            filter_handlers if filter_type == "CUSTOM" and filter_handlers else None
        )

    def get_prompt(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """获取提示消息"""
        if self.prompt_func:
            return self.prompt_func(context)
        return f"请输入{self.name}:"

    def get_keyboard(
        self, context: ContextTypes.DEFAULT_TYPE
    ) -> Union[ReplyKeyboardMarkup, ForceReply]:
        """获取键盘"""
        if self.keyboard_func:
            return self.keyboard_func(context)
        return ForceReply(selective=True)

    def validate(
        self, user_input: Any, context: ContextTypes.DEFAULT_TYPE
    ) -> Tuple[bool, Optional[str]]:
        """验证用户输入"""
        if self.validator:
            return self.validator(user_input, context)
        return True, None

    async def handle(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: Any
    ) -> Optional[int]:
        """处理用户输入"""
        if self.handler_func:
            # 当 user_input 是 Message 对象且不等于 update.message 时，
            # 这可能是媒体组中的一条消息，需要特殊处理
            if isinstance(user_input, Message) and user_input != update.message:
                # 创建处理该消息的临时上下文
                # 此处我们不修改原始的 update 对象，而是直接把消息传给处理函数
                return await self.handler_func(update, context, user_input)
            else:
                return await self.handler_func(update, context, user_input)
        return None

    def get_filters(self) -> List[MessageHandler]:
        """获取适用于此步骤的过滤器"""
        if self.filter_type == "CUSTOM" and self.filter_handlers:
            return self.filter_handlers

        filter_map = {
            "TEXT": filters.TEXT & ~filters.COMMAND,
            "PHOTO": filters.PHOTO,
            "DOCUMENT": filters.Document.ALL,
            "MEDIA": filters.PHOTO
            | filters.Document.ALL
            | filters.VIDEO
            | filters.AUDIO,
            "ALL": filters.ALL & ~filters.COMMAND,
        }

        return filter_map.get(self.filter_type, filters.TEXT & ~filters.COMMAND)


class ConversationChain:
    """
    命令链条处理类，用于创建和管理一系列问答交互的会话

    这个类简化了创建复杂会话流程的过程，支持：
    - 多步骤会话状态流转
    - 不同类型的用户输入验证
    - 自动消息清理
    - 会话数据存储与管理
    - 媒体组处理
    """

    def __init__(
        self,
        name: str,
        command: Optional[str] = None,
        description: str = "",
        clean_messages: bool = True,
        clean_delay: int = 3,
        per_message: bool = False,
        media_wait_timeout: int = 3,  # 新增：媒体组等待超时时间（秒）
    ):
        """
        初始化会话链条

        Args:
            name: 会话名称，用于标识
            command: 启动会话的命令(不包含'/')，可选。如果为None则必须通过按钮触发
            description: 会话描述
            clean_messages: 是否在会话结束后清理消息
            clean_delay: 清理消息的延迟时间(秒)
            per_message: 是否为每条消息创建单独的会话实例
            media_wait_timeout: 媒体组等待超时时间（秒），超过此时间无新消息则视为媒体组发送完成
        """
        self.name = name
        self.command = command
        self.description = description
        self.clean_messages = clean_messages
        self.clean_delay = clean_delay
        self.per_message = per_message

        # 会话状态与处理函数
        self.states = {}
        self.entry_handler = None
        self.steps = []
        self._next_state_id = 0

        # 数据键名前缀，用于在context.user_data中存储相关数据
        self.data_prefix = f"{name}_"

        # 消息键，用于在context.user_data中存储待清理的消息ID
        self.messages_key = f"{self.data_prefix}messages"

        # 取消命令处理器
        self.fallbacks = [CommandHandler("cancel", self._cancel_handler)]

        # 媒体组处理相关
        self.media_group_key = f"{self.data_prefix}media_group"
        self.media_group_completion_handlers = {}
        self.media_group_timeout = 15  # 整体超时时间（秒）
        self.media_group_check_interval = 1  # 检查间隔（秒）
        self.media_wait_timeout = media_wait_timeout  # 无新消息等待时间（秒）

        # 按钮入口点列表
        self.button_entry_points = []

    def add_entry_point(self, handler_func: Callable, is_button_handler: bool = False):
        """
        添加会话入口点处理函数

        Args:
            handler_func: 处理会话启动命令的函数
            is_button_handler: 是否为处理按钮点击的函数，如果是则会传递按钮ID
        """
        if is_button_handler:

            async def wrapped_handler(
                update: Update, context: ContextTypes.DEFAULT_TYPE
            ):
                # 从回调查询中获取按钮ID
                query = update.callback_query
                if query:
                    button_id = query.data
                    return await handler_func(update, context, button_id)
                return await handler_func(update, context, None)

            self.entry_handler = wrapped_handler
        else:
            self.entry_handler = handler_func
        return self

    def add_step(
        self,
        name: str,
        handler_func: Callable,
        validator: Optional[Callable] = None,
        keyboard_func: Optional[Callable] = None,
        prompt_func: Optional[Callable] = None,
        data_key: Optional[str] = None,
        filter_type: str = "TEXT",
        filter_handlers: Optional[List[Tuple]] = None,
    ):
        """
        添加会话步骤

        Args:
            name: 步骤名称
            handler_func: 处理用户输入的函数
            validator: 验证用户输入的函数，若未提供则不做验证
            keyboard_func: 生成回复键盘的函数，若未提供则不使用键盘
            prompt_func: 生成提示消息的函数
            data_key: 存储用户响应的数据键，若未提供则使用步骤名称
            filter_type: 消息过滤器类型，可选 "TEXT", "PHOTO", "DOCUMENT", "ALL", "MEDIA", "CUSTOM"
            filter_handlers: 自定义过滤器和处理函数列表，仅当 filter_type="CUSTOM" 时使用
        """
        step = ConversationStep(
            name=name,
            handler_func=handler_func,
            validator=validator,
            keyboard_func=keyboard_func,
            prompt_func=prompt_func,
            data_key=data_key,
            filter_type=filter_type,
            filter_handlers=filter_handlers,
        )

        # 设置步骤ID
        step.id = self._next_state_id
        self._next_state_id += 1

        self.steps.append(step)
        return self

    def add_step_from_template(self, template: ConversationStep, **overrides):
        """
        从模板添加会话步骤，允许覆盖特定属性

        Args:
            template: 步骤模板
            **overrides: 要覆盖的属性
        """
        # 创建模板的浅拷贝
        step = ConversationStep(
            name=template.name,
            handler_func=template.handler_func,
            validator=template.validator,
            keyboard_func=template.keyboard_func,
            prompt_func=template.prompt_func,
            data_key=template.data_key,
            filter_type=template.filter_type,
            filter_handlers=template.filter_handlers,
        )

        # 应用覆盖
        for key, value in overrides.items():
            if hasattr(step, key):
                setattr(step, key, value)

        # 设置步骤ID
        step.id = self._next_state_id
        self._next_state_id += 1

        self.steps.append(step)
        return self

    def create_step_template(
        self,
        name: str,
        handler_func: Optional[Callable] = None,
        validator: Optional[Callable] = None,
        keyboard_func: Optional[Callable] = None,
        prompt_func: Optional[Callable] = None,
        data_key: Optional[str] = None,
        filter_type: str = "TEXT",
        filter_handlers: Optional[List[Tuple]] = None,
    ) -> ConversationStep:
        """
        创建步骤模板，但不添加到链条中

        Args:
            name: 步骤名称
            handler_func: 处理用户输入的函数
            validator: 验证用户输入的函数
            keyboard_func: 生成回复键盘的函数
            prompt_func: 生成提示消息的函数
            data_key: 存储用户响应的数据键
            filter_type: 消息过滤器类型
            filter_handlers: 自定义过滤器和处理函数列表

        Returns:
            ConversationStep: 步骤模板
        """
        return ConversationStep(
            name=name,
            handler_func=handler_func,
            validator=validator,
            keyboard_func=keyboard_func,
            prompt_func=prompt_func,
            data_key=data_key,
            filter_type=filter_type,
            filter_handlers=filter_handlers,
        )

    async def _clean_messages(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int):
        """清理会话过程中产生的所有消息"""
        if not self.clean_messages:
            return

        message_ids = context.user_data.get(self.messages_key, [])
        if not message_ids:
            return

        try:
            # 尝试批量删除消息
            await context.bot.delete_messages(chat_id=chat_id, message_ids=message_ids)
        except Exception as e:
            logger.error(f"批量删除消息失败: {e}，尝试逐个删除")
            # 如果批量删除失败，改为逐个删除
            for msg_id in message_ids:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=msg_id)
                except Exception as inner_e:
                    logger.error(f"删除消息(ID: {msg_id})失败: {inner_e}")

        # 清理完成后移除消息ID列表
        if self.messages_key in context.user_data:
            del context.user_data[self.messages_key]

    async def _delayed_clean_messages(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, delay: int = None
    ):
        """延迟一段时间后清理消息"""
        delay = delay or self.clean_delay
        await asyncio.sleep(delay)
        await self._clean_messages(context, chat_id)

    async def _ensure_message_cleanup(
        self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, next_state: int
    ) -> int:
        """确保在返回 ConversationHandler.END 时触发消息清理"""
        if next_state == ConversationHandler.END:
            # 设置延迟清理任务
            asyncio.create_task(self._delayed_clean_messages(context, chat_id))
        return next_state

    async def _record_message(
        self, context: ContextTypes.DEFAULT_TYPE, message: Message
    ):
        """记录消息ID以便后续清理"""
        if not self.clean_messages:
            return

        if self.messages_key not in context.user_data:
            context.user_data[self.messages_key] = []

        context.user_data[self.messages_key].append(message.message_id)

    async def _entry_point_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """默认的入口点处理函数"""
        # 初始化消息列表
        context.user_data[self.messages_key] = []

        # 记录用户命令消息
        await self._record_message(context, update.message)

        # 如果有自定义入口函数，则调用它
        if self.entry_handler:
            next_state = await self.entry_handler(update, context)
            if next_state is not None:
                chat_id = update.effective_chat.id
                return await self._ensure_message_cleanup(context, chat_id, next_state)

        # 使用默认行为 - 进入第一个步骤
        if not self.steps:
            logger.error(f"会话链条 '{self.name}' 没有定义任何步骤!")
            # 使用end_conversation结束会话
            return await self.end_conversation(update, context)

        first_step = self.steps[0]

        # 创建提示消息
        prompt_text = first_step.get_prompt(context)

        # 创建键盘
        keyboard = first_step.get_keyboard(context)

        # 发送提示消息
        message = await update.message.reply_text(
            prompt_text, reply_markup=keyboard, disable_notification=True
        )

        # 记录消息ID
        await self._record_message(context, message)

        return first_step.id

    async def _cancel_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE
    ) -> int:
        """取消对话的处理函数"""
        # 记录用户的取消命令消息
        await self._record_message(context, update.message)

        # 清理会话数据(保留消息ID列表)
        for step in self.steps:
            data_key = f"{self.data_prefix}{step.data_key}"
            if data_key in context.user_data:
                del context.user_data[data_key]

        # 使用end_conversation结束会话并发送取消消息
        return await self.end_conversation(
            update, context, message=f"❌ 已取消{self.description or '操作'}。"
        )

    async def _step_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, step_index: int
    ) -> int:
        """处理单个步骤的用户输入"""
        user_input = update.message.text if update.message.text else update.message
        chat_id = update.effective_chat.id

        # 检查是否是媒体组的一部分
        if hasattr(update.message, "media_group_id") and update.message.media_group_id:
            # 如果是媒体组，处理媒体组文件
            media_group_id = update.message.media_group_id
            return await self._handle_media_group(
                update, context, step_index, media_group_id
            )

        # 添加日志
        logger.debug(
            f"[{self.name}] 处理步骤 {step_index}: {self.steps[step_index].name}, 用户输入: {user_input}"
        )

        # 记录用户的消息
        await self._record_message(context, update.message)

        current_step = self.steps[step_index]
        next_step_index = step_index + 1

        # 检查用户是否取消操作 (虽然有专门的取消处理器，但用户也可能直接回复"取消")
        if isinstance(user_input, str) and (
            user_input.lower() == "❌ 取消" or user_input.lower() == "/cancel"
        ):
            logger.debug(f"[{self.name}] 用户选择取消操作")
            return await self._cancel_handler(update, context)

        # 验证用户输入
        is_valid, error_message = current_step.validate(user_input, context)
        if not is_valid:
            # 发送错误消息
            logger.debug(f"[{self.name}] 用户输入无效: {error_message}")
            error_msg = await update.message.reply_text(
                error_message or f"❌ 无效的{current_step.name}，请重新输入。",
                reply_markup=ForceReply(selective=True),
                disable_notification=True,
            )

            # 记录错误消息
            await self._record_message(context, error_msg)

            # 保持在当前状态
            return current_step.id

        # 存储用户的有效输入
        data_key = f"{self.data_prefix}{current_step.data_key}"
        context.user_data[data_key] = user_input
        logger.debug(f"[{self.name}] 存储用户输入 '{data_key}': {user_input}")

        # 如果有自定义处理函数，调用它
        logger.debug(f"[{self.name}] 调用步骤处理函数: {current_step.name}")
        next_state = await current_step.handle(update, context, user_input)
        if next_state is not None:
            logger.debug(f"[{self.name}] 步骤处理函数返回了自定义状态: {next_state}")
            return await self._ensure_message_cleanup(context, chat_id, next_state)

        # 检查是否还有下一步
        if next_step_index >= len(self.steps):
            # 如果没有下一步，结束对话
            logger.debug(f"[{self.name}] 没有更多步骤，结束对话")
            return await self.end_conversation(update, context)

        # 准备下一步
        next_step = self.steps[next_step_index]
        logger.debug(f"[{self.name}] 准备下一步: {next_step.name}")

        # 创建提示消息
        prompt_text = next_step.get_prompt(context)

        # 创建键盘
        keyboard = next_step.get_keyboard(context)

        # 发送提示消息
        logger.debug(f"[{self.name}] 发送下一步提示: {prompt_text[:50]}...")
        message = await update.message.reply_text(
            prompt_text, reply_markup=keyboard, disable_notification=True
        )

        # 记录消息ID
        await self._record_message(context, message)

        logger.debug(f"[{self.name}] 转入下一步: {next_step.name} (ID: {next_step.id})")
        return next_step.id

    async def _handle_media_group(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        step_index: int,
        media_group_id: str,
    ) -> int:
        """处理媒体组消息"""
        chat_id = update.effective_chat.id
        current_step = self.steps[step_index]

        # 记录用户的消息
        await self._record_message(context, update.message)

        # 初始化媒体组信息
        if self.media_group_key not in context.user_data:
            context.user_data[self.media_group_key] = {}

        # 初始化特定媒体组的信息
        if media_group_id not in context.user_data[self.media_group_key]:
            response_msg = await update.message.reply_text(
                "收到多个文件，正在处理，请稍后...",
                disable_notification=True,
                reply_markup=ReplyKeyboardRemove(),
            )
            await self._record_message(context, response_msg)

            # 首次接收到此媒体组
            processing_msg = await update.message.reply_text(
                "📤 正在接收媒体组，请等待所有文件上传完成...",
                disable_notification=True,
            )

            # 记录处理消息
            await self._record_message(context, processing_msg)

            # 创建媒体组信息
            context.user_data[self.media_group_key][media_group_id] = {
                "step_index": step_index,
                "messages": [update.message],
                "processing_msg": processing_msg,
                "last_update_time": asyncio.get_event_loop().time(),
                "completed": False,
                "next_step_triggered": False,  # 标记是否已触发下一步
            }

            # 创建处理媒体组的任务
            context.user_data[f"{self.data_prefix}media_processing"] = True
            task = asyncio.create_task(
                self._process_media_group(update, context, media_group_id)
            )

            # 存储任务对象，便于后续引用
            context.user_data[f"{self.data_prefix}media_task"] = task
            logger.debug(f"[{self.name}] 开始接收媒体组 {media_group_id}")
        else:
            # 继续接收同一媒体组的消息
            media_group_data = context.user_data[self.media_group_key][media_group_id]
            media_group_data["messages"].append(update.message)
            media_group_data["last_update_time"] = asyncio.get_event_loop().time()

            # 更新处理消息，显示已接收的文件数量
            try:
                processing_msg = media_group_data["processing_msg"]
                count = len(media_group_data["messages"])
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_msg.message_id,
                    text=f"📤 正在接收媒体组，已接收 {count} 个文件...",
                )
            except Exception as e:
                logger.error(f"更新媒体组接收消息失败: {e}")

            logger.debug(
                f"[{self.name}] 继续接收媒体组 {media_group_id}，当前共 {count} 个文件"
            )

        # 创建一个特殊状态ID，表示正在处理媒体
        media_processing_state = f"MEDIA_PROCESSING_{current_step.id}"

        # 将这个特殊状态与当前步骤关联起来
        if not hasattr(self, "_media_processing_states"):
            self._media_processing_states = {}
        self._media_processing_states[media_processing_state] = current_step.id

        # 返回媒体处理状态，保持在当前步骤
        return media_processing_state

    async def _process_media_group(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, media_group_id: str
    ):
        """处理媒体组的异步任务，等待所有媒体文件接收完成后处理"""
        try:
            # 初始等待以确保Telegram有时间发送媒体组的所有文件
            await asyncio.sleep(1.0)

            media_group_data = context.user_data[self.media_group_key][media_group_id]
            step_index = media_group_data["step_index"]
            current_step = self.steps[step_index]
            chat_id = update.effective_chat.id

            # 等待直到超时，或者检测到媒体组完成（一段时间内没有新文件）
            start_time = asyncio.get_event_loop().time()
            completed_wait = False

            while (
                asyncio.get_event_loop().time() - start_time < self.media_group_timeout
            ):
                # 计算自上次接收文件以来的时间
                time_since_last_update = (
                    asyncio.get_event_loop().time()
                    - media_group_data["last_update_time"]
                )

                # 如果在规定时间内没有新的消息，认为媒体组已完成
                if time_since_last_update >= self.media_wait_timeout:
                    logger.debug(
                        f"[{self.name}] 媒体组 {media_group_id} 超过 {self.media_wait_timeout}秒 未收到新消息，视为完成"
                    )
                    media_group_data["completed"] = True
                    completed_wait = True
                    break

                await asyncio.sleep(self.media_group_check_interval)
                logger.debug(
                    f"[{self.name}] 等待媒体组 {media_group_id} 完成，已等待 {time_since_last_update:.1f} 秒"
                )

            # 处理超时情况
            if not completed_wait:
                logger.warning(
                    f"[{self.name}] 媒体组 {media_group_id} 等待超时，强制完成处理"
                )
                media_group_data["completed"] = True

            # 更新处理消息，提示正在处理
            processing_msg = media_group_data["processing_msg"]
            messages_count = len(media_group_data["messages"])
            try:
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_msg.message_id,
                    text=f"⏳ 正在处理 {messages_count} 个媒体文件...",
                )
            except Exception as e:
                logger.error(f"更新媒体组处理消息失败: {e}")

            # 获取所有媒体消息并处理
            messages = media_group_data["messages"]
            custom_states = []
            logger.debug(
                f"[{self.name}] 开始处理媒体组 {media_group_id} 中的 {len(messages)} 个消息"
            )

            # 创建消息处理结果跟踪
            processed_files = []

            # 处理每个媒体文件
            for i, msg in enumerate(messages):
                # 提取文件类型和ID信息用于日志和跟踪
                file_type = "未知类型"
                file_id = None

                if hasattr(msg, "photo") and msg.photo:
                    file_type = "照片"
                    file_id = msg.photo[-1].file_id if msg.photo else None
                elif hasattr(msg, "document") and msg.document:
                    file_type = f"文档({msg.document.mime_type})"
                    file_id = msg.document.file_id if msg.document else None
                elif hasattr(msg, "video") and msg.video:
                    file_type = "视频"
                    file_id = msg.video.file_id if msg.video else None
                elif hasattr(msg, "audio") and msg.audio:
                    file_type = "音频"
                    file_id = msg.audio.file_id if msg.audio else None

                logger.debug(
                    f"[{self.name}] 处理媒体组中的第 {i+1}/{len(messages)} 个文件，类型: {file_type}, ID: {file_id}"
                )

                # 创建一个临时更新对象，将当前消息设置为update.message
                # 这很重要，因为需要确保每个消息都被正确处理，而不是重复处理第一个消息
                temp_update = Update(update.update_id, message=msg)

                # 记录在跟踪列表中
                processed_files.append(
                    {"index": i + 1, "type": file_type, "file_id": file_id}
                )

                # 调用步骤的处理函数，传入临时更新对象和原始消息
                logger.debug(
                    f"[{self.name}] 调用处理函数处理第 {i+1} 个文件: {file_id}"
                )
                next_state = await current_step.handle(temp_update, context, msg)
                if next_state is not None and next_state != current_step.id:
                    # 如果处理函数返回了不同的状态，记录它
                    logger.debug(
                        f"[{self.name}] 文件 {i+1} 的处理函数返回了状态: {next_state}"
                    )
                    custom_states.append(next_state)
                else:
                    logger.debug(f"[{self.name}] 文件 {i+1} 处理完成")

            # 处理完成后更新消息
            try:
                # 获取附件统计信息（如果有的话）
                attachments_info = ""
                attachments = context.user_data.get("compose_attachments", [])
                if attachments:
                    total_size = sum(
                        len(att.get("content", b"")) for att in attachments
                    )
                    total_size_mb = total_size / (1024 * 1024)
                    attachments_info = f"\n共添加 {len(attachments)} 个附件，总大小: {total_size_mb:.2f} MB"

                # 更新处理消息，显示处理结果
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_msg.message_id,
                    text=f"✅ 已处理 {len(messages)} 个媒体文件{attachments_info}",
                )

                # 记录处理的文件详情
                file_details = "\n".join(
                    [
                        f"{f['index']}. {f['type']} ({f['file_id'][:10]}...)"
                        for f in processed_files[:3]
                    ]
                )
                if len(processed_files) > 3:
                    file_details += f"\n...及其他 {len(processed_files) - 3} 个文件"
                logger.debug(f"[{self.name}] 媒体组处理完成，文件详情:\n{file_details}")

            except Exception as e:
                logger.error(f"更新媒体组结果消息失败: {e}")
                logger.error(traceback.format_exc())

            # 确定下一个状态
            if custom_states:
                # 有自定义状态，使用最后一个
                next_state = custom_states[-1]
                logger.debug(f"[{self.name}] 使用自定义状态: {next_state}")
            else:
                # 检查是否还有下一步
                next_step_index = step_index + 1
                if next_step_index < len(self.steps):
                    # 有下一步，准备下一步
                    next_step = self.steps[next_step_index]
                    logger.debug(
                        f"[{self.name}] 媒体组处理完成，准备下一步: {next_step.name}"
                    )

                    # 创建提示消息
                    prompt_text = next_step.get_prompt(context)

                    # 创建键盘
                    keyboard = next_step.get_keyboard(context)

                    # 发送提示消息
                    message = await context.bot.send_message(
                        chat_id=chat_id,
                        text=prompt_text,
                        reply_markup=keyboard,
                        disable_notification=True,
                    )

                    # 记录消息ID
                    if self.messages_key in context.user_data:
                        context.user_data[self.messages_key].append(message.message_id)

                    # 使用下一步状态
                    next_state = next_step.id
                    logger.info(
                        f"[{self.name}] 媒体组处理完成，进入下一步: {next_step.name} (ID: {next_step.id})"
                    )
                else:
                    # 没有下一步，准备结束会话
                    logger.info(f"[{self.name}] 媒体组处理完成，没有更多步骤，结束会话")
                    # 发送结束消息
                    end_msg = await context.bot.send_message(
                        chat_id=chat_id,
                        text=f"✅ {self.description or '操作'}已完成。",
                        reply_markup=ReplyKeyboardRemove(),
                        disable_notification=True,
                    )

                    # 记录消息ID
                    if self.messages_key in context.user_data:
                        context.user_data[self.messages_key].append(end_msg.message_id)

                    # 使用结束状态
                    next_state = ConversationHandler.END

            # 存储下一步状态
            context.user_data[f"{self.data_prefix}next_state"] = next_state

            # 标记媒体组处理已完成，并已触发下一步
            media_group_data["next_step_triggered"] = True

            # 删除媒体组数据，释放内存
            if media_group_id in context.user_data[self.media_group_key]:
                del context.user_data[self.media_group_key][media_group_id]

            # 标记媒体处理已完成
            context.user_data[f"{self.data_prefix}media_processing"] = False
            logger.debug(f"[{self.name}] 媒体组 {media_group_id} 处理完成")

        except Exception as e:
            logger.error(f"处理媒体组时出错: {e}")
            logger.error(traceback.format_exc())
            # 标记媒体处理已完成，即使出错
            context.user_data[f"{self.data_prefix}media_processing"] = False
            # 尝试更新处理消息，显示错误
            try:
                processing_msg = media_group_data["processing_msg"]
                await context.bot.edit_message_text(
                    chat_id=chat_id,
                    message_id=processing_msg.message_id,
                    text=f"❌ 处理媒体文件时出错: {str(e)[:50]}...",
                )
            except Exception:
                pass

    async def _media_processing_handler(
        self, update: Update, context: ContextTypes.DEFAULT_TYPE, step_id: int
    ) -> int:
        """处理媒体组处理期间收到的消息"""
        # 记录消息
        await self._record_message(context, update.message)

        # 提取消息信息用于日志
        message_text = (
            update.message.text if hasattr(update.message, "text") else "非文本消息"
        )
        logger.debug(f"[{self.name}] 媒体处理期间接收到消息: {message_text}")

        # 检查是否是取消命令
        if (
            isinstance(message_text, str) 
            and (message_text.lower() == "❌ 取消" or message_text.lower() == "/cancel")
        ):
            logger.info(f"[{self.name}] 用户在媒体处理期间取消操作: {message_text}")
            # 移除媒体处理标记
            if f"{self.data_prefix}media_processing" in context.user_data:
                del context.user_data[f"{self.data_prefix}media_processing"]
            
            # 移除媒体任务
            if f"{self.data_prefix}media_task" in context.user_data:
                task = context.user_data[f"{self.data_prefix}media_task"]
                if not task.done():
                    task.cancel()
                del context.user_data[f"{self.data_prefix}media_task"]
            
            # 清空媒体组数据
            if self.media_group_key in context.user_data:
                context.user_data[self.media_group_key] = {}
            
            # 使用取消处理程序结束会话
            return await self._cancel_handler(update, context)

        # 检查是否有媒体组ID (可能是另一个媒体组的消息)
        if hasattr(update.message, "media_group_id") and update.message.media_group_id:
            media_group_id = update.message.media_group_id
            # 检查是否是我们正在处理的媒体组
            if (
                self.media_group_key in context.user_data
                and media_group_id in context.user_data[self.media_group_key]
            ):
                logger.debug(
                    f"[{self.name}] 收到同一媒体组的附加消息: {media_group_id}"
                )
                # 直接调用媒体组处理函数
                return await self._handle_media_group(
                    update,
                    context,
                    context.user_data[self.media_group_key][media_group_id][
                        "step_index"
                    ],
                    media_group_id,
                )

        # 检查是否已完成媒体处理
        if not context.user_data.get(f"{self.data_prefix}media_processing", False):
            # 媒体处理已完成，获取下一步状态
            next_state = context.user_data.get(f"{self.data_prefix}next_state")

            if next_state is not None:
                # 清除临时状态标记
                if f"{self.data_prefix}next_state" in context.user_data:
                    del context.user_data[f"{self.data_prefix}next_state"]
                if f"{self.data_prefix}media_processing" in context.user_data:
                    del context.user_data[f"{self.data_prefix}media_processing"]

                # 重定向到下一步状态
                logger.debug(f"[{self.name}] 媒体处理已完成，转向下一步: {next_state}")

                # 如果下一步是结束状态，直接返回
                if next_state == ConversationHandler.END:
                    return ConversationHandler.END

                # 查找下一步对应的步骤对象
                next_step = None
                for step in self.steps:
                    if step.id == next_state:
                        next_step = step
                        break

                if next_step:
                    # 立即处理新收到的用户输入
                    return await self._process_next_step_input(
                        update, context, next_step, next_state
                    )

                # 如果找不到下一步对象，只返回状态
                return next_state
            else:
                # 如果没有下一步状态，回到当前步骤
                logger.debug(
                    f"[{self.name}] 媒体处理已完成，但未找到下一步状态，返回当前步骤: {step_id}"
                )
                return step_id

        # 媒体处理尚未完成，发送等待消息
        wait_msg = await update.message.reply_text(
            "⏳ 正在处理媒体附件，请稍候...", disable_notification=True
        )

        # 记录消息ID
        await self._record_message(context, wait_msg)

        # 保持在媒体处理状态
        return f"MEDIA_PROCESSING_{step_id}"

    async def _process_next_step_input(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        next_step: ConversationStep,
        next_state: int,
    ) -> int:
        """处理媒体组完成后收到的用户输入"""
        # 提取用户输入
        user_input = update.message.text if update.message.text else update.message
        chat_id = update.effective_chat.id

        logger.debug(
            f"[{self.name}] 处理媒体组完成后的新用户输入: {user_input if isinstance(user_input, str) else '非文本输入'}"
        )
        
        # 检查用户是否取消操作
        if isinstance(user_input, str) and (
            user_input.lower() == "❌ 取消" or user_input.lower() == "/cancel"
        ):
            logger.debug(f"[{self.name}] 用户在媒体组完成后选择取消操作")
            return await self._cancel_handler(update, context)

        # 验证用户输入
        is_valid, error_message = next_step.validate(user_input, context)

        if not is_valid:
            # 发送错误消息
            logger.debug(f"[{self.name}] 用户输入无效: {error_message}")
            error_msg = await update.message.reply_text(
                error_message or f"❌ 无效的{next_step.name}，请重新输入。",
                reply_markup=ForceReply(selective=True),
                disable_notification=True,
            )

            # 记录错误消息
            await self._record_message(context, error_msg)

            # 返回下一步状态，让用户重试
            return next_state

        # 存储用户的有效输入
        data_key = f"{self.data_prefix}{next_step.data_key}"
        context.user_data[data_key] = user_input
        logger.debug(f"[{self.name}] 存储用户输入 '{data_key}': {user_input}")

        # 调用下一步的处理函数
        logger.debug(f"[{self.name}] 调用下一步处理函数: {next_step.name}")
        result = await next_step.handle(update, context, user_input)
        return result if result is not None else next_state

    def add_button_entry_point(self, handler_func: Callable, pattern: str):
        """
        添加按钮回调入口点

        Args:
            handler_func: 处理按钮回调的函数，接收(update, context, button_id)参数
            pattern: 回调数据模式，用于匹配按钮回调查询
        """

        # 创建一个包装函数，将回调数据传递给处理函数
        async def button_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
            # 初始化消息列表
            context.user_data[self.messages_key] = []

            # 从回调查询中获取按钮ID
            button_id = update.callback_query.data

            # 调用原始处理函数
            next_state = await handler_func(update, context, button_id)

            # 确保在返回 ConversationHandler.END 时触发消息清理
            chat_id = update.effective_chat.id
            return await self._ensure_message_cleanup(context, chat_id, next_state)

        # 将按钮处理函数和模式添加到入口点列表
        self.button_entry_points.append((button_handler, pattern))
        return self

    def build(self) -> ConversationHandler:
        """构建会话处理器"""
        # 创建会话状态映射
        states_dict = {}

        for i, step in enumerate(self.steps):
            # 为每个步骤创建一个处理函数
            handler_func = lambda update, context, step_idx=i: self._step_handler(
                update, context, step_idx
            )

            # 处理自定义过滤器
            if step.filter_type == "CUSTOM" and step.filter_handlers:
                # 直接使用传递的过滤器和处理函数列表
                handlers = []
                for filter_obj, handler in step.filter_handlers:
                    # 创建一个包装函数，以调用原始处理函数
                    async def wrapped_handler(
                        update, context, original_handler=handler, step_idx=i
                    ):
                        # 提取用户输入
                        user_input = (
                            update.message.text
                            if update.message.text
                            else update.message
                        )
                        # 首先记录消息
                        await self._record_message(context, update.message)
                        # 调用原始处理函数
                        return await original_handler(update, context, user_input)

                    handlers.append(MessageHandler(filter_obj, wrapped_handler))

                states_dict[step.id] = handlers
            else:
                # 根据过滤器类型创建消息处理器
                message_filter = step.get_filters()

                states_dict[step.id] = [MessageHandler(message_filter, handler_func)]

            # 为每个步骤添加媒体处理状态
            media_processing_state = f"MEDIA_PROCESSING_{step.id}"
            # 为媒体处理状态创建处理函数
            media_handler_func = (
                lambda update, context, step_id=step.id: self._media_processing_handler(
                    update, context, step_id
                )
            )
            # 使用与步骤相同的过滤器
            if step.filter_type == "CUSTOM" and step.filter_handlers:
                handlers = []
                for filter_obj, _ in step.filter_handlers:
                    handlers.append(MessageHandler(filter_obj, media_handler_func))
                states_dict[media_processing_state] = handlers
            else:
                message_filter = step.get_filters()
                states_dict[media_processing_state] = [
                    MessageHandler(message_filter, media_handler_func)
                ]

        # 创建entry_points列表
        entry_points = []

        # 添加命令入口点
        if self.command is not None:
            entry_points.append(CommandHandler(self.command, self._entry_point_handler))

        # 添加自定义入口点处理器
        if hasattr(self, "entry_handler") and self.entry_handler:
            entry_points.append(
                CommandHandler(
                    self.command or f"{self.name}_command", self.entry_handler
                )
            )

        # 添加按钮回调入口点
        for handler_func, pattern in self.button_entry_points:
            entry_points.append(CallbackQueryHandler(handler_func, pattern=pattern))

        # 确保至少有一种触发方式
        if not entry_points and not self.button_entry_points:
            raise ValueError(
                "必须提供command参数、设置entry_handler或添加按钮回调入口点"
            )

        # 创建会话处理器
        return ConversationHandler(
            entry_points=entry_points,
            states=states_dict,
            fallbacks=self.fallbacks,
            name=f"{self.name}_conversation",
            persistent=False,
            per_message=self.per_message,
        )

    def get_data(self, context: ContextTypes.DEFAULT_TYPE) -> Dict[str, Any]:
        """获取此会话中收集的所有数据"""
        result = {}
        for step in self.steps:
            data_key = f"{self.data_prefix}{step.data_key}"
            if data_key in context.user_data:
                result[step.data_key] = context.user_data[data_key]
        return result

    def clear_data(self, context: ContextTypes.DEFAULT_TYPE):
        """清理此会话中收集的所有数据"""
        for step in self.steps:
            data_key = f"{self.data_prefix}{step.data_key}"
            if data_key in context.user_data:
                del context.user_data[data_key]

    def wrap_with_owner_check(
        self, handler: ConversationHandler, check_owner_func: Callable
    ) -> ConversationHandler:
        """
        用权限检查函数包装会话处理器的所有入口点和状态处理函数

        Args:
            handler: ConversationHandler实例
            check_owner_func: 检查权限的函数，接收一个回调函数并返回包装后的回调函数

        Returns:
            包装后的ConversationHandler实例
        """
        # 包装入口点
        for i, entry_point in enumerate(handler.entry_points):
            if hasattr(entry_point, "callback"):
                entry_point.callback = check_owner_func(entry_point.callback)

        # 包装状态处理函数
        for state, handlers in handler.states.items():
            wrapped_handlers = []
            for handler_obj in handlers:
                if hasattr(handler_obj, "callback"):
                    handler_obj.callback = check_owner_func(handler_obj.callback)
                wrapped_handlers.append(handler_obj)
            handler.states[state] = wrapped_handlers

        # 包装回退处理函数
        for i, fallback in enumerate(handler.fallbacks):
            if hasattr(fallback, "callback"):
                fallback.callback = check_owner_func(fallback.callback)

        return handler

    async def end_conversation(
        self,
        update: Update,
        context: ContextTypes.DEFAULT_TYPE,
        message=None,
        reply_markup=None,
    ) -> int:
        """结束会话并自动清理消息

        Args:
            update: Telegram更新对象
            context: 上下文对象
            message: 结束消息，如果提供则发送
            reply_markup: 消息的回复标记，默认为移除键盘

        Returns:
            ConversationHandler.END
        """
        chat_id = update.effective_chat.id

        # 如果提供了结束消息，发送它
        if message:
            if reply_markup is None:
                reply_markup = ReplyKeyboardRemove()

            end_msg = await update.message.reply_text(
                message,
                reply_markup=reply_markup,
                disable_notification=True,
            )
            await self._record_message(context, end_msg)

        # 设置延迟清理任务
        asyncio.create_task(self._delayed_clean_messages(context, chat_id))

        return ConversationHandler.END
