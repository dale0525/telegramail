import asyncio
import smtplib
from email import encoders
from email.mime.base import MIMEBase
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr, formatdate, make_msgid
from typing import Iterable, Optional, Any

from app.utils import Logger

logger = Logger().get_logger(__name__)


def _normalize_addrs(addrs: Optional[Iterable[str]]) -> list[str]:
    if not addrs:
        return []
    normalized: list[str] = []
    for a in addrs:
        if not a:
            continue
        a = a.strip()
        if not a:
            continue
        normalized.append(a)
    return normalized


def _chunk(items: list[str], size: int) -> list[list[str]]:
    if size <= 0:
        return [items]
    return [items[i : i + size] for i in range(0, len(items), size)]


def build_email_message(
    *,
    from_email: str,
    from_name: Optional[str],
    to_addrs: list[str],
    cc_addrs: Optional[list[str]] = None,
    subject: str,
    text_body: str,
    html_body: Optional[str] = None,
    reply_to: Optional[str] = None,
    in_reply_to: Optional[str] = None,
    references: Optional[list[str]] = None,
    message_id: Optional[str] = None,
    date: Optional[str] = None,
    attachments: Optional[list[dict[str, Any]]] = None,
) -> MIMEMultipart:
    attachments = attachments or []
    has_attachments = len(attachments) > 0

    # If there are attachments, root must be multipart/mixed, with an inner
    # multipart/alternative for text/plain + text/html.
    msg = MIMEMultipart("mixed" if has_attachments else "alternative")

    msg["Subject"] = subject
    msg["From"] = formataddr(((from_name or "").strip(), from_email))
    msg["To"] = ", ".join(_normalize_addrs(to_addrs))
    msg["Date"] = date or formatdate(localtime=True)
    msg["Message-ID"] = message_id or make_msgid()

    cc = _normalize_addrs(cc_addrs)
    if cc:
        msg["Cc"] = ", ".join(cc)

    if reply_to:
        msg["Reply-To"] = reply_to

    if in_reply_to:
        msg["In-Reply-To"] = in_reply_to

    if references:
        refs = [r for r in references if r]
        if refs:
            msg["References"] = " ".join(refs)

    if has_attachments:
        alt = MIMEMultipart("alternative")
        alt.attach(MIMEText(text_body or "", "plain", "utf-8"))
        if html_body:
            alt.attach(MIMEText(html_body, "html", "utf-8"))
        msg.attach(alt)
    else:
        msg.attach(MIMEText(text_body or "", "plain", "utf-8"))
        if html_body:
            msg.attach(MIMEText(html_body, "html", "utf-8"))

    for att in attachments:
        filename = (att.get("filename") or "").strip()
        data = att.get("data") or b""
        mime_type = (att.get("mime_type") or "application/octet-stream").strip()

        if not filename:
            filename = "attachment"

        if not isinstance(data, (bytes, bytearray)):
            data = bytes(data)

        if "/" in mime_type:
            maintype, subtype = mime_type.split("/", 1)
        else:
            maintype, subtype = "application", "octet-stream"

        part = MIMEBase(maintype, subtype)
        part.set_payload(data)
        encoders.encode_base64(part)
        part.add_header("Content-Disposition", "attachment", filename=filename)
        msg.attach(part)

    return msg


class SMTPClient:
    def __init__(
        self,
        *,
        server: str,
        port: int,
        username: str,
        password: str,
        use_ssl: bool,
        max_recipients_per_email: int = 50,
        timeout_seconds: int = 30,
    ):
        self.server = server
        self.port = int(port)
        self.username = username
        self.password = password
        self.use_ssl = bool(use_ssl)
        self.max_recipients_per_email = int(max_recipients_per_email)
        self.timeout_seconds = int(timeout_seconds)

    async def send_email(self, **kwargs) -> bool:
        loop = asyncio.get_running_loop()
        return await loop.run_in_executor(None, lambda: self.send_email_sync(**kwargs))

    def send_email_sync(
        self,
        *,
        from_email: str,
        from_name: Optional[str],
        to_addrs: Optional[list[str]] = None,
        cc_addrs: Optional[list[str]] = None,
        bcc_addrs: Optional[list[str]] = None,
        subject: str,
        text_body: str,
        html_body: Optional[str] = None,
        reply_to: Optional[str] = None,
        in_reply_to: Optional[str] = None,
        references: Optional[list[str]] = None,
        message_id: Optional[str] = None,
        date: Optional[str] = None,
        attachments: Optional[list[dict[str, Any]]] = None,
    ) -> bool:
        to_list = _normalize_addrs(to_addrs)
        cc_list = _normalize_addrs(cc_addrs)
        bcc_list = _normalize_addrs(bcc_addrs)

        total = len(to_list) + len(cc_list) + len(bcc_list)

        # Helper to send one message
        def _send_one(*, header_to: list[str], header_cc: list[str], rcpt: list[str]) -> None:
            msg = build_email_message(
                from_email=from_email,
                from_name=from_name,
                to_addrs=header_to,
                cc_addrs=header_cc,
                subject=subject,
                text_body=text_body,
                html_body=html_body,
                reply_to=reply_to,
                in_reply_to=in_reply_to,
                references=references,
                message_id=message_id,
                date=date,
                attachments=attachments,
            )
            self._send_via_smtp(from_email=from_email, recipients=rcpt, message=msg)

        try:
            if total <= self.max_recipients_per_email:
                _send_one(
                    header_to=to_list or [from_email],
                    header_cc=cc_list,
                    rcpt=to_list + cc_list + bcc_list,
                )
                return True

            # If too many recipients, avoid duplicating delivery to To/Cc.
            if to_list or cc_list:
                _send_one(header_to=to_list, header_cc=cc_list, rcpt=to_list + cc_list)

            # Send BCC in chunks (To: from_email, no Cc)
            for chunk in _chunk(bcc_list, self.max_recipients_per_email):
                if not chunk:
                    continue
                _send_one(header_to=[from_email], header_cc=[], rcpt=chunk)

            return True
        except Exception as e:
            logger.error(f"Failed to send email: {e}")
            return False

    def _send_via_smtp(self, *, from_email: str, recipients: list[str], message: MIMEMultipart) -> None:
        if not recipients:
            return

        if self.use_ssl:
            smtp_cls = smtplib.SMTP_SSL
        else:
            smtp_cls = smtplib.SMTP

        with smtp_cls(self.server, self.port, timeout=self.timeout_seconds) as smtp:
            smtp.ehlo()
            if not self.use_ssl:
                try:
                    smtp.starttls()
                    smtp.ehlo()
                except Exception:
                    # STARTTLS not available; continue in plain if configured that way.
                    pass

            if self.username:
                smtp.login(self.username, self.password)

            smtp.sendmail(from_email, recipients, message.as_string())
