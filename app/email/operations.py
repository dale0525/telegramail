"""
EmailOperations类 - 用于处理邮件操作
"""
import logging
import traceback
from datetime import datetime
from typing import Optional, List, Dict, Any, Tuple

from app.database.operations import (
    get_email_by_id, get_email_account_by_id, add_reply_to_email,
    add_forward_from_email
)
from app.email.smtp_client import SMTPClient

logger = logging.getLogger(__name__)

class EmailOperations:
    """封装所有与邮件相关的操作"""
    
    @staticmethod
    async def reply_to_email(email_id: int, reply_text: str, sender_name: str = None) -> Tuple[bool, str]:
        """
        回复指定邮件
        
        Args:
            email_id: 要回复的邮件ID
            reply_text: 回复内容
            sender_name: 发件人名称，可选
            
        Returns:
            (成功状态, 错误消息或成功消息)
        """
        # 获取原始邮件信息
        email = get_email_by_id(email_id)
        if not email:
            return False, "找不到原始邮件"
        
        # 获取账户信息
        account = get_email_account_by_id(email.account_id)
        if not account:
            return False, "找不到邮件账户"
        
        try:
            # 创建SMTP客户端
            smtp_client = SMTPClient(
                server=account.smtp_server,
                port=account.smtp_port,
                username=account.username,
                password=account.password
            )
            
            # 准备回复邮件
            to_addresses = [email.sender]
            
            # 如果原始邮件有回复地址，优先使用
            if email.reply_to:
                to_addresses = [email.reply_to]
            
            # 设置主题（如果原主题不包含Re:，则添加）
            subject = email.subject
            if not subject.lower().startswith("re:"):
                subject = f"Re: {subject}"
            
            # 构建回复引用
            quoted_body = f"\n\n-------- 原始邮件 --------\n发件人: {email.sender}\n日期: {email.date}\n主题: {email.subject}\n\n{email.text_content or email.html_content or '(邮件内容为空或不支持的格式)'}"
            
            # 完整回复内容
            full_reply = f"{reply_text}{quoted_body}"
            
            # 发送邮件
            from_addr = account.email
            if sender_name:
                from_addr = f"{sender_name} <{account.email}>"
            
            success = await smtp_client.send_email(
                from_addr=from_addr,
                to_addrs=to_addresses,
                subject=subject,
                text_body=full_reply,
                cc_addrs=[],
                bcc_addrs=[],
                reply_to=account.email
            )
            
            if success:
                # 记录回复信息到数据库
                reply_id = add_reply_to_email(
                    email_id=email_id,
                    reply_text=reply_text,
                    reply_date=datetime.now(),
                    sender=account.email
                )
                
                if reply_id:
                    return True, f"邮件回复成功发送至 {to_addresses[0]}"
                else:
                    return True, f"邮件已发送，但记录回复信息失败"
            else:
                return False, "发送回复邮件失败"
        
        except Exception as e:
            logger.error(f"回复邮件时出错: {str(e)}")
            logger.error(traceback.format_exc())
            return False, f"回复邮件时出错: {str(e)}"
    
    @staticmethod
    async def forward_email(email_id: int, to_address: str, forward_note: str = None, sender_name: str = None) -> Tuple[bool, str]:
        """
        转发指定邮件
        
        Args:
            email_id: 要转发的邮件ID
            to_address: 转发目标地址
            forward_note: 转发附言，可选
            sender_name: 发件人名称，可选
            
        Returns:
            (成功状态, 错误消息或成功消息)
        """
        # 获取原始邮件信息
        email = get_email_by_id(email_id)
        if not email:
            return False, "找不到原始邮件"
        
        # 获取账户信息
        account = get_email_account_by_id(email.account_id)
        if not account:
            return False, "找不到邮件账户"
        
        try:
            # 创建SMTP客户端
            smtp_client = SMTPClient(
                server=account.smtp_server,
                port=account.smtp_port,
                username=account.username,
                password=account.password
            )
            
            # 设置主题（如果原主题不包含Fwd:，则添加）
            subject = email.subject
            if not subject.lower().startswith("fwd:") and not subject.lower().startswith("fw:"):
                subject = f"Fwd: {subject}"
            
            # 构建转发邮件内容
            forward_header = f"-------- 转发邮件 --------\n发件人: {email.sender}\n收件人: {email.recipient}\n日期: {email.date}\n主题: {email.subject}\n"
            
            # 添加转发附言
            note_text = ""
            if forward_note:
                note_text = f"{forward_note}\n\n"
            
            # 根据可用内容选择使用文本或HTML
            if email.html_content:
                # 如果有HTML内容，构建HTML格式的转发邮件
                html_content = f"<p>{note_text}</p><hr><p><b>-------- 转发邮件 --------</b><br>发件人: {email.sender}<br>收件人: {email.recipient}<br>日期: {email.date}<br>主题: {email.subject}</p><hr>{email.html_content}"
                # 同时准备纯文本版本
                text_content = f"{note_text}{forward_header}\n\n{email.text_content or '(仅HTML内容，请查看HTML版本)'}"
            else:
                # 只有纯文本内容
                html_content = None
                text_content = f"{note_text}{forward_header}\n\n{email.text_content or '(邮件内容为空或不支持的格式)'}"
            
            # 准备附件
            attachments = []
            if email.has_attachments:
                # TODO: 实现附件获取和转发
                pass
            
            # 发送邮件
            from_addr = account.email
            if sender_name:
                from_addr = f"{sender_name} <{account.email}>"
            
            success = await smtp_client.send_email(
                from_addr=from_addr,
                to_addrs=[to_address],
                subject=subject,
                text_body=text_content,
                html_body=html_content,
                cc_addrs=[],
                bcc_addrs=[],
                reply_to=account.email,
                attachments=attachments
            )
            
            if success:
                # 记录转发信息到数据库
                forward_id = add_forward_from_email(
                    email_id=email_id,
                    forward_to=to_address,
                    forward_note=forward_note,
                    forward_date=datetime.now(),
                    sender=account.email
                )
                
                if forward_id:
                    return True, f"邮件已成功转发至 {to_address}"
                else:
                    return True, f"邮件已转发，但记录转发信息失败"
            else:
                return False, "发送转发邮件失败"
        
        except Exception as e:
            logger.error(f"转发邮件时出错: {str(e)}")
            logger.error(traceback.format_exc())
            return False, f"转发邮件时出错: {str(e)}" 