"""
邮件监听和轮询模块，用于定期检查IMAP邮箱中的新邮件。
"""
import asyncio
import logging
import time
from datetime import datetime
from typing import Dict, List, Set, Optional, Tuple

from app.database.models import EmailAccount
from app.database.operations import get_all_active_accounts, save_email_metadata, get_message_by_message_id
from app.email.imap_client import IMAPClient

# 配置日志
logger = logging.getLogger(__name__)

class EmailMonitor:
    """
    邮件监听器，用于监控IMAP邮箱中的新邮件。
    """
    
    def __init__(self, polling_interval: int = 60):
        """
        初始化邮件监听器。
        
        Args:
            polling_interval: 轮询间隔（秒）
        """
        self.polling_interval = polling_interval
        self.last_check_time: Dict[int, datetime] = {}  # 账户ID -> 上次检查时间
        self.seen_message_ids: Dict[int, Set[str]] = {}  # 账户ID -> 已见消息ID集合
        self.running = False
        self.task = None
    
    async def start(self):
        """
        启动邮件监听器。
        """
        if self.running:
            logger.warning("邮件监听器已在运行中")
            return
        
        self.running = True
        self.task = asyncio.create_task(self._polling_loop())
        logger.info("邮件监听器已启动")
    
    async def stop(self):
        """
        停止邮件监听器。
        """
        if not self.running:
            logger.warning("邮件监听器未在运行")
            return
        
        self.running = False
        if self.task:
            self.task.cancel()
            try:
                await self.task
            except asyncio.CancelledError:
                pass
        logger.info("邮件监听器已停止")
    
    async def _polling_loop(self):
        """
        邮件轮询循环。
        """
        logger.info("邮件轮询循环已启动")
        while self.running:
            try:
                logger.info("开始轮询检查邮件")
                await self._check_all_accounts()
            except Exception as e:
                logger.error(f"检查邮件账户时发生错误: {e}")
            
            # 等待下一次轮询
            logger.info(f"等待 {self.polling_interval} 秒后进行下一次轮询")
            await asyncio.sleep(self.polling_interval)
    
    async def _check_all_accounts(self):
        """
        检查所有活跃邮件账户的新邮件。
        """
        accounts = get_all_active_accounts()
        account_count = len(accounts)
        logger.info(f"检查 {account_count} 个活跃邮件账户的新邮件")
        
        if account_count == 0:
            logger.warning("没有找到活跃的邮件账户！")
            return
        
        for account in accounts:
            logger.info(f"正在检查账户：{account.email}")
            try:
                new_emails = await self._check_account(account)
                if new_emails:
                    count = len(new_emails)
                    logger.info(f"账户 {account.email} 发现 {count} 封新邮件")
                    # 处理新邮件（保存到数据库并发送通知）
                    await self._process_new_emails(account, new_emails)
                else:
                    logger.info(f"账户 {account.email} 没有发现新邮件")
            except Exception as e:
                logger.error(f"检查账户 {account.email} 时发生错误: {e}")
    
    async def _check_account(self, account: EmailAccount) -> List[Dict]:
        """
        检查单个邮件账户的新邮件。
        
        Args:
            account: 邮件账户对象
            
        Returns:
            新邮件列表
        """
        # 初始化账户的数据结构（如果不存在）
        if account.id not in self.last_check_time:
            self.last_check_time[account.id] = datetime.now()
            self.seen_message_ids[account.id] = set()
        
        # 创建IMAP客户端并连接
        client = IMAPClient(account=account)
        if not await client.connect():
            logger.error(f"无法连接到账户 {account.email} 的IMAP服务器")
            return []
        
        try:
            # 选择收件箱
            if not await client.select_mailbox():
                logger.error(f"无法选择账户 {account.email} 的收件箱")
                return []
            
            # 搜索未读邮件
            message_nums = await client.search_unseen()
            if not message_nums:
                return []
            
            new_emails = []
            for msg_num in message_nums:
                # 获取邮件
                email_data = await client.fetch_message(msg_num)
                if not email_data:
                    continue
                
                # 检查是否已处理过此邮件
                message_id = email_data.get('message_id', '')
                if message_id and message_id in self.seen_message_ids[account.id]:
                    continue
                
                # 将消息ID加入已见集合
                if message_id:
                    self.seen_message_ids[account.id].add(message_id)
                
                # 添加到新邮件列表
                new_emails.append(email_data)
            
            # 更新上次检查时间
            self.last_check_time[account.id] = datetime.now()
            
            # 对邮件进行排序，确保按照从旧到新的顺序处理
            # 这样可以先处理被引用的邮件，再处理引用别人的邮件
            if new_emails:
                new_emails.sort(key=lambda x: x.get('date', datetime.now()))
            
            return new_emails
        finally:
            # 断开连接
            client.disconnect()
    
    async def _process_new_emails(self, account: EmailAccount, emails: List[Dict]):
        """
        处理新邮件（保存到数据库并发送通知）。
        
        Args:
            account: 邮件账户对象
            emails: 新邮件列表（已按日期从旧到新排序）
        """
        from app.bot.notifications import send_email_notification
        from app.database.operations import get_message_by_message_id
        
        for email_data in emails:
            # 首先检查邮件是否已经存在于数据库中
            message_id = email_data.get('message_id', '')
            if message_id:
                existing_email = get_message_by_message_id(message_id, account.id)
                if existing_email:
                    # 如果邮件已存在，检查是否有Telegram消息ID
                    if existing_email.telegram_message_id:
                        logger.info(f"邮件已存在于数据库中且已发送过通知，跳过处理: {message_id}")
                        continue
                    else:
                        logger.info(f"邮件已存在于数据库中但未发送通知，继续处理: {message_id}")
            
            # 保存邮件元数据到数据库
            email_id = save_email_metadata(account.id, email_data)
            if not email_id:
                logger.error(f"保存邮件元数据失败: {message_id}")
                continue
                
            # 记录发现新邮件的日志
            logger.info(f"发现账户 {account.email} 的新邮件，ID: {email_id}")
            
            # 获取应用上下文并发送通知
            try:
                # 尝试从全局获取应用实例
                from app import get_bot_application
                application = get_bot_application()
                if application:
                    # 确保email_data包含所有必要信息，包括正文内容
                    body_text_len = len(email_data.get('body_text', '')) if email_data.get('body_text') else 0
                    body_html_len = len(email_data.get('body_html', '')) if email_data.get('body_html') else 0
                    logger.info(f"发送邮件通知，包含正文数据: body_text={body_text_len}字节, body_html={body_html_len}字节")
                    
                    # 传递应用上下文和完整的email_data（包含正文）
                    await send_email_notification(application, account.id, email_data, email_id)
                    logger.info(f"已发送新邮件通知，邮件ID: {email_id}")
                else:
                    logger.warning("无法发送通知：应用上下文不可用")
            except Exception as e:
                logger.error(f"发送邮件通知时发生错误: {e}")
    
    async def check_emails(self, context=None):
        """
        手动检查所有账户的新邮件，并发送通知。
        
        Args:
            context: 应用上下文，用于发送通知
        """
        accounts = get_all_active_accounts()
        account_count = len(accounts)
        logger.info(f"手动检查 {account_count} 个活跃邮件账户的新邮件")
        
        if account_count == 0:
            logger.warning("没有找到活跃的邮件账户！")
            return 0
        
        # 用于跟踪新邮件的数量
        new_email_count = 0
        
        for account in accounts:
            logger.info(f"正在检查账户：{account.email}")
            try:
                new_emails = await self._check_account(account)
                if new_emails:
                    count = len(new_emails)
                    new_email_count += count
                    logger.info(f"账户 {account.email} 发现 {count} 封新邮件")
                    
                    # 处理新邮件
                    await self._process_new_emails(account, new_emails)
                else:
                    logger.info(f"账户 {account.email} 没有发现新邮件")
            except Exception as e:
                logger.error(f"检查账户 {account.email} 时发生错误: {e}")
                
        return new_email_count


# 单例模式
_monitor_instance = None

def get_email_monitor(polling_interval: int = 60) -> EmailMonitor:
    """
    获取EmailMonitor单例。
    
    Args:
        polling_interval: 轮询间隔（秒）
        
    Returns:
        EmailMonitor实例
    """
    global _monitor_instance
    if _monitor_instance is None:
        _monitor_instance = EmailMonitor(polling_interval)
    return _monitor_instance 