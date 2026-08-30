"""Narrow IMAP adapter for reliable UID-based v2 ingestion/deletion.

It reuses the established connection setup while avoiding the legacy client's
database and Telegram delivery side effects.
"""
from __future__ import annotations

import asyncio
import email
import re
from typing import Any

from app.email_utils.imap_connection import V2IMAPClient
from app.email_utils.text import decode_email_address, decode_email_subject, get_email_body

from .types import Attachment, FetchedMessages, IncomingMail


class IMAPTransport:
    def __init__(self, account: dict[str, Any], *, client_cls: type[Any] = V2IMAPClient) -> None:
        self.account = account
        self.client = client_cls(account)

    async def fetch_messages(self, mailbox: str = "INBOX", *, after_uid: int = 0) -> list[IncomingMail]:
        """Fetch UIDs newer than the durable mailbox cursor, independent of Seen."""
        return list((await self.fetch_incremental(mailbox, after_uid=after_uid)).messages)

    async def fetch_message(self, mailbox: str, uid: str | int) -> IncomingMail:
        """Fetch and parse one exact IMAP UID for history/backfill operations."""
        return await asyncio.to_thread(self._fetch_message_sync, mailbox, str(uid))

    async def fetch_incremental(self, mailbox: str = "INBOX", *, after_uid: int = 0) -> FetchedMessages:
        return await asyncio.to_thread(self._fetch_messages_sync, mailbox, after_uid)

    async def verify(self, mailbox: str = "INBOX") -> bool:
        """Verify authentication and a selectable mailbox without fetching mail."""
        return await asyncio.to_thread(self._verify_sync, mailbox)

    async def mark_read(self, mailbox: str, uid: str | int, *, uidvalidity: str | None = None) -> bool:
        """Mark one exact IMAP UID as read (``\\Seen``).

        The UID and optional UIDVALIDITY guard ensure that a delayed projection
        cannot accidentally mark a different message after the provider reuses a
        UID in a new mailbox epoch.
        """
        return await asyncio.to_thread(self._mark_read_sync, mailbox, str(uid), uidvalidity)

    def _verify_sync(self, mailbox: str) -> bool:
        if not self.client.connect():
            raise ConnectionError("unable to connect to IMAP")
        try:
            status, _ = self.client.conn.select(mailbox)
            if status != "OK":
                raise RuntimeError("unable to select IMAP mailbox")
            return True
        finally:
            self.client.disconnect()

    def _mark_read_sync(self, mailbox: str, uid: str, uidvalidity: str | None = None) -> bool:
        if not uid.isdigit():
            raise ValueError("IMAP UID must be numeric")
        if not self.client.connect():
            raise ConnectionError("unable to connect to IMAP")
        try:
            status, select_data = self.client.conn.select(mailbox)
            if status != "OK":
                raise RuntimeError(f"unable to select IMAP mailbox {mailbox!r}")
            expected_uidvalidity = str(uidvalidity or "")
            if expected_uidvalidity:
                current_uidvalidity = self._uidvalidity(select_data)
                if current_uidvalidity != expected_uidvalidity:
                    raise RuntimeError("mailbox UIDVALIDITY changed; refusing unsafe read flag")
            status, _ = self.client.conn.uid("STORE", uid, "+FLAGS.SILENT", r"(\Seen)")
            return status == "OK"
        finally:
            self.client.disconnect()

    def _fetch_messages_sync(self, mailbox: str, after_uid: int = 0) -> FetchedMessages:
        if not self.client.connect():
            raise ConnectionError("unable to connect to IMAP")
        try:
            status, data = self.client.conn.select(mailbox)
            if status != "OK":
                raise RuntimeError(f"unable to select IMAP mailbox {mailbox!r}")
            # ``UNSEEN`` is not a delivery cursor: a message can already be read
            # before our process observes it.  IMAP UIDs are monotonic per
            # mailbox, so use the durable maximum UID as the only fetch boundary.
            start_uid = max(1, int(after_uid) + 1)
            status, rows = self.client.conn.uid("SEARCH", None, "UID", f"{start_uid}:*")
            if status != "OK":
                return FetchedMessages((), self._uidvalidity(data))
            ids = (rows[0] or b"").split() if rows else []
            result: list[IncomingMail] = []
            for uid in ids:
                uid_text = uid.decode("ascii", errors="ignore") if isinstance(uid, bytes) else str(uid)
                if not uid_text.isdigit():
                    continue
                status, body_data = self.client.conn.uid("FETCH", uid_text, "(BODY.PEEK[])")
                if status != "OK" or not body_data:
                    continue
                raw = next((item[1] for item in body_data if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)), None)
                if raw is None:
                    continue
                result.append(self._parse(raw, mailbox, uid_text))
            return FetchedMessages(tuple(result), self._uidvalidity(data))
        finally:
            self.client.disconnect()

    def _fetch_message_sync(self, mailbox: str, uid: str) -> IncomingMail:
        if not str(uid).isdigit():
            raise ValueError("IMAP UID must be numeric")
        if not self.client.connect():
            raise ConnectionError("unable to connect to IMAP")
        try:
            status, _ = self.client.conn.select(mailbox)
            if status != "OK":
                raise RuntimeError(f"unable to select IMAP mailbox {mailbox!r}")
            status, body_data = self.client.conn.uid("FETCH", str(uid), "(BODY.PEEK[])")
            if status != "OK" or not body_data:
                raise LookupError("IMAP message was not found")
            raw = next(
                (
                    item[1]
                    for item in body_data
                    if isinstance(item, tuple) and len(item) > 1 and isinstance(item[1], bytes)
                ),
                None,
            )
            if raw is None:
                raise LookupError("IMAP message body was not returned")
            return self._parse(raw, mailbox, str(uid))
        finally:
            self.client.disconnect()

    def _uidvalidity(self, select_data: Any) -> str | None:
        def explicit_uidvalidity(values: Any) -> str | None:
            for value in values or ():
                text = value.decode("ascii", errors="ignore") if isinstance(value, bytes) else str(value)
                match = re.search(r"UIDVALIDITY\s+(\d+)", text, flags=re.IGNORECASE)
                if match:
                    return match.group(1)
            return None

        selected = explicit_uidvalidity(select_data)
        if selected:
            return selected
        response = getattr(self.client.conn, "response", None)
        if callable(response):
            try:
                response_type, values = response("UIDVALIDITY")
            except Exception:
                return None
            explicit = explicit_uidvalidity((response_type, *(values or ())))
            if explicit:
                return explicit
            # ``imaplib.IMAP4.response("UIDVALIDITY")`` normally returns
            # ``("UIDVALIDITY", [b"<number>"])``. Bare numbers are valid only
            # in this dedicated response, never in ``select()`` data where they
            # represent the mailbox's EXISTS count.
            for value in values or ():
                text = value.decode("ascii", errors="ignore") if isinstance(value, bytes) else str(value)
                if re.fullmatch(r"\d+", text.strip()):
                    return text.strip()
        return None

    def _parse(self, raw: bytes, mailbox: str, uid: str) -> IncomingMail:
        msg = email.message_from_bytes(raw)
        attachments: list[Attachment] = []
        for part in msg.walk():
            payload = part.get_payload(decode=True)
            if payload is None:
                continue
            content_id = str(part.get("Content-ID") or "").strip()
            # Only image parts are usable as HTML ``cid:`` sources.  Do not
            # persist arbitrary MIME bodies just because they carry a Content-ID.
            if content_id and part.get_content_maintype().lower() == "image":
                attachments.append(Attachment(
                    part.get_filename() or "inline-image",
                    payload,
                    part.get_content_type(),
                    content_id=content_id,
                    is_inline=True,
                ))
                continue
            if "attachment" in str(part.get("Content-Disposition", "")).lower():
                attachments.append(Attachment(part.get_filename() or "attachment", payload, part.get_content_type()))
        text, html = get_email_body(msg)
        split = lambda value: tuple(filter(None, (decode_email_address(value or "") or "").split(",")))
        return IncomingMail(
            account_id=self.account["id"], mailbox=mailbox, uid=uid,
            message_id=msg.get("Message-ID"), sender=decode_email_address(msg.get("From", "")),
            to=split(msg.get("To")), cc=split(msg.get("Cc")), subject=decode_email_subject(msg.get("Subject", "")),
            text_body=text, html_body=html, received_at=msg.get("Date", ""), in_reply_to=msg.get("In-Reply-To"),
            references=tuple(re.findall(r"<[^>]+>", msg.get("References", ""))), attachments=tuple(attachments), raw=raw,
        )

    async def delete(self, mapping: dict[str, Any]) -> bool:
        return await asyncio.to_thread(self._delete_sync, mapping)

    def _delete_sync(self, mapping: dict[str, Any]) -> bool:
        uid, mailbox = str(mapping["uid"]), str(mapping.get("mailbox") or "INBOX")
        if not self.client.connect():
            raise ConnectionError("unable to connect to IMAP")
        try:
            select_status, select_data = self.client.conn.select(mailbox)
            if select_status != "OK":
                return False
            expected_uidvalidity = str(mapping.get("uidvalidity") or "")
            if expected_uidvalidity:
                current_uidvalidity = self._uidvalidity(select_data)
                if current_uidvalidity != expected_uidvalidity:
                    raise RuntimeError("mailbox UIDVALIDITY changed; refusing unsafe delete")
            # A retry may run after the provider accepted the earlier STORE but
            # the worker lost the acknowledgement (or could not safely issue a
            # UID EXPUNGE).  Once the exact UID is absent from its original
            # mailbox, repeating the mutation cannot improve the outcome and
            # would leave the durable saga stuck forever.
            search_status, search_rows = self.client.conn.uid(
                "SEARCH", None, "UID", uid
            )
            if search_status == "OK" and search_rows:
                existing_uids = {
                    value
                    for row in search_rows
                    for value in (
                        row.split()
                        if isinstance(row, bytes)
                        else str(row or "").split()
                    )
                }
                expected_uid = uid.encode("ascii") if any(
                    isinstance(value, bytes) for value in existing_uids
                ) else uid
                if expected_uid not in existing_uids:
                    return True
            # Gmail maps IMAP folders to labels. Deleting from INBOX with a
            # plain \Deleted flag can archive the message instead of putting it
            # in Trash, so apply Gmail's system Trash label to this exact UID.
            if self._supports_capability("X-GM-EXT-1"):
                status, _ = self.client.conn.uid(
                    "STORE", uid, "+X-GM-LABELS", r"(\Trash)"
                )
                if status == "OK":
                    return True
            # Some providers (notably iCloud) expose a server-side Trash
            # mailbox but omit MOVE from CAPABILITY.  When a special-use Trash
            # mailbox is discoverable, try the exact-UID MOVE regardless of the
            # advertised capability; a rejected command is harmless and lets
            # the safe flag-verification path below decide what to do next.
            trash_mailbox = self._find_trash_mailbox()
            if trash_mailbox and (self._supports_capability("MOVE") or self._is_icloud_server()):
                try:
                    status, _ = self.client.conn.uid("MOVE", uid, trash_mailbox)
                except Exception:
                    status = "NO"
                if status == "OK":
                    return True
            status, _ = self.client.conn.uid("STORE", uid, "+FLAGS.SILENT", r"(\Deleted)")
            if status != "OK":
                return False
            # A plain EXPUNGE deletes *every* \Deleted message in the selected
            # mailbox. Require UIDPLUS and target only this provider UID.  Some
            # servers do not offer UIDPLUS but do durably hide an exact UID once
            # it carries the \Deleted flag; verify that state rather than
            # issuing an unsafe mailbox-wide EXPUNGE.
            if self._supports_uid_expunge():
                status, _ = self.client.conn.uid("EXPUNGE", uid)
                return status == "OK"
            if self._uid_is_deleted(uid):
                return True
            # Do not leave a \Deleted flag behind for a later unrelated
            # mailbox-wide EXPUNGE to consume.
            self.client.conn.uid("STORE", uid, "-FLAGS.SILENT", r"(\Deleted)")
            return False
        finally:
            self.client.disconnect()

    def _supports_uid_expunge(self) -> bool:
        return self._supports_capability("UIDPLUS")

    def _uid_is_deleted(self, uid: str) -> bool:
        """Verify that this exact UID has the server's ``\\Deleted`` flag."""

        status, rows = self.client.conn.uid("SEARCH", None, "DELETED")
        if status != "OK":
            return False
        values = {
            value.decode("ascii", errors="ignore") if isinstance(value, bytes) else str(value)
            for row in rows or ()
            for value in (row.split() if isinstance(row, bytes) else str(row or "").split())
        }
        return str(uid) in values

    def _is_icloud_server(self) -> bool:
        account = getattr(self.client, "account_info", {}) or {}
        return str(account.get("imap_server") or "").strip().lower() == "imap.mail.me.com"

    def _find_trash_mailbox(self) -> str | None:
        """Return the mailbox marked with the IMAP ``\\Trash`` special use."""
        list_method = getattr(self.client.conn, "list", None)
        if not callable(list_method):
            return None
        try:
            status, rows = list_method("", "*")
        except Exception:
            # iCloud rejects imaplib's unquoted empty LIST reference.  Its
            # private command helper accepts the correctly quoted RFC form.
            raw_command = getattr(self.client.conn, "_simple_command", None)
            if not callable(raw_command):
                return None
            try:
                status, rows = raw_command("LIST", '""', '"*"')
            except Exception:
                return None
        if status != "OK":
            return '"Deleted Messages"' if self._is_icloud_server() else None
        for row in rows or ():
            raw = row[1] if isinstance(row, tuple) and len(row) > 1 else row
            value = raw.decode("utf-8", errors="replace") if isinstance(raw, bytes) else str(raw or "")
            flags_match = re.match(r"^\((?P<flags>[^)]*)\)\s+", value)
            if not flags_match or not re.search(r"(?:^|\s)\\Trash(?:$|\s)", flags_match.group("flags"), re.IGNORECASE):
                continue
            mailbox = value[flags_match.end():].strip()
            # LIST normally quotes the delimiter and mailbox. Keep escaped
            # characters intact enough for imaplib's command encoder.
            quoted = re.match(r'^(?:"(?:[^"\\]|\\.)*"\s+)?"(?P<name>(?:[^"\\]|\\.)*)"$', mailbox)
            if quoted:
                return quoted.group("name").replace(r'\\"', '"').replace(r"\\\\", r"\\")
            return mailbox.split()[-1].strip('"') or None
        # iCloud does not mark its Deleted Messages mailbox with \Trash, but
        # the quoted mailbox is stable and selecting it is supported.
        return '"Deleted Messages"' if self._is_icloud_server() else None

    def _supports_capability(self, expected: str) -> bool:
        capabilities = getattr(self.client.conn, "capabilities", ()) or ()
        for capability in capabilities:
            value = capability.decode("ascii", errors="ignore") if isinstance(capability, bytes) else str(capability)
            if value.upper() == expected.upper():
                return True
        response = getattr(self.client.conn, "response", None)
        if callable(response):
            try:
                _, values = response("CAPABILITY")
                joined = b" ".join(v if isinstance(v, bytes) else str(v).encode() for v in (values or ()))
                return expected.upper().encode("ascii") in joined.upper().split()
            except Exception:
                return False
        return False
