"""
Main entry point for TelegramMail application.
"""
import logging
import os
import sys
from pathlib import Path
from dotenv import load_dotenv

from app.bot_async import run_polling
from app.database.models import init_db

# 设置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.INFO
)
logger = logging.getLogger(__name__)

def main():
    """主函数，启动Telegram Bot应用程序"""
    # 加载环境变量
    load_dotenv()
    
    # 获取Telegram Bot令牌
    token = os.getenv("TELEGRAM_BOT_TOKEN")
    if not token:
        logger.error("未设置TELEGRAM_BOT_TOKEN环境变量")
        sys.exit(1)
    
    # 初始化数据库
    logger.info("检查并初始化数据库...")
    try:
        init_db()
        logger.info("数据库初始化完成")
    except Exception as e:
        logger.error(f"初始化数据库时发生错误: {e}")
        sys.exit(1)
    
    try:
        # 运行机器人（轮询模式）
        run_polling(token)
    except Exception as e:
        logger.error(f"运行机器人时发生错误: {e}")
        sys.exit(1)

if __name__ == "__main__":
    main() 