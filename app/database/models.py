"""
Database models for TelegramMail.
"""

from datetime import datetime
from typing import List, Optional

from sqlalchemy import (
    Boolean,
    Column,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    create_engine,
)
from sqlalchemy.ext.declarative import declarative_base
from sqlalchemy.orm import relationship, sessionmaker

from app.utils.config import config

Base = declarative_base()


class EmailAccount(Base):
    """
    Model for email accounts.
    """

    __tablename__ = "email_accounts"

    id = Column(Integer, primary_key=True)

    # Account information
    email = Column(String(255), nullable=False, unique=True)
    name = Column(String(255), nullable=True)

    # IMAP settings
    imap_server = Column(String(255), nullable=False)
    imap_port = Column(Integer, nullable=False, default=993)
    imap_use_ssl = Column(Boolean, nullable=False, default=True)

    # SMTP settings
    smtp_server = Column(String(255), nullable=False)
    smtp_port = Column(Integer, nullable=False, default=465)
    smtp_use_ssl = Column(Boolean, nullable=False, default=True)

    # Credentials
    username = Column(String(255), nullable=False)
    password = Column(String(255), nullable=False)

    # Account settings
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    messages = relationship("EmailMessage", back_populates="account")

    def __repr__(self):
        return f"<EmailAccount {self.email}>"


class EmailMessage(Base):
    """
    Model for email messages.
    """

    __tablename__ = "email_messages"

    id = Column(Integer, primary_key=True)

    # Email metadata
    message_id = Column(String(255), nullable=False, index=True)
    account_id = Column(
        Integer, ForeignKey("email_accounts.id", ondelete="CASCADE"), nullable=False
    )

    # Email content
    subject = Column(Text, nullable=True)
    sender = Column(String(255), nullable=False)
    recipients = Column(Text, nullable=False)  # Stored as JSON
    cc = Column(Text, nullable=True)  # Stored as JSON
    bcc = Column(Text, nullable=True)  # Stored as JSON
    date = Column(DateTime, nullable=False)

    # Email内容
    text_content = Column(Text, nullable=True)  # 纯文本内容
    html_content = Column(Text, nullable=True)  # HTML内容

    # Reference fields for threading
    in_reply_to = Column(String(255), nullable=True)  # Direct parent message ID

    # Telegram mapping
    telegram_message_id = Column(String(255), nullable=True)

    # Status
    is_read = Column(Boolean, default=False)
    is_deleted = Column(Boolean, default=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    # Relationships
    account = relationship("EmailAccount", back_populates="messages")
    attachments = relationship(
        "EmailAttachment", back_populates="message", cascade="all, delete-orphan"
    )
    replies = relationship(
        "EmailReply", back_populates="email", cascade="all, delete-orphan"
    )
    forwards = relationship(
        "EmailForward", back_populates="email", cascade="all, delete-orphan"
    )

    @property
    def has_attachments(self) -> bool:
        """检查邮件是否有附件"""
        return len(self.attachments) > 0

    @property
    def attachment_count(self) -> int:
        """获取邮件附件数量"""
        return len(self.attachments)

    def __repr__(self):
        return f"<EmailMessage {self.subject}>"


class EmailAttachment(Base):
    """
    Model for email attachments.
    """

    __tablename__ = "email_attachments"

    id = Column(Integer, primary_key=True)

    # Attachment metadata
    message_id = Column(Integer, ForeignKey("email_messages.id"), nullable=False)
    filename = Column(String(255), nullable=False)
    content_type = Column(String(255), nullable=False)

    # Telegram file reference
    telegram_file_id = Column(String(255), nullable=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    # Relationships
    message = relationship("EmailMessage", back_populates="attachments")

    def __repr__(self):
        return f"<EmailAttachment {self.filename}>"


class UserSettings(Base):
    """
    Model for user notification settings.
    """

    __tablename__ = "user_settings"

    id = Column(Integer, primary_key=True)

    # Telegram chat ID
    chat_id = Column(String(255), nullable=False, unique=True, index=True)

    # Email notification settings
    receive_all_emails = Column(Boolean, default=True)
    receive_important_only = Column(Boolean, default=False)
    show_previews = Column(Boolean, default=True)
    show_full_content = Column(Boolean, default=True)  # 显示更完整的邮件内容
    show_attachments = Column(Boolean, default=True)  # 显示附件

    # Notification preferences
    notify_on_new_email = Column(Boolean, default=True)
    notify_with_sound = Column(Boolean, default=True)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)
    updated_at = Column(DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

    def __repr__(self):
        return f"<UserSettings chat_id={self.chat_id}>"


class UserAccountLink(Base):
    """
    Model for linking Telegram users to email accounts.
    """

    __tablename__ = "user_account_links"

    id = Column(Integer, primary_key=True)

    # Telegram chat ID
    chat_id = Column(String(255), nullable=False, index=True)

    # Email account ID
    account_id = Column(Integer, ForeignKey("email_accounts.id"), nullable=False)

    # Timestamps
    created_at = Column(DateTime, default=datetime.utcnow)

    def __repr__(self):
        return f"<UserAccountLink chat_id={self.chat_id}, account_id={self.account_id}>"


class EmailReply(Base):
    """邮件回复记录表"""

    __tablename__ = "email_replies"

    id = Column(Integer, primary_key=True)
    email_id = Column(Integer, ForeignKey("email_messages.id", ondelete="CASCADE"))
    reply_text = Column(Text)
    reply_date = Column(DateTime)
    sender = Column(String(255))

    # 添加关联关系
    email = relationship("EmailMessage", back_populates="replies")


class EmailForward(Base):
    """邮件转发记录表"""

    __tablename__ = "email_forwards"

    id = Column(Integer, primary_key=True)
    email_id = Column(Integer, ForeignKey("email_messages.id", ondelete="CASCADE"))
    forward_to = Column(String(255))  # 转发目标地址
    forward_note = Column(Text, nullable=True)  # 转发附言
    forward_date = Column(DateTime)
    sender = Column(String(255))

    # 添加关联关系
    email = relationship("EmailMessage", back_populates="forwards")


# Database setup
def init_db():
    """
    Initialize the database.

    Ensures the database directory exists and creates all tables.
    """
    # 确保数据库目录存在
    db_url = config.DATABASE_URL
    if db_url.startswith("sqlite:///"):
        # 解析SQLite文件路径
        import os
        from pathlib import Path

        # 提取数据库文件路径
        db_path = db_url.replace("sqlite:///", "")

        # 如果是相对路径，确保目录存在
        if not os.path.isabs(db_path):
            db_dir = os.path.dirname(db_path)
            if db_dir and not os.path.exists(db_dir):
                os.makedirs(db_dir, exist_ok=True)

    # 创建引擎并初始化表
    engine = create_engine(config.DATABASE_URL)
    Base.metadata.create_all(engine)
    return engine


def get_session():
    """
    Get a database session.
    """
    engine = create_engine(config.DATABASE_URL)
    Session = sessionmaker(bind=engine)
    return Session()
