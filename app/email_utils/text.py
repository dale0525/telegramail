import email, html, re, html2text
from email.header import decode_header
from typing import Optional, Tuple

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
