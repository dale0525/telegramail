"""
IMAP client for retrieving emails.
"""
import imaplib
import email
import re
import json
import asyncio
import os
import time
import logging
from datetime import datetime, timedelta
from email.header import decode_header
from email.utils import parseaddr, parsedate_to_datetime
from typing import Dict, List, Optional, Tuple, Union
import ssl
import traceback

from app.database.models import EmailAccount
from app.utils.config import config

# 配置日志记录器
logger = logging.getLogger(__name__)

class IMAPClient:
    """
    IMAP client for retrieving emails.
    """
    
    def __init__(self, account=None, server=None, port=None, username=None, password=None, use_ssl=True):
        """
        Initialize the IMAP client.
        
        Args:
            account: Email account object (optional)
            server: IMAP server address (optional, required if account is None)
            port: IMAP server port (optional, required if account is None)
            username: Username (optional, required if account is None)
            password: Password (optional, required if account is None)
            use_ssl: Whether to use SSL (optional, required if account is None)
        """
        self.account = account
        self.server = server if account is None else account.imap_server
        self.port = port if account is None else account.imap_port
        self.username = username if account is None else account.username
        self.password = password if account is None else account.password
        self.use_ssl = use_ssl if account is None else account.imap_use_ssl
        self.conn = None
    
    async def connect(self) -> bool:
        """
        Connect to the IMAP server.
        
        Returns:
            True if the connection was successful, False otherwise
        """
        try:
            # 使用 asyncio.to_thread 将同步操作移至线程池
            loop = asyncio.get_event_loop()
            
            def _connect():
                # 根据端口号决定使用SSL还是非SSL连接
                # 993是标准的IMAP SSL端口
                if self.port == 993:
                    self.conn = imaplib.IMAP4_SSL(self.server, self.port)
                else:
                    self.conn = imaplib.IMAP4(self.server, self.port)
                
                self.conn.login(self.username, self.password)
                return True
                
            return await loop.run_in_executor(None, _connect)
        except (imaplib.IMAP4.error, ConnectionRefusedError, OSError) as e:
            print(f"Error connecting to IMAP server: {e}")
            return False
    
    def disconnect(self):
        """
        Disconnect from the IMAP server.
        """
        if self.conn:
            try:
                self.conn.logout()
            except imaplib.IMAP4.error:
                pass
            self.conn = None
    
    async def test_connection(self) -> bool:
        """
        测试IMAP连接
        
        Returns:
            连接是否成功
        """
        # 使用异步运行同步代码
        loop = asyncio.get_event_loop()
        try:
            return await loop.run_in_executor(None, self._test_connection_sync)
        except Exception as e:
            print(f"测试IMAP连接时出错: {e}")
            return False
            
    def _test_connection_sync(self) -> bool:
        """同步测试IMAP连接，尝试多种连接方式"""
        # 先尝试根据当前端口使用推荐的连接方式
        if self._try_connection(self.port):
            return True
            
        # 如果失败，尝试其他常见端口
        alternative_ports = [993, 143]
        for port in alternative_ports:
            if port != self.port and self._try_connection(port):
                # 更新实例的端口和SSL设置
                self.port = port
                self.use_ssl = (port == 993)
                print(f"找到可用的IMAP配置: 端口 {port}, SSL: {self.use_ssl}")
                return True
                
        return False
        
    async def _try_connection(self, max_attempts=1) -> bool:
        """
        尝试连接到IMAP服务器
        
        Args:
            max_attempts: 最大尝试次数
            
        Returns:
            是否成功连接
        """
        attempt = 0
        while attempt < max_attempts:
            attempt += 1
            try:
                logger.info(f"尝试连接IMAP服务器 {self.server}:{self.port} (尝试 {attempt}/{max_attempts})")
                
                # 已经有连接的情况
                if self.conn:
                    try:
                        # 测试连接是否有效
                        status, _ = self.conn.noop()
                        if status == 'OK':
                            logger.info("已有IMAP连接并且可用")
                            return True
                        else:
                            logger.warning("已有IMAP连接但不可用，将重新连接")
                            self.disconnect()
                    except Exception as e:
                        logger.warning(f"测试已有IMAP连接出错: {e}，将重新连接")
                        self.disconnect()
                
                # 创建新连接
                if self.use_ssl:
                    self.conn = imaplib.IMAP4_SSL(self.server, self.port, timeout=30)
                else:
                    self.conn = imaplib.IMAP4(self.server, self.port, timeout=30)
                
                # 登录
                logger.info(f"使用用户名 {self.username} 登录IMAP服务器")
                self.conn.login(self.username, self.password)
                logger.info("IMAP服务器登录成功")
                return True
                
            except (imaplib.IMAP4.error, ssl.SSLError) as e:
                logger.error(f"IMAP连接或登录失败 (尝试 {attempt}/{max_attempts}): {e}")
                self.disconnect()
                
                if attempt < max_attempts:
                    # 等待一些时间再重试
                    wait_time = 2 * attempt  # 2秒, 4秒, 6秒...
                    logger.info(f"将在 {wait_time} 秒后重试连接")
                    await asyncio.sleep(wait_time)
                else:
                    logger.error(f"达到最大尝试次数 ({max_attempts})，IMAP连接失败")
            except Exception as e:
                logger.error(f"尝试连接IMAP服务器时发生未知错误: {e}")
                logger.error(traceback.format_exc())
                self.disconnect()
                
                if attempt < max_attempts:
                    await asyncio.sleep(2 * attempt)
                else:
                    logger.error("达到最大尝试次数，IMAP连接失败")
        
        return False
    
    def _ensure_connection(self, max_attempts=1) -> bool:
        """
        确保IMAP连接存在并可用
        
        Args:
            max_attempts: 最大尝试次数
            
        Returns:
            连接是否可用
        """
        if not self.conn:
            try:
                # 尝试异步连接在同步方法中的处理
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(self._try_connection(max_attempts))
            except RuntimeError:
                # 如果没有事件循环，创建一个新的
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(self._try_connection(max_attempts))
                finally:
                    loop.close()
        
        # 检查连接是否活跃
        try:
            status, _ = self.conn.noop()
            return status == 'OK'
        except Exception as e:
            logger.error(f"检查IMAP连接时出错: {e}")
            # 尝试重新连接
            self.disconnect()
            try:
                loop = asyncio.get_event_loop()
                return loop.run_until_complete(self._try_connection(max_attempts))
            except RuntimeError:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                try:
                    return loop.run_until_complete(self._try_connection(max_attempts))
                finally:
                    loop.close()
    
    async def get_mailboxes(self) -> List[str]:
        """
        Get a list of available mailboxes.
        
        Returns:
            List of mailbox names
        """
        if not self.conn:
            if not await self.connect():
                return []
        
        try:
            loop = asyncio.get_event_loop()
            
            def _get_mailboxes():
                status, mailboxes = self.conn.list()
                if status != 'OK':
                    return []
                
                result = []
                for mailbox in mailboxes:
                    # Parse mailbox name
                    match = re.search(rb'"([^"]+)"$', mailbox)
                    if match:
                        name = match.group(1).decode('utf-8', errors='ignore')
                        result.append(name)
                
                return result
            
            return await loop.run_in_executor(None, _get_mailboxes)
        except imaplib.IMAP4.error as e:
            print(f"Error getting mailboxes: {e}")
            return []
    
    async def select_mailbox(self, folder: str = 'INBOX') -> bool:
        """
        选择邮箱文件夹
        
        Args:
            folder: 文件夹名称
            
        Returns:
            是否成功选择
        """
        logger.info(f"尝试选择邮箱文件夹: {folder}")
        if not self.conn:
            if not await self._try_connection(max_attempts=3):
                logger.error("无法连接到IMAP服务器，不能选择邮箱文件夹")
                return False
            
        try:
            # 为常见的发件箱名称提供特殊处理
            if folder.lower() in ['sent', 'sent items', 'sent mail', '已发送', '已发送邮件']:
                # 获取账户邮箱域名，这有助于确定提供商
                email_domain = self.account.email.split('@')[-1].lower() if '@' in self.account.email else ''
                
                logger.info(f"尝试为邮箱域名 '{email_domain}' 选择发件箱")
                
                # 定义不同邮箱提供商可能使用的发件箱名称
                # 先尝试特殊文件夹
                special_sent_folders = []
                
                # Gmail 特殊文件夹处理
                if 'gmail' in email_domain:
                    # 使用纯ASCII字符版本优先，避免编码问题
                    special_sent_folders = [
                        '"[Gmail]/Sent Mail"',  # 英文版
                        '[Gmail]/Sent',         # 简化版
                        '[Gmail]/Sent Mail',    # 不带引号版本
                    ]
                    # 如果需要处理中文名称，需要确保正确编码
                    try:
                        # 单独处理包含中文的文件夹名，使用utf-8编码
                        gmail_cn_folders = [
                            '[Gmail]/已发送邮件'.encode('utf-8').decode('utf-8'),
                            '"[Gmail]/已发送邮件"'.encode('utf-8').decode('utf-8')
                        ]
                        special_sent_folders.extend(gmail_cn_folders)
                    except Exception as e:
                        logger.warning(f"处理Gmail中文文件夹名称时出错: {e}")
                        
                    logger.info(f"检测到Gmail账户，将尝试Gmail特殊文件夹: {special_sent_folders}")
                # Outlook/Hotmail
                elif any(domain in email_domain for domain in ['outlook', 'hotmail', 'live']):
                    special_sent_folders = ['"已发送邮件"', '"Sent Items"']
                    logger.info(f"检测到Outlook/Hotmail账户，将尝试特殊文件夹: {special_sent_folders}")
                # QQ 邮箱
                elif 'qq.com' in email_domain:
                    special_sent_folders = ['"已发送"', '"Sent Messages"']
                    logger.info(f"检测到QQ邮箱账户，将尝试特殊文件夹: {special_sent_folders}")
                # 网易163/126邮箱
                elif any(domain in email_domain for domain in ['163.com', '126.com']):
                    special_sent_folders = ['"已发送"', '"已发送邮件"']
                    logger.info(f"检测到网易邮箱账户，将尝试特殊文件夹: {special_sent_folders}")
                    
                # 尝试特殊文件夹
                for sent_folder in special_sent_folders:
                    try:
                        logger.info(f"尝试选择特殊发件箱: {sent_folder}")
                        try:
                            # 处理可能包含非ASCII字符的文件夹名
                            if any(ord(c) > 127 for c in sent_folder):
                                # 使用UTF-8编码处理非ASCII字符
                                encoded_folder = sent_folder.encode('utf-8')
                                status, _ = self.conn.select(encoded_folder)
                            else:
                                status, _ = self.conn.select(sent_folder)
                        except UnicodeEncodeError:
                            # 如果遇到编码错误，尝试使用UTF-7编码（IMAP标准）
                            logger.info(f"使用UTF-7重新尝试选择文件夹: {sent_folder}")
                            import binascii
                            try:
                                # IMAP使用修改后的UTF-7编码
                                encoded_folder = sent_folder.encode('utf-7').replace(b'+', b'&').replace(b'/', b',')
                                status, _ = self.conn.select(encoded_folder)
                            except (UnicodeError, binascii.Error):
                                # 跳过此文件夹并继续
                                logger.warning(f"无法编码文件夹名称: {sent_folder}")
                                continue
                            
                        if status == 'OK':
                            logger.info(f"成功选择特殊发件箱: {sent_folder}")
                            return True
                    except Exception as e:
                        logger.warning(f"选择特殊发件箱 '{sent_folder}' 时出错: {e}")
                        
                # 通用的发件箱尝试列表
                standard_sent_folders = [
                    'Sent', '"Sent"', '已发送', '"已发送"',
                    'Sent Items', '"Sent Items"', '已发送邮件', '"已发送邮件"',
                    'Sent Mail', '"Sent Mail"', 'Sent Messages', '"Sent Messages"'
                ]
                
                # 尝试标准文件夹
                for sent_folder in standard_sent_folders:
                    try:
                        logger.info(f"尝试选择标准发件箱: {sent_folder}")
                        status, _ = self.conn.select(sent_folder)
                        if status == 'OK':
                            logger.info(f"成功选择标准发件箱: {sent_folder}")
                            return True
                    except Exception as e:
                        logger.warning(f"选择标准发件箱 '{sent_folder}' 时出错: {e}")
                
                # 如果以上都失败，尝试列出所有文件夹并查找包含"sent"或"发送"的文件夹
                try:
                    logger.info("尝试列出所有邮箱文件夹...")
                    typ, mailbox_list = self.conn.list()
                    if typ == 'OK':
                        for mailbox_info in mailbox_list:
                            if not mailbox_info:
                                continue
                            
                            # 解码文件夹信息
                            try:
                                decoded_mailbox = mailbox_info.decode('utf-8')
                                logger.info(f"发现邮箱文件夹: {decoded_mailbox}")
                                
                                # 查找包含sent或发送的文件夹
                                lower_decoded = decoded_mailbox.lower()
                                if 'sent' in lower_decoded or '发送' in lower_decoded:
                                    # 提取文件夹名称
                                    parts = decoded_mailbox.split(' ')
                                    if len(parts) >= 2:
                                        folder_name = parts[-1].strip('"')
                                        logger.info(f"发现可能的发件箱: {folder_name}")
                                        
                                        try:
                                            status, _ = self.conn.select(folder_name)
                                            if status == 'OK':
                                                logger.info(f"成功选择发现的发件箱: {folder_name}")
                                                return True
                                        except Exception as e:
                                            logger.warning(f"选择发现的发件箱 '{folder_name}' 时出错: {e}")
                            except Exception as e:
                                logger.warning(f"解析邮箱文件夹时出错: {e}")
                except Exception as e:
                    logger.warning(f"列出邮箱文件夹时出错: {e}")
                    
                # 如果所有尝试都失败，则默认尝试选择INBOX
                logger.warning("未找到发件箱，将尝试使用INBOX")
                try:
                    status, _ = self.conn.select('INBOX')
                    if status == 'OK':
                        logger.info("无法找到发件箱，已选择INBOX")
                        return True
                except Exception as e:
                    logger.error(f"选择INBOX时出错: {e}")
                    return False
                    
                logger.error("无法找到任何可用的发件箱")
                return False
            else:
                # 处理非发件箱的普通文件夹
                try:
                    status, _ = self.conn.select(folder)
                    return status == 'OK'
                except Exception as e:
                    logger.error(f"选择文件夹 '{folder}' 时出错: {e}")
                    return False
        except Exception as e:
            logger.error(f"select_mailbox出错: {e}")
            return False
    
    async def search_unseen(self) -> List[str]:
        """
        Search for unseen messages.
        
        Returns:
            List of message IDs
        """
        return await self._search('UNSEEN')
    
    async def search_all(self) -> List[str]:
        """
        Search for all messages.
        
        Returns:
            List of message IDs
        """
        return await self._search('ALL')
    
    async def search_recent(self, days=3) -> List[str]:
        """
        Search for recent messages.
        
        Args:
            days: Number of days
            
        Returns:
            List of message IDs
        """
        return await self._search(f'SINCE {days}')
    
    async def get_latest_sent_email(self):
        """
        获取最新发送的邮件
        
        Returns:
            Dict: 包含邮件信息的字典，获取失败时返回None
        """
        try:
            # 确保连接存在 - 直接调用异步连接方法而不是通过_ensure_connection
            if not self.conn:
                logger.info("尝试连接到IMAP服务器以获取最新发送邮件")
                connection_success = await self._try_connection(max_attempts=3)
                if not connection_success:
                    logger.error("无法连接到IMAP服务器，无法获取最新发送邮件")
                    return None
            else:
                # 测试连接是否有效
                try:
                    status, _ = self.conn.noop()
                    if status != 'OK':
                        logger.warning("IMAP连接不可用，尝试重新连接")
                        self.disconnect()
                        connection_success = await self._try_connection(max_attempts=3)
                        if not connection_success:
                            logger.error("重新连接IMAP服务器失败")
                            return None
                except Exception as e:
                    logger.warning(f"测试IMAP连接时出错: {e}，尝试重新连接")
                    self.disconnect()
                    connection_success = await self._try_connection(max_attempts=3)
                    if not connection_success:
                        logger.error("重新连接IMAP服务器失败")
                        return None
                
            logger.info("开始获取最新发送邮件...")
            
            # 选择发件箱
            mailbox_selected = False
            retry_count = 0
            while not mailbox_selected and retry_count < 3:
                logger.info(f"尝试选择发件箱 (尝试 {retry_count + 1}/3)")
                mailbox_selected = await self.select_mailbox('sent')
                if not mailbox_selected:
                    logger.warning(f"选择发件箱失败，等待1秒后重试")
                    await asyncio.sleep(1)
                    retry_count += 1
            
            if not mailbox_selected:
                logger.error("无法选择发件箱，放弃获取最新发送邮件")
                return None
                
            logger.info("发件箱选择成功，正在搜索所有邮件...")
            
            # 搜索所有邮件
            try:
                search_response = self.conn.search(None, 'ALL')
                if not search_response or search_response[0] != 'OK':
                    logger.error(f"搜索发件箱邮件失败: {search_response}")
                    return None
                
                message_numbers = search_response[1][0].split()
                if not message_numbers:
                    logger.warning("发件箱中没有找到任何邮件")
                    return None
                    
                # 获取最新邮件（最后一封）
                latest_id = message_numbers[-1]
                logger.info(f"找到最新发送邮件ID: {latest_id.decode() if isinstance(latest_id, bytes) else latest_id}，正在获取详情...")
                
                # 获取邮件数据
                fetch_response = self.conn.fetch(latest_id, '(RFC822)')
                if not fetch_response or fetch_response[0] != 'OK':
                    logger.error(f"获取邮件ID {latest_id} 的内容失败: {fetch_response}")
                    return None
                    
                # 解析邮件 - 从元组列表中获取原始邮件数据
                raw_email = None
                for response_part in fetch_response[1]:
                    if isinstance(response_part, tuple):
                        raw_email = response_part[1]
                        break
                
                if not raw_email:
                    logger.error("无法从FETCH响应中提取邮件内容")
                    return None
                    
                logger.info("成功获取邮件原始数据，正在解析...")
                email_message = email.message_from_bytes(raw_email)
                
                # 解析邮件内容
                parsed_email = self._parse_email_message(email_message)
                if parsed_email:
                    logger.info(f"成功解析最新发送邮件: 主题='{parsed_email.get('subject')}', 收件人={parsed_email.get('recipients')}")
                    return parsed_email
                else:
                    logger.error("解析最新发送邮件失败")
                    return None
            except imaplib.IMAP4.error as e:
                logger.error(f"IMAP操作错误: {e}")
                return None
        except imaplib.IMAP4.error as e:
            logger.error(f"IMAP错误: {e}")
            return None
        except ssl.SSLError as e:
            logger.error(f"SSL错误: {e}")
            return None
        except Exception as e:
            logger.error(f"获取最新发送邮件时发生异常: {e}")
            logger.error(traceback.format_exc())
            return None
        finally:
            # 不要在这里关闭连接，因为可能还有其他操作需要使用连接
            pass
    
    async def _search(self, criterion: str) -> List[str]:
        """
        Search for messages based on a criterion.
        
        Args:
            criterion: Search criterion
            
        Returns:
            List of message IDs
        """
        if not self.conn:
            if not await self.connect():
                return []
        
        try:
            loop = asyncio.get_event_loop()
            
            def _search_exec():
                status, data = self.conn.search(None, criterion)
                if status != 'OK':
                    return []
                
                # Get the message IDs
                message_nums = data[0].split()
                return [x.decode('utf-8') for x in message_nums]
            
            return await loop.run_in_executor(None, _search_exec)
        except imaplib.IMAP4.error as e:
            print(f"Error searching messages with criterion {criterion}: {e}")
            return []
    
    async def fetch_message(self, message_num: str) -> Optional[Dict]:
        """
        Fetch a message by number.
        
        Args:
            message_num: Message number
            
        Returns:
            Message data dictionary or None if an error occurred
        """
        if not self.conn:
            if not await self.connect():
                return None
        
        try:
            loop = asyncio.get_event_loop()
            
            def _fetch():
                status, data = self.conn.fetch(message_num, '(RFC822)')
                if status != 'OK':
                    return None
                
                raw_email = data[0][1]
                email_message = email.message_from_bytes(raw_email)
                
                # Get message ID
                message_id = email_message.get('Message-ID', '')
                
                # Get subject
                subject = self._decode_header_str(email_message.get('Subject', ''))
                
                # Get sender
                sender = self._decode_header_str(email_message.get('From', ''))
                
                # Get recipients
                to = self._decode_header_str(email_message.get('To', ''))
                recipients = [x.strip() for x in to.split(',') if x.strip()]
                
                # Get CC
                cc = self._decode_header_str(email_message.get('Cc', ''))
                cc_list = [x.strip() for x in cc.split(',') if x.strip()] if cc else []
                
                # Get BCC
                bcc = self._decode_header_str(email_message.get('Bcc', ''))
                bcc_list = [x.strip() for x in bcc.split(',') if x.strip()] if bcc else []
                
                # Get date
                date_str = email_message.get('Date', '')
                date = None
                if date_str:
                    try:
                        date = parsedate_to_datetime(date_str)
                    except (ValueError, TypeError):
                        date = datetime.now()
                else:
                    date = datetime.now()
                
                # Get reference headers for threading
                in_reply_to = email_message.get('In-Reply-To', '')
                references_str = email_message.get('References', '')
                references = [ref.strip() for ref in references_str.split(' ') if ref.strip()] if references_str else []
                
                # Get body and inline images
                body_text, body_html, inline_images = self._get_email_body(email_message)
                
                # 构建返回的邮件数据
                email_data = {
                    'message_id': message_id,
                    'subject': subject,
                    'sender': sender,
                    'recipients': recipients,
                    'cc': cc_list,
                    'bcc': bcc_list,
                    'date': date,
                    'in_reply_to': in_reply_to,
                    'references': references,
                    'body_text': body_text,
                    'body_html': body_html,
                    'attachments': [],  # 将在后面填充
                    'inline_images': inline_images  # 添加内联图片
                }
                
                # Get attachments
                attachments = []
                for part in email_message.walk():
                    if part.get_content_maintype() == 'multipart':
                        continue
                    
                    if part.get('Content-Disposition') is None:
                        continue
                    
                    # 如果这是内联图片且已经处理过，则跳过
                    content_id = part.get("Content-ID", "")
                    if content_id:
                        cid = content_id.strip("<>")
                        if cid in inline_images:
                            continue
                    
                    # 跳过没有文件名的部分或非附件部分
                    content_disposition = str(part.get("Content-Disposition", ""))
                    if "attachment" not in content_disposition:
                        continue
                        
                    filename = part.get_filename()
                    if not filename:
                        continue
                    
                    filename = self._decode_header_str(filename)
                    
                    # Get content type
                    content_type = part.get_content_type()
                    
                    # Get attachment data
                    attachment_data = part.get_payload(decode=True)
                    
                    # Add to attachments list
                    attachments.append({
                        'filename': filename,
                        'content_type': content_type,
                        'data': attachment_data,
                        'size': len(attachment_data) if attachment_data else 0,
                    })
                
                # Add attachments to email data
                email_data['attachments'] = attachments
                
                return email_data
            
            return await loop.run_in_executor(None, _fetch)
        except imaplib.IMAP4.error as e:
            print(f"Error fetching message {message_num}: {e}")
            return None
    
    async def mark_as_read(self, message_num: str) -> bool:
        """
        Mark a message as read.
        
        Args:
            message_num: Message number
            
        Returns:
            True if successful, False otherwise
        """
        if not self.conn:
            if not await self.connect():
                return False
        
        try:
            loop = asyncio.get_event_loop()
            
            def _mark_as_read():
                # Mark as read by adding the \Seen flag
                status, data = self.conn.store(message_num, '+FLAGS', '\\Seen')
                return status == 'OK'
            
            return await loop.run_in_executor(None, _mark_as_read)
        except imaplib.IMAP4.error as e:
            print(f"Error marking message {message_num} as read: {e}")
            return False
    
    async def delete_message(self, message_num: str) -> bool:
        """
        删除邮件。
        
        Args:
            message_num: 邮件编号
            
        Returns:
            是否成功删除
        """
        if not self.conn:
            if not await self.connect():
                print(f"无法连接到IMAP服务器，无法删除邮件 {message_num}")
                return False
        
        try:
            loop = asyncio.get_event_loop()
            
            def _delete_message():
                try:
                    # 标记为删除
                    print(f"尝试标记邮件 {message_num} 为删除状态...")
                    status1, data1 = self.conn.store(message_num, '+FLAGS', '\\Deleted')
                    if status1 != 'OK':
                        print(f"标记邮件 {message_num} 为删除状态失败: {status1}")
                        return False
                    
                    print(f"标记邮件 {message_num} 为删除状态成功")
                    
                    # 执行删除操作
                    print(f"尝试从服务器执行删除操作...")
                    status2, data2 = self.conn.expunge()
                    if status2 != 'OK':
                        print(f"从服务器删除邮件 {message_num} 失败: {status2}")
                        return False
                    
                    print(f"从服务器删除邮件 {message_num} 成功")
                    return True
                except imaplib.IMAP4.error as e:
                    print(f"删除邮件 {message_num} 过程中出现IMAP错误: {e}")
                    return False
                except Exception as e:
                    print(f"删除邮件 {message_num} 过程中出现异常: {e}")
                    return False
            
            result = await loop.run_in_executor(None, _delete_message)
            if result:
                print(f"删除邮件 {message_num} 完成")
            else:
                print(f"删除邮件 {message_num} 操作失败")
            return result
        except Exception as e:
            print(f"删除邮件 {message_num} 时发生错误: {e}")
            return False
    
    def _decode_header_str(self, s: str) -> str:
        """
        Decode a header string.
        
        Args:
            s: Header string
            
        Returns:
            Decoded string
        """
        decoded_parts = []
        for part, encoding in decode_header(s):
            if isinstance(part, bytes):
                if encoding:
                    try:
                        decoded_parts.append(part.decode(encoding))
                    except (UnicodeDecodeError, LookupError):
                        decoded_parts.append(part.decode('utf-8', errors='ignore'))
                else:
                    decoded_parts.append(part.decode('utf-8', errors='ignore'))
            else:
                decoded_parts.append(part)
        
        return ''.join(decoded_parts)
    
    def _get_email_body(self, email_message) -> Tuple[str, str, Dict]:
        """
        Get the text and HTML body of an email.
        
        Args:
            email_message: Email message object
            
        Returns:
            Tuple of (text body, HTML body, inline images)
        """
        body_text = ""
        body_html = ""
        
        # 添加一个字典来保存内联图片，键为CID，值为图片数据
        inline_images = {}
        
        if email_message.is_multipart():
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = str(part.get("Content-Disposition", ""))
                content_id = part.get("Content-ID", "")
                
                # 检测内联图片(CID)
                if content_id and (content_type.startswith('image/') or 'inline' in content_disposition):
                    # 从Content-ID中提取CID值（去除括号）
                    cid = content_id.strip("<>")
                    try:
                        image_data = part.get_payload(decode=True)
                        if image_data:
                            inline_images[cid] = {
                                'data': image_data,
                                'content_type': content_type,
                                'filename': self._decode_header_str(part.get_filename() or f"inline_{cid}")
                            }
                    except Exception as e:
                        print(f"Error processing inline image {cid}: {e}")
                
                # 处理常规内容
                if "attachment" in content_disposition:
                    continue
                
                try:
                    body = part.get_payload(decode=True)
                    if body:
                        charset = part.get_content_charset() or 'utf-8'
                        try:
                            decoded_body = body.decode(charset)
                        except (UnicodeDecodeError, LookupError):
                            decoded_body = body.decode('utf-8', errors='ignore')
                        
                        if content_type == "text/plain":
                            body_text = decoded_body
                        elif content_type == "text/html":
                            body_html = decoded_body
                except Exception as e:
                    print(f"Error getting email body part: {e}")
        else:
            # Not multipart - get payload directly
            content_type = email_message.get_content_type()
            try:
                body = email_message.get_payload(decode=True)
                if body:
                    charset = email_message.get_content_charset() or 'utf-8'
                    try:
                        decoded_body = body.decode(charset)
                    except (UnicodeDecodeError, LookupError):
                        decoded_body = body.decode('utf-8', errors='ignore')
                    
                    if content_type == "text/plain":
                        body_text = decoded_body
                    elif content_type == "text/html":
                        body_html = decoded_body
            except Exception as e:
                print(f"Error getting email body: {e}")
        
        return body_text, body_html, inline_images
    
    async def search_by_message_id(self, message_id: str) -> List[str]:
        """
        通过Message-ID搜索邮件。
        
        Args:
            message_id: 邮件的Message-ID
            
        Returns:
            邮件ID列表
        """
        if not message_id:
            print("搜索邮件：Message-ID为空")
            return []
        
        # 去除邮件ID中的尖括号，如果有的话
        clean_message_id = message_id.strip('<>')
        print(f"搜索邮件ID: {clean_message_id}")
        
        # 尝试不同的搜索方式
        # 1. 精确匹配 Message-ID
        search_criterion = f'HEADER Message-ID "{clean_message_id}"'
        result = await self._search(search_criterion)
        
        # 如果找到了结果，直接返回
        if result:
            print(f"使用精确匹配找到邮件: {len(result)} 个")
            return result
        
        # 2. 尝试转义特殊字符后再次搜索
        # 有些邮件服务器可能需要特殊处理Message-ID中的特殊字符
        escaped_message_id = clean_message_id.replace('"', '\\"').replace('\\', '\\\\')
        search_criterion = f'HEADER Message-ID "{escaped_message_id}"'
        result = await self._search(search_criterion)
        
        if result:
            print(f"使用转义特殊字符匹配找到邮件: {len(result)} 个")
            return result
        
        # 3. 如果还找不到，尝试部分匹配
        # 某些邮件服务器可能在存储时改变了Message-ID的格式
        # 尝试使用Message-ID的一部分进行搜索
        if len(clean_message_id) > 20:
            # 使用Message-ID的中间部分（通常是最稳定的部分）
            mid_part = clean_message_id[10:30]
            search_criterion = f'HEADER Message-ID "{mid_part}"'
            result = await self._search(search_criterion)
            
            if result:
                print(f"使用部分匹配找到邮件: {len(result)} 个")
                return result
        
        print(f"未找到Message-ID为 {clean_message_id} 的邮件")
        return []
    
    def _parse_email_message(self, email_message):
        """
        解析邮件消息对象，提取所需信息
        
        Args:
            email_message: 邮件消息对象
            
        Returns:
            包含邮件信息的字典
        """
        try:
            logger.info("开始解析邮件消息")
            
            # 获取基本邮件信息
            message_id = email_message.get('Message-ID', '')
            subject = self._decode_header(email_message.get('Subject', '无主题'))
            sender = self._decode_header(email_message.get('From', ''))
            
            # 处理日期
            date_str = email_message.get('Date', '')
            date = datetime.now()  # 默认为当前时间
            if date_str:
                try:
                    date = parsedate_to_datetime(date_str)
                except:
                    logger.warning(f"无法解析邮件日期: {date_str}，使用当前时间")
            
            # 解析收件人
            to_str = email_message.get('To', '')
            recipients = []
            if to_str:
                # 分割多个收件人
                to_addresses = to_str.split(',')
                for addr in to_addresses:
                    addr = addr.strip()
                    if addr:
                        recipients.append(addr)
            
            # 解析抄送人
            cc_str = email_message.get('Cc', '')
            cc = []
            if cc_str:
                cc_addresses = cc_str.split(',')
                for addr in cc_addresses:
                    addr = addr.strip()
                    if addr:
                        cc.append(addr)
            
            # 解析密送人（一般在收到的邮件中不可见）
            bcc_str = email_message.get('Bcc', '')
            bcc = []
            if bcc_str:
                bcc_addresses = bcc_str.split(',')
                for addr in bcc_addresses:
                    addr = addr.strip()
                    if addr:
                        bcc.append(addr)
            
            # 获取引用信息
            in_reply_to = email_message.get('In-Reply-To', '')
            references = []
            references_str = email_message.get('References', '')
            if references_str:
                references = [ref.strip() for ref in references_str.split(' ') if ref.strip()]
            
            # 解析邮件内容和附件
            body_text = ""
            body_html = ""
            attachments = []
            inline_images = {}
            
            # 遍历所有部分
            for part in email_message.walk():
                content_type = part.get_content_type()
                content_disposition = part.get('Content-Disposition', '')
                
                # 处理邮件正文
                if not content_disposition:
                    if content_type == 'text/plain':
                        charset = part.get_content_charset() or 'utf-8'
                        try:
                            payload = part.get_payload(decode=True)
                            if payload:
                                body_text = payload.decode(charset, errors='replace')
                        except Exception as e:
                            logger.error(f"解析纯文本内容时出错: {e}")
                    
                    elif content_type == 'text/html':
                        charset = part.get_content_charset() or 'utf-8'
                        try:
                            payload = part.get_payload(decode=True)
                            if payload:
                                body_html = payload.decode(charset, errors='replace')
                        except Exception as e:
                            logger.error(f"解析HTML内容时出错: {e}")
                
                # 处理附件
                elif 'attachment' in content_disposition:
                    filename = part.get_filename()
                    if filename:
                        filename = self._decode_header(filename)
                        try:
                            attachment_data = part.get_payload(decode=True)
                            attachment_size = len(attachment_data) if attachment_data else 0
                            
                            attachments.append({
                                'filename': filename,
                                'content_type': content_type,
                                'data': attachment_data,
                                'size': attachment_size
                            })
                            logger.info(f"解析到附件: {filename}, 大小: {attachment_size} 字节")
                        except Exception as e:
                            logger.error(f"处理附件 {filename} 时出错: {e}")
                
                # 处理内联图片
                elif 'inline' in content_disposition:
                    content_id = part.get('Content-ID', '')
                    if content_id:
                        # 移除内联图片ID中的尖括号
                        content_id = content_id.strip('<>')
                        try:
                            image_data = part.get_payload(decode=True)
                            if image_data:
                                inline_images[content_id] = {
                                    'content_type': content_type,
                                    'data': image_data
                                }
                                logger.info(f"解析到内联图片: Content-ID={content_id}")
                        except Exception as e:
                            logger.error(f"处理内联图片 {content_id} 时出错: {e}")
            
            # 构建结果字典
            email_data = {
                'message_id': message_id,
                'subject': subject,
                'sender': sender,
                'recipients': recipients,
                'cc': cc,
                'bcc': bcc,
                'date': date,
                'in_reply_to': in_reply_to,
                'references': references,
                'body_text': body_text,
                'body_html': body_html,
                'attachments': attachments,
                'inline_images': inline_images
            }
            
            logger.info(f"邮件解析完成: 主题='{subject}', 发件人='{sender}', 收件人={recipients}")
            return email_data
            
        except Exception as e:
            logger.error(f"解析邮件时发生异常: {e}")
            logger.error(traceback.format_exc())
            return None
            
    def _decode_header(self, header):
        """
        解码邮件头信息
        
        Args:
            header: 需要解码的头信息
            
        Returns:
            解码后的字符串
        """
        if not header:
            return ""
            
        try:
            decoded_parts = decode_header(header)
            decoded_string = ""
            
            for content, charset in decoded_parts:
                if isinstance(content, bytes):
                    if charset:
                        decoded_string += content.decode(charset, errors='replace')
                    else:
                        # 尝试常见编码
                        try:
                            decoded_string += content.decode('utf-8', errors='replace')
                        except:
                            try:
                                decoded_string += content.decode('gbk', errors='replace')
                            except:
                                decoded_string += content.decode('latin1', errors='replace')
                else:
                    decoded_string += content if content else ""
                    
            return decoded_string
        except Exception as e:
            logger.error(f"解码邮件头 '{header}' 时出错: {e}")
            return header  # 如果解码失败，返回原始头信息 