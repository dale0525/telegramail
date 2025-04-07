"""
Database operations module for TelegramMail.
"""
import json
import logging
import os
from typing import List, Optional, Union
from datetime import datetime

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import selectinload

from app.database.models import (EmailAccount, EmailAttachment, EmailMessage,
                              get_session)
from app.utils.config import config


def get_email_account_by_id(account_id: int) -> Optional[EmailAccount]:
    """
    获取邮件账户信息
    
    Args:
        account_id: 账户ID
        
    Returns:
        账户对象或None
    """
    session = get_session()
    try:
        return session.query(EmailAccount).filter_by(id=account_id).first()
    finally:
        session.close()


class AccountOperations:
    """
    Operations for email accounts.
    """
    
    @staticmethod
    def add_account(
        email: str,
        name: str,
        imap_server: str,
        imap_port: int,
        smtp_server: str,
        smtp_port: int,
        username: str,
        password: str,
        imap_use_ssl: bool = True,
        smtp_use_ssl: bool = True
    ) -> Optional[EmailAccount]:
        """
        Add a new email account.
        
        Args:
            email: Email address
            name: Account name
            imap_server: IMAP server address
            imap_port: IMAP server port
            smtp_server: SMTP server address
            smtp_port: SMTP server port
            username: Account username
            password: Account password
            imap_use_ssl: Whether to use SSL for IMAP
            smtp_use_ssl: Whether to use SSL for SMTP
            
        Returns:
            The created account object or None if an error occurred
        """
        session = get_session()
        try:
            account = EmailAccount(
                email=email,
                name=name,
                imap_server=imap_server,
                imap_port=imap_port,
                imap_use_ssl=imap_use_ssl,
                smtp_server=smtp_server,
                smtp_port=smtp_port,
                smtp_use_ssl=smtp_use_ssl,
                username=username,
                password=password
            )
            session.add(account)
            session.flush()  # 获取ID而不提交
            
            # 保存ID
            account_id = account.id
            
            # 提交
            session.commit()
            
            # 返回新创建账户的ID
            return session.query(EmailAccount).get(account_id)
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error adding account: {e}")
            return None
        finally:
            session.close()
    
    @staticmethod
    def get_account(account_id: int) -> Optional[EmailAccount]:
        """
        Get an email account by ID.
        
        Args:
            account_id: Account ID
            
        Returns:
            The account object or None if not found
        """
        session = get_session()
        try:
            return session.query(EmailAccount).filter_by(id=account_id).first()
        finally:
            session.close()
    
    @staticmethod
    def get_account_by_email(email: str) -> Optional[EmailAccount]:
        """
        Get an email account by email address.
        
        Args:
            email: Email address
            
        Returns:
            The account object or None if not found
        """
        session = get_session()
        try:
            return session.query(EmailAccount).filter_by(email=email).first()
        finally:
            session.close()
    
    @staticmethod
    def get_all_active_accounts() -> List[EmailAccount]:
        """
        Get all active email accounts.
        
        Returns:
            List of active account objects
        """
        session = get_session()
        try:
            return session.query(EmailAccount).filter_by(is_active=True).all()
        finally:
            session.close()
    
    @staticmethod
    def update_account(account_id: int, **kwargs) -> bool:
        """
        Update an email account.
        
        Args:
            account_id: Account ID
            **kwargs: Fields to update
            
        Returns:
            True if successful, False otherwise
        """
        session = get_session()
        try:
            account = session.query(EmailAccount).filter_by(id=account_id).first()
            if not account:
                return False
            
            for key, value in kwargs.items():
                if hasattr(account, key):
                    setattr(account, key, value)
            
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error updating account: {e}")
            return False
        finally:
            session.close()
    
    @staticmethod
    def delete_account(account_id: int) -> bool:
        """
        Delete an email account.
        
        Args:
            account_id: Account ID
            
        Returns:
            True if successful, False otherwise
        """
        session = get_session()
        try:
            account = session.query(EmailAccount).filter_by(id=account_id).first()
            if not account:
                return False
            
            session.delete(account)
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error deleting account: {e}")
            return False
        finally:
            session.close()


