"""
异步启动Telegram Bot的助手模块。
"""
import asyncio
import logging
import os
import signal
from telegram import Update

from app.bot.bot import create_bot, start_email_monitor, stop_email_monitor

logger = logging.getLogger(__name__)

async def setup_bot(token):
    """
    设置机器人命令菜单
    
    Args:
        token: Telegram Bot API令牌
        
    Returns:
        配置好的TelegramMailBot实例
    """
    bot = create_bot(token)
    
    # 设置命令菜单
    commands = [
        ("start", "启动机器人"),
        ("help", "显示帮助信息"),
        ("settings", "管理设置"),
        ("check", "手动检查新邮件"),
        ("compose", "撰写新邮件"),
        ("accounts", "管理邮件账户"),
        ("addaccount", "添加新邮件账户"),
    ]
    
    # 设置命令菜单
    await bot.application.bot.set_my_commands(commands)
    return bot

def run_polling(token):
    """
    使用轮询模式运行机器人
    
    Args:
        token: Telegram Bot API令牌
    """
    logger.info("使用轮询模式启动TelegramMail机器人...")
    
    # 创建新的事件循环
    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    
    # 设置信号处理
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, lambda: loop.stop())
    
    # 创建和设置机器人
    bot = None
    
    async def setup():
        nonlocal bot
        bot = await setup_bot(token)
        await bot.application.initialize()
        await bot.application.start()
        await bot.application.updater.start_polling()
        logger.info("机器人已启动并开始轮询...")
        
        # 设置全局应用实例
        from app import set_bot_application
        set_bot_application(bot.application)
        
        # 启动邮件监听器
        logger.info("启动邮件监听器...")
        await start_email_monitor(bot.application)
    
    # 运行设置
    loop.run_until_complete(setup())
    
    try:
        # 运行事件循环直到停止
        loop.run_forever()
    except (KeyboardInterrupt, SystemExit):
        logger.info("收到退出信号，正在关闭...")
    finally:
        # 清理资源
        if bot:
            loop.run_until_complete(bot.application.updater.stop())
            
            # 停止邮件监听器
            logger.info("停止邮件监听器...")
            loop.run_until_complete(stop_email_monitor(bot.application))
            
            loop.run_until_complete(bot.application.stop())
            loop.run_until_complete(bot.application.shutdown())
        
        # 关闭事件循环
        loop.close()
        logger.info("机器人已关闭。") 