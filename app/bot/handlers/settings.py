"""
Settings handlers for TelegramMail Bot.
"""
import logging
from telegram import Update, InlineKeyboardMarkup, InlineKeyboardButton
from telegram.ext import ContextTypes
from typing import Dict, Any

# 配置日志
logger = logging.getLogger(__name__)

async def handle_settings_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """
    处理设置菜单的回调查询
    """
    query = update.callback_query
    await query.answer()
    
    callback_data = query.data
    
    if callback_data == "settings_notifications":
        await handle_notification_settings(update, context)
    elif callback_data == "settings_accounts":
        await handle_account_settings(update, context)
    elif callback_data == "settings_display":
        await handle_display_settings(update, context)
    elif callback_data == "settings_privacy":
        await handle_privacy_settings(update, context)
    elif callback_data.startswith("back_to_settings"):
        await back_to_settings_menu(update, context)
    elif callback_data.startswith("delete_account_"):
        # 处理删除账户请求
        account_id = int(callback_data.split("_")[-1])
        await handle_delete_account(update, context, account_id)
    elif callback_data.startswith("confirm_delete_account_"):
        # 处理确认删除账户
        account_id = int(callback_data.split("_")[-1])
        await handle_confirm_delete_account(update, context, account_id)
    elif callback_data == "cancel_delete_account":
        # 处理取消删除账户
        await handle_cancel_delete_account(update, context)
    else:
        logger.warning(f"未知设置回调数据: {callback_data}")
        await query.edit_message_text(
            "抱歉，发生了未知错误。请使用 /settings 重新打开设置菜单。"
        )

async def handle_notification_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理通知设置"""
    query = update.callback_query
    
    # 这里应该从数据库获取用户当前的通知设置
    # 为了演示，我们假设默认值
    receive_all_emails = True
    receive_important_only = False
    silent_mode = False
    
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if receive_all_emails else '❌'} 接收所有邮件", 
                callback_data="toggle_all_emails"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if receive_important_only else '❌'} 仅重要邮件", 
                callback_data="toggle_important_emails"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if silent_mode else '❌'} 静音模式", 
                callback_data="toggle_silent_mode"
            ),
        ],
        [
            InlineKeyboardButton("« 返回", callback_data="back_to_settings"),
        ],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔔 <b>通知设置</b>\n\n"
        "配置您希望如何接收邮件通知。",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_account_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理账户设置"""
    query = update.callback_query
    
    # 这里应该从数据库获取用户的邮件账户
    # 为了演示，我们假设有一些账户
    accounts = [
        {"name": "工作邮箱", "email": "work@example.com", "active": True},
        {"name": "个人邮箱", "email": "personal@example.com", "active": False},
    ]
    
    keyboard = []
    for account in accounts:
        status = "✅ 已启用" if account["active"] else "❌ 已禁用"
        keyboard.append([
            InlineKeyboardButton(
                f"{account['name']} ({status})",
                callback_data=f"account_{account['email']}"
            )
        ])
    
    keyboard.append([InlineKeyboardButton("+ 添加新账户", callback_data="add_account")])
    keyboard.append([InlineKeyboardButton("« 返回", callback_data="back_to_settings")])
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "📧 <b>账户管理</b>\n\n"
        "管理您的邮件账户。",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_display_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理显示设置"""
    query = update.callback_query
    
    # 这里应该从数据库获取用户的显示设置
    # 为了演示，我们假设默认值
    show_previews = True
    compact_mode = False
    render_html = True
    
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if show_previews else '❌'} 显示邮件预览", 
                callback_data="toggle_previews"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if compact_mode else '❌'} 紧凑模式", 
                callback_data="toggle_compact_mode"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if render_html else '❌'} 渲染HTML", 
                callback_data="toggle_render_html"
            ),
        ],
        [
            InlineKeyboardButton("« 返回", callback_data="back_to_settings"),
        ],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🖥️ <b>显示设置</b>\n\n"
        "配置邮件如何显示。",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def handle_privacy_settings(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """处理隐私设置"""
    query = update.callback_query
    
    # 这里应该从数据库获取用户的隐私设置
    # 为了演示，我们假设默认值
    cache_emails = True
    auto_delete = False
    
    keyboard = [
        [
            InlineKeyboardButton(
                f"{'✅' if cache_emails else '❌'} 缓存邮件内容", 
                callback_data="toggle_cache"
            ),
        ],
        [
            InlineKeyboardButton(
                f"{'✅' if auto_delete else '❌'} 自动删除旧邮件", 
                callback_data="toggle_auto_delete"
            ),
        ],
        [
            InlineKeyboardButton("🗑️ 清除所有数据", callback_data="clear_all_data"),
        ],
        [
            InlineKeyboardButton("« 返回", callback_data="back_to_settings"),
        ],
    ]
    
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text(
        "🔒 <b>隐私设置</b>\n\n"
        "管理您的数据和隐私选项。",
        reply_markup=reply_markup,
        parse_mode="HTML"
    )

async def back_to_settings_menu(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """返回主设置菜单"""
    query = update.callback_query
    
    keyboard = [
        [
            InlineKeyboardButton("通知设置", callback_data="settings_notifications"),
            InlineKeyboardButton("账户管理", callback_data="settings_accounts"),
        ],
        [
            InlineKeyboardButton("显示选项", callback_data="settings_display"),
            InlineKeyboardButton("隐私设置", callback_data="settings_privacy"),
        ],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)
    await query.edit_message_text("⚙️ 请选择要管理的设置类别：", reply_markup=reply_markup)

# Note: handle_delete_account, handle_confirm_delete_account, handle_cancel_delete_account
# functions are imported from account.py to avoid circular imports
from .account import handle_delete_account, handle_confirm_delete_account, handle_cancel_delete_account
