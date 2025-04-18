import asyncio
from typing import Any, Callable, Dict, List, Optional, Tuple
from enum import Enum, auto
from aiotdlib import Client
from aiotdlib.api import (
    UpdateNewMessage,
    Message,
    ReplyMarkupShowKeyboard,
    KeyboardButton,
    KeyboardButtonTypeText,
    UpdateNewCallbackQuery,
)
from app.i18n import _
from app.utils.logger import Logger
from app.bot.utils import answer_callback, send_and_delete_message

logger = Logger().get_logger(__name__)


class ConversationState(Enum):
    """Conversation States"""

    IDLE = auto()  # conversation idle
    ACTIVE = auto()  # conversation ongoing
    FINISHED = auto()  # conversation finished
    CANCELLED = auto()  # conversation cancelled


class Conversation:
    """
    manage bot command chains
    """

    _instances: Dict[Tuple[int, int], "Conversation"] = {}

    @classmethod
    def get_instance(cls, chat_id: int, user_id: int) -> Optional["Conversation"]:
        """get conversation instance for certain user"""
        key = (chat_id, user_id)
        return cls._instances.get(key)

    @classmethod
    def create_conversation(
        cls,
        client: Client,
        chat_id: int,
        user_id: int,
        steps: List[Dict[str, Any]],
        context: Dict[str, Any] = None,
    ) -> "Conversation":
        """create a new conversation instance"""
        key = (chat_id, user_id)

        # if there's already a conversation, cancel it
        if key in cls._instances:
            cls._instances[key].cancel()

        instance = cls(client, chat_id, user_id, steps, context or {})
        cls._instances[key] = instance
        return instance

    @classmethod
    def remove_conversation(cls, chat_id: int, user_id: int) -> None:
        """remove certain user's conversation instance"""
        key = (chat_id, user_id)
        if key in cls._instances:
            del cls._instances[key]

    def __init__(
        self,
        client: Client,
        chat_id: int,
        user_id: int,
        steps: List[Dict[str, Any]],
        context: Dict[str, Any] = None,
        finish_message: str = None,
        finish_message_type: str = "info",  # "info", "success", "error"
        finish_message_delete_after: int = None,  # seconds, None means not auto-delete
    ):
        """
        init a conversation

        Args:
            client: bot client
            chat_id: chat id
            user_id: user id
            steps: A list of conversation steps. Each step is a dict containing following keys:
                  - text: hint text
                  - key: key to store user's answer
                  optional keys:
                  - validate: function to validate if user's answer is valid. Retures (bool, str) to indicate whether the answer is valid and error message
                  - process: function to handle the answer and return processed result
                  - reply_markup: optional reply markup to send with the hint text
            context: initial context
            finish_message: message to send when conversation finishes (optional)
            finish_message_type: "info", "success", "error" (for future extensibility, optional)
            finish_message_delete_after: seconds after which finish message will be deleted (optional)
        """
        self.client = client
        self.chat_id = chat_id
        self.user_id = user_id
        self.steps = steps
        self.context = context or {}
        if "client" not in self.context:
            self.context["client"] = self.client
        if "chat_id" not in self.context:
            self.context["chat_id"] = self.chat_id
        if "user_id" not in self.context:
            self.context["user_id"] = self.user_id
        self.current_step = 0
        self.state = ConversationState.IDLE
        self.messages: List[int] = (
            []
        )  # store message ids of the conversation, so they can be deleted when the conversation is finished
        self._on_finish_handlers: List[Callable] = []
        self._on_cancel_handlers: List[Callable] = []
        self.finish_message = finish_message
        self.finish_message_type = finish_message_type
        self.finish_message_delete_after = finish_message_delete_after

    async def start(self) -> None:
        """start the conversation"""
        if self.state != ConversationState.IDLE:
            return

        self.state = ConversationState.ACTIVE
        await self._send_current_step()

    async def _send_current_step(self) -> None:
        """send hint text of current step, handling skips, actions, and dynamic text"""
        if self.current_step >= len(self.steps):
            await self._finish()
            return

        step = self.steps[self.current_step]

        # --- Skip Logic ---
        if "skip" in step and callable(step["skip"]):
            if step["skip"](self.context):
                logger.debug(
                    f"Skipping step {self.current_step} (key: {step.get('key', 'N/A')}) based on context."
                )
                self.current_step += 1
                await self._send_current_step()
                return
        # --- End Skip Logic ---

        # --- Action Step Logic (e.g., Verification) ---
        if "action" in step and callable(step["action"]):
            action_func = step["action"]
            pre_action_message_key = step.get("pre_action_message_key")
            delete_pre_action = step.get("delete_pre_action_message", True)
            success_message_key = step.get("success_message_key")
            logger.debug(
                f"Executing action step {self.current_step} (key: {step.get('key', 'action')})"
            )

            pre_action_message_id = None
            if pre_action_message_key:
                try:
                    pre_action_message = await self.client.send_text(
                        chat_id=self.chat_id,
                        text=_(pre_action_message_key),
                        disable_notification=True,
                    )
                    pre_action_message_id = pre_action_message.id
                    self.messages.append(pre_action_message_id)
                except Exception as e:
                    logger.error(
                        f"Failed to send pre-action message '{pre_action_message_key}': {e}"
                    )
                    # Decide if this error should terminate? For now, continue to action.

            try:
                # Execute the action
                result = action_func(self.context)
                if asyncio.iscoroutine(result):
                    success, message = await result
                else:
                    success, message = (
                        result  # Assume sync actions return tuple directly
                    )

                # Handle result
                if success:
                    logger.info(f"Action step {self.current_step} successful.")
                    if success_message_key:
                        try:
                            # Maybe make success message auto-delete? Using send_and_delete for now.
                            await send_and_delete_message(
                                client=self.client,
                                chat_id=self.chat_id,
                                text=_(success_message_key),
                                delete_after_seconds=3,  # Auto-delete after 3s
                            )
                        except Exception as e:
                            logger.error(
                                f"Failed to send success message '{success_message_key}': {e}"
                            )
                    # Move to the next step
                    self.current_step += 1
                    await self._send_current_step()
                    return
                else:
                    # Action failed
                    logger.warning(
                        f"Action step {self.current_step} failed. Reason: {message}"
                    )
                    if step.get("terminate_on_fail", False):
                        fail_message_key = step.get("fail_message_key", "error_generic")
                        final_error_message = f"{_(fail_message_key)}\n{message}"
                        logger.warning(
                            f"Terminating conversation due to failed action step {self.current_step}."
                        )
                        await self.cancel(
                            send_message=True, custom_message=final_error_message
                        )
                        return  # Stop processing further steps
                    else:
                        # Action failed but we continue (maybe store error in context?)
                        # self.context[f"{step.get('key', 'action')}_error"] = message
                        logger.warning(
                            f"Action step {self.current_step} failed but terminate_on_fail is False. Continuing."
                        )
                        # Move to the next step even if action failed but didn't terminate
                        self.current_step += 1
                        await self._send_current_step()
                        return

            except Exception as e:
                logger.error(
                    f"Error executing action for step {self.current_step}: {e}",
                    exc_info=True,
                )

                # Terminate on unexpected exception in action?
                if step.get(
                    "terminate_on_fail", True
                ):  # Default to terminate on unexpected exception
                    logger.warning(
                        f"Terminating conversation due to unexpected error in action step {self.current_step}."
                    )
                    await self.cancel(
                        send_message=True, custom_message=_("error_generic")
                    )
                    return
                else:
                    # Log and continue if possible
                    logger.warning(
                        f"Unexpected error in action step {self.current_step} but terminate_on_fail is False. Continuing."
                    )
                    self.current_step += 1
                    await self._send_current_step()
                    return
        # --- End Action Step Logic ---

        # --- Resolve Step Text (Only for non-action steps) ---
        # If it's an action step, we usually don't send text, handled above.
        # If the step doesn't have 'action', proceed to send text.
        if not ("action" in step and callable(step["action"])):
            step_text_value = step["text"]
            if callable(step_text_value):
                try:
                    # Call the function, passing the current context
                    step_text_value = step_text_value(self.context)
                except Exception as e:
                    logger.error(
                        f"Error evaluating dynamic text for step {self.current_step}: {e}"
                    )
                    # Fallback or cancel? For now, fallback to a generic error message
                    step_text_value = _(
                        "error_generating_step_text"
                    )  # Need this i18n key
                # Ensure it's a string after potential evaluation
                if not isinstance(step_text_value, str):
                    logger.error(
                        f"Step text for step {self.current_step} resolved to non-string: {type(step_text_value)}"
                    )
                    step_text_value = _("error_generating_step_text")

        reply_markup = step.get("reply_markup")

        # Default cancel keyboard if no markup provided
        if reply_markup is None:
            reply_markup = ReplyMarkupShowKeyboard(
                rows=[
                    [KeyboardButton(text=_("cancel"), type=KeyboardButtonTypeText())]
                ],
                one_time=True,
                resize_keyboard=True,
            )

        # Send the resolved text
        message = await self.client.send_text(
            chat_id=self.chat_id,
            text=step_text_value,  # Use the resolved string value
            reply_markup=reply_markup,
            disable_notification=True,
        )
        self.messages.append(message.id)

    async def handle_update(self, update: UpdateNewMessage) -> bool:
        """
        Handle incoming text message updates for the conversation.

        Returns:
            bool: True if the message was handled by this conversation, False otherwise.
        """
        if self.state != ConversationState.ACTIVE:
            return False

        message = update.message

        # Ignore messages from other users or non-text messages for standard flow
        if message.sender_id.user_id != self.user_id:
            return False

        # Check if the received text matches the localized "cancel" text
        if (
            hasattr(message.content, "text")
            and hasattr(message.content.text, "text")
            and message.content.text.text == _("cancel")
        ):
            self.messages.append(message.id)  # Record cancel message ID
            await self.cancel(send_message=True)
            return True

        # Process regular text input
        if hasattr(message.content, "text") and hasattr(message.content.text, "text"):
            self.messages.append(message.id)  # Record user message ID
            await self._process_input(message.content.text.text, is_callback=False)
            return True
        else:
            # If it's not text and not /cancel, ignore for conversation flow
            return False

    async def handle_callback_update(self, update: UpdateNewCallbackQuery) -> bool:
        """
        Handle incoming callback query updates for the conversation.

        Returns:
            bool: True if the callback was handled by this conversation, False otherwise.
        """
        if self.state != ConversationState.ACTIVE:
            # Try to answer the callback even if conversation is not active to remove loading state
            try:
                await self.client.api.answer_callback_query(
                    update.id,
                    text=_("conversation_expired_or_not_found"),
                    url="",
                    cache_time=1,
                )
            except Exception as e:
                logger.warning(
                    f"Failed to answer callback query for inactive conversation: {e}"
                )
            return False

        # Process the callback data as input
        # Correctly access callback data via update.payload.data
        await self._process_input(
            update.payload.data.decode(), is_callback=True, callback_id=update.id
        )
        return True

    # Renamed from _process_response and generalized
    async def _process_input(
        self, input_data: str, is_callback: bool, callback_id: Optional[int] = None
    ) -> None:
        """Process either text input or callback data, handling optional skips."""
        if self.current_step >= len(self.steps):
            # Handle unexpected input when conversation should be finished
            logger.warning(
                f"Received input '{input_data}' when conversation is already finished."
            )
            if is_callback and callback_id:
                try:
                    await self.client.api.answer_callback_query(
                        callback_id,
                        text=_("conversation_already_finished"),
                        url="",
                        cache_time=1,
                    )
                except Exception as e:
                    logger.warning(
                        f"Failed to answer callback for finished conversation: {e}"
                    )
            return

        step = self.steps[self.current_step]
        answer_data = input_data

        # --- Handle Skip for Optional Text Steps --- (Before Validation)
        # Only applies to non-callback inputs and if the step is optional
        if not is_callback and step.get("optional", False) and answer_data == "/skip":
            logger.debug(
                f"User skipped optional step {self.current_step} (key: {step.get('key', 'N/A')})"
            )
            # Don't store '/skip' in context
            self.current_step += 1
            # Proceed to next step or finish
            if self.current_step < len(self.steps):
                await self._send_current_step()
            else:
                await self._finish()
            return  # IMPORTANT: Return early to bypass validation/processing
        # --- End Skip Handling ---

        # --- Validation --- (Skip validation for callbacks)
        if not is_callback:
            if "validate" in step and callable(step["validate"]):
                try:
                    is_valid, error_msg = step["validate"](answer_data)
                    if not is_valid:
                        error_message = await self.client.send_text(
                            chat_id=self.chat_id,
                            text=error_msg,
                            disable_notification=True,
                        )
                        self.messages.append(error_message.id)
                        # Don't advance step, wait for valid input
                        return
                except Exception as e:
                    logger.error(
                        f"Error during validation for step {self.current_step}: {e}"
                    )
                    error_text = _("validation_error_occurred")  # Need i18n key
                    error_message = await self.client.send_text(
                        self.chat_id, error_text
                    )
                    self.messages.append(error_message.id)
                    return  # Stop processing this step on validation error

        # --- Processing --- (Applies to both text and callback data)
        processed_answer = answer_data
        if "process" in step and callable(step["process"]):
            try:
                processed_answer = step["process"].__call__(
                    answer_data
                )  # Ensure lambda/func is called
            except Exception as e:
                logger.error(
                    f"Error processing input for step {self.current_step} key '{step.get('key', 'N/A')}': {e}"
                )
                error_text = _("processing_error")
                if is_callback and callback_id:
                    try:
                        await self.client.api.answer_callback_query(
                            callback_id,
                            text=error_text,
                            url="",
                            cache_time=1,
                        )
                    except Exception as e_ans:
                        logger.warning(
                            f"Failed to answer callback query during processing error: {e_ans}"
                        )
                else:
                    error_message = await self.client.send_text(
                        self.chat_id, error_text
                    )
                    self.messages.append(error_message.id)
                return  # Stop processing this step on processing error

        # --- Answer Callback Query (if applicable) ---
        if is_callback and callback_id:
            try:
                await answer_callback(client=self.client, callback_query_id=callback_id)
            except Exception as e:
                logger.warning(f"Failed to answer callback query {callback_id}: {e}")
                # Continue processing even if answering fails

        # --- Store Answer --- (Only store if step wasn't skipped)
        # Skip logic already returned, so we always store here if we reach this point.
        self.context[step["key"]] = processed_answer

        # --- Execute Post Process Hook --- (Keep existing logic)
        if "post_process" in step and callable(step["post_process"]):
            try:
                # Pass context and the processed answer to the post_process function
                post_process_result = step["post_process"](
                    self.context, processed_answer
                )
                if asyncio.iscoroutine(post_process_result):
                    await post_process_result
            except Exception as e:
                logger.error(
                    f"Error executing post_process for step {self.current_step} key '{step.get('key', 'N/A')}': {e}"
                )
                # Decide how to handle post_process errors. Maybe cancel conversation?
                # For now, just log and continue, but this might lead to unexpected states.
        # --- End Post Process Hook ---

        # --- Advance Step ---
        self.current_step += 1

        # --- Send Next Step or Finish ---
        if self.current_step < len(self.steps):
            await self._send_current_step()
        else:
            await self._finish()

    async def _finish(self) -> None:
        """finish conversation"""
        self.state = ConversationState.FINISHED

        # call all finish handlers
        for handler in self._on_finish_handlers:
            try:
                result = handler(self.context)
                if asyncio.iscoroutine(result):
                    await result
            except Exception as e:
                logger.error(f"Error in finish handler: {e}")

        # 发送结束提示信息（如有配置）
        if self.finish_message:
            await send_and_delete_message(
                client=self.client,
                chat_id=self.chat_id,
                text=self.finish_message,
                delete_after_seconds=self.finish_message_delete_after,
            )
            # The new utility handles both sending and delayed deletion

        # remove self
        await self.clean_messages()
        Conversation.remove_conversation(self.chat_id, self.user_id)

    async def cancel(
        self, send_message: bool = False, custom_message: Optional[str] = None
    ) -> None:
        """cancel the conversation and call handlers"""
        if self.state != ConversationState.ACTIVE:
            return

        self.state = ConversationState.CANCELLED
        logger.info(
            f"Conversation cancelled for user {self.user_id} in chat {self.chat_id}"
        )

        # Call cancel handlers
        for handler in self._on_cancel_handlers:  # Iterate through cancel handlers
            try:
                # Cancel handlers might not need context, or pass self? Decide based on need.
                # Assuming they don't need arguments for now.
                await handler()
            except Exception as e:
                logger.error(
                    f"Error executing cancel handler for user {self.user_id}: {e}",
                    exc_info=True,
                )

        if send_message:
            # Use custom message if provided, otherwise default cancellation message
            message_to_send = custom_message or _("operation_cancelled")
            await send_and_delete_message(
                client=self.client,
                chat_id=self.chat_id,
                text=message_to_send,
                delete_after_seconds=3,
            )

        await self.clean_messages()
        self.__class__.remove_conversation(self.chat_id, self.user_id)

    async def clean_messages(self) -> None:
        """clean all messages of this conversation"""
        await self.client.api.delete_messages(
            chat_id=self.chat_id, message_ids=self.messages, revoke=True
        )

    def on_finish(self, handler: Callable[[Dict[str, Any]], Any]) -> None:
        """
        register callback handler when conversation is finished

        Args:
            handler: callback function which receive context as param
        """
        self._on_finish_handlers.append(handler)

    def on_cancel(self, handler: Callable[[], Any]) -> None:
        """register a handler to call when the conversation is cancelled"""
        if callable(handler):
            self._on_cancel_handlers.append(handler)

    def get_context(self) -> Dict[str, Any]:
        """get context of current conversation"""
        return self.context.copy()
