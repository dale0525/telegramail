from app.i18n import _
import smtplib
import imaplib
import socket
import ssl
from app.utils import Logger
from app.utils.decorators import retry_on_fail
from app.email_utils.connection_factory import (
    ConnectionFactory,
    SMTP_SSL_PORT,
    IMAP_SSL_PORT,
)

logger = Logger().get_logger(__name__)

# Define common ports
SMTP_SSL_PORT = 465
SMTP_STARTTLS_PORT = 587
SMTP_PLAIN_PORT = 25
IMAP_SSL_PORT = 993
IMAP_PLAIN_PORT = 143


@retry_on_fail(max_retries=2, retry_delay=2.0, log_failure=True)
def verify_account_credentials(context: dict) -> tuple[bool, str]:
    """
    Attempts to verify email account credentials by connecting to SMTP and IMAP servers,
    trying multiple connection methods and ports if necessary.

    Args:
        context: A dictionary containing account details like email, password,
                 smtp_server, smtp_port, smtp_ssl, imap_server, imap_port, imap_ssl.

    Returns:
        A tuple (bool, str):
            - True if both SMTP and IMAP connections are successful, False otherwise.
            - An error message string (using i18n) if verification fails, otherwise an empty string.
    """
    email = context.get("email")
    password = context.get("password")
    smtp_server = context.get("smtp_server")
    smtp_port = context.get("smtp_port")
    smtp_ssl = context.get(
        "smtp_ssl", None
    )  # Use None to let logic decide based on port
    imap_server = context.get("imap_server")
    imap_port = context.get("imap_port")
    imap_ssl = context.get(
        "imap_ssl", None
    )  # Use None to let logic decide based on port

    if not all([email, password, smtp_server, smtp_port, imap_server, imap_port]):
        logger.error("Missing required account details for verification.")
        return False, _("account_verification_failed_missing_details")

    # --- SMTP Verification ---
    logger.info(f"Starting SMTP verification for {email}...")
    # Interpret string port to int
    try:
        smtp_port_int = int(smtp_port)
    except (ValueError, TypeError):
        logger.error(f"Invalid SMTP port specified: {smtp_port}")
        return (
            False,
            _("account_verification_failed_invalid_port") + f" (SMTP: {smtp_port})",
        )

    # Determine default SSL based on port if not explicitly set
    if smtp_ssl is None:
        smtp_ssl_bool = smtp_port_int == SMTP_SSL_PORT
    else:
        smtp_ssl_bool = bool(smtp_ssl)

    # Use ConnectionFactory to try multiple SMTP connection methods
    smtp_ok, smtp_msg, smtp_conn = ConnectionFactory.try_multiple_smtp_connections(
        smtp_server, smtp_port_int, email, password, smtp_ssl_bool
    )

    # Close connection if it was established
    if smtp_conn:
        try:
            smtp_conn.quit()
        except Exception:
            pass

    if not smtp_ok:
        logger.error(f"SMTP verification failed for {email}: {smtp_msg}")
        return False, smtp_msg

    # --- IMAP Verification ---
    logger.info(f"Starting IMAP verification for {email}...")
    # Interpret string port to int
    try:
        imap_port_int = int(imap_port)
    except (ValueError, TypeError):
        logger.error(f"Invalid IMAP port specified: {imap_port}")
        return (
            False,
            _("account_verification_failed_invalid_port") + f" (IMAP: {imap_port})",
        )

    # Determine default SSL based on port if not explicitly set
    if imap_ssl is None:
        imap_ssl_bool = imap_port_int == IMAP_SSL_PORT
    else:
        imap_ssl_bool = bool(imap_ssl)

    # Use ConnectionFactory to try multiple IMAP connection methods
    imap_ok, imap_msg, imap_conn = ConnectionFactory.try_multiple_imap_connections(
        imap_server, imap_port_int, email, password, imap_ssl_bool
    )

    # Close connection if it was established
    if imap_conn:
        try:
            imap_conn.logout()
        except Exception:
            pass

    if not imap_ok:
        logger.error(f"IMAP verification failed for {email}: {imap_msg}")
        return False, imap_msg

    logger.info(f"Account verification successful for {email}")
    return True, ""  # Success
