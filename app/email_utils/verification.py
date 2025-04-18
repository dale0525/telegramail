from app.i18n import _
import smtplib
import imaplib
import socket
import ssl
from app.utils.logger import Logger

logger = Logger().get_logger(__name__)

# Define common ports
SMTP_SSL_PORT = 465
SMTP_STARTTLS_PORT = 587
SMTP_PLAIN_PORT = 25
IMAP_SSL_PORT = 993
IMAP_PLAIN_PORT = 143

def _verify_smtp(
    server_addr: str, port: int, use_ssl: bool, email: str, password: str
) -> tuple[bool, str]:
    """Helper function to verify SMTP credentials."""
    connection_methods = []

    # Determine connection attempt order based on port and ssl flag
    if use_ssl and port == SMTP_SSL_PORT:
        connection_methods.append(("SSL", smtplib.SMTP_SSL, port))
        connection_methods.append(("STARTTLS", smtplib.SMTP, SMTP_STARTTLS_PORT)) # Try STARTTLS on common port as fallback
    elif not use_ssl and port == SMTP_STARTTLS_PORT:
        connection_methods.append(("STARTTLS", smtplib.SMTP, port))
        connection_methods.append(("SSL", smtplib.SMTP_SSL, SMTP_SSL_PORT)) # Try SSL on common port as fallback
    elif port == SMTP_SSL_PORT: # Port suggests SSL, but flag might be wrong
        connection_methods.append(("SSL", smtplib.SMTP_SSL, port))
        connection_methods.append(("STARTTLS", smtplib.SMTP, SMTP_STARTTLS_PORT))
    elif port == SMTP_STARTTLS_PORT: # Port suggests STARTTLS, but flag might be wrong
        connection_methods.append(("STARTTLS", smtplib.SMTP, port))
        connection_methods.append(("SSL", smtplib.SMTP_SSL, SMTP_SSL_PORT))
    else: # Unknown or non-standard port, try based on ssl flag first
        if use_ssl:
            connection_methods.append(("SSL", smtplib.SMTP_SSL, port))
            connection_methods.append(("STARTTLS", smtplib.SMTP, port)) # Try STARTTLS on same port
        else:
            connection_methods.append(("STARTTLS", smtplib.SMTP, port))
            connection_methods.append(("SSL", smtplib.SMTP_SSL, port)) # Try SSL on same port
        # Add common ports as fallbacks
        connection_methods.append(("STARTTLS", smtplib.SMTP, SMTP_STARTTLS_PORT))
        connection_methods.append(("SSL", smtplib.SMTP_SSL, SMTP_SSL_PORT))


    last_error = None
    last_error_msg = ""

    for method_name, conn_class, conn_port in connection_methods:
        logger.debug(f"Attempting SMTP {method_name} connection to {server_addr}:{conn_port} for {email}")
        context = ssl.create_default_context()
        try:
            # For non-SSL connections, avoid passing context initially
            kwargs = {'host': server_addr, 'port': conn_port, 'timeout': 15}
            if conn_class is smtplib.SMTP_SSL:
                kwargs['context'] = context

            with conn_class(**kwargs) as server:
                # For plain SMTP, attempt STARTTLS
                if method_name == "STARTTLS" and conn_class is smtplib.SMTP:
                    # Check if STARTTLS is supported before calling it
                    server.ehlo() # Needed before checking features
                    if server.has_extn('STARTTLS'):
                        logger.debug("Server supports STARTTLS, attempting upgrade...")
                        server.starttls(context=context)
                        server.ehlo() # Re-ehlo after STARTTLS
                    else:
                        logger.debug("Server does not support STARTTLS, proceeding with plain login (if supported).")
                        # Some servers might allow login without STARTTLS on port 587/25, though less secure.

                server.login(email, password)
                logger.info(f"SMTP {method_name} verification successful for {email} via {server_addr}:{conn_port}")
                return True, "" # Success

        except smtplib.SMTPAuthenticationError as e:
            logger.warning(f"SMTP {method_name} authentication failed for {email} on {server_addr}:{conn_port}: {e}")
            last_error = e
            last_error_msg = _("account_verification_failed_auth")
            # Auth error likely means server/port is correct, stop trying other methods
            return False, last_error_msg
        except smtplib.SMTPNotSupportedError as e:
             logger.warning(f"SMTP {method_name} feature not supported (e.g., STARTTLS or AUTH) for {email} on {server_addr}:{conn_port}: {e}")
             last_error = e
             last_error_msg = _("account_verification_failed_smtp_feature_not_supported") + f": {e}"
        except smtplib.SMTPConnectError as e:
            logger.warning(f"SMTP {method_name} connection failed for {email} to {server_addr}:{conn_port}: {e}")
            last_error = e
            last_error_msg = _("account_verification_failed_smtp_connect") + f": {e}"
        except smtplib.SMTPServerDisconnected as e:
            logger.warning(f"SMTP {method_name} server disconnected for {email} on {server_addr}:{conn_port}: {e}")
            last_error = e
            last_error_msg = _("account_verification_failed_smtp_disconnect") + f": {e}"
        except ssl.SSLError as e:
            logger.warning(f"SMTP {method_name} SSL error for {email} on {server_addr}:{conn_port}: {e}")
            last_error = e
            last_error_msg = _("account_verification_failed_smtp_ssl") + f": {e}"
            # If 'wrong version number' specifically, it might indicate STARTTLS is needed on a port expecting direct SSL
            if "wrong version number" in str(e).lower() and method_name == "SSL":
                 logger.info("SSL wrong version number detected, possibly needs STARTTLS.")
                 # Let the loop continue to try STARTTLS if available
        except (TimeoutError, socket.timeout) as e:
            logger.warning(f"SMTP {method_name} connection timed out for {email} to {server_addr}:{conn_port}: {e}")
            last_error = e
            last_error_msg = _("account_verification_failed_smtp_timeout")
        except (socket.gaierror, OSError) as e: # Catch DNS resolution errors and other OS errors
             logger.warning(f"SMTP {method_name} connection error (DNS/OS) for {email} to {server_addr}:{conn_port}: {e}")
             last_error = e
             last_error_msg = _("account_verification_failed_smtp_dns_os") + f": {e}"
             # DNS error likely means server address is wrong, stop trying
             return False, last_error_msg
        except Exception as e:
            logger.error(f"Unexpected SMTP {method_name} error for {email} on {server_addr}:{conn_port}: {e}", exc_info=True)
            last_error = e
            last_error_msg = _("account_verification_failed_smtp_other") + f": {e}"

    logger.error(f"All SMTP verification attempts failed for {email}. Last error: {last_error}")
    return False, last_error_msg if last_error_msg else _("account_verification_failed_smtp_unknown")