class MessageOperations:
    """
    Operations for email messages.
    """
    
    @staticmethod
    def add_message(
        account_id: int,
        message_id: str,
        subject: str,
        sender: str,
        recipients: List[str],
        date,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        is_read: bool = False
    ) -> Optional[EmailMessage]:
        """
        Add a new email message.
        
        Args:
            account_id: Account ID
            message_id: Email message ID
            subject: Email subject
            sender: Sender email
            recipients: List of recipient emails
            date: Email date
            cc: List of CC emails
            bcc: List of BCC emails
            is_read: Whether the message has been read
            
        Returns:
            The created message object or None if an error occurred
        """
        session = get_session()
        try:
            recipients_json = json.dumps(recipients)
            cc_json = json.dumps(cc) if cc else None
            bcc_json = json.dumps(bcc) if bcc else None
            
            message = EmailMessage(
                account_id=account_id,
                message_id=message_id,
                subject=subject,
                sender=sender,
                recipients=recipients_json,
                cc=cc_json,
                bcc=bcc_json,
                date=date,
                is_read=is_read
            )
            session.add(message)
            session.commit()
            return message
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error adding message: {e}")
            return None
        finally:
            session.close()
    
    @staticmethod
    def get_message(message_id: int) -> Optional[EmailMessage]:
        """
        Get an email message by ID.
        
        Args:
            message_id: Message ID
            
        Returns:
            The message object or None if not found
        """
        session = get_session()
        try:
            # 使用selectinload加载attachments关系
            return session.query(EmailMessage).options(
                selectinload(EmailMessage.attachments)
            ).filter_by(id=message_id).first()
        finally:
            session.close()
    
    @staticmethod
    def get_message_by_email_id(email_message_id: str, account_id: int) -> Optional[EmailMessage]:
        """
        Get an email message by email message ID and account ID.
        
        Args:
            email_message_id: Email message ID
            account_id: Account ID
            
        Returns:
            The message object or None if not found
        """
        session = get_session()
        try:
            # 使用selectinload加载attachments关系
            return session.query(EmailMessage).options(
                selectinload(EmailMessage.attachments)
            ).filter_by(
                message_id=email_message_id, account_id=account_id
            ).first()
        finally:
            session.close()
    
    @staticmethod
    def get_message_by_telegram_id(telegram_message_id: str) -> Optional[EmailMessage]:
        """
        Get an email message by Telegram message ID.
        
        Args:
            telegram_message_id: Telegram message ID
            
        Returns:
            The message object or None if not found
        """
        session = get_session()
        try:
            # 使用selectinload加载attachments关系
            return session.query(EmailMessage).options(
                selectinload(EmailMessage.attachments)
            ).filter_by(
                telegram_message_id=telegram_message_id
            ).first()
        finally:
            session.close()
    
    @staticmethod
    def update_message(message_id: int, **kwargs) -> bool:
        """
        Update an email message.
        
        Args:
            message_id: Message ID
            **kwargs: Fields to update
            
        Returns:
            True if successful, False otherwise
        """
        session = get_session()
        try:
            message = session.query(EmailMessage).filter_by(id=message_id).first()
            if not message:
                return False
            
            for key, value in kwargs.items():
                if hasattr(message, key):
                    setattr(message, key, value)
            
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error updating message: {e}")
            return False
        finally:
            session.close()
    
    @staticmethod
    def mark_as_read(message_id: int) -> bool:
        """
        Mark an email message as read.
        
        Args:
            message_id: Message ID
            
        Returns:
            True if successful, False otherwise
        """
        return MessageOperations.update_message(message_id, is_read=True)
    
    @staticmethod
    def mark_as_deleted(message_id: int) -> bool:
        """
        Mark an email message as deleted.
        
        Args:
            message_id: Message ID
            
        Returns:
            True if successful, False otherwise
        """
        return MessageOperations.update_message(message_id, is_deleted=True)

    @staticmethod
    def get_email_id_by_telegram_message_id(telegram_message_id: str) -> Optional[int]:
        """
        根据Telegram消息ID获取对应的邮件ID
        
        Args:
            telegram_message_id: Telegram消息ID
            
        Returns:
            邮件ID，如果找不到则返回None
        """
        session = get_session()
        try:
            message = session.query(EmailMessage).filter_by(
                telegram_message_id=telegram_message_id
            ).first()
            return message.id if message else None
        except Exception as e:
            print(f"根据Telegram消息ID获取邮件ID时出错: {e}")
            return None
        finally:
            session.close()


