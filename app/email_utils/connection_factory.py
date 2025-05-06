import imaplib
import smtplib
import socket
import ssl
from typing import Tuple, Optional, Any, Union
from app.utils import Logger, retry_on_fail
from app.i18n import _

logger = Logger().get_logger(__name__)

# Define common ports
SMTP_SSL_PORT = 465
SMTP_STARTTLS_PORT = 587
SMTP_PLAIN_PORT = 25
IMAP_SSL_PORT = 993
IMAP_PLAIN_PORT = 143


class ConnectionFactory:
    """
    Factory class for creating and managing email service connections (IMAP/SMTP)
    """

    @staticmethod
    def create_imap_connection(
        server_addr: str, port: int, use_ssl: bool, timeout: Optional[int] = None
    ) -> Union[imaplib.IMAP4_SSL, imaplib.IMAP4]:
        """
        Create an IMAP connection with proper configuration

        Args:
            server_addr: IMAP server address
            port: IMAP server port
            use_ssl: Whether to use SSL for the connection
            timeout: Optional connection timeout in seconds

        Returns:
            An IMAP4 or IMAP4_SSL connection object
        """
        kwargs = {"host": server_addr, "port": port}
        if timeout is not None:
            kwargs["timeout"] = timeout

        if use_ssl:
            return imaplib.IMAP4_SSL(**kwargs)
        else:
            return imaplib.IMAP4(**kwargs)

    @staticmethod
    def create_smtp_connection(
        server_addr: str, port: int, use_ssl: bool, timeout: Optional[int] = None
    ) -> Union[smtplib.SMTP_SSL, smtplib.SMTP]:
        """
        Create an SMTP connection with proper configuration

        Args:
            server_addr: SMTP server address
            port: SMTP server port
            use_ssl: Whether to use SSL for the connection
            timeout: Optional connection timeout in seconds

        Returns:
            An SMTP or SMTP_SSL connection object
        """
        kwargs = {"host": server_addr, "port": port}
        if timeout is not None:
            kwargs["timeout"] = timeout

        if use_ssl:
            context = ssl.create_default_context()
            kwargs["context"] = context
            return smtplib.SMTP_SSL(**kwargs)
        else:
            return smtplib.SMTP(**kwargs)

    @staticmethod
    def try_imap_connection(
        server_addr: str,
        port: int,
        email: str,
        password: str,
        use_ssl: bool = None,
        timeout: int = 15,
    ) -> Tuple[bool, str, Any]:
        """
        Try to establish an IMAP connection with the given parameters

        Args:
            server_addr: IMAP server address
            port: IMAP server port
            email: Email address
            password: Email password
            use_ssl: Whether to use SSL (if None, determined by port)
            timeout: Connection timeout in seconds

        Returns:
            Tuple of (success, error_message, connection_object)
        """
        # Determine SSL setting based on port if not explicitly specified
        if use_ssl is None:
            use_ssl = port == IMAP_SSL_PORT

        try:
            # Create connection
            mail = ConnectionFactory.create_imap_connection(
                server_addr, port, use_ssl, timeout
            )

            # Login
            status, messages = mail.login(email, password)
            if status != "OK":
                raise imaplib.IMAP4.error(
                    f"Login failed with status {status}: {messages}"
                )

            return True, "", mail

        except imaplib.IMAP4.error as e:
            logger.warning(
                f"IMAP login/connection error for {email} on {server_addr}:{port}: {e}"
            )

            # More specific error message detection
            error_str = str(e).lower()
            if (
                "authenticationfailed" in error_str
                or "login failed" in error_str
                or "invalid credentials" in error_str
            ):
                error_msg = _("account_verification_failed_auth")
            elif "timed out" in error_str:
                error_msg = _("account_verification_failed_imap_timeout")
            else:
                error_msg = _("account_verification_failed_imap_login") + f": {e}"

            return False, error_msg, None

        except (TimeoutError, socket.timeout) as e:
            logger.warning(
                f"IMAP connection timed out for {email} to {server_addr}:{port}: {e}"
            )
            return False, _("account_verification_failed_imap_timeout"), None

        except ssl.SSLError as e:
            logger.warning(f"IMAP SSL error for {email} on {server_addr}:{port}: {e}")
            return False, _("account_verification_failed_imap_ssl") + f": {e}", None

        except (socket.gaierror, OSError) as e:
            logger.warning(
                f"IMAP connection error (DNS/OS) for {email} to {server_addr}:{port}: {e}"
            )
            return False, _("account_verification_failed_imap_dns_os") + f": {e}", None

        except Exception as e:
            logger.error(
                f"Unexpected IMAP error for {email} on {server_addr}:{port}: {e}",
                exc_info=True,
            )
            return False, _("account_verification_failed_imap_other") + f": {e}", None

    @staticmethod
    @retry_on_fail(max_retries=2, retry_delay=1.0, exceptions=TimeoutError)
    def try_smtp_connection(
        server_addr: str,
        port: int,
        email: str,
        password: str,
        use_ssl: bool = None,
        use_starttls: bool = None,
        timeout: int = 15,
    ) -> Tuple[bool, str, Any]:
        """
        Try to establish an SMTP connection with the given parameters

        Args:
            server_addr: SMTP server address
            port: SMTP server port
            email: Email address
            password: Email password
            use_ssl: Whether to use SSL (if None, determined by port)
            use_starttls: Whether to use STARTTLS (if None, determined by port)
            timeout: Connection timeout in seconds

        Returns:
            Tuple of (success, error_message, connection_object)
        """
        # Determine connection method based on port if not explicitly specified
        if use_ssl is None and use_starttls is None:
            if port == SMTP_SSL_PORT:
                use_ssl = True
                use_starttls = False
            elif port == SMTP_STARTTLS_PORT:
                use_ssl = False
                use_starttls = True
            else:
                use_ssl = False
                use_starttls = False

        context = ssl.create_default_context()

        try:
            # Create connection
            if use_ssl:
                server = smtplib.SMTP_SSL(
                    host=server_addr, port=port, timeout=timeout, context=context
                )
                server.ehlo()
            else:
                server = smtplib.SMTP(host=server_addr, port=port, timeout=timeout)
                server.ehlo()

                # Use STARTTLS if requested and supported
                if use_starttls:
                    if server.has_extn("STARTTLS"):
                        server.starttls(context=context)
                        server.ehlo()  # Re-identify after STARTTLS
                    else:
                        logger.warning(
                            f"STARTTLS requested but not supported by {server_addr}:{port}"
                        )

            # Login
            server.login(email, password)

            # Determine connection method name for logging
            if use_ssl:
                method_name = "SSL"
            elif use_starttls:
                method_name = "STARTTLS"
            else:
                method_name = "Plain"

            logger.info(
                f"SMTP {method_name} connection successful for {email} via {server_addr}:{port}"
            )

            return True, "", server

        except smtplib.SMTPAuthenticationError as e:
            logger.warning(
                f"SMTP authentication failed for {email} on {server_addr}:{port}: {e}"
            )
            return False, _("account_verification_failed_auth"), None

        except smtplib.SMTPNotSupportedError as e:
            logger.warning(
                f"SMTP feature not supported for {email} on {server_addr}:{port}: {e}"
            )
            return (
                False,
                _("account_verification_failed_smtp_feature_not_supported") + f": {e}",
                None,
            )

        except smtplib.SMTPConnectError as e:
            logger.warning(
                f"SMTP connection failed for {email} to {server_addr}:{port}: {e}"
            )
            return False, _("account_verification_failed_smtp_connect") + f": {e}", None

        except smtplib.SMTPServerDisconnected as e:
            logger.warning(
                f"SMTP server disconnected for {email} on {server_addr}:{port}: {e}"
            )
            return (
                False,
                _("account_verification_failed_smtp_disconnect") + f": {e}",
                None,
            )

        except ssl.SSLError as e:
            logger.warning(f"SMTP SSL error for {email} on {server_addr}:{port}: {e}")
            return False, _("account_verification_failed_smtp_ssl") + f": {e}", None

        except (TimeoutError, socket.timeout) as e:
            logger.warning(
                f"SMTP connection timed out for {email} to {server_addr}:{port}: {e}"
            )
            return False, _("account_verification_failed_smtp_timeout"), None

        except (socket.gaierror, OSError) as e:
            logger.warning(
                f"SMTP connection error (DNS/OS) for {email} to {server_addr}:{port}: {e}"
            )
            return False, _("account_verification_failed_smtp_dns_os") + f": {e}", None

        except Exception as e:
            logger.error(
                f"Unexpected SMTP error for {email} on {server_addr}:{port}: {e}",
                exc_info=True,
            )
            return False, _("account_verification_failed_smtp_other") + f": {e}", None

    @staticmethod
    def try_multiple_imap_connections(
        server_addr: str,
        port: int,
        email: str,
        password: str,
        use_ssl: Optional[bool] = None,
    ) -> Tuple[bool, str, Any]:
        """
        Try multiple IMAP connection methods to find the working one

        Args:
            server_addr: IMAP server address
            port: IMAP server port
            email: Email address
            password: Email password
            use_ssl: Whether to use SSL (if None, determined by port)

        Returns:
            Tuple of (success, error_message, connection_object)
        """
        connection_methods = []

        # Determine connection attempt order
        if use_ssl and port == IMAP_SSL_PORT:
            connection_methods.append((True, port))  # SSL on specified port
            connection_methods.append(
                (False, IMAP_PLAIN_PORT)
            )  # Plain on standard port as fallback
        elif not use_ssl and port == IMAP_PLAIN_PORT:
            connection_methods.append((False, port))  # Plain on specified port
            connection_methods.append(
                (True, IMAP_SSL_PORT)
            )  # SSL on standard port as fallback
        elif port == IMAP_SSL_PORT:  # Port suggests SSL
            connection_methods.append((True, port))
            connection_methods.append((False, IMAP_PLAIN_PORT))
        elif port == IMAP_PLAIN_PORT:  # Port suggests Plain
            connection_methods.append((False, port))
            connection_methods.append((True, IMAP_SSL_PORT))
        else:  # Unknown port
            if use_ssl:
                connection_methods.append((True, port))  # SSL on specified port
                connection_methods.append((False, port))  # Plain on same port
            else:
                connection_methods.append((False, port))  # Plain on specified port
                connection_methods.append((True, port))  # SSL on same port
            # Add common ports as fallbacks
            connection_methods.append((True, IMAP_SSL_PORT))  # SSL on standard port
            connection_methods.append(
                (False, IMAP_PLAIN_PORT)
            )  # Plain on standard port

        last_error_msg = ""

        for use_ssl_val, port_val in connection_methods:
            method_name = "SSL" if use_ssl_val else "Plain"
            logger.debug(
                f"Attempting IMAP {method_name} connection to {server_addr}:{port_val} for {email}"
            )

            success, error_msg, conn = ConnectionFactory.try_imap_connection(
                server_addr, port_val, email, password, use_ssl_val
            )

            if success:
                return True, "", conn

            # If authentication failed, stop trying other methods
            if "account_verification_failed_auth" in error_msg:
                return False, error_msg, None

            # For DNS errors, no need to try other combinations
            if "account_verification_failed_imap_dns_os" in error_msg:
                return False, error_msg, None

            last_error_msg = error_msg

        logger.error(f"All IMAP verification attempts failed for {email}.")
        return (
            False,
            last_error_msg or _("account_verification_failed_imap_unknown"),
            None,
        )

    @staticmethod
    def try_multiple_smtp_connections(
        server_addr: str,
        port: int,
        email: str,
        password: str,
        use_ssl: Optional[bool] = None,
    ) -> Tuple[bool, str, Any]:
        """
        Try multiple SMTP connection methods to find the working one

        Args:
            server_addr: SMTP server address
            port: SMTP server port
            email: Email address
            password: Email password
            use_ssl: Whether to use SSL (if None, determined by port)

        Returns:
            Tuple of (success, error_message, connection_object)
        """
        connection_methods = []

        # Determine connection attempt order
        if use_ssl:
            connection_methods.append(
                ("SSL", True, False, port)
            )  # SSL on specified port
            connection_methods.append(("STARTTLS", False, True, port))
        else:  # Unknown port
            connection_methods.append(("PLAIN", False, False, port))

        last_error_msg = ""

        for method_name, use_ssl_val, use_starttls_val, port_val in connection_methods:
            logger.debug(
                f"Attempting SMTP {method_name} connection to {server_addr}:{port_val} for {email}"
            )

            success, error_msg, conn = ConnectionFactory.try_smtp_connection(
                server_addr, port_val, email, password, use_ssl_val, use_starttls_val
            )

            if success:
                return True, "", conn

            # If authentication failed, stop trying other methods
            if "account_verification_failed_auth" in error_msg:
                return False, error_msg, None

            # For DNS errors, no need to try other combinations
            if "account_verification_failed_smtp_dns_os" in error_msg:
                return False, error_msg, None

            last_error_msg = error_msg

        logger.error(f"All SMTP verification attempts failed for {email}.")
        return (
            False,
            last_error_msg or _("account_verification_failed_smtp_unknown"),
            None,
        )