def _verify_imap(
    server_addr: str, port: int, use_ssl: bool, email: str, password: str
) -> tuple[bool, str]:
    """Helper function to verify IMAP credentials."""
    connection_methods = []

    # Determine connection attempt order
    if use_ssl and port == IMAP_SSL_PORT:
        connection_methods.append(("SSL", imaplib.IMAP4_SSL, port))
        connection_methods.append(("Plain", imaplib.IMAP4, IMAP_PLAIN_PORT)) # Fallback
    elif not use_ssl and port == IMAP_PLAIN_PORT:
        connection_methods.append(("Plain", imaplib.IMAP4, port))
        connection_methods.append(("SSL", imaplib.IMAP4_SSL, IMAP_SSL_PORT)) # Fallback
    elif port == IMAP_SSL_PORT: # Port suggests SSL
        connection_methods.append(("SSL", imaplib.IMAP4_SSL, port))
        connection_methods.append(("Plain", imaplib.IMAP4, IMAP_PLAIN_PORT))
    elif port == IMAP_PLAIN_PORT: # Port suggests Plain
        connection_methods.append(("Plain", imaplib.IMAP4, port))
        connection_methods.append(("SSL", imaplib.IMAP4_SSL, IMAP_SSL_PORT))
    else: # Unknown port
        if use_ssl:
            connection_methods.append(("SSL", imaplib.IMAP4_SSL, port))
            connection_methods.append(("Plain", imaplib.IMAP4, port)) # Try plain on same port
        else:
            connection_methods.append(("Plain", imaplib.IMAP4, port))
            connection_methods.append(("SSL", imaplib.IMAP4_SSL, port)) # Try SSL on same port
        # Add common ports as fallbacks
        connection_methods.append(("SSL", imaplib.IMAP4_SSL, IMAP_SSL_PORT))
        connection_methods.append(("Plain", imaplib.IMAP4, IMAP_PLAIN_PORT))

    last_error = None
    last_error_msg = ""

    for method_name, conn_class, conn_port in connection_methods:
        logger.debug(f"Attempting IMAP {method_name} connection to {server_addr}:{conn_port} for {email}")
        try:
            # Note: imaplib doesn't have an explicit timeout parameter in constructor in all versions
            # Timeout is handled implicitly by socket operations
            mail = conn_class(server_addr, conn_port)
            # Login
            status, messages = mail.login(email, password)
            if status != 'OK':
                 # Should ideally be caught by IMAP4.error, but check explicitly
                 raise imaplib.IMAP4.error(f"Login failed with status {status}: {messages}")

            # Optionally, select inbox to ensure it's accessible
            status, _ = mail.select("inbox", readonly=True)
            if status != "OK":
                logger.warning(f"IMAP inbox selection failed for {email} on {server_addr}:{conn_port} (Status: {status})")
                # Consider if this is a hard failure or just a warning. Treating as warning for now.
                # return False, _("account_verification_failed_imap_inbox")
            
            logger.info(f"IMAP {method_name} verification successful for {email} via {server_addr}:{conn_port}")
            try:
                 mail.logout()
            except Exception:
                 pass # Ignore logout errors
            return True, "" # Success

        except imaplib.IMAP4.error as e:
            logger.warning(f"IMAP {method_name} login/connection error for {email} on {server_addr}:{conn_port}: {e}")
            last_error = e
            # IMAP errors often contain useful info directly (e.g., 'AUTHENTICATIONFAILED')
            error_str = str(e).lower()
            if "authenticationfailed" in error_str or "login failed" in error_str or "invalid credentials" in error_str:
                last_error_msg = _("account_verification_failed_auth")
                # Auth error likely means server/port correct, stop trying
                try: mail.shutdown() # Try to close connection if object exists
                except Exception: pass
                return False, last_error_msg
            elif "timed out" in error_str:
                 last_error_msg = _("account_verification_failed_imap_timeout")
            else:
                 last_error_msg = _("account_verification_failed_imap_login") + f": {e}"
        except (TimeoutError, socket.timeout, ssl.SSLError) as e: # Catch timeout and SSL errors during connection phase
            logger.warning(f"IMAP {method_name} connection error (Timeout/SSL) for {email} to {server_addr}:{conn_port}: {e}")
            last_error = e
            if isinstance(e, ssl.SSLError):
                last_error_msg = _("account_verification_failed_imap_ssl") + f": {e}"
            else:
                last_error_msg = _("account_verification_failed_imap_timeout")
        except (socket.gaierror, OSError) as e: # Catch DNS resolution errors and other OS errors
             logger.warning(f"IMAP {method_name} connection error (DNS/OS) for {email} to {server_addr}:{conn_port}: {e}")
             last_error = e
             last_error_msg = _("account_verification_failed_imap_dns_os") + f": {e}"
             # DNS error likely means server address is wrong, stop trying
             return False, last_error_msg
        except Exception as e:
            logger.error(f"Unexpected IMAP {method_name} error for {email} on {server_addr}:{conn_port}: {e}", exc_info=True)
            last_error = e
            last_error_msg = _("account_verification_failed_imap_other") + f": {e}"
        finally:
            # Ensure connection is closed if it exists and an error occurred
            if last_error and 'mail' in locals() and hasattr(mail, 'state') and mail.state != 'LOGOUT':
                try:
                    mail.shutdown() # Use shutdown for cleaner close attempt
                except Exception:
                    pass

    logger.error(f"All IMAP verification attempts failed for {email}. Last error: {last_error}")
    return False, last_error_msg if last_error_msg else _("account_verification_failed_imap_unknown")


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
    smtp_ssl = context.get("smtp_ssl", None) # Use None to let logic decide based on port
    imap_server = context.get("imap_server")
    imap_port = context.get("imap_port")
    imap_ssl = context.get("imap_ssl", None) # Use None to let logic decide based on port

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
        return False, _("account_verification_failed_invalid_port") + f" (SMTP: {smtp_port})"

    # Determine default SSL based on port if not explicitly set
    if smtp_ssl is None:
        smtp_ssl_bool = (smtp_port_int == SMTP_SSL_PORT)
    else:
        smtp_ssl_bool = bool(smtp_ssl)

    smtp_ok, smtp_msg = _verify_smtp(
        smtp_server, smtp_port_int, smtp_ssl_bool, email, password
    )
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
        return False, _("account_verification_failed_invalid_port") + f" (IMAP: {imap_port})"

    # Determine default SSL based on port if not explicitly set
    if imap_ssl is None:
        imap_ssl_bool = (imap_port_int == IMAP_SSL_PORT)
    else:
        imap_ssl_bool = bool(imap_ssl)

    imap_ok, imap_msg = _verify_imap(
        imap_server, imap_port_int, imap_ssl_bool, email, password
    )
    if not imap_ok:
        logger.error(f"IMAP verification failed for {email}: {imap_msg}")
        return False, imap_msg

    logger.info(f"Account verification successful for {email}")
    return True, ""  # Success