class AttachmentOperations:
    """
    Operations for email attachments.
    """
    
    @staticmethod
    def add_attachment(
        message_id: int,
        filename: str,
        content_type: str,
        telegram_file_id: Optional[str] = None
    ) -> Optional[EmailAttachment]:
        """
        Add a new email attachment.
        
        Args:
            message_id: Message ID
            filename: Attachment filename
            content_type: Attachment content type
            telegram_file_id: Telegram file ID
            
        Returns:
            The created attachment object or None if an error occurred
        """
        session = get_session()
        try:
            attachment = EmailAttachment(
                message_id=message_id,
                filename=filename,
                content_type=content_type,
                telegram_file_id=telegram_file_id
            )
            session.add(attachment)
            session.commit()
            return attachment
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error adding attachment: {e}")
            return None
        finally:
            session.close()
    
    @staticmethod
    def get_attachments_for_message(message_id: int) -> List[EmailAttachment]:
        """
        Get all attachments for a message.
        
        Args:
            message_id: Message ID
            
        Returns:
            List of attachment objects
        """
        session = get_session()
        try:
            return session.query(EmailAttachment).filter_by(message_id=message_id).all()
        finally:
            session.close()
    
    @staticmethod
    def update_telegram_file_id(attachment_id: int, telegram_file_id: str) -> bool:
        """
        Update the Telegram file ID for an attachment.
        
        Args:
            attachment_id: Attachment ID
            telegram_file_id: Telegram file ID
            
        Returns:
            True if successful, False otherwise
        """
        session = get_session()
        try:
            attachment = session.query(EmailAttachment).filter_by(id=attachment_id).first()
            if not attachment:
                return False
            
            attachment.telegram_file_id = telegram_file_id
            session.commit()
            return True
        except SQLAlchemyError as e:
            session.rollback()
            print(f"Error updating attachment: {e}")
            return False
        finally:
            session.close()

def get_user_emails(user_id: int, page: int = 0, page_size: int = 10):
    """
    获取用户的邮件（分页）
    
    Args:
        user_id: 用户ID (在这个版本中，使用 chat_id 作为用户ID)
        page: 页码，从0开始
        page_size: 每页数量
        
    Returns:
        邮件列表
    """
    # 简单的假数据返回，实际实现时需要和数据库对接
    session = get_session()
    try:
        return session.query(EmailMessage) \
            .order_by(EmailMessage.date.desc()) \
            .offset(page * page_size) \
            .limit(page_size) \
            .all()
    finally:
        session.close()

def get_email_by_id(email_id: int) -> Optional[EmailMessage]:
    """
    通过ID获取邮件
    
    Args:
        email_id: 邮件ID
        
    Returns:
        邮件对象或None
    """
    session = get_session()
    try:
        # 使用selectinload或joinedload加载attachments关系
        email = session.query(EmailMessage).options(
            selectinload(EmailMessage.attachments)
        ).filter_by(id=email_id).first()
        return email
    finally:
        session.close()

def mark_email_as_read(email_id: int) -> bool:
    """
    将邮件标记为已读
    
    Args:
        email_id: 邮件ID
        
    Returns:
        是否成功
    """
    session = get_session()
    try:
        # 使用selectinload加载attachments关系
        email = session.query(EmailMessage).options(
            selectinload(EmailMessage.attachments)
        ).filter_by(id=email_id).first()
        if not email:
            return False
        
        email.is_read = True
        session.commit()
        return True
    except SQLAlchemyError:
        session.rollback()
        return False
    finally:
        session.close()

def delete_email(email_id: int) -> bool:
    """
    删除邮件（从数据库中彻底删除）
    
    Args:
        email_id: 邮件ID
        
    Returns:
        是否成功
    """
    session = get_session()
    try:
        # 使用selectinload加载attachments关系
        email = session.query(EmailMessage).options(
            selectinload(EmailMessage.attachments)
        ).filter_by(id=email_id).first()
        if not email:
            return False
        
        # 删除邮件及其关联的attachments（通过级联删除）
        session.delete(email)
        session.commit()
        return True
    except SQLAlchemyError as e:
        session.rollback()
        print(f"删除邮件时出错: {e}")
        return False
    finally:
        session.close()

