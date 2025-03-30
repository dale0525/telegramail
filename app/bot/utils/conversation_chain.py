"""
命令链条抽象模块 - 用于创建一系列问答交互的会话流程
此模块提供了一种灵活的方式来创建 Telegram 机器人中的多步骤会话交互
"""
import asyncio
import logging
from typing import Dict, List, Any, Callable, Optional, Union, Tuple
from telegram import Update, Message, ReplyKeyboardMarkup, ReplyKeyboardRemove, ForceReply
from telegram.ext import ContextTypes, ConversationHandler, CommandHandler, MessageHandler, filters
from datetime import datetime

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
        filter_handlers: Optional[List[Tuple]] = None
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
        self.filter_handlers = filter_handlers if filter_type == "CUSTOM" and filter_handlers else None
        
    def get_prompt(self, context: ContextTypes.DEFAULT_TYPE) -> str:
        """获取提示消息"""
        if self.prompt_func:
            return self.prompt_func(context)
        return f"请输入{self.name}:"
        
    def get_keyboard(self, context: ContextTypes.DEFAULT_TYPE) -> Union[ReplyKeyboardMarkup, ForceReply]:
        """获取键盘"""
        if self.keyboard_func:
            return self.keyboard_func(context)
        return ForceReply(selective=True)
        
    def validate(self, user_input: Any, context: ContextTypes.DEFAULT_TYPE) -> Tuple[bool, Optional[str]]:
        """验证用户输入"""
        if self.validator:
            return self.validator(user_input, context)
        return True, None
        
    async def handle(self, update: Update, context: ContextTypes.DEFAULT_TYPE, user_input: Any) -> Optional[int]:
        """处理用户输入"""
        if self.handler_func:
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
            "MEDIA": filters.PHOTO | filters.Document.ALL | filters.VIDEO | filters.AUDIO,
            "ALL": filters.ALL & ~filters.COMMAND
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
        command: str,
        description: str = "",
        clean_messages: bool = True,
        clean_delay: int = 3,
        per_message: bool = False
    ):
        """
        初始化会话链条
        
        Args:
            name: 会话名称，用于标识
            command: 启动会话的命令，不包含'/'
            description: 会话描述
            clean_messages: 是否在会话结束后清理消息
            clean_delay: 清理消息的延迟时间(秒)
            per_message: 是否为每条消息创建单独的会话实例
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
    
    def add_entry_point(self, handler_func: Callable):
        """
        添加会话入口点处理函数
        
        Args:
            handler_func: 处理会话启动命令的函数
        """
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
        filter_handlers: Optional[List[Tuple]] = None
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
            filter_handlers=filter_handlers
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
            filter_handlers=template.filter_handlers
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
        filter_handlers: Optional[List[Tuple]] = None
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
            filter_handlers=filter_handlers
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
            await context.bot.delete_messages(
                chat_id=chat_id,
                message_ids=message_ids
            )
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
    
    async def _delayed_clean_messages(self, context: ContextTypes.DEFAULT_TYPE, chat_id: int, delay: int = None):
        """延迟一段时间后清理消息"""
        delay = delay or self.clean_delay
        await asyncio.sleep(delay)
        await self._clean_messages(context, chat_id)
        
    async def _record_message(self, context: ContextTypes.DEFAULT_TYPE, message: Message):
        """记录消息ID以便后续清理"""
        if not self.clean_messages:
            return
            
        if self.messages_key not in context.user_data:
            context.user_data[self.messages_key] = []
            
        context.user_data[self.messages_key].append(message.message_id)
    
    async def _entry_point_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """默认的入口点处理函数"""
        # 初始化消息列表
        context.user_data[self.messages_key] = []
        
        # 记录用户命令消息
        await self._record_message(context, update.message)
        
        # 如果有自定义入口函数，则调用它
        if self.entry_handler:
            next_state = await self.entry_handler(update, context)
            if next_state is not None:
                return next_state
                
        # 使用默认行为 - 进入第一个步骤
        if not self.steps:
            logger.error(f"会话链条 '{self.name}' 没有定义任何步骤!")
            return ConversationHandler.END
            
        first_step = self.steps[0]
        
        # 创建提示消息
        prompt_text = first_step.get_prompt(context)
        
        # 创建键盘
        keyboard = first_step.get_keyboard(context)
        
        # 发送提示消息
        message = await update.message.reply_text(
            prompt_text,
            reply_markup=keyboard,
            disable_notification=True
        )
        
        # 记录消息ID
        await self._record_message(context, message)
        
        return first_step.id
    
    async def _cancel_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
        """取消对话的处理函数"""
        chat_id = update.effective_chat.id
        
        # 记录用户的取消命令消息
        await self._record_message(context, update.message)
        
        # 发送取消确认消息
        cancel_msg = await update.message.reply_text(
            f"❌ 已取消{self.description or '操作'}。",
            reply_markup=ReplyKeyboardRemove(),
            disable_notification=True
        )
        
        # 记录取消确认消息
        await self._record_message(context, cancel_msg)
        
        # 清理会话数据(保留消息ID列表)
        for step in self.steps:
            data_key = f"{self.data_prefix}{step.data_key}"
            if data_key in context.user_data:
                del context.user_data[data_key]
        
        # 设置延迟清理任务
        asyncio.create_task(
            self._delayed_clean_messages(context, chat_id)
        )
        
        return ConversationHandler.END
    
    async def _step_handler(self, update: Update, context: ContextTypes.DEFAULT_TYPE, step_index: int) -> int:
        """处理单个步骤的用户输入"""
        user_input = update.message.text if update.message.text else update.message
        chat_id = update.effective_chat.id
        
        # 记录用户的消息
        await self._record_message(context, update.message)
        
        current_step = self.steps[step_index]
        next_step_index = step_index + 1
        
        # 检查用户是否取消操作 (虽然有专门的取消处理器，但用户也可能直接回复"取消")
        if isinstance(user_input, str) and (user_input.lower() == "❌ 取消" or user_input.lower() == "/cancel"):
            return await self._cancel_handler(update, context)
        
        # 验证用户输入
        is_valid, error_message = current_step.validate(user_input, context)
        if not is_valid:
            # 发送错误消息
            error_msg = await update.message.reply_text(
                error_message or f"❌ 无效的{current_step.name}，请重新输入。",
                reply_markup=ForceReply(selective=True),
                disable_notification=True
            )
            
            # 记录错误消息
            await self._record_message(context, error_msg)
            
            # 保持在当前状态
            return current_step.id
        
        # 存储用户的有效输入
        data_key = f"{self.data_prefix}{current_step.data_key}"
        context.user_data[data_key] = user_input
        
        # 如果有自定义处理函数，调用它
        next_state = await current_step.handle(update, context, user_input)
        if next_state is not None:
            return next_state
        
        # 检查是否还有下一步
        if next_step_index >= len(self.steps):
            # 如果没有下一步，结束对话
            # 通常应该有专门的完成处理函数，这里只是简单地结束
            return ConversationHandler.END
        
        # 准备下一步
        next_step = self.steps[next_step_index]
        
        # 创建提示消息
        prompt_text = next_step.get_prompt(context)
        
        # 创建键盘
        keyboard = next_step.get_keyboard(context)
        
        # 发送提示消息
        message = await update.message.reply_text(
            prompt_text,
            reply_markup=keyboard,
            disable_notification=True
        )
        
        # 记录消息ID
        await self._record_message(context, message)
        
        return next_step.id
    
    def build(self) -> ConversationHandler:
        """构建会话处理器"""
        # 创建会话状态映射
        states_dict = {}
        
        for i, step in enumerate(self.steps):
            # 为每个步骤创建一个处理函数
            handler_func = lambda update, context, step_idx=i: self._step_handler(update, context, step_idx)
            
            # 处理自定义过滤器
            if step.filter_type == "CUSTOM" and step.filter_handlers:
                # 直接使用传递的过滤器和处理函数列表
                handlers = []
                for filter_obj, handler in step.filter_handlers:
                    # 创建一个包装函数，以调用原始处理函数
                    async def wrapped_handler(update, context, original_handler=handler, step_idx=i):
                        # 提取用户输入
                        user_input = update.message.text if update.message.text else update.message
                        # 首先记录消息
                        await self._record_message(context, update.message)
                        # 调用原始处理函数
                        return await original_handler(update, context, user_input)
                    
                    handlers.append(MessageHandler(filter_obj, wrapped_handler))
                
                states_dict[step.id] = handlers
            else:
                # 根据过滤器类型创建消息处理器
                message_filter = step.get_filters()
                
                states_dict[step.id] = [
                    MessageHandler(message_filter, handler_func)
                ]
        
        # 创建会话处理器
        return ConversationHandler(
            entry_points=[CommandHandler(self.command, self._entry_point_handler)],
            states=states_dict,
            fallbacks=self.fallbacks,
            name=f"{self.name}_conversation",
            persistent=False,
            per_message=self.per_message
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
                
    async def check_media_group_completion(self, update: Update, context: ContextTypes.DEFAULT_TYPE, 
                                         media_group_id: str, processing_msg: Message = None,
                                         completion_callback: Callable = None):
        """
        检查媒体组是否已完成处理
        
        Args:
            update: Telegram更新对象
            context: 对话上下文
            media_group_id: 媒体组ID
            processing_msg: 处理中的状态消息
            completion_callback: 媒体组完成后的回调函数
        """
        chat_id = update.effective_chat.id
        
        # 初始化媒体组跟踪数据
        if self.media_group_key not in context.user_data:
            context.user_data[self.media_group_key] = {}
            
        if media_group_id not in context.user_data[self.media_group_key]:
            context.user_data[self.media_group_key][media_group_id] = {
                "processed_count": 0,
                "files": [],
                "last_update_time": datetime.now(),
                "completion_task": None
            }
        
        # 更新媒体组的最后更新时间
        media_group = context.user_data[self.media_group_key][media_group_id]
        media_group["last_update_time"] = datetime.now()
        
        try:
            # 等待初始延迟
            await asyncio.sleep(2.0)
            
            # 记录初始计数
            initial_count = media_group["processed_count"]
            last_count = initial_count
            
            # 检查周期
            max_checks = 5  # 最多检查5次
            for i in range(max_checks):
                # 等待一段时间后检查计数是否有变化
                await asyncio.sleep(1.0)
                
                # 获取当前计数
                if self.media_group_key not in context.user_data or media_group_id not in context.user_data[self.media_group_key]:
                    return
                    
                current_count = context.user_data[self.media_group_key][media_group_id]["processed_count"]
                
                # 如果计数增加，表示还在接收附件
                if current_count > last_count:
                    last_count = current_count
                    continue
                
                # 如果计数没有变化，且已经检查了多次，认为所有附件都已接收
                if i >= 2:  # 至少检查3次才能确定
                    logger.info(f"媒体组 {media_group_id} 所有附件已接收完毕（共{current_count}个）")
                    break
            
            # 如果有处理状态消息，先删除它
            if processing_msg:
                try:
                    await context.bot.delete_message(chat_id=chat_id, message_id=processing_msg.message_id)
                    # 从记录列表中移除，避免后续重复删除
                    if self.messages_key in context.user_data and processing_msg.message_id in context.user_data[self.messages_key]:
                        context.user_data[self.messages_key].remove(processing_msg.message_id)
                except Exception as e:
                    logger.error(f"删除处理状态消息失败: {e}")
            
            # 如果有完成回调函数，调用它
            if completion_callback:
                await completion_callback(update, context, media_group_id)
            
            # 清理媒体组状态
            if self.media_group_key in context.user_data and media_group_id in context.user_data[self.media_group_key]:
                del context.user_data[self.media_group_key][media_group_id]
                
        except asyncio.CancelledError:
            # 任务被取消，什么都不做
            pass
        except Exception as e:
            logger.error(f"检查媒体组完成时出错: {e}")
            import traceback
            logger.error(traceback.format_exc()) 