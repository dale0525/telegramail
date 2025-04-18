"""
SMTP client for sending emails.
"""

import logging
import smtplib
import asyncio
from email.mime.application import MIMEApplication
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from typing import Dict, List, Optional, Tuple, Union
import ssl
import time

logger = logging.getLogger(__name__)


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
        初始化SMTP客户端。

        Args:
            account: 邮箱账户对象（可选）
            server: SMTP服务器地址（如果account为None则必需）
            port: SMTP服务器端口（如果account为None则必需）
            username: 用户名（如果account为None则必需）
            password: 密码（如果account为None则必需）
            use_ssl: 是否使用SSL（如果account为None则必需）
        """
        self.account = account
        self.server = server if account is None else account.smtp_server
        self.port = port if account is None else account.smtp_port
        self.username = username if account is None else account.username
        self.password = password if account is None else account.password
        self.use_ssl = use_ssl if account is None else account.smtp_use_ssl
        self.conn = None
        self.last_error = None
        self.connection_method = None
        self.error_analysis = {}  # 存储分析的错误信息

    async def connect(self) -> bool:
        """
        连接到 SMTP 服务器。

        尝试通过多种连接方式（SSL, STARTTLS, 普通连接）连接到SMTP服务器，
        并自动适应不同的服务器要求。

        Returns:
            bool: 连接成功返回 True，否则返回 False
        """
        max_retries = 3
        retry_count = 0
        backoff_factor = 1.5  # 用于指数退避策略
        timeout = 30

        # 首先尝试使用指定的端口和SSL设置
        if self.use_ssl is not None:
            # 当端口是465但use_ssl被明确设置为False时，调整连接尝试顺序
            if self.port == 465 and not self.use_ssl:
                logger.warning("检测到端口465但SSL设置为False，可能存在配置错误")
                connection_attempts = [
                    self._try_starttls_connection,
                    self._try_ssl_connection,
                ]
            # 当端口是587/25但use_ssl被明确设置为True时，调整连接尝试顺序
            elif self.port in [587, 25] and self.use_ssl:
                logger.warning(
                    f"检测到端口{self.port}但SSL设置为True，可能存在配置错误"
                )
                connection_attempts = [
                    self._try_ssl_connection,
                    self._try_starttls_connection,
                ]
            else:
                connection_attempts = [self._try_preferred_connection]
        else:
            # 如果未指定连接方式，根据端口号自动选择尝试顺序
            if self.port == 465:
                connection_attempts = [
                    self._try_ssl_connection,
                    self._try_starttls_connection,
                ]
            elif self.port in [587, 25]:
                connection_attempts = [
                    self._try_starttls_connection,
                    self._try_ssl_connection,
                ]
            else:
                # 对于其他端口，尝试所有方法
                connection_attempts = [
                    self._try_ssl_connection,
                    self._try_starttls_connection,
                    self._try_plain_connection,
                ]

        # 记录所有尝试中的错误，以便后续分析
        all_errors = []

        while retry_count <= max_retries:
            for connection_method in connection_attempts:
                method_name = connection_method.__name__.replace("_try_", "").replace(
                    "_connection", ""
                )
                try:
                    logger.info(
                        f"尝试使用 {method_name} 连接 {self.server}:{self.port}"
                    )
                    if await connection_method(timeout):
                        logger.info(f"SMTP连接成功，使用方法: {self.connection_method}")
                        return True
                except Exception as e:
                    error_info = {
                        "method": method_name,
                        "error": e,
                        "error_type": type(e).__name__,
                        "error_msg": str(e),
                    }
                    all_errors.append(error_info)

                    self.last_error = str(e)
                    logger.info(f"使用 {method_name} 连接失败: {e}")

                    # 特殊处理SSL错误
                    if isinstance(e, ssl.SSLError) and "wrong version number" in str(e):
                        logger.warning(
                            f"检测到SSL版本不匹配错误，服务器可能不支持SSL直连而需要STARTTLS"
                        )
                        # 如果是SSL错误并且下一个尝试不是STARTTLS，则立即尝试STARTTLS
                        if method_name.upper() == "SSL" and "STARTTLS" not in str(
                            connection_attempts[0]
                        ):
                            logger.info("自动调整连接策略，优先尝试STARTTLS")
                            # 将STARTTLS连接方式移到尝试列表前面
                            starttls_methods = [
                                m
                                for m in connection_attempts
                                if "starttls" in m.__name__.lower()
                            ]
                            if starttls_methods:
                                connection_attempts.remove(starttls_methods[0])
                                connection_attempts.insert(0, starttls_methods[0])

            # 所有方法都失败，准备重试
            retry_count += 1
            if retry_count <= max_retries:
                wait_time = backoff_factor**retry_count
                logger.info(
                    f"SMTP服务器连接失败，第{retry_count}次重试，等待{wait_time}秒"
                )
                await asyncio.sleep(wait_time)  # 使用指数退避策略

        # 所有尝试都失败，进行错误分析
        if all_errors:
            # 分析最后一个错误，通常是最有代表性的
            last_error = all_errors[-1]["error"]
            analysis = await self._analyze_connection_error(
                last_error, self.server, self.port, all_errors[-1]["method"]
            )

            # 记录分析结果
            if "problem" in analysis:
                logger.error(f"SMTP连接问题: {analysis['problem']}")

            if "suggestions" in analysis and analysis["suggestions"]:
                logger.info("连接建议:")
                for suggestion in analysis["suggestions"]:
                    logger.info(f"  - {suggestion}")

            if "alternative_ports" in analysis and analysis["alternative_ports"]:
                alt_ports = ", ".join(map(str, analysis["alternative_ports"]))
                logger.info(f"可尝试的替代端口: {alt_ports}")

            if "service_detected" in analysis:
                logger.info(f"检测到服务: {analysis['service_detected']}")
                if "suggested_settings" in analysis:
                    settings = analysis["suggested_settings"]
                    port_str = ", ".join(map(str, settings.get("ports", [])))
                    logger.info(
                        f"推荐设置: 服务器 {settings.get('server')}, 端口 {port_str}"
                    )
                    if "notes" in settings:
                        logger.info(f"注意: {settings['notes']}")

        logger.error(
            f"SMTP服务器连接失败(达到最大重试次数{max_retries})，最后错误: {self.last_error}"
        )
        return False

    async def _try_preferred_connection(self, timeout: int) -> bool:
        """根据预先配置的设置尝试连接"""
        if self.use_ssl:
            return await self._try_ssl_connection(timeout)
        else:
            return await self._try_starttls_connection(timeout)

    async def _try_ssl_connection(self, timeout: int) -> bool:
        """尝试使用SSL连接SMTP服务器"""
        loop = asyncio.get_event_loop()
        try:
            # 在异步环境中运行同步的SMTP连接代码
            self.conn = await loop.run_in_executor(
                None, lambda: self._create_ssl_connection(timeout)
            )
            await loop.run_in_executor(None, self.conn.ehlo)
            await loop.run_in_executor(
                None, lambda: self.conn.login(self.username, self.password)
            )
            self.connection_method = "SSL"
            return True
        except Exception as e:
            logger.info(f"使用SSL连接失败: {e}")
            self.disconnect()  # 确保失败后断开连接
            return False

    def _create_ssl_connection(self, timeout: int) -> smtplib.SMTP_SSL:
        """创建SSL连接"""
        # 创建自定义SSL上下文，支持多种协议版本
        context = ssl.create_default_context()

        # 尝试支持更多的SSL协议版本
        try:
            # 允许旧版本SSL/TLS协议，以提高兼容性
            context.options &= ~ssl.OP_NO_TLSv1
            context.options &= ~ssl.OP_NO_TLSv1_1
            # 禁用证书验证，用于一些使用自签名证书的服务器
            # context.check_hostname = False
            # context.verify_mode = ssl.CERT_NONE
        except AttributeError:
            # 某些Python版本可能不支持特定的SSL选项
            logger.info("当前Python环境不支持自定义SSL选项设置")

        try:
            # 尝试使用自定义SSL上下文创建连接
            conn = smtplib.SMTP_SSL(
                self.server, self.port, timeout=timeout, context=context
            )
            return conn
        except ssl.SSLError as e:
            # 特殊处理SSL版本错误
            if "wrong version number" in str(e):
                logger.warning("SSL版本不匹配，服务器可能不支持SSL直接连接")
                raise ssl.SSLError(f"SSL版本不匹配，尝试使用STARTTLS替代: {e}")
            raise

    async def _try_starttls_connection(self, timeout: int) -> bool:
        """尝试使用STARTTLS连接SMTP服务器"""
        loop = asyncio.get_event_loop()
        try:
            # 创建连接
            self.conn = await loop.run_in_executor(
                None, lambda: self._create_plain_connection(timeout)
            )
            # EHLO
            await loop.run_in_executor(None, self.conn.ehlo)

            # 检查STARTTLS支持
            if self.conn.has_extn("STARTTLS"):
                context = ssl.create_default_context()
                await loop.run_in_executor(
                    None, lambda: self.conn.starttls(context=context)
                )
                await loop.run_in_executor(
                    None, self.conn.ehlo
                )  # STARTTLS后需要再次EHLO

            # 登录
            await loop.run_in_executor(
                None, lambda: self.conn.login(self.username, self.password)
            )
            self.connection_method = "STARTTLS"
            return True
        except Exception as e:
            logger.info(f"使用STARTTLS连接失败: {e}")
            self.disconnect()  # 确保失败后断开连接
            return False

    async def _try_plain_connection(self, timeout: int) -> bool:
        """尝试使用普通非加密连接SMTP服务器"""
        loop = asyncio.get_event_loop()
        try:
            # 创建连接
            self.conn = await loop.run_in_executor(
                None, lambda: self._create_plain_connection(timeout)
            )
            # EHLO
            await loop.run_in_executor(None, self.conn.ehlo)
            # 登录
            await loop.run_in_executor(
                None, lambda: self.conn.login(self.username, self.password)
            )
            self.connection_method = "PLAIN"
            logger.warning("使用非加密连接到SMTP服务器，不推荐用于生产环境")
            return True
        except Exception as e:
            logger.info(f"使用普通连接失败: {e}")
            self.disconnect()  # 确保失败后断开连接
            return False

    def _create_plain_connection(self, timeout: int) -> smtplib.SMTP:
        """创建普通SMTP连接"""
        conn = smtplib.SMTP(self.server, self.port, timeout=timeout)
        return conn

    def disconnect(self):
        """
        断开与SMTP服务器的连接。
        """
        if self.conn:
            try:
                self.conn.quit()
            except (smtplib.SMTPException, OSError, IOError):
                try:
                    self.conn.close()
                except:
                    pass
            self.conn = None
            self.connection_method = None

    async def test_connection(self) -> bool:
        """
        测试SMTP连接

        Returns:
            连接是否成功
        """
        try:
            # 先尝试连接
            if await self.connect():
                # 连接成功后立即断开
                self.disconnect()
                return True
            return False
        except Exception as e:
            logger.info(f"测试SMTP连接时出错: {e}")
            self.disconnect()  # 确保失败后断开连接
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
                    logger.info(f"尝试在端口 {port} 使用STARTTLS连接失败: {e}")
                    return False
        except Exception as e:
            logger.info(f"尝试在端口 {port} 连接SMTP服务器失败: {e}")
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
            logger.info(f"发送邮件时发生错误: {e}")
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
                    # 尝试连接
                    if not self._try_reconnect():
                        logger.error("无法建立SMTP连接，邮件发送失败")
                        # 尝试下一次重试
                        retry_count += 1
                        wait_time = 1.5**retry_count  # 指数退避
                        if retry_count <= max_retries:
                            logger.info(
                                f"将在 {wait_time:.1f} 秒后重试连接 ({retry_count}/{max_retries})"
                            )
                            time.sleep(wait_time)  # 使用同步sleep而不是异步sleep
                        continue

                # 发送邮件前确保连接仍然活跃
                try:
                    self.conn.noop()  # 发送NOOP命令测试连接状态
                except (smtplib.SMTPServerDisconnected, smtplib.SMTPException):
                    # 如果连接已断开，重新连接
                    self.disconnect()
                    if not self._try_reconnect():
                        logger.error("尝试重新连接SMTP服务器失败，邮件发送失败")
                        # 尝试下一次重试
                        retry_count += 1
                        wait_time = 1.5**retry_count  # 指数退避
                        if retry_count <= max_retries:
                            logger.info(
                                f"将在 {wait_time:.1f} 秒后重试连接 ({retry_count}/{max_retries})"
                            )
                            time.sleep(wait_time)  # 使用同步sleep而不是异步sleep
                        continue

                # 发送邮件
                try:
                    # 使用显式的命令顺序，而不是直接使用sendmail
                    from_ok = self.conn.mail(from_addr)
                    if from_ok[0] != 250:
                        logger.error(f"MAIL FROM 命令失败: {from_ok}")
                        raise smtplib.SMTPException(f"MAIL FROM 失败: {from_ok}")

                    # 对每个收件人执行RCPT TO
                    for recipient in all_recipients:
                        rcpt_ok = self.conn.rcpt(recipient)
                        if rcpt_ok[0] != 250 and rcpt_ok[0] != 251:
                            logger.error(f"RCPT TO {recipient} 命令失败: {rcpt_ok}")
                            raise smtplib.SMTPException(f"RCPT TO 失败: {rcpt_ok}")

                    # 发送邮件数据
                    data_ok = self.conn.data(msg.as_string())
                    if data_ok[0] != 250:
                        logger.error(f"DATA 命令失败: {data_ok}")
                        raise smtplib.SMTPException(f"DATA 失败: {data_ok}")

                    logger.info(f"邮件成功发送至 {len(all_recipients)} 个收件人")
                    return True

                except (smtplib.SMTPServerDisconnected, smtplib.SMTPException) as e:
                    # 连接或发送过程中出现错误
                    logger.error(f"发送邮件过程中出错: {e}")
                    self.disconnect()  # 确保断开有问题的连接

                    # 尝试下一次重试
                    retry_count += 1
                    if retry_count <= max_retries:
                        wait_time = 1.5**retry_count  # 指数退避
                        logger.info(
                            f"发送邮件失败，将在 {wait_time:.1f} 秒后第 {retry_count}/{max_retries} 次重试"
                        )
                        time.sleep(wait_time)  # 使用同步sleep而不是异步sleep
                    else:
                        logger.error(f"达到最大重试次数 ({max_retries})，邮件发送失败")
                        return False

            except Exception as e:
                # 捕获所有其他异常
                logger.error(f"发送邮件时发生未预期错误: {e}")
                self.disconnect()  # 确保断开连接

                # 尝试下一次重试
                retry_count += 1
                if retry_count <= max_retries:
                    wait_time = 1.5**retry_count  # 指数退避
                    logger.info(
                        f"发送邮件失败，将在 {wait_time:.1f} 秒后第 {retry_count}/{max_retries} 次重试"
                    )
                    time.sleep(wait_time)  # 使用同步sleep而不是异步sleep
                else:
                    logger.error(f"达到最大重试次数 ({max_retries})，邮件发送失败")
                    return False

        # 所有重试都失败
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

    def _try_reconnect(self) -> bool:
        """尝试重新连接到SMTP服务器（同步方法）"""
        try:
            # 创建一个新的事件循环用于同步环境中执行异步代码
            loop = asyncio.new_event_loop()
            try:
                asyncio.set_event_loop(loop)
                # 执行异步连接方法并获取结果
                return loop.run_until_complete(self._reconnect_async())
            finally:
                # 确保清理事件循环
                loop.close()
                asyncio.set_event_loop(None)
        except Exception as e:
            logger.error(f"_try_reconnect 执行异步连接时出错: {e}")
            # 出现异常时回退到旧方法
            return self._try_reconnect_fallback()
            
    async def _reconnect_async(self) -> bool:
        """异步重连方法，重用connect方法的逻辑"""
        # 记录上一次成功的连接方法
        previous_method = self.connection_method
        
        # 基于上一次成功的连接方法设置连接尝试顺序
        if previous_method:
            logger.info(f"尝试使用上次成功的连接方法 {previous_method} 重连")
            
            # 根据已知连接方法构建优先级顺序
            if previous_method == "SSL":
                connection_attempts = [self._try_ssl_connection, self._try_starttls_connection]
            elif previous_method == "STARTTLS":
                connection_attempts = [self._try_starttls_connection, self._try_ssl_connection]
            elif previous_method == "PLAIN":
                connection_attempts = [self._try_plain_connection, self._try_starttls_connection, self._try_ssl_connection]
        else:
            # 如果没有已知方法，使用标准的基于端口的策略
            if self.port == 465:
                connection_attempts = [self._try_ssl_connection, self._try_starttls_connection]
            elif self.port in [587, 25]:
                connection_attempts = [self._try_starttls_connection, self._try_ssl_connection]
            else:
                connection_attempts = [
                    self._try_ssl_connection, 
                    self._try_starttls_connection,
                    self._try_plain_connection
                ]
        
        # 无需重试，只尝试一次连接
        timeout = 30
        
        # 尝试每种连接方法
        for connection_method in connection_attempts:
            method_name = connection_method.__name__.replace("_try_", "").replace("_connection", "")
            try:
                logger.info(f"重连: 尝试使用 {method_name} 连接 {self.server}:{self.port}")
                if await connection_method(timeout):
                    logger.info(f"重连成功，使用方法: {self.connection_method}")
                    return True
            except Exception as e:
                self.last_error = str(e)
                logger.info(f"重连: 使用 {method_name} 连接失败: {e}")
                # 特殊处理SSL错误
                if isinstance(e, ssl.SSLError) and "wrong version number" in str(e):
                    logger.warning("重连: 检测到SSL版本不匹配错误，尝试切换到STARTTLS")
                    # 如果是SSL错误并且下一个尝试不是STARTTLS，则立即尝试STARTTLS
                    if method_name.upper() == "SSL":
                        starttls_methods = [m for m in connection_attempts if "starttls" in m.__name__.lower()]
                        if starttls_methods:
                            try:
                                if await starttls_methods[0](timeout):
                                    return True
                            except Exception as e2:
                                logger.info(f"重连: 紧急切换到STARTTLS失败: {e2}")
        
        # 所有方法都失败
        logger.error(f"SMTP重连失败，错误: {self.last_error}")
        return False
    
    def _try_reconnect_fallback(self) -> bool:
        """在无法使用异步方法时的后备重连方案"""
        logger.info("使用后备重连方法")
        
        # 优先尝试上次成功的连接方法
        if self.connection_method == "SSL":
            if self._try_reconnect_ssl():
                return True
        elif self.connection_method == "STARTTLS":
            if self._try_reconnect_starttls():
                return True
        elif self.connection_method == "PLAIN":
            if self._try_reconnect_plain():
                return True
            
        # 根据端口尝试合适的方法
        if self.port == 465:
            return self._try_reconnect_ssl() or self._try_reconnect_starttls()
        elif self.port in [587, 25]:
            return self._try_reconnect_starttls() or self._try_reconnect_ssl()
        
        # 最后尝试所有方法
        return (self._try_reconnect_ssl() or 
                self._try_reconnect_starttls() or 
                self._try_reconnect_plain())

    def _try_reconnect_ssl(self) -> bool:
        """尝试使用SSL方式重连"""
        try:
            context = ssl.create_default_context()
            # 尝试更兼容的SSL设置
            try:
                context.options &= ~ssl.OP_NO_TLSv1
                context.options &= ~ssl.OP_NO_TLSv1_1
            except AttributeError:
                pass
                
            self.conn = smtplib.SMTP_SSL(
                self.server, self.port, timeout=30, context=context
            )
            self.conn.ehlo()
            self.conn.login(self.username, self.password)
            self.connection_method = "SSL"
            logger.info("成功使用SSL方式连接")
            return True
        except ssl.SSLError as e:
            if "wrong version number" in str(e):
                logger.warning(f"SSL版本不匹配错误: {e}，自动切换到STARTTLS")
                return self._try_reconnect_starttls()
            logger.info(f"使用SSL方法重连失败: {e}")
            return False
        except Exception as e:
            logger.info(f"使用SSL方法重连失败: {e}")
            return False
    
    def _try_reconnect_starttls(self) -> bool:
        """尝试使用STARTTLS方式重连"""
        try:
            self.conn = smtplib.SMTP(self.server, self.port, timeout=30)
            self.conn.ehlo()
            if self.conn.has_extn("STARTTLS"):
                context = ssl.create_default_context()
                self.conn.starttls(context=context)
                self.conn.ehlo()
            self.conn.login(self.username, self.password)
            self.connection_method = "STARTTLS"
            logger.info("成功使用STARTTLS方式连接")
            return True
        except Exception as e:
            logger.info(f"使用STARTTLS方法重连失败: {e}")
            return False
    
    def _try_reconnect_plain(self) -> bool:
        """尝试使用无加密方式重连"""
        try:
            self.conn = smtplib.SMTP(self.server, self.port, timeout=30)
            self.conn.ehlo()
            self.conn.login(self.username, self.password)
            self.connection_method = "PLAIN"
            logger.warning("使用非加密连接到SMTP服务器，不推荐用于生产环境")
            return True
        except Exception as e:
            logger.info(f"使用PLAIN方法重连失败: {e}")
            return False

    async def _analyze_connection_error(self, error, server, port, method):
        """
        分析连接错误，并提供解决建议

        Args:
            error: 错误对象
            server: 服务器地址
            port: 尝试的端口
            method: 尝试的连接方法

        Returns:
            dict: 包含错误分析和建议的字典
        """
        analysis = {
            "server": server,
            "port": port,
            "method": method,
            "error_type": type(error).__name__,
            "error_message": str(error),
            "suggestions": [],
            "alternative_ports": [],
        }

        error_str = str(error).lower()

        # SSL版本错误
        if "wrong version number" in error_str and "ssl" in method.lower():
            analysis["problem"] = "SSL版本不匹配"
            analysis["suggestions"].append(
                "服务器可能不支持直接SSL连接，尝试使用STARTTLS"
            )
            if port == 465:
                analysis["alternative_ports"] = [587, 25]
                analysis["suggestions"].append(f"尝试使用端口587或25，配合STARTTLS")

        # 连接被拒绝
        elif "connection refused" in error_str:
            analysis["problem"] = "连接被拒绝"
            analysis["suggestions"].append("检查服务器地址和端口是否正确")
            analysis["suggestions"].append("检查服务器防火墙设置")
            if port == 465:
                analysis["alternative_ports"] = [587, 25]
            elif port == 587:
                analysis["alternative_ports"] = [465, 25]
            elif port == 25:
                analysis["alternative_ports"] = [465, 587]

        # 超时错误
        elif "timeout" in error_str:
            analysis["problem"] = "连接超时"
            analysis["suggestions"].append("检查网络连接和服务器状态")
            analysis["suggestions"].append("服务器可能阻止了连接请求")

        # 认证错误
        elif "authentication" in error_str or "auth" in error_str:
            analysis["problem"] = "认证失败"
            analysis["suggestions"].append("检查用户名和密码是否正确")
            analysis["suggestions"].append("确认账户是否启用了SMTP访问权限")
            analysis["suggestions"].append("检查是否需要应用专用密码")

        # 通用错误
        else:
            analysis["problem"] = "未知错误"
            analysis["suggestions"].append("查看服务器文档了解支持的连接方式")
            analysis["suggestions"].append("联系邮件服务提供商获取正确的SMTP设置")

        # 尝试自动检测常见邮件服务器设置
        if server:
            domain = server.lower()
            if "gmail" in domain:
                analysis["service_detected"] = "Gmail"
                analysis["suggested_settings"] = {
                    "server": "smtp.gmail.com",
                    "ports": [587, 465],
                    "requires_auth": True,
                    "notes": "Gmail需要启用安全性较低的应用访问或使用应用专用密码",
                }
            elif "outlook" in domain or "hotmail" in domain:
                analysis["service_detected"] = "Outlook/Hotmail"
                analysis["suggested_settings"] = {
                    "server": "smtp.office365.com",
                    "ports": [587],
                    "requires_auth": True,
                }
            elif "yahoo" in domain:
                analysis["service_detected"] = "Yahoo"
                analysis["suggested_settings"] = {
                    "server": "smtp.mail.yahoo.com",
                    "ports": [587, 465],
                    "requires_auth": True,
                    "notes": "Yahoo可能需要应用专用密码",
                }

        # 保存分析结果
        self.error_analysis = analysis
        return analysis
