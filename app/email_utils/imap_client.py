import email
import time
import re
import json
import sqlite3
import imaplib
from typing import Any  # Import imaplib for specific exceptions
from app.utils import Logger
from app.database import DBManager
from app.email_utils.connection_factory import ConnectionFactory
from app.email_utils.text import (
    decode_email_address,
    decode_email_subject,
    get_email_body,
)
from app.email_utils.identity import extract_delivered_to_candidates
from app.utils.decorators import retry_on_fail

logger = Logger().get_logger(__name__)


class IMAPClient:
    """IMAP client for connecting to email servers and fetching emails"""

    def __init__(self, account: dict[str, Any]) -> None:
        """
        Initialize IMAP client for a specific email account

        Args:
            email_addr: Email address to connect to
        """
        self.account_info = account
        self.email_addr = account["email"]
        self.conn = None
        self.db_manager = DBManager()

    def connect(self) -> bool:
        """
        Connect to the IMAP server using account information

        Returns:
            bool: True if connection successful, False otherwise
        """
        try:
            if not self.account_info:
                logger.error(f"Account information not found for {self.email_addr}")
                return False

            # Use ConnectionFactory to create the connection
            success, error_msg, self.conn = ConnectionFactory.try_imap_connection(
                self.account_info["imap_server"],
                self.account_info["imap_port"],
                self.account_info["email"],
                self.account_info["password"],
                self.account_info["imap_ssl"],
            )

            if success:
                logger.info(
                    f"Successfully connected to IMAP server for {self.email_addr}"
                )
                return True
            else:
                logger.error(f"Failed to connect to IMAP server: {error_msg}")
                return False

        except Exception as e:
            logger.error(f"Failed to connect to IMAP server: {e}")
            return False

    def disconnect(self) -> None:
        """Disconnect from the IMAP server"""
        if self.conn:
            try:
                self.conn.logout()
                logger.info(f"Disconnected from IMAP server for {self.email_addr}")
            except Exception as e:
                logger.error(
                    f"Error disconnecting from IMAP server for {self.email_addr}: {e}"
                )
            finally:
                self.conn = None

    async def fetch_unread_emails(self) -> int:
        """
        Fetch unread emails from inbox and store them in SQLite database
        without marking them as read on the server

        Returns:
            int: Number of unread emails fetched
        """
        if not self.conn:
            if not self.connect():
                return 0

        processed_count = 0
        last_noop_time = time.time()  # Track last NOOP time
        noop_interval = 60  # Send NOOP every 60 seconds (adjust as needed)

        try:
            # Select inbox
            self.conn.select("INBOX")

            # Search for unread emails
            status, messages = self.conn.search(None, "UNSEEN")
            if status != "OK":
                logger.error("Failed to search for unread emails.")
                return 0

            email_ids = messages[0].split()
            if not email_ids:
                logger.info(f"No unread emails found for {self.email_addr}")
                return 0

            logger.info(f"Found {len(email_ids)} unread emails for {self.email_addr}")

            # Phase 1: Quick fetch - Get all email contents and store in database
            email_data_list = []
            for i, email_id in enumerate(email_ids):
                try:
                    # Check connection status before processing each email
                    if not self.conn:
                        logger.warning("Connection lost, attempting to reconnect...")
                        if not self.connect():
                            logger.error("Failed to reconnect, stopping email fetch.")
                            return processed_count
                        self.conn.select("INBOX")

                    # Keep connection alive
                    current_time = time.time()
                    if current_time - last_noop_time > noop_interval:
                        try:
                            logger.debug(
                                f"Sending NOOP to keep connection alive (Email index: {i})."
                            )
                            status, _ = self.conn.noop()
                            if status == "OK":
                                last_noop_time = current_time
                            else:
                                logger.warning(
                                    f"NOOP command failed with status {status}. Attempting reconnect."
                                )
                                self.disconnect()
                                if not self.connect():
                                    logger.error(
                                        "Reconnect failed after NOOP failure, stopping email fetch."
                                    )
                                    return processed_count
                                self.conn.select("INBOX")
                                last_noop_time = time.time()
                        except (
                            imaplib.IMAP4.abort,
                            imaplib.IMAP4.error,
                            ConnectionResetError,
                        ) as e:
                            logger.warning(
                                f"NOOP failed due to connection error: {e}. Attempting reconnect."
                            )
                            self.disconnect()
                            if not self.connect():
                                logger.error(
                                    "Reconnect failed after NOOP error, stopping email fetch."
                                )
                                return processed_count
                            self.conn.select("INBOX")
                            last_noop_time = time.time()

                    # Get UID
                    status, uid_data = self.conn.fetch(email_id, "(UID)")
                    if status != "OK":
                        logger.error(
                            f"Failed to fetch UID for email {email_id}: {uid_data}"
                        )
                        continue

                    uid_response = uid_data[0].decode("utf-8")
                    uid_match = re.search(r"UID (\d+)", uid_response)
                    uid = uid_match.group(1) if uid_match else None

                    if not uid:
                        logger.error(f"Failed to extract UID for email {email_id}")
                        continue

                    # Get email content
                    status, msg_data = self.conn.fetch(email_id, "(BODY.PEEK[])")
                    if status != "OK":
                        logger.error(f"Failed to fetch email {email_id}: {msg_data}")
                        continue

                    # Parse email
                    raw_email = msg_data[0][1]
                    msg = email.message_from_bytes(raw_email)

                    # Get email details
                    message_id = msg.get("Message-ID", "")
                    sender = decode_email_address(msg.get("From", ""))
                    recipient = decode_email_address(msg.get("To", ""))
                    cc = decode_email_address(msg.get("Cc", ""))
                    bcc = decode_email_address(msg.get("Bcc", ""))
                    subject = decode_email_subject(msg.get("Subject", ""))
                    email_date = msg.get("Date", "")
                    delivered_to = extract_delivered_to_candidates(msg)

                    # Get email body
                    body_text, body_html = get_email_body(msg)

                    # Prepare email data
                    email_data = {
                        "email_account": self.account_info["id"],
                        "message_id": message_id,
                        "sender": sender,
                        "recipient": recipient,
                        "cc": cc,
                        "bcc": bcc,
                        "subject": subject,
                        "email_date": email_date,
                        "body_text": body_text,
                        "body_html": body_html,
                        "uid": uid,
                        "delivered_to": json.dumps(delivered_to),
                        "raw_email": raw_email,  # Store raw email for later processing
                    }

                    # Store email data for later processing
                    email_data_list.append(email_data)

                except (
                    imaplib.IMAP4.abort,
                    imaplib.IMAP4.error,
                    ConnectionResetError,
                ) as conn_err:
                    logger.error(
                        f"IMAP connection error processing email {email_id}: {conn_err}. Attempting reconnect."
                    )
                    self.disconnect()
                    if not self.connect():
                        logger.error(
                            "Reconnect failed after processing error, stopping email fetch."
                        )
                        return processed_count
                    self.conn.select("INBOX")
                    last_noop_time = time.time()
                    continue

                except Exception as e:
                    logger.error(
                        f"Non-connection error processing email {email_id}: {e}"
                    )
                    continue

            # Disconnect from IMAP after fetching all emails
            self.disconnect()

            # Phase 2: Process emails and send to Telegram
            from app.user.email_telegram import EmailTelegramSender

            email_sender = EmailTelegramSender()

            for email_data in email_data_list:
                try:
                    # Check if email exists and insert if not
                    email_db_id, is_new_email = self._execute_db_transaction(
                        email_data, email_data["uid"]
                    )
                    if not is_new_email or not email_db_id:
                        logger.debug(
                            f"Email with UID {email_data['uid']} already exists or failed to insert, skipping"
                        )
                        continue

                    # Process attachments
                    msg = email.message_from_bytes(email_data["raw_email"])
                    attachments = []
                    for part in msg.walk():
                        content_disposition = str(part.get("Content-Disposition"))
                        if "attachment" in content_disposition:
                            filename = part.get_filename()
                            if filename:
                                payload = part.get_payload(decode=True)
                                content_type = part.get_content_type()
                                attachments.append(
                                    {
                                        "filename": filename,
                                        "content_type": content_type,
                                        "data": payload,
                                    }
                                )

                    # Prepare data for Telegram
                    telegram_data = {
                        "id": email_db_id,
                        "email_account": email_data["email_account"],
                        "message_id": email_data["message_id"],
                        "sender": email_data["sender"],
                        "recipient": email_data["recipient"],
                        "cc": email_data["cc"],
                        "bcc": email_data["bcc"],
                        "subject": email_data["subject"],
                        "email_date": email_data["email_date"],
                        "body_text": email_data["body_text"],
                        "body_html": email_data["body_html"],
                        "uid": email_data["uid"],
                        "attachments": attachments,
                    }

                    # Send to Telegram
                    result = await email_sender.send_email_to_telegram(telegram_data)
                    if result:
                        logger.info(
                            f"Successfully sent email {email_db_id} to Telegram"
                        )
                        processed_count += 1
                        # Reconnect to mark email as read
                        if self.connect():
                            self.mark_email_as_read(email_data["uid"])
                            self.disconnect()
                    else:
                        logger.error(f"Failed to send email {email_db_id} to Telegram")

                except Exception as e:
                    logger.error(f"Error processing email {email_data['uid']}: {e}")
                    continue

            return processed_count

        except Exception as e:
            logger.error(f"Error fetching unread emails: {e}")
            return processed_count
        finally:
            # Ensure we're disconnected
            self.disconnect()

    def mark_email_as_read(self, uid: str) -> bool:
        """
        Mark an email as read both in the IMAP server and the database

        Args:
            uid: UID of the email to mark as read

        Returns:
            bool: True if successful, False otherwise
        """
        if not self.conn:
            if not self.connect():
                return False

        try:
            # Select inbox
            self.conn.select("INBOX")

            # Mark as read on server (add \Seen flag)
            status, response = self.conn.uid("STORE", uid, "+FLAGS", r"(\Seen)")
            if status != "OK":
                logger.error(f"Failed to mark email as read on server: {response}")
                return False

            return True

        except Exception as e:
            logger.error(f"Error marking email as read: {e}")
            return False
        finally:
            self.disconnect()

    @retry_on_fail(
        max_retries=3,
        retry_delay=1.0,
        exceptions=(sqlite3.OperationalError, sqlite3.DatabaseError),
        retry_on_error_message="database is locked",
    )
    def _execute_db_transaction(self, email_data, uid):
        """
        Execute a database transaction to check if email exists and insert if not

        Args:
            email_data: Email data dictionary
            uid: Email UID

        Returns:
            Tuple[int, bool]: (email_db_id, is_new_email) - database ID and whether it's new
        """
        conn = None
        try:
            conn = self.db_manager._get_connection()
            cursor = conn.cursor()

            # Check if email exists
            cursor.execute(
                "SELECT id FROM emails WHERE email_account = ? AND uid = ?",
                (self.account_info["id"], uid),
            )
            existing_email = cursor.fetchone()

            if existing_email:
                # Email already exists
                conn.close()
                return existing_email[0], False

            # Insert email
            cursor.execute(
                """
                INSERT OR IGNORE INTO emails
                (email_account, message_id, sender, recipient, cc, bcc, subject, email_date,
                 body_text, body_html, uid, delivered_to)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    email_data["email_account"],
                    email_data["message_id"],
                    email_data["sender"],
                    email_data["recipient"],
                    email_data["cc"],
                    email_data["bcc"],
                    email_data["subject"],
                    email_data["email_date"],
                    email_data["body_text"],
                    email_data["body_html"],
                    email_data["uid"],
                    email_data.get("delivered_to"),
                ),
            )

            email_db_id = cursor.lastrowid
            conn.commit()
            conn.close()
            return email_db_id, True

        except Exception as e:
            logger.error(f"Error executing database transaction: {e}")
            if conn:
                try:
                    conn.rollback()
                except Exception:
                    pass
            raise  # Let the decorator handle retries
        finally:
            if conn:
                try:
                    conn.close()
                except Exception:
                    pass

    def delete_email_by_uid(self, uid: str) -> bool:
        if not self.conn:
            if not self.connect():
                logger.error(f"Failed to connect to IMAP server for {self.email_addr}")
                return False

        try:
            self.conn.select("INBOX")

            # If UID is already gone from INBOX, treat it as success and clean local DB
            try:
                search_status, search_data = self.conn.uid(
                    "SEARCH", None, f"UID {uid}"
                )
                if search_status == "OK":
                    found = bool((search_data[0] or b"").strip())
                    if not found:
                        logger.info(
                            f"Email UID {uid} not found in INBOX; treating as already deleted"
                        )
                        db_result = self.db_manager.delete_email_by_uid(
                            self.account_info, uid
                        )
                        if not db_result:
                            logger.warning(
                                f"Could not remove email with UID {uid} from database"
                            )
                        return True
            except Exception as search_err:
                logger.warning(
                    f"Failed to verify existence for UID {uid} before deletion: {search_err}"
                )

            status, response = self.conn.uid("STORE", uid, "+FLAGS", r"(\Deleted)")
            if status != "OK":
                logger.error(f"Failed to mark email {uid} as deleted: {response}")
                return False

            logger.info(f"Successfully marked email with UID {uid} for deletion")

            # Ensure the deletion is actually applied on server (provider behavior differs)
            expunged = False
            try:
                expunge_status, expunge_resp = self.conn.uid("EXPUNGE", uid)
                if expunge_status == "OK":
                    expunged = True
                else:
                    logger.warning(
                        f"UID EXPUNGE failed for email {uid}: {expunge_resp}"
                    )
            except Exception as uid_expunge_err:
                logger.debug(
                    f"UID EXPUNGE not supported or failed for {uid}: {uid_expunge_err}"
                )

            if not expunged:
                expunge_status, expunge_resp = self.conn.expunge()
                if expunge_status != "OK":
                    logger.error(
                        f"EXPUNGE failed after marking email {uid} deleted: {expunge_resp}"
                    )
                    return False

            # delete from local db only after server-side delete succeeded
            db_result = self.db_manager.delete_email_by_uid(self.account_info, uid)
            if not db_result:
                logger.warning(f"Could not remove email with UID {uid} from database")
            return True

        except (
            imaplib.IMAP4.abort,
            imaplib.IMAP4.error,
            ConnectionResetError,
        ) as conn_err:
            logger.error(
                f"IMAP connection error while deleting email {uid}: {conn_err}"
            )
            return False
        except Exception as e:
            logger.error(f"Error deleting email with UID {uid}: {e}")
            return False
        finally:
            self.disconnect()
