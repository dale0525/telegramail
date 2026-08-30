"""Telegram-native inbox panel and durable mail-delete callbacks."""

from __future__ import annotations

import asyncio
import html
from typing import Any, Awaitable, Callable, Mapping
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

from app.integrations.telegram_http import TelegramApiError
from app.services.telegram_html import sanitize_telegram_limited_html, telegram_limited_html_to_text
from app.services.telegram_mail_links import sanitize_important_links


PAGE_SIZE = 5
CALLBACK_PREFIX = "tm"


def topic_delete_keyboard(
    thread_id: int,
    *,
    action_links: list[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Build Topic-local controls, optionally including extracted mail links.

    The delete button remains the first row for backwards compatibility.  URL
    buttons are deliberately supplied as data rather than interpolated into
    Telegram HTML, so an untrusted email can never become markup or callback
    data.
    """

    rows: list[list[dict[str, Any]]] = [[{
        "text": "🗑 删除邮件",
        "callback_data": f"{CALLBACK_PREFIX}:delete:ask:{int(thread_id)}:topic:0",
        "style": "danger",
    }]]
    # Keyboard builders are also called by repair/callback paths, so validate
    # once more at this trust boundary even when the caller already sanitized
    # the LLM result.
    for link in sanitize_important_links(action_links):
        caption = str(link.get("caption") or "打开链接").strip()
        url = str(link.get("link") or link.get("url") or "").strip()
        if not caption or not url:
            continue
        rows.append([{"text": caption[:64], "url": url}])
    return {"inline_keyboard": rows}


def topic_action_keyboard(action_links: list[Mapping[str, Any]] | None = None) -> dict[str, Any]:
    """Build a URL-only keyboard for projections without a persisted thread id."""

    rows: list[list[dict[str, Any]]] = []
    for link in sanitize_important_links(action_links):
        caption = str(link.get("caption") or "打开链接").strip()
        url = str(link.get("link") or link.get("url") or "").strip()
        if caption and url:
            rows.append([{"text": caption[:64], "url": url}])
    return {"inline_keyboard": rows}


def _confirmation_keyboard(thread_id: int, context: str, cursor: str | int | None) -> dict[str, Any]:
    token = _cursor_token(cursor)
    callback_id = str(int(thread_id))
    # Keep legacy decimal callbacks readable, but compact extreme SQLite ids
    # so both confirmation actions always fit Telegram's 64-byte limit.
    decimal_suffix = f"{callback_id}:{context}:{token}"
    if len(f"{CALLBACK_PREFIX}:delete:confirm:{decimal_suffix}") > 64:
        callback_id = f"b{_base36(int(thread_id))}"
    suffix = f"{callback_id}:{context}:{token}"
    return {
        "inline_keyboard": [[
            {
                "text": "确认删除",
                "callback_data": f"{CALLBACK_PREFIX}:delete:confirm:{suffix}",
                "style": "danger",
            },
            {"text": "取消", "callback_data": f"{CALLBACK_PREFIX}:delete:cancel:{suffix}"},
        ]]
    }


class TelegramMailBotUi:
    """One reusable inbox message plus Topic-local delete controls."""

    def __init__(self, repository: Any, *, mini_app_url: str | None = None) -> None:
        self.repository = repository
        self.mini_app_url = mini_app_url
        self.client: Any = None
        self._topic_repair_locks: dict[int, asyncio.Lock] = {}
        self._bot_username: str | None = None
        self._wake_workers: Callable[[], None] | None = None
        self._refresh_task: asyncio.Task[Any] | None = None
        self._refresh_pending = False
        self._delete_tasks: set[asyncio.Task[Any]] = set()
        self._last_render_cursor: str | None = None
        self._last_render_search: str | None = None

    def bind_client(self, client: Any) -> None:
        self.client = client

    def bind_worker_wake(self, wake: Callable[[], None]) -> None:
        self._wake_workers = wake

    async def retire_inbox_panel(self) -> bool:
        """Delete the obsolete aggregate inbox message once and clear its pointer."""

        if self.client is None:
            raise RuntimeError("Telegram UI client is not bound")
        binding = self.repository.get_admin_binding()
        if not binding:
            return False
        chat_id = binding.get("inbox_panel_chat_id")
        message_id = binding.get("inbox_panel_message_id")
        if chat_id is None or message_id is None:
            return False
        try:
            await self.client.delete_message(int(chat_id), int(message_id))
        except TelegramApiError as exc:
            description = exc.description.casefold()
            if "message to delete not found" not in description:
                return False
        clear = getattr(self.repository, "clear_inbox_panel_message", None)
        if not callable(clear):
            raise RuntimeError("repository cannot clear the retired inbox panel")
        clear(int(binding["telegram_user_id"]))
        return True

    def _schedule_delete_task(self, action: Awaitable[Any], *, name: str) -> None:
        """Run a Telegram delete-side effect without delaying callback ack."""

        task = asyncio.create_task(action, name=name)
        self._delete_tasks.add(task)

        def consume(completed: asyncio.Task[Any]) -> None:
            self._delete_tasks.discard(completed)
            try:
                completed.result()
            except asyncio.CancelledError:
                pass
            except Exception:
                # The durable worker owns retries.  A best-effort UI task must
                # never turn a persisted delete request into a failed callback.
                pass

        task.add_done_callback(consume)

    def request_inbox_refresh(self) -> None:
        """Debounce delivery-triggered Inbox refreshes into one panel update."""

        if self.client is None:
            return
        if self._refresh_task is not None and not self._refresh_task.done():
            self._refresh_pending = True
            return
        try:
            asyncio.get_running_loop()
        except RuntimeError:
            return
        self._refresh_pending = False
        self._refresh_task = asyncio.create_task(self._debounced_inbox_refresh(), name="telegramail-inbox-refresh")

    async def _debounced_inbox_refresh(self) -> None:
        try:
            # A short window absorbs a mailbox batch while keeping a single new-mail
            # panel responsive for normal one-message delivery.
            await asyncio.sleep(0.8)
            binding = self.repository.get_admin_binding()
            if binding is None or binding.get("private_chat_id") is None:
                return
            # Do not interrupt historical browsing.  The Topic notification remains
            # the authoritative new-mail signal; the user can tap 最新 explicitly.
            cursor = binding.get("inbox_panel_cursor") or self._last_render_cursor
            if cursor:
                return
            await self.show_inbox(
                binding,
                cursor=None,
                search=binding.get("inbox_panel_search"),
                force_new=False,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Delivery callbacks are fire-and-forget. A transient Telegram or
            # database error must not leave the debounce task in an unobserved
            # failed state; the worker will request another refresh later.
            return
        finally:
            self._refresh_task = None
            if self._refresh_pending:
                self._refresh_pending = False
                self.request_inbox_refresh()

    async def backfill_topic_delete_controls(self, *, limit: int = 5, delay_seconds: float = 1.5) -> int:
        """Add delete controls to historical Topic messages without blocking startup."""

        if self.client is None:
            raise RuntimeError("Telegram UI client is not bound")
        targets = self.repository.list_topic_delete_markup_targets(limit=limit)
        for index, target in enumerate(targets):
            success = False
            try:
                await self._edit_markup(
                    int(target["telegram_chat_id"]),
                    int(target["telegram_message_id"]),
                    reply_markup=topic_delete_keyboard(
                        int(target["thread_id"]),
                        action_links=(
                            _target_important_links(target)
                            if int(target.get("part_index") or 0) == 0
                            else None
                        ),
                    ),
                )
                success = True
            except TelegramApiError:
                # Rotate failures behind untouched rows and retry on a later pass.
                pass
            finally:
                self.repository.complete_topic_delete_markup_backfill(
                    int(target["delivery_part_id"]), success=success
                )
            if index + 1 < len(targets) and delay_seconds > 0:
                await asyncio.sleep(float(delay_seconds))
        return len(targets)

    async def handle_message(self, message: dict[str, Any]) -> bool:
        # Telegram Topics are the inbox.  /start is handled by the dispatcher so
        # it can refresh the Mini App menu, while /inbox intentionally has no
        # aggregate panel to recreate.
        return False

    async def show_inbox(
        self, binding: Mapping[str, Any], *, cursor: str | None = None,
        search: str | None = None, page: int | None = None, force_new: bool = False,
    ) -> None:
        if self.client is None:
            raise RuntimeError("Telegram UI client is not bound")
        chat_id = int(binding["private_chat_id"])
        if page is not None:
            cursor = self._cursor_for_legacy_page(int(page), search=search)
        text, markup = self._render_inbox(cursor=cursor, search=search)
        cursor = self._last_render_cursor
        search = self._last_render_search
        existing_chat = binding.get("inbox_panel_chat_id")
        existing_message = binding.get("inbox_panel_message_id")
        if not force_new and existing_chat is not None and existing_message is not None:
            try:
                await self._edit_text(
                    int(existing_chat), int(existing_message), text,
                    parse_mode="HTML", reply_markup=markup,
                )
                self.repository.set_inbox_panel_state(
                    int(binding["telegram_user_id"]),
                    cursor=cursor,
                    search=search,
                )
                return
            except TelegramApiError as exc:
                # Only replace a panel Telegram definitively says is unavailable.
                if _is_not_modified(exc):
                    return
                description = exc.description.casefold()
                unavailable = "message to edit not found" in description or "message can't be edited" in description
                if exc.ambiguous or not unavailable:
                    raise
        sent = await self.client.send_message(chat_id, text, parse_mode="HTML", reply_markup=markup)
        message = sent[0] if isinstance(sent, list) and sent else sent
        message_id = message.get("message_id") if isinstance(message, Mapping) else None
        if not isinstance(message_id, int):
            raise RuntimeError("Telegram inbox panel message id is missing")
        self.repository.set_inbox_panel_message(
            int(binding["telegram_user_id"]), chat_id, message_id,
            cursor=cursor, search=search,
        )
        if force_new and existing_chat is not None and existing_message is not None:
            try:
                await self.client.delete_message(
                    int(existing_chat), int(existing_message)
                )
            except TelegramApiError:
                # The new panel is already durable and visible. Failure to clean
                # up an older bot message must not make the command look failed.
                pass

    async def show_latest_inbox(self) -> None:
        """Post a fresh Inbox panel so it remains at the latest chat position."""

        binding = self.repository.get_admin_binding()
        if binding is None or binding.get("private_chat_id") is None:
            return
        await self.show_inbox(binding, cursor=None, search=None, force_new=True)

    async def handle_callback(self, callback: dict[str, Any]) -> dict[str, Any]:
        data = callback.get("data")
        if not isinstance(data, str) or not data.startswith(f"{CALLBACK_PREFIX}:"):
            return {}
        binding = self._binding_for_callback(callback)
        if binding is None:
            return {"text": "无权执行此操作", "show_alert": True}
        message = callback.get("message")
        if not isinstance(message, Mapping):
            return {"text": "该操作已失效", "show_alert": True}
        chat = message.get("chat")
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        message_id = message.get("message_id")
        if not isinstance(chat_id, int) or not isinstance(message_id, int):
            return {"text": "该操作已失效", "show_alert": True}

        parts = data.split(":")
        try:
            if parts[:2] == [CALLBACK_PREFIX, "inbox"]:
                return await self._handle_inbox(parts, chat_id, message_id, binding=binding)
            if parts[:2] == [CALLBACK_PREFIX, "r"]:
                # Compact deep-page Topic repair callback.  It keeps even
                # 64-bit SQLite ids under Telegram's 64-byte callback limit.
                compact = [CALLBACK_PREFIX, "inbox", "rebuild", parts[2], parts[3], parts[4]]
                return await self._handle_inbox(compact, chat_id, message_id, binding=binding)
            if parts[:2] == [CALLBACK_PREFIX, "delete"]:
                return await self._handle_delete(parts, message, chat_id, message_id, binding=binding)
        except (IndexError, KeyError, TypeError, ValueError):
            return {"text": "该操作已失效，请刷新邮箱面板", "show_alert": True}
        return {"text": "该操作已失效", "show_alert": True}

    async def _handle_inbox(
        self, parts: list[str], chat_id: int, message_id: int,
        *, binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = parts[2]
        if action == "list":
            # Legacy callback: tm:inbox:list:<page>. New callbacks use named
            # latest/refresh/older actions and compact cursors.
            token = parts[3] if len(parts) > 3 else "0"
            if token.isdigit():
                cursor = self._cursor_for_legacy_page(int(token), search=(binding or {}).get("inbox_panel_search"))
            else:
                cursor = None if token in {"0", "latest", "none"} else token
            text, markup = self._render_inbox(cursor=cursor, search=(binding or {}).get("inbox_panel_search"))
        elif action == "latest":
            text, markup = self._render_inbox(cursor=None, search=(binding or {}).get("inbox_panel_search"))
        elif action == "refresh":
            token = parts[3] if len(parts) > 3 else (binding or {}).get("inbox_panel_cursor")
            text, markup = self._render_inbox(cursor=token, search=(binding or {}).get("inbox_panel_search"))
        elif action == "older":
            token = parts[3] if len(parts) > 3 else None
            text, markup = self._render_inbox(cursor=token, search=(binding or {}).get("inbox_panel_search"))
        elif action == "clear":
            text, markup = self._render_inbox(cursor=None, search=None)
        elif action == "view":
            thread_id, cursor = int(parts[3]), self._normalize_callback_cursor(parts[4] if len(parts) > 4 else "0", binding)
            prepared = await self._prepare_topic(thread_id)
            topic_url = prepared.get("topic_url")
            text, markup = self._render_detail(
                thread_id,
                cursor,
                topic_url=str(topic_url) if isinstance(topic_url, str) else None,
            )
            resolved_cursor = self._last_render_cursor
            await self._edit_text(
                chat_id, message_id, text, parse_mode="HTML", reply_markup=markup
            )
            if binding is not None:
                try:
                    self.repository.set_inbox_panel_state(
                        int(binding["telegram_user_id"]), cursor=resolved_cursor,
                        search=(binding.get("inbox_panel_search") if binding else None),
                    )
                except (AttributeError, PermissionError):
                    pass
            return {}
        elif action in {"open", "rebuild"}:
            # ``open`` keeps older repair buttons working. A Topic deleted from
            # private-chat history can still accept every Bot API probe, so a
            # user-requested repair must create a fresh Topic unconditionally.
            raw_thread_id = parts[3]
            raw_cursor = parts[4] if len(parts) > 4 else "0"
            thread_id = int(raw_thread_id) if raw_cursor in {"0", "latest", "none"} else _parse_compact_int(raw_thread_id)
            cursor = self._normalize_callback_cursor(raw_cursor, binding)
            expected_topic_id = (
                (int(parts[5]) if raw_cursor in {"0", "latest", "none"} else _parse_compact_int(parts[5]))
                if action == "rebuild" and len(parts) > 5 else None
            )
            prepared = await self._rebuild_topic(
                thread_id, expected_topic_id=expected_topic_id
            )
            topic_url = prepared.get("topic_url")
            text, markup = self._render_detail(
                thread_id,
                cursor,
                topic_url=str(topic_url) if isinstance(topic_url, str) else None,
            )
            resolved_cursor = self._last_render_cursor
            await self._edit_text(
                chat_id, message_id, text, parse_mode="HTML", reply_markup=markup
            )
            if binding is not None:
                try:
                    self.repository.set_inbox_panel_state(
                        # A deleted thread may have caused _render_detail to
                        # fall back one page; persist that resolved location.
                        int(binding["telegram_user_id"]),
                        cursor=resolved_cursor,
                        search=(binding.get("inbox_panel_search") if binding else None),
                    )
                except (AttributeError, PermissionError):
                    pass
            if topic_url:
                return {
                    "text": (
                        "Topic 已重建，邮件正在重新投递，请点击“进入 Topic”"
                        if prepared.get("repaired")
                        else "Topic 已经重建，请点击“进入 Topic”"
                    )
                }
            return {
                "text": str(prepared.get("text") or "暂时无法准备 Telegram Topic"),
                "show_alert": bool(prepared.get("show_alert", True)),
            }
        else:
            raise ValueError("unknown inbox action")
        resolved_cursor = self._last_render_cursor
        resolved_search = self._last_render_search
        await self._edit_text(
            chat_id, message_id, text, parse_mode="HTML", reply_markup=markup
        )
        if binding is not None:
            try:
                self.repository.set_inbox_panel_state(
                    int(binding["telegram_user_id"]),
                    cursor=resolved_cursor,
                    search=resolved_search,
                )
            except (AttributeError, PermissionError):
                pass
        return {}

    async def _prepare_topic(self, thread_id: int) -> dict[str, Any]:
        """Build a deep link for the stored mapping without claiming it is live."""

        thread = self.repository.get_telegram_inbox_thread(int(thread_id))
        if thread is None:
            return {"text": "邮件不存在或已经删除", "show_alert": True}
        topic_id = thread.get("telegram_message_thread_id")
        if topic_id is None:
            return {"text": "该邮件尚未创建 Telegram Topic", "show_alert": True}
        try:
            username = await self._get_bot_username()
        except TelegramApiError:
            username = None
        if not username:
            return {"text": "无法确定 Bot 用户名", "show_alert": True}
        return {"topic_url": f"https://t.me/{username}/{int(topic_id)}"}

    async def _rebuild_topic(
        self, thread_id: int, *, expected_topic_id: int | None = None
    ) -> dict[str, Any]:
        lock = self._topic_repair_locks.setdefault(int(thread_id), asyncio.Lock())
        async with lock:
            thread = self.repository.get_telegram_inbox_thread(int(thread_id))
            if thread is None:
                return {"text": "邮件不存在或已经删除", "show_alert": True}
            chat_id = thread.get("telegram_chat_id")
            topic_id = thread.get("telegram_message_thread_id")
            if chat_id is None or topic_id is None:
                return {"text": "该邮件尚未创建 Telegram Topic", "show_alert": True}
            chat_id, topic_id = int(chat_id), int(topic_id)
            if expected_topic_id is not None and topic_id != int(expected_topic_id):
                prepared = await self._prepare_topic(int(thread_id))
                prepared["repaired"] = False
                return prepared
            try:
                replacement = await self.client.create_forum_topic(
                    chat_id, _topic_name(thread)
                )
                replacement_id = int(replacement["message_thread_id"])
                try:
                    current = self.repository.replace_deleted_topic(
                        int(thread_id),
                        expected_chat_id=chat_id,
                        expected_message_thread_id=topic_id,
                        new_chat_id=chat_id,
                        new_message_thread_id=replacement_id,
                    )
                except Exception:
                    await self._delete_empty_topic(chat_id, replacement_id)
                    return {"text": "Topic 修复失败，请稍后重试", "show_alert": True}
                if current is None:
                    await self._delete_empty_topic(chat_id, replacement_id)
                    return {"text": "邮件不存在或已经删除", "show_alert": True}
                if not bool(current.get("topic_replaced")):
                    await self._delete_empty_topic(chat_id, replacement_id)
                topic_id = int(current.get("telegram_message_thread_id") or 0)
                if not topic_id:
                    return {"text": "Topic 修复失败，请稍后重试", "show_alert": True}
                repaired = bool(current.get("topic_replaced"))
                if repaired and self._wake_workers is not None:
                    self._wake_workers()
            except TelegramApiError as exc:
                return {
                    "text": "暂时无法打开 Telegram Topic，请稍后重试",
                    "show_alert": not exc.ambiguous,
                }
            try:
                username = await self._get_bot_username()
            except TelegramApiError:
                username = None
            if not username:
                return {"text": "无法确定 Bot 用户名", "show_alert": True}
            return {
                "topic_url": f"https://t.me/{username}/{topic_id}",
                "repaired": repaired,
            }

    async def _delete_empty_topic(self, chat_id: int, topic_id: int) -> None:
        try:
            await self.client.delete_forum_topic(chat_id, topic_id)
        except TelegramApiError:
            pass

    async def _optimistically_delete_topic(
        self, operation_id: int, chat_id: int, topic_id: int
    ) -> None:
        """Remove a confirmed Topic immediately and persist the completed phase."""

        delete_topic = getattr(self.client, "delete_forum_topic", None)
        if not callable(delete_topic):
            return
        try:
            deleted = await delete_topic(int(chat_id), int(topic_id))
        except TelegramApiError:
            # A timeout may already have removed the Topic, while a stale
            # mapping may still point elsewhere.  Leave the phase pending so
            # the durable worker can retry without guessing which occurred.
            return
        if deleted is not True:
            return
        marker = getattr(self.repository, "mark_delete_topic_deleted", None)
        if callable(marker):
            marker(int(operation_id))

    async def _remove_deleted_thread_from_panel(
        self,
        chat_id: int,
        message_id: int,
        *,
        cursor: str | None,
        search: str | None,
        telegram_user_id: int | None,
    ) -> None:
        """Refresh the Inbox panel after the durable operation hides its row."""

        text, markup = self._render_inbox(cursor=cursor, search=search)
        resolved_cursor = self._last_render_cursor
        resolved_search = self._last_render_search
        last_error: TelegramApiError | None = None
        # This task is detached from the callback acknowledgement. Brief
        # background retries remove a stale card after a transient Bot API
        # failure without keeping the user's button spinner active.
        for delay in (0.0, 0.25, 1.0, 3.0):
            if delay:
                await asyncio.sleep(delay)
            try:
                await self._edit_text(
                    int(chat_id), int(message_id), text,
                    parse_mode="HTML", reply_markup=markup,
                )
                last_error = None
                break
            except TelegramApiError as exc:
                last_error = exc
        if last_error is not None:
            raise last_error
        if telegram_user_id is not None:
            try:
                self.repository.set_inbox_panel_state(
                    int(telegram_user_id),
                    cursor=resolved_cursor,
                    search=resolved_search,
                )
            except (AttributeError, PermissionError):
                pass

    async def _get_bot_username(self) -> str | None:
        if self._bot_username:
            return self._bot_username
        bot = await self.client.get_me()
        username = bot.get("username") if isinstance(bot, Mapping) else None
        if isinstance(username, str) and username:
            self._bot_username = username.lstrip("@")
        return self._bot_username

    async def _handle_delete(
        self,
        parts: list[str],
        message: Mapping[str, Any],
        chat_id: int,
        message_id: int,
        *,
        binding: Mapping[str, Any] | None = None,
    ) -> dict[str, Any]:
        action = parts[2]
        if action == "status":
            operation = self.repository.get_delete(int(parts[3]))
            status = operation.get("status") if operation else "unknown"
            labels = {
                "queued": "等待删除",
                "deleting": "正在删除",
                "deleted": "已删除",
                "failed": "删除失败，系统将重试",
            }
            return {"text": labels.get(str(status), "状态未知"), "show_alert": status == "failed"}
        thread_id = _parse_callback_thread_id(parts[3])
        context = parts[4]
        cursor = self._normalize_callback_cursor(parts[5] if len(parts) > 5 else "0", binding)
        thread = self.repository.get_telegram_inbox_thread(thread_id)
        if thread is None or int(thread.get("telegram_chat_id") or 0) != chat_id:
            return {"text": "邮件不存在或已经删除", "show_alert": True}
        callback_topic = message.get("message_thread_id")
        if context == "topic" and int(callback_topic or 0) != int(thread.get("telegram_message_thread_id") or 0):
            return {"text": "邮件 Topic 不匹配", "show_alert": True}
        if context not in {"topic", "panel"}:
            raise ValueError("invalid delete context")

        if action == "ask":
            if context == "topic":
                await self._edit_markup(
                    chat_id,
                    message_id,
                reply_markup=_confirmation_keyboard(thread_id, context, cursor),
                )
            else:
                text, markup = self._render_delete_confirmation(thread, cursor)
                await self._edit_text(
                    chat_id, message_id, text, parse_mode="HTML", reply_markup=markup
                )
            return {"text": "请再次确认：这会同时删除邮箱和 Telegram 中的邮件"}
        if action == "cancel":
            if context == "topic":
                await self._edit_markup(
                    chat_id,
                    message_id,
                    reply_markup=topic_delete_keyboard(
                        thread_id,
                        action_links=thread.get("llm_important_links_json"),
                    ),
                )
            else:
                text, markup = self._render_detail(thread_id, cursor)
                await self._edit_text(
                    chat_id, message_id, text, parse_mode="HTML", reply_markup=markup
                )
            return {"text": "已取消"}
        if action == "confirm":
            operation = self.repository.create_delete_operation(
                int(thread["account_id"]),
                f"telegram-thread-delete:{thread_id}",
                thread_id=thread_id,
            )
            operation_id = int(operation["id"])
            topic_delete_already_requested = bool(operation.get("topic_delete_requested"))
            # Record the user-requested Topic deletion before the detached Bot
            # call starts.  If the process exits after Telegram accepts the
            # request but before the phase flag is written, the worker can
            # safely treat a subsequent "Topic not found" response as the
            # already-completed Topic phase.
            mark_requested = getattr(self.repository, "mark_delete_topic_requested", None)
            if callable(mark_requested):
                mark_requested(operation_id)
            topic_chat_id = operation.get("telegram_chat_id") or thread.get("telegram_chat_id")
            topic_id = operation.get("telegram_message_thread_id") or thread.get("telegram_message_thread_id")
            if (
                not bool(operation.get("topic_deleted"))
                and not topic_delete_already_requested
                and topic_chat_id is not None
                and topic_id is not None
            ):
                self._schedule_delete_task(
                    self._optimistically_delete_topic(
                        operation_id, int(topic_chat_id), int(topic_id)
                    ),
                    name=f"telegramail-delete-topic-{operation_id}",
                )
            panel_chat_id = chat_id if context == "panel" else (binding or {}).get("inbox_panel_chat_id")
            panel_message_id = message_id if context == "panel" else (binding or {}).get("inbox_panel_message_id")
            if isinstance(panel_chat_id, int) and isinstance(panel_message_id, int):
                self._schedule_delete_task(
                    self._remove_deleted_thread_from_panel(
                        panel_chat_id,
                        panel_message_id,
                        cursor=cursor,
                        search=(binding or {}).get("inbox_panel_search"),
                        telegram_user_id=(
                            int(binding["telegram_user_id"])
                            if binding is not None
                            and binding.get("telegram_user_id") is not None
                            else None
                        ),
                    ),
                    name=f"telegramail-delete-panel-{operation_id}",
                )
            # The durable worker owns mailbox retries. Wake it now, while the
            # Topic/panel changes above run independently of callback ack.
            if self._wake_workers is not None:
                self._wake_workers()
            # Let the detached tasks start. Fake clients used in tests complete
            # in this tick; real network calls remain pending while the handler
            # returns and Telegram ends the button spinner immediately.
            await asyncio.sleep(0)
            return {"text": "正在删除邮件"}
        raise ValueError("unknown delete action")

    async def _edit_text(self, chat_id: int, message_id: int, text: str, **extra: Any) -> Any:
        try:
            return await self.client.edit_message_text(chat_id, message_id, text, **extra)
        except TelegramApiError as exc:
            if _is_not_modified(exc):
                return True
            raise

    async def _edit_markup(self, chat_id: int, message_id: int, **extra: Any) -> Any:
        try:
            return await self.client.edit_message_reply_markup(chat_id, message_id, **extra)
        except TelegramApiError as exc:
            if _is_not_modified(exc):
                return True
            raise

    def _render_inbox(
        self, cursor: str | int | None = None, *, search: str | None = None,
        page: int | None = None,
    ) -> tuple[str, dict[str, Any]]:
        if page is not None:
            cursor = self._cursor_for_legacy_page(int(page), search=search)
        if isinstance(cursor, int):
            cursor = self._cursor_for_legacy_page(cursor, search=search)
        cursor = None if cursor is None or cursor in {"0", "latest", "none", ""} else str(cursor)
        result = self.repository.list_telegram_inbox_threads(
            limit=PAGE_SIZE, cursor=cursor, search=search,
        )
        items = result.get("items") or []
        # A delete can make the current historical page empty.  Fall back one
        # cursor boundary so the panel never strands the user on a blank page.
        if not items and cursor:
            previous = self.repository.get_previous_telegram_inbox_cursor(cursor, search=search)
            if previous != cursor:
                cursor = previous
                result = self.repository.list_telegram_inbox_threads(
                    limit=PAGE_SIZE, cursor=cursor, search=search,
                )
                items = result.get("items") or []
        self._last_render_cursor = cursor
        self._last_render_search = str(search).strip() if search and str(search).strip() else None

        title = "<b>📥 全部邮件</b>"
        if search:
            title += f" · 搜索：{html.escape(_truncate(_one_line(search, ''), 60))}"
        lines = [title, ""]
        rows: list[list[dict[str, Any]]] = []
        if not items:
            lines.append("暂无邮件")
        for index, item in enumerate(items, start=1):
            unread = "● " if str(item.get("llm_priority") or "").lower() == "high" else ""
            subject = _truncate(_one_line(item.get("subject"), "(无主题)"), 80)
            sender = _truncate(_one_line(item.get("sender"), "未知发件人"), 80)
            summary = item.get("llm_summary")
            snippet = _one_line(
                telegram_limited_html_to_text(summary) if summary else item.get("body_text"),
                "无正文",
            )
            lines.extend([
                f"<b>{index}. {html.escape(unread + subject)}</b>",
                html.escape(sender),
                html.escape(_truncate(snippet, 90)),
                "",
            ])
            rows.append([{
                "text": f"查看 {index}",
                "callback_data": f"{CALLBACK_PREFIX}:inbox:view:{int(item['thread_id'])}:{_cursor_token(cursor)}",
            }, {
                "text": "🗑",
                "callback_data": f"{CALLBACK_PREFIX}:delete:ask:{int(item['thread_id'])}:panel:{_cursor_token(cursor)}",
                "style": "danger",
            }])
        navigation: list[dict[str, Any]] = []
        if cursor:
            navigation.append({"text": "最新", "callback_data": f"{CALLBACK_PREFIX}:inbox:latest"})
        navigation.append({"text": "刷新", "callback_data": f"{CALLBACK_PREFIX}:inbox:refresh:{_cursor_token(cursor)}"})
        next_cursor = result.get("next_cursor")
        if next_cursor:
            navigation.append({"text": "更早 ›", "callback_data": f"{CALLBACK_PREFIX}:inbox:older:{next_cursor}"})
        rows.append(navigation)
        if search:
            rows.append([{"text": "清除搜索", "callback_data": f"{CALLBACK_PREFIX}:inbox:clear"}])
        if self.mini_app_url:
            rows.append([
                {"text": "📋 在 Mini App 中批量管理", "web_app": {"url": _mini_app_link(self.mini_app_url, action="manage")}},
            ])
            rows.append([{"text": "✍️ 撰写邮件", "web_app": {"url": _mini_app_link(self.mini_app_url, action="compose")}}])
        return "\n".join(lines).rstrip(), {"inline_keyboard": rows}

    def _render_detail(
        self, thread_id: int, cursor: str | int | None, *, topic_url: str | None = None
    ) -> tuple[str, dict[str, Any]]:
        if isinstance(cursor, int):
            cursor = self._cursor_for_legacy_page(cursor)
        self._last_render_cursor = cursor
        item = self.repository.get_telegram_inbox_thread(thread_id)
        if item is None:
            return self._render_inbox(cursor=cursor)
        summary = _one_line(item.get("llm_summary"), "")
        content = _truncate(summary or str(item.get("body_text") or "(无正文)").strip(), 3000)
        rendered_content = sanitize_telegram_limited_html(content) if summary else html.escape(content)
        delivery_status = str(item.get("telegram_delivery_status") or "")
        topic_id = item.get("telegram_message_thread_id")
        if delivery_status == "delivered" and topic_id is not None:
            delivery_note = (
                "数据库记录显示该邮件已投递到 Telegram Topic；"
                "若 Topic 已被清理，请点击“重建 Topic”恢复。"
            )
        elif delivery_status == "ambiguous":
            delivery_note = "Telegram Topic 投递状态待确认；当前显示数据库中的邮件内容。"
        elif delivery_status == "failed":
            delivery_note = "Telegram Topic 投递失败，系统将重试；当前显示数据库中的邮件内容。"
        else:
            delivery_note = "该邮件尚未投递到 Telegram Topic；当前显示数据库中的邮件内容。"
        lines = [
            f"<b>{html.escape(_truncate(_one_line(item.get('subject'), '(无主题)'), 200))}</b>",
            f"发件人：{html.escape(_truncate(_one_line(item.get('sender'), '未知发件人'), 200))}",
            f"时间：{html.escape(_truncate(_one_line(item.get('email_date'), '—'), 100))}",
            "",
            rendered_content or "(无正文)",
            "",
            delivery_note,
        ]
        return "\n".join(lines), self._detail_keyboard(
            thread_id,
            cursor,
            topic_id=int(topic_id) if topic_id is not None else None,
            topic_url=topic_url,
        )

    def _render_delete_confirmation(
        self, thread: Mapping[str, Any], cursor: str | int | None
    ) -> tuple[str, dict[str, Any]]:
        thread_id = int(thread["thread_id"])
        subject = html.escape(
            _truncate(_one_line(thread.get("subject"), "(无主题)"), 200)
        )
        sender = html.escape(
            _truncate(_one_line(thread.get("sender"), "未知发件人"), 200)
        )
        text = "\n".join([
            "<b>确认删除这封邮件？</b>",
            "",
            f"<b>主题：</b>{subject}",
            f"<b>发件人：</b>{sender}",
            "",
            "确认后会同时删除邮箱服务器中的邮件和 Telegram Topic。",
        ])
        return text, _confirmation_keyboard(thread_id, "panel", cursor)

    def _detail_keyboard(
        self,
        thread_id: int,
        cursor: str | int | None,
        *,
        topic_id: int | None,
        topic_url: str | None = None,
    ) -> dict[str, Any]:
        token = _cursor_token(cursor)
        rows = [[
            {"text": "‹ 返回全部", "callback_data": f"{CALLBACK_PREFIX}:inbox:list:{token}"},
            {
                "text": "🗑 删除邮件",
                "callback_data": f"{CALLBACK_PREFIX}:delete:ask:{int(thread_id)}:panel:{token}",
                "style": "danger",
            },
        ]]
        if topic_id is not None:
            if topic_url:
                rows.append([{"text": "进入 Topic", "url": topic_url}])
            rebuild_data = (
                f"{CALLBACK_PREFIX}:inbox:rebuild:{_compact_callback_id(thread_id, token)}:{token}:{_compact_callback_id(topic_id, token)}"
                if token == "0" else
                f"{CALLBACK_PREFIX}:r:{_compact_callback_id(thread_id, token)}:{token}:{_compact_callback_id(topic_id, token)}"
            )
            rows.append([{
                "text": "重建 Topic",
                "callback_data": rebuild_data,
            }])
        if self.mini_app_url:
            rows.append([{
                "text": "回复 / 转发",
                "web_app": {"url": _mini_app_link(self.mini_app_url, action="reply", thread_id=int(thread_id))},
            }])
        return {"inline_keyboard": rows}

    def _cursor_for_legacy_page(self, page: int, *, search: str | None = None) -> str | None:
        """Translate old numeric callbacks to keyset boundaries during rollout."""

        if int(page) < 0 or int(page) > 100:
            raise ValueError("legacy page is out of range; use the newer cursor buttons")
        boundary: str | None = None
        for _ in range(max(0, int(page))):
            result = self.repository.list_telegram_inbox_threads(
                limit=PAGE_SIZE, cursor=boundary, search=search,
            )
            next_cursor = result.get("next_cursor")
            if not next_cursor:
                return boundary
            boundary = str(next_cursor)
        return boundary

    @staticmethod
    def _normalize_callback_cursor(value: str, binding: Mapping[str, Any] | None = None) -> str | None:
        token = str(value or "0")
        if token in {"0", "latest", "none"}:
            return None
        return token

    def _binding_for_message(self, message: Mapping[str, Any]) -> Mapping[str, Any] | None:
        sender = message.get("from")
        chat = message.get("chat")
        user_id = sender.get("id") if isinstance(sender, Mapping) else None
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        return self._authorized_binding(user_id, chat_id)

    def _binding_for_callback(self, callback: Mapping[str, Any]) -> Mapping[str, Any] | None:
        sender = callback.get("from")
        message = callback.get("message")
        chat = message.get("chat") if isinstance(message, Mapping) else None
        user_id = sender.get("id") if isinstance(sender, Mapping) else None
        chat_id = chat.get("id") if isinstance(chat, Mapping) else None
        return self._authorized_binding(user_id, chat_id)

    def _authorized_binding(self, user_id: Any, chat_id: Any) -> Mapping[str, Any] | None:
        if not isinstance(user_id, int) or not isinstance(chat_id, int):
            return None
        binding = self.repository.get_admin_binding()
        if not binding:
            return None
        if int(binding["telegram_user_id"]) != user_id:
            return None
        if int(binding.get("private_chat_id") or 0) != chat_id:
            return None
        return binding


def _one_line(value: Any, fallback: str) -> str:
    text = " ".join(str(value or "").replace("\x00", "").split())
    return text or fallback


def _target_important_links(target: Mapping[str, Any]) -> list[dict[str, str]]:
    """Return persisted LLM links for a historical delivery part.

    Historical backfill targets may expose either a decoded ``important_links``
    list or the raw JSON column while repositories roll forward.  The old
    ``body_text``/``body_html`` extraction path is intentionally not consulted.
    """

    for key in ("important_links", "llm_important_links_json", "important_links_json"):
        if key not in target or target.get(key) is None:
            continue
        cleaned = sanitize_important_links(target.get(key))
        if cleaned:
            return cleaned
    return []


def _mini_app_link(base_url: str, **params: Any) -> str:
    """Add a small route payload without discarding configured query params."""

    parsed = urlsplit(str(base_url))
    query = list(parse_qsl(parsed.query, keep_blank_values=True))
    for key, value in params.items():
        if value is not None:
            query.append((str(key), str(value)))
    return urlunsplit((parsed.scheme, parsed.netloc, parsed.path, urlencode(query), parsed.fragment))


def _cursor_token(cursor: str | int | None) -> str:
    """Return a compact callback-safe token; latest is represented as ``0``."""

    if cursor is None or cursor == "" or cursor == 0:
        return "0"
    return str(cursor)


def _base36(value: int) -> str:
    value = int(value)
    if value < 0:
        raise ValueError("callback id must be positive")
    alphabet = "0123456789abcdefghijklmnopqrstuvwxyz"
    if value == 0:
        return "0"
    digits: list[str] = []
    while value:
        value, remainder = divmod(value, 36)
        digits.append(alphabet[remainder])
    return "".join(reversed(digits))


def _parse_compact_int(value: str) -> int:
    token = str(value or "").strip().lower()
    if not token or any(char not in "0123456789abcdefghijklmnopqrstuvwxyz" for char in token):
        raise ValueError("invalid callback id")
    return int(token, 36)


def _parse_callback_thread_id(value: str) -> int:
    token = str(value or "").strip()
    if token.startswith("b"):
        return _parse_compact_int(token[1:])
    return int(token)


def _compact_callback_id(value: int, cursor_token: str) -> str:
    # Keep the old decimal callback shape for the latest page so existing
    # clients can still complete a pending Topic repair during rollout.
    return str(int(value)) if cursor_token == "0" else _base36(int(value))


def _truncate(value: str, limit: int) -> str:
    return value if len(value) <= limit else value[: max(0, limit - 1)].rstrip() + "…"


def _topic_name(thread: Mapping[str, Any]) -> str:
    sender = _one_line(thread.get("sender"), "未知发件人")
    subject = _one_line(thread.get("subject"), "(无主题)")
    return _truncate(f"{sender} · {subject}", 128)


def _is_not_modified(error: TelegramApiError) -> bool:
    return "message is not modified" in error.description.casefold()
