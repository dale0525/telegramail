"""
会话步骤模块 - 定义会话流程中的步骤结构和行为

此模块提供了 ConversationStep 类，封装了会话中单个步骤的所有属性和行为，
支持子链调用、关键词触发等高级功能。
"""

import logging
from typing import Dict, List, Any, Callable, Optional, Union, Tuple
from telegram import Update, Message, ReplyKeyboardMarkup, ForceReply
from telegram.ext import ContextTypes, ConversationHandler, filters

# 配置日志
logger = logging.getLogger(__name__)


class ConversationStep:
    """
    会话步骤类，封装了会话中单个步骤的所有属性和行为

    这个类使得每个步骤的定义更加清晰和结构化，同时提供了更好的类型提示。
    支持子链调用、关键词触发和结束等高级功能。
    """

    def __init__(
        self,
        name: str,
        handler_func: Callable,
        validator: Optional[Callable] = None,
        keyboard_func: Optional[Callable] = None,
        prompt_func: Optional[Callable] = None,
        filter_type: str = "TEXT",
        filter_handlers: Optional[List[Tuple]] = None,
        auto_execute: bool = False,
        sub_chain=None,  # 子链，用于在步骤中嵌套另一个会话链
        trigger_keywords: Optional[List[str]] = None,  # 触发子链的关键词列表
        end_keywords: Optional[List[str]] = None,  # 结束子链的关键词列表
    ):
        """
        初始化会话步骤

        Args:
            name: 步骤名称
            handler_func: 处理用户输入的函数
            validator: 验证用户输入的函数，若未提供则不做验证
            keyboard_func: 生成回复键盘的函数，若未提供则不使用键盘
            prompt_func: 生成提示消息的函数
            filter_type: 消息过滤器类型，可选 "TEXT", "PHOTO", "DOCUMENT", "ALL", "MEDIA", "CUSTOM"
            filter_handlers: 自定义过滤器和处理函数列表，仅当 filter_type="CUSTOM" 时使用
            auto_execute: 是否自动执行而不等待用户输入，适用于不需要用户交互的步骤
            sub_chain: 子链，用于在步骤中嵌套另一个会话链
            trigger_keywords: 触发子链的关键词列表
            end_keywords: 结束子链的关键词列表
        """
        self.id = None  # 将在添加到ConversationChain时设置
        self.name = name
        self.handler_func = handler_func
        self.validator = validator
        self.keyboard_func = keyboard_func
        self.prompt_func = prompt_func
        self.filter_type = filter_type
        self.filter_handlers = (
            filter_handlers if filter_type == "CUSTOM" and filter_handlers else None
        )
        self.auto_execute = auto_execute  # 是否自动执行而不等待用户输入

        # 子链相关属性
        self.sub_chain = sub_chain
        self.trigger_keywords = trigger_keywords or []
        self.end_keywords = end_keywords or []
        self.parent_chain = None  # 将在添加到ConversationChain时设置
        self.is_in_sub_chain = False  # 标记当前是否在子链中

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

    def get_filters(self):
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

    def check_trigger_keywords(self, user_input: str) -> bool:
        """检查用户输入是否匹配触发关键词"""
        if not self.trigger_keywords or not self.sub_chain:
            return False

        if not isinstance(user_input, str):
            return False

        return any(
            keyword.lower() == user_input.lower() for keyword in self.trigger_keywords
        )

    def check_end_keywords(self, user_input: str) -> bool:
        """检查用户输入是否匹配结束关键词"""
        if not self.end_keywords:
            return False

        if not isinstance(user_input, str):
            return False

        return any(
            keyword.lower() == user_input.lower() for keyword in self.end_keywords
        )
