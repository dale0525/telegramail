def is_chat_not_found_error(exc: BaseException) -> bool:
    code = getattr(exc, "code", None)
    message = getattr(exc, "message", None) or str(exc)
    return code == 400 and "chat not found" in str(message).lower()
