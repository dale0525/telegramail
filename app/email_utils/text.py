import email, html, re, html2text
from email.header import decode_header
from typing import Optional, Tuple
from bs4 import BeautifulSoup

from app.utils import Logger

logger = Logger().get_logger(__name__)


def decode_email_address(address: Optional[str]) -> str:
    """
    Decode email address which might be encoded

    Args:
        address: Raw email address that might be encoded

    Returns:
        str: Decoded address
    """
    if not address:
        return ""

    # Use email.utils to parse the address
    try:
        addresses = email.utils.getaddresses([address])
        decoded_addresses = []

        for name, addr in addresses:
            # Decode the name part if needed
            if name:
                decoded_name = decode_email_subject(name)

                # Handle HTML entities like &nbsp;
                decoded_name = html.unescape(decoded_name)

                # Replace multiple spaces with single space
                decoded_name = re.sub(r"\s+", " ", decoded_name)

                # Remove any control characters
                decoded_name = re.sub(r"[\x00-\x1F\x7F]", "", decoded_name)

                # Trim whitespace
                decoded_name = decoded_name.strip()

                decoded_addresses.append(f"{decoded_name} <{addr}>")
            else:
                decoded_addresses.append(addr)

        return ", ".join(decoded_addresses)
    except Exception as e:
        logger.error(f"Error decoding email address: {e}")
        return address  # Return original if decoding fails


def decode_email_subject(subject: Optional[str]) -> str:
    """
    Decode email subject which might be encoded

    Args:
        subject: Raw email subject that might be encoded

    Returns:
        str: Decoded subject
    """
    if not subject:
        return ""

    decoded_chunks = []
    chunks = decode_header(subject)

    for chunk, encoding in chunks:
        if isinstance(chunk, bytes):
            if encoding:
                try:
                    decoded_chunks.append(chunk.decode(encoding))
                except:
                    decoded_chunks.append(chunk.decode("utf-8", errors="replace"))
            else:
                decoded_chunks.append(chunk.decode("utf-8", errors="replace"))
        else:
            decoded_chunks.append(chunk)

    return "".join(decoded_chunks)


def get_email_body(msg) -> Tuple[str, str]:
    """
    Extract plain text and HTML body from email message

    Args:
        msg: Email message object

    Returns:
        Tuple[str, str]: (plain text body, HTML body)
    """
    body_text = ""
    body_html = ""

    if msg.is_multipart():
        for part in msg.walk():
            content_type = part.get_content_type()
            content_disposition = str(part.get("Content-Disposition"))

            # Skip attachments
            if "attachment" in content_disposition:
                continue

            # Get the body
            try:
                payload = part.get_payload(decode=True)
                if payload:
                    charset = part.get_content_charset() or "utf-8"
                    try:
                        decoded_payload = payload.decode(charset)
                    except:
                        decoded_payload = payload.decode("utf-8", errors="replace")

                    if content_type == "text/plain":
                        body_text = decoded_payload
                    elif content_type == "text/html":
                        body_html = decoded_payload
            except Exception as e:
                logger.error(f"Error decoding email part: {e}")

        # If body_text is still empty, try to extract text from HTML using html2text
        if not body_text and body_html:
            try:
                h = html2text.HTML2Text()
                body_text = h.handle(body_html)
            except Exception as e:
                logger.error(f"Error converting HTML to text using html2text: {e}")
    else:
        # Not multipart
        content_type = msg.get_content_type()
        payload = msg.get_payload(decode=True)
        if payload:
            charset = msg.get_content_charset() or "utf-8"
            try:
                decoded_payload = payload.decode(charset)
            except:
                decoded_payload = payload.decode("utf-8", errors="replace")

            if content_type == "text/plain":
                body_text = decoded_payload
            elif content_type == "text/html":
                body_html = decoded_payload

        # If body_text is still empty, try to extract text from HTML using html2text
        if not body_text and body_html:
            try:
                h = html2text.HTML2Text()
                body_text = h.handle(body_html)
            except Exception as e:
                logger.error(f"Error converting HTML to text using html2text: {e}")

    return body_text, body_html


