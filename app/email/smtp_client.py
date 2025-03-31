"""
SMTP client for sending emails.
"""

import smtplib
import asyncio
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple, Union

from app.database.models import EmailAccount
from app.utils.config import config


class SMTPClient:
    """
    SMTP client for sending emails.
    """

    def __init__(
        self,
        account=None,
        server=None,
        port=None,
        username=None,
        password=None,
        use_ssl=False,
    ):
        """
        Initialize the SMTP client.

        Args:
            account: Email account object (optional)
            server: SMTP server address (optional, required if account is None)
            port: SMTP server port (optional, required if account is None)
            username: Username (optional, required if account is None)
            password: Password (optional, required if account is None)
            use_ssl: Whether to use SSL (optional, required if account is None)
        """
        self.account = account
        self.server = server if account is None else account.smtp_server
        self.port = port if account is None else account.smtp_port
        self.username = username if account is None else account.username
        self.password = password if account is None else account.password
        self.use_ssl = use_ssl if account is None else account.smtp_use_ssl
        self.conn = None

    async def connect(self) -> bool:
        """
        Connect to the SMTP server.

        Returns:
            True if the connection was successful, False otherwise
        """
        max_retries = 3
        retry_count = 0

        while retry_count <= max_retries:
            try:
                # 首先尝试根据配置连接
                if self.use_ssl or self.port == 465:  # 标准SSL/TLS端口
                    try:
                        # 添加更多SSL上下文参数，解决版本不兼容问题
                        import ssl

                        context = ssl.create_default_context()
                        self.conn = smtplib.SMTP_SSL(
                            self.server, self.port, context=context
                        )
                        # 设置超时
                        self.conn.timeout = 30
                        # 确保先进行EHLO
                        self.conn.ehlo()
                        self.conn.login(self.username, self.password)
                        return True
                    except Exception as e:
                        print(f"使用SSL连接失败: {e}，尝试非SSL方式...")
                        # 失败则尝试非SSL方式

                # 尝试使用STARTTLS (端口587/25通常使用这种方式)
                try:
                    self.conn = smtplib.SMTP(self.server, self.port)
                    # 设置超时
                    self.conn.timeout = 30
                    self.conn.ehlo()
                    if self.conn.has_extn("STARTTLS"):
                        try:
                            import ssl

                            context = ssl.create_default_context()
                            self.conn.starttls(context=context)
                            self.conn.ehlo()  # 必须在STARTTLS后再次调用EHLO
                        except Exception as tls_e:
                            print(f"STARTTLS失败: {tls_e}，尝试没有TLS的连接...")
                    self.conn.login(self.username, self.password)
                    return True
                except Exception as e:
                    print(f"使用STARTTLS连接失败: {e}")

                    # 如果当前端口连接失败，尝试其他常见端口
                    if self.port not in [465, 587, 25]:
                        for port in [465, 587, 25]:
                            print(f"尝试在端口 {port} 连接...")
                            try:
                                if port == 465:
                                    import ssl

                                    context = ssl.create_default_context()
                                    self.conn = smtplib.SMTP_SSL(
                                        self.server, port, context=context
                                    )
                                    self.conn.timeout = 30
                                    self.conn.ehlo()
                                else:
                                    self.conn = smtplib.SMTP(self.server, port)
                                    self.conn.timeout = 30
                                    self.conn.ehlo()
                                    if self.conn.has_extn("STARTTLS"):
                                        try:
                                            import ssl

                                            context = ssl.create_default_context()
                                            self.conn.starttls(context=context)
                                            self.conn.ehlo()  # 必须在STARTTLS后再次调用EHLO
                                        except Exception as tls_e:
                                            print(
                                                f"STARTTLS失败: {tls_e}，尝试没有TLS的连接..."
                                            )

                                self.conn.login(self.username, self.password)
                                # 更新端口和SSL设置
                                self.port = port
                                self.use_ssl = port == 465
                                print(f"成功连接到端口 {port}, SSL: {self.use_ssl}")
                                return True
                            except Exception as inner_e:
                                print(f"在端口 {port} 连接失败: {inner_e}")

                    return False

            except (smtplib.SMTPException, ConnectionRefusedError, OSError) as e:
                retry_count += 1
                if retry_count > max_retries:
                    print(f"SMTP服务器连接失败(达到最大重试次数{max_retries}): {e}")
                    return False
                print(f"SMTP服务器连接失败(第{retry_count}次重试): {e}")
                await asyncio.sleep(1)  # 重试前等待1秒

    def disconnect(self):
        """
        Disconnect from the SMTP server.
        """
        if self.conn:
            try:
                self.conn.quit()
            except smtplib.SMTPException:
                pass
            self.conn = None

    async def test_connection(self) -> bool:
        """
        测试SMTP连接

        Returns:
            连接是否成功
        """
        loop = asyncio.get_event_loop()
        try:
            # 在异步环境中运行同步代码
            return await loop.run_in_executor(None, self._test_connection_sync)
        except Exception as e:
            print(f"测试SMTP连接时出错: {e}")
            return False

    def _test_connection_sync(self) -> bool:
        """同步测试SMTP连接，尝试多种连接方式"""
        # 先尝试根据端口使用推荐的连接方式
        if self._try_connection(self.port):
            return True

        # 如果失败，尝试其他常见端口
        alternative_ports = [465, 587, 25]
        for port in alternative_ports:
            if port != self.port and self._try_connection(port):
                # 更新实例的端口和SSL设置
                self.port = port
                self.use_ssl = port == 465
                print(f"找到可用的SMTP配置: 端口 {port}, SSL: {self.use_ssl}")
                return True

        return False

    def _try_connection(self, port: int) -> bool:
        """尝试使用指定端口连接SMTP服务器"""
        try:
            if port == 465:  # 标准SSL端口
                import ssl

                context = ssl.create_default_context()
                conn = smtplib.SMTP_SSL(self.server, port, timeout=10, context=context)
                conn.ehlo()  # 确保先进行EHLO
                conn.login(self.username, self.password)
                conn.quit()
                return True
            else:  # 尝试使用STARTTLS (端口587/25)
                try:
                    conn = smtplib.SMTP(self.server, port, timeout=10)
                    conn.ehlo()
                    if conn.has_extn("STARTTLS"):
                        import ssl

                        context = ssl.create_default_context()
                        conn.starttls(context=context)
                        conn.ehlo()  # 必须在STARTTLS后再次调用EHLO
                    conn.login(self.username, self.password)
                    conn.quit()
                    return True
                except Exception as e:
                    print(f"尝试在端口 {port} 使用STARTTLS连接失败: {e}")
                    return False
        except Exception as e:
            print(f"尝试在端口 {port} 连接SMTP服务器失败: {e}")
            return False

    async def send_email(
        self,
        from_addr: str,
        to_addrs: Union[str, List[str]],
        subject: str,
        text_body: str,
        html_body: Optional[str] = None,
        cc_addrs: Optional[List[str]] = None,
        bcc_addrs: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
        attachments: Optional[List[Dict]] = None,
        max_retries: int = 3,
    ) -> bool:
        """
        异步发送邮件

        Args:
            from_addr: 发件人地址
            to_addrs: 收件人地址（单个字符串或字符串列表）
            subject: 邮件主题
            text_body: 纯文本内容
            html_body: HTML内容(可选)
            cc_addrs: 抄送地址列表(可选)
            bcc_addrs: 密送地址列表(可选)
            reply_to: 回复地址(可选)
            attachments: 附件列表(可选)，格式为[{filename, content, content_type}]
            max_retries: 最大重试次数(默认3次)

        Returns:
            是否发送成功
        """
        loop = asyncio.get_event_loop()
        try:
            # 在异步环境中运行同步代码
            return await loop.run_in_executor(
                None,
                lambda: self._send_email_sync(
                    from_addr,
                    to_addrs,
                    subject,
                    text_body,
                    html_body,
                    cc_addrs,
                    bcc_addrs,
                    reply_to,
                    attachments,
                    max_retries,
                ),
            )
        except Exception as e:
            print(f"发送邮件时发生错误: {e}")
            return False

    def _send_email_sync(
        self,
        from_addr: str,
        to_addrs: Union[str, List[str]],
        subject: str,
        text_body: str,
        html_body: Optional[str] = None,
        cc_addrs: Optional[List[str]] = None,
        bcc_addrs: Optional[List[str]] = None,
        reply_to: Optional[str] = None,
        attachments: Optional[List[Dict]] = None,
        max_retries: int = 3,
    ) -> bool:
        """同步发送邮件"""
        retry_count = 0

        while retry_count <= max_retries:
            try:
                # 创建一个多部分消息
                msg = MIMEMultipart("alternative")
                msg["Subject"] = subject
                msg["From"] = from_addr

                # 确保 to_addrs 是列表，并正确处理可能包含逗号的字符串
                if isinstance(to_addrs, str):
                    # 如果字符串中包含逗号，按逗号分割
                    if "," in to_addrs:
                        to_addrs_list = [
                            addr.strip() for addr in to_addrs.split(",") if addr.strip()
                        ]
                    else:
                        to_addrs_list = [to_addrs.strip()]
                else:
                    to_addrs_list = [addr.strip() for addr in to_addrs if addr.strip()]

                msg["To"] = ", ".join(to_addrs_list)

                if cc_addrs:
                    # 确保 cc_addrs 列表中的每个项目都不包含逗号
                    processed_cc = []
                    for addr in cc_addrs:
                        if isinstance(addr, str) and "," in addr:
                            # 如果字符串中包含逗号，按逗号分割
                            processed_cc.extend(
                                [a.strip() for a in addr.split(",") if a.strip()]
                            )
                        elif addr:
                            processed_cc.append(addr.strip())
                    cc_addrs = processed_cc
                    msg["Cc"] = ", ".join(cc_addrs)

                if reply_to:
                    msg["Reply-To"] = reply_to

                # 添加文本和HTML部分
                msg.attach(MIMEText(text_body, "plain"))
                if html_body:
                    msg.attach(MIMEText(html_body, "html"))

                # 添加附件
                if attachments:
                    for attachment in attachments:
                        part = MIMEApplication(attachment["content"])
                        part.add_header(
                            "Content-Disposition",
                            "attachment",
                            filename=attachment["filename"],
                        )
                        if "content_type" in attachment:
                            part.add_header("Content-Type", attachment["content_type"])
                        msg.attach(part)

                # 确定所有收件人
                all_recipients = to_addrs_list.copy()
                if cc_addrs:
                    all_recipients.extend(cc_addrs)
                if bcc_addrs:
                    # 确保 bcc_addrs 列表中的每个项目都不包含逗号
                    processed_bcc = []
                    for addr in bcc_addrs:
                        if isinstance(addr, str) and "," in addr:
                            # 如果字符串中包含逗号，按逗号分割
                            processed_bcc.extend(
                                [a.strip() for a in addr.split(",") if a.strip()]
                            )
                        elif addr:
                            processed_bcc.append(addr.strip())
                    bcc_addrs = processed_bcc
                    all_recipients.extend(bcc_addrs)

                # 使用已有连接或建立新连接
                if not self.conn:
                    # 连接方式取决于之前成功连接的方式
                    if self.use_ssl:
                        import ssl

                        context = ssl.create_default_context()
                        self.conn = smtplib.SMTP_SSL(
                            self.server, self.port, context=context
                        )
                        self.conn.ehlo()  # 确保先执行EHLO
                    else:
                        self.conn = smtplib.SMTP(self.server, self.port)
                        self.conn.ehlo()
                        if self.conn.has_extn("STARTTLS"):
                            import ssl

                            context = ssl.create_default_context()
                            self.conn.starttls(context=context)
                            self.conn.ehlo()  # STARTTLS后再次EHLO

                    self.conn.login(self.username, self.password)

                # 发送邮件前确保连接仍然活跃
                try:
                    self.conn.noop()  # 发送NOOP命令测试连接状态
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPException):
                    # 如果连接已断开，重新连接
                    self.disconnect()
                    if self.use_ssl:
                        import ssl

                        context = ssl.create_default_context()
                        self.conn = smtplib.SMTP_SSL(
                            self.server, self.port, context=context
                        )
                        self.conn.ehlo()
                    else:
                        self.conn = smtplib.SMTP(self.server, self.port)
                        self.conn.ehlo()
                        if self.conn.has_extn("STARTTLS"):
                            import ssl

                            context = ssl.create_default_context()
                            self.conn.starttls(context=context)
                            self.conn.ehlo()

                    self.conn.login(self.username, self.password)

                # 发送邮件
                try:
                    # 使用显式的命令顺序，而不是直接使用sendmail
                    from_ok = self.conn.mail(from_addr)
                    if from_ok[0] != 250:
                        print(f"MAIL FROM 命令失败: {from_ok}")
                        raise smtplib.SMTPException(f"MAIL FROM 失败: {from_ok}")

                    # 对每个收件人执行RCPT TO
                    for recipient in all_recipients:
                        rcpt_ok = self.conn.rcpt(recipient)
                        if rcpt_ok[0] != 250 and rcpt_ok[0] != 251:
                            print(f"RCPT TO {recipient} 命令失败: {rcpt_ok}")
                            raise smtplib.SMTPException(f"RCPT TO 失败: {rcpt_ok}")

                    # 发送邮件数据
                    data_ok = self.conn.data(msg.as_string())
                    if data_ok[0] != 250:
                        print(f"DATA 命令失败: {data_ok}")
                        raise smtplib.SMTPException(f"DATA 失败: {data_ok}")

                    return True
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPException) as e:
                    # 如果发送失败，尝试重新连接并再次发送
                    retry_count += 1
                    print(
                        f"发送邮件失败，第 {retry_count} 次重试 (最大 {max_retries} 次): {e}"
                    )

                    # 如果已经到达最大重试次数，返回失败
                    if retry_count > max_retries:
                        print(f"达到最大重试次数 ({max_retries})，发送失败")
                        return False

                    # 重试前断开连接
                    self.disconnect()

                    # 尝试不同的连接方法
                    connected = False

                    # 尝试SSL连接
                    try:
                        if self.port == 465 or self.use_ssl:
                            import ssl

                            context = ssl.create_default_context()
                            self.conn = smtplib.SMTP_SSL(
                                self.server, self.port, context=context
                            )
                            self.conn.ehlo()
                            self.conn.login(self.username, self.password)
                            connected = True
                    except Exception as ssl_e:
                        print(f"SSL连接失败: {ssl_e}")

                    # 如果SSL失败，尝试STARTTLS
                    if not connected:
                        try:
                            self.conn = smtplib.SMTP(self.server, self.port)
                            self.conn.ehlo()
                            if self.conn.has_extn("STARTTLS"):
                                import ssl

                                context = ssl.create_default_context()
                                self.conn.starttls(context=context)
                                self.conn.ehlo()
                            self.conn.login(self.username, self.password)
                            connected = True
                        except Exception as tls_e:
                            print(f"STARTTLS连接失败: {tls_e}")

                    # 如果重新连接失败，继续下一次重试
                    if not connected:
                        print(f"第 {retry_count} 次重试连接失败")
                        continue

            except Exception as e:
                retry_count += 1
                print(
                    f"发送邮件时发生错误，第 {retry_count} 次重试 (最大 {max_retries} 次): {e}"
                )

                # 如果已经到达最大重试次数，返回失败
                if retry_count > max_retries:
                    print(f"达到最大重试次数 ({max_retries})，发送失败")
                    return False

                # 重试前断开连接
                self.disconnect()

        # 如果所有尝试都失败，返回失败
        return False

    def send_reply(
        self,
        original_subject: str,
        original_sender: str,
        recipients: List[str],
        body_text: str,
        body_html: Optional[str] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Tuple[str, bytes, Optional[str]]]] = None,
    ) -> bool:
        """
        Send a reply to an email.

        Args:
            original_subject: Original email subject
            original_sender: Original sender email address
            recipients: List of recipient email addresses
            body_text: Plain text message body
            body_html: HTML message body (optional)
            cc: List of CC recipients (optional)
            bcc: List of BCC recipients (optional)
            attachments: List of (filename, data, content_type) tuples (optional)

        Returns:
            True if the email was sent successfully, False otherwise
        """
        # Add Re: prefix if not already present
        if not original_subject.lower().startswith("re:"):
            subject = f"Re: {original_subject}"
        else:
            subject = original_subject

        return self.send_email(
            subject=subject,
            to_addrs=recipients,
            text_body=body_text,
            html_body=body_html,
            cc_addrs=cc,
            bcc_addrs=bcc,
            attachments=attachments,
        )

    def send_forward(
        self,
        original_subject: str,
        recipients: List[str],
        body_text: str,
        body_html: Optional[str] = None,
        cc: Optional[List[str]] = None,
        bcc: Optional[List[str]] = None,
        attachments: Optional[List[Tuple[str, bytes, Optional[str]]]] = None,
    ) -> bool:
        """
        Forward an email.

        Args:
            original_subject: Original email subject
            recipients: List of recipient email addresses
            body_text: Plain text message body
            body_html: HTML message body (optional)
            cc: List of CC recipients (optional)
            bcc: List of BCC recipients (optional)
            attachments: List of (filename, data, content_type) tuples (optional)

        Returns:
            True if the email was sent successfully, False otherwise
        """
        # Add Fwd: prefix if not already present
        if not original_subject.lower().startswith("fwd:"):
            subject = f"Fwd: {original_subject}"
        else:
            subject = original_subject

        return self.send_email(
            subject=subject,
            to_addrs=recipients,
            text_body=body_text,
            html_body=body_html,
            cc_addrs=cc,
            bcc_addrs=bcc,
            attachments=attachments,
        )