def save_email_metadata(account_id: int, email_data: dict) -> Optional[int]:
    """
    保存邮件元数据到数据库
    
    Args:
        account_id: 账户ID
        email_data: 邮件数据字典
        
    Returns:
        保存的邮件ID，如果保存失败则返回None
    """
    session = get_session()
    try:
        # 提取邮件数据
        message_id = email_data.get('message_id', '')
        subject = email_data.get('subject', '')
        sender = email_data.get('sender', '')
        recipients = email_data.get('recipients', [])
        cc = email_data.get('cc', [])
        bcc = email_data.get('bcc', [])
        date = email_data.get('date', datetime.now())
        # 提取引用关系（只存储直接回复的邮件ID）
        in_reply_to = email_data.get('in_reply_to', '')
        
        # 提取邮件内容
        text_content = email_data.get('body_text', '')
        html_content = email_data.get('body_html', '')
        
        # 检查邮件是否已存在
        existing_message = session.query(EmailMessage).options(
            selectinload(EmailMessage.attachments)
        ).filter_by(
            account_id=account_id, message_id=message_id
        ).first()
        if existing_message:
            return existing_message.id
        
        # 创建新邮件记录
        message = EmailMessage(
            account_id=account_id,
            message_id=message_id,
            subject=subject,
            sender=sender,
            recipients=json.dumps(recipients),
            cc=json.dumps(cc) if cc else None,
            bcc=json.dumps(bcc) if bcc else None,
            date=date,
            in_reply_to=in_reply_to,
            text_content=text_content,
            html_content=html_content,
            is_read=False,
            is_deleted=False
        )
        session.add(message)
        session.commit()
        
        # 处理附件
        attachments = email_data.get('attachments', [])
        if attachments:
            for attachment in attachments:
                filename = attachment.get('filename', '')
                content_type = attachment.get('content_type', '')
                
                if filename and content_type:
                    att = EmailAttachment(
                        message_id=message.id,
                        filename=filename,
                        content_type=content_type
                    )
                    session.add(att)
            
            session.commit()
        
        return message.id
    except SQLAlchemyError as e:
        session.rollback()
        print(f"保存邮件元数据时出错: {e}")
        return None
    finally:
        session.close()

def get_all_active_accounts() -> List[EmailAccount]:
    """
    获取所有活跃的邮箱账户
    
    Returns:
        活跃账户列表
    """
    return AccountOperations.get_all_active_accounts()

def get_chat_ids_for_account(account_id: int) -> List[str]:
    """
    获取与邮件账户关联的聊天ID列表
    
    简化版：只返回配置中设置的所有者聊天ID
    该系统设计为只有一个用户（所有者）
    
    Args:
        account_id: 账户ID（在单用户系统中不使用此参数）
        
    Returns:
        聊天ID列表，只包含所有者的聊天ID
    """
    # 直接返回配置中设置的所有者聊天ID，不进行数据库查询
    if config.OWNER_CHAT_ID:
        return [config.OWNER_CHAT_ID]
    else:
        print("警告: OWNER_CHAT_ID未设置，无法发送通知")
        return []


def update_email_telegram_message_id(email_id: int, message_id: str) -> bool:
    """
    更新邮件的Telegram消息ID映射
    
    Args:
        email_id: 邮件ID
        message_id: Telegram消息ID
        
    Returns:
        是否成功
    """
    session = get_session()
    try:
        email = session.query(EmailMessage).filter_by(id=email_id).first()
        if not email:
            return False
        
        email.telegram_message_id = message_id
        session.commit()
        return True
    except SQLAlchemyError:
        session.rollback()
        return False
    finally:
        session.close()

def get_emails_with_telegram_ids(limit: int = 100, offset: int = 0) -> List[EmailMessage]:
    """
    获取带有Telegram消息ID的邮件列表
    
    Args:
        limit: 限制返回数量，避免一次查询过多记录
        offset: 查询偏移量，用于分页
        
    Returns:
        带有Telegram消息ID的邮件列表
    """
    session = get_session()
    try:
        return session.query(EmailMessage).options(
            selectinload(EmailMessage.attachments)
        ).filter(
            EmailMessage.telegram_message_id.isnot(None)  # 确保有Telegram消息ID
        ).order_by(
            EmailMessage.created_at.desc()                # 按创建时间倒序，优先检查最新的邮件
        ).offset(offset).limit(limit).all()
    except Exception as e:
        print(f"获取带有Telegram消息ID的邮件时出错: {e}")
        return []
    finally:
        session.close()

def get_message_by_message_id(message_id: str, account_id: int) -> Optional[EmailMessage]:
    """
    根据邮件的Message-ID查找邮件记录
    
    Args:
        message_id: 邮件的Message-ID
        account_id: 账户ID
        
    Returns:
        邮件记录或None
    """
    session = get_session()
    try:
        return session.query(EmailMessage).filter_by(
            message_id=message_id,
            account_id=account_id
        ).first()
    finally:
        session.close()