def remove_spaces_and_urls(text: str) -> str:
    """
    Removes all whitespace characters and URLs from a string.

    Args:
        text: The input string.

    Returns:
        The string with whitespace and URLs removed.
    """
    if not text:
        return ""
    # Remove URLs
    text_no_urls = re.sub(r"http\S+|www\.\S+", "", text)
    # Remove all whitespace (spaces, tabs, newlines, etc.)
    text_no_spaces_or_urls = "".join(text_no_urls.split())
    return text_no_spaces_or_urls


def clean_html_content(html_content: str) -> str:
    """
    预处理HTML内容，移除样式和脚本，保留链接信息，转换为纯文本

    Args:
        html_content: 原始HTML内容

    Returns:
        str: 处理后的纯文本内容，保留链接信息
    """
    if not html_content or not html_content.strip():
        return ""

    try:
        # 使用BeautifulSoup解析HTML
        soup = BeautifulSoup(html_content, "html.parser")

        # 移除所有style标签
        for style in soup.find_all("style"):
            style.decompose()

        # 移除所有script标签
        for script in soup.find_all("script"):
            script.decompose()

        # 处理链接：将<a href="url">链接文本</a>转换为"链接文本 (url)"
        for link in soup.find_all("a", href=True):
            href = link.get("href", "")
            link_text = link.get_text(strip=True)

            # 只处理http/https链接
            if href.startswith(("http://", "https://")):
                if link_text:
                    # 如果链接文本存在，格式化为"链接文本 (url)"
                    new_text = f"{link_text} ({href})"
                else:
                    # 如果没有链接文本，直接使用URL
                    new_text = href
                link.replace_with(new_text)
            else:
                # 对于非http链接，只保留文本
                link.replace_with(link_text if link_text else "")

        # 移除所有HTML标签的style属性
        for tag in soup.find_all():
            if tag.has_attr("style"):
                del tag["style"]

        # 获取纯文本内容
        text_content = soup.get_text()

        # 清理多余的空白字符
        # 将多个连续的空白字符（包括换行符）替换为单个空格或换行符
        text_content = re.sub(r"\n\s*\n", "\n\n", text_content)  # 保留段落分隔
        text_content = re.sub(
            r"[ \t]+", " ", text_content
        )  # 多个空格/制表符替换为单个空格
        text_content = re.sub(r"\n ", "\n", text_content)  # 移除行首空格
        text_content = text_content.strip()

        return text_content

    except Exception as e:
        logger.error(f"Error preprocessing HTML content: {e}")
        # 如果HTML处理失败，尝试简单的正则表达式清理
        try:
            # 移除script和style标签及其内容
            html_content = re.sub(
                r"<script[^>]*>.*?</script>",
                "",
                html_content,
                flags=re.DOTALL | re.IGNORECASE,
            )
            html_content = re.sub(
                r"<style[^>]*>.*?</style>",
                "",
                html_content,
                flags=re.DOTALL | re.IGNORECASE,
            )

            # 简单处理链接
            html_content = re.sub(
                r'<a[^>]*href=["\']([^"\']*)["\'][^>]*>(.*?)</a>',
                r"\2 (\1)",
                html_content,
                flags=re.IGNORECASE,
            )

            # 移除所有HTML标签
            html_content = re.sub(r"<[^>]+>", "", html_content)

            # 清理空白字符
            html_content = re.sub(r"\n\s*\n", "\n\n", html_content)
            html_content = re.sub(r"[ \t]+", " ", html_content)
            html_content = html_content.strip()

            return html_content
        except Exception as fallback_error:
            logger.error(f"Fallback HTML processing also failed: {fallback_error}")
            return ""
