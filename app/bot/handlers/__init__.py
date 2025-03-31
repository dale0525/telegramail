"""
Telegram Bot handlers package.
This module exports all handler functions from submodules.
"""

# 从各个模块导入所有处理函数
from .settings import *

# 删除 email_reply 导入
# from .email_forward import *
from .email_delete import *
from .account import *
from .email_compose import *
from .commands import *
from .utils import *

# 导出所有公共函数
__all__ = [
    # Settings handlers
    "handle_settings_callback",
    "handle_notification_settings",
    "handle_account_settings",
    "handle_display_settings",
    "handle_privacy_settings",
    "back_to_settings_menu",
    # 删除 Email reply handlers 部分
    # Email forward handlers - 移除
    # 'handle_forward_email',
    # 'forward_command_handler',
    # Email delete handlers
    "handle_delete_email",
    "handle_delete_confirmation",
    "handle_delete_cancellation",
    "_clear_delete_context",
    # Account handlers
    "handle_add_account_callback",
    "handle_enter_email",
    "handle_enter_name",
    "handle_enter_username",
    "handle_enter_password",
    "test_account_connection",
    "handle_account_conversation",
    "handle_cancel_account",
    "handle_delete_account",
    "handle_confirm_delete_account",
    "handle_cancel_delete_account",
    # 新的Compose handlers (使用ConversationChain)
    "get_compose_handler",
    "process_attachment",
    "send_composed_email",
    # Command handlers
    "start_command",
    "help_command",
    "accounts_command",
    "check_command",
    "settings_callback",
    "addaccount_command",
    "reply_command_handler",
    # Utility handlers
    "delete_message",
    "delete_last_step_messages",
    "clean_compose_messages",
    "delayed_clean_compose_messages",
    "delayed_delete_message",
    "_check_media_group_completion",
]
