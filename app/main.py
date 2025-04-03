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

# 加载环境变量
load_dotenv()

# 从配置中读取日志级别
log_level_str = os.getenv("LOG_LEVEL", "INFO")
log_level = getattr(logging, log_level_str)

# 设置日志
logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s", 
    level=logging.WARNING  # 默认级别为WARNING，这样第三方库只会显示WARNING及以上级别的日志
)

# 为应用自身的日志设置指定的级别
app_logger = logging.getLogger("app")
app_logger.setLevel(log_level)

# 为主模块设置日志记录器
logger = logging.getLogger(__name__)

# 使用过滤器来过滤掉不需要的包的日志
class PackageFilter(logging.Filter):
    """
    过滤特定包的日志
    """
    def filter(self, record):
        # 只允许app包的日志和__main__日志通过
        return record.name.startswith("app.") or record.name == "__main__" or record.name == "app"

# 添加过滤器到根日志处理器
for handler in logging.root.handlers:
    handler.addFilter(PackageFilter())

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