def _find_reference_telegram_message_id(in_reply_to: str, references: List[str], account_id: int) -> Optional[str]:
    """
    查找引用或回复的邮件对应的Telegram消息ID。
    
    首先检查in_reply_to，如果找不到再按照references列表从后向前查找。
    
    Args:
        in_reply_to: 邮件的In-Reply-To字段
        references: 邮件的References字段列表
        account_id: 账户ID
        
    Returns:
        对应的Telegram消息ID或None
    """
    session = get_session()
    try:
        # 首先检查in_reply_to，这是直接回复的邮件
        if in_reply_to:
            message = session.query(EmailMessage).filter_by(
                message_id=in_reply_to,
                account_id=account_id
            ).first()
            if message and message.telegram_message_id:
                return message.telegram_message_id
        
        # 如果找不到，检查references（从后向前，因为后面的通常是最近的引用）
        if references:
            # 从后向前查找，这样优先找到最近的引用
            for ref_id in reversed(references):
                message = session.query(EmailMessage).filter_by(
                    message_id=ref_id,
                    account_id=account_id
                ).first()
                if message and message.telegram_message_id:
                    return message.telegram_message_id
        
        return None
    except Exception as e:
        logger.error(f"查找引用邮件的Telegram消息ID时出错: {e}")
        return None
    finally:
        session.close()

def find_reference_telegram_message_id(in_reply_to: str, references: List[str], account_id: int) -> Optional[str]:
    """
    查找引用或回复的邮件对应的Telegram消息ID
    
    首先检查in_reply_to，如果找不到再按照references列表从后向前查找
    
    Args:
        in_reply_to: 邮件的In-Reply-To字段
        references: 邮件的References字段，通常是JSON字符串解析后的列表
        account_id: 账户ID
        
    Returns:
        对应的Telegram消息ID或None
    """
    session = get_session()
    try:
        # 首先检查in_reply_to
        if in_reply_to:
            message = session.query(EmailMessage).filter_by(
                message_id=in_reply_to,
                account_id=account_id
            ).first()
            if message and message.telegram_message_id:
                return message.telegram_message_id
        
        # 如果找不到，检查references（从后向前，因为后面的通常是最近的引用）
        if references:
            # 如果references是JSON字符串，先解析
            if isinstance(references, str):
                try:
                    references = json.loads(references)
                except json.JSONDecodeError:
                    references = []
            
            # 从后向前查找
            for ref_id in reversed(references):
                message = session.query(EmailMessage).filter_by(
                    message_id=ref_id,
                    account_id=account_id
                ).first()
                if message and message.telegram_message_id:
                    return message.telegram_message_id
        
        return None
    except Exception as e:
        print(f"查找引用邮件的Telegram消息ID时出错: {e}")
        return None
    finally:
        session.close()

def update_attachment_telegram_id(email_id: int, filename: str, telegram_file_id: str) -> bool:
    """
    更新附件的Telegram文件ID
    
    Args:
        email_id: 邮件ID
        filename: 附件文件名
        telegram_file_id: Telegram文件ID
        
    Returns:
        是否更新成功
    """
    session = get_session()
    try:
        attachment = session.query(EmailAttachment).filter_by(
            message_id=email_id,
            filename=filename
        ).first()
        
        if attachment:
            attachment.telegram_file_id = telegram_file_id
            session.commit()
            return True
        return False
    except Exception as e:
        print(f"更新附件的Telegram文件ID时出错: {e}")
        session.rollback()
        return False
    finally:
        session.close()

def get_attachment_telegram_ids(email_id: int) -> List[str]:
    """
    获取邮件所有附件的Telegram消息ID
    
    Args:
        email_id: 邮件ID
        
    Returns:
        附件的Telegram消息ID列表
    """
    session = get_session()
    try:
        attachments = session.query(EmailAttachment).filter_by(
            message_id=email_id
        ).all()
        
        # 收集所有非空的Telegram文件ID
        telegram_ids = []
        for attachment in attachments:
            if attachment.telegram_file_id:
                telegram_ids.append(attachment.telegram_file_id)
        
        return telegram_ids
    except Exception as e:
        print(f"获取附件的Telegram文件ID时出错: {e}")
        return []
    finally:
        session.close() 