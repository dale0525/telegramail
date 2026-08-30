"""Production ASGI entry point for TelegramMail v2.

The application runs one Uvicorn process. Its FastAPI lifespan owns the HTTP
Bot API webhook adapter and mail workers; phone-number login and Telegram user
sessions are not part of this startup path.
"""

from __future__ import annotations

import asyncio
import os
from contextlib import asynccontextmanager
from pathlib import Path

import uvicorn
from fastapi.responses import JSONResponse
from fastapi.routing import APIRoute

from app.api import create_app as create_api_app
from app.core.config import Settings
from app.db import V2Repository
from app.integrations.telegram_http import TelegramBotApiClient
from app.services.telegram_mail_ui import TelegramMailBotUi
from app.workers.runtime import create_v2_worker_runtime


def create_app():
    """Build the single-process v2 HTTP/Bot/worker application."""
    settings = Settings.from_env()
    if not settings.telegram_bot_token:
        raise ValueError("TELEGRAM_BOT_TOKEN is required")
    if not settings.webhook_secret:
        raise ValueError("TELEGRAM_WEBHOOK_SECRET is required")
    if not settings.web_base_url or not settings.web_base_url.startswith("https://"):
        raise ValueError("WEB_BASE_URL is required and must use HTTPS")

    data_dir = Path(os.getenv("TELEGRAMAIL_DATA_DIR", "data"))
    database = V2Repository(data_dir / "telegramail-v2.db")
    telegram_ui = TelegramMailBotUi(database, mini_app_url=settings.web_base_url)
    telegram = TelegramBotApiClient(
        settings.telegram_bot_token,
        mini_app_url=settings.web_base_url,
        proxy=settings.telegram_bot_api_proxy,
        message_handler=telegram_ui.handle_message,
        callback_handler=telegram_ui.handle_callback,
    )
    telegram_ui.bind_client(telegram)
    app = create_api_app(settings, db=database, telegram=telegram)
    _install_worker_readiness_route(app)
    upstream_lifespan = app.router.lifespan_context

    webhook_url = _webhook_url(settings)
    if not webhook_url.startswith("https://"):
        raise ValueError("WEB_BASE_URL must use HTTPS")

    @asynccontextmanager
    async def lifecycle(asgi_app):
        async with upstream_lifespan(asgi_app):
            # This is a deployment gate: private Topics must be enabled before
            # a worker can project mail into the Bot API.
            await telegram.ensure_topics_enabled()
            await telegram.set_webhook(
                webhook_url,
                secret_token=settings.webhook_secret,
                allowed_updates=["message", "callback_query", "my_chat_member"],
            )
            # Topics are the inbox now. Remove the previously persisted aggregate
            # panel once so new mail no longer duplicates itself in All Messages.
            await telegram_ui.retire_inbox_panel()
            # This worker owns the durable mail queues in the same process as the
            # webhook server, so SQLite never has competing application writers.
            worker_runtime = create_v2_worker_runtime(
                database,
                telegram,
            )
            asgi_app.state.worker_runtime = worker_runtime
            telegram_ui.bind_worker_wake(worker_runtime.wake)
            await worker_runtime.start()
            topic_control_backfill = asyncio.create_task(
                _run_topic_control_backfill(telegram_ui),
                name="telegramail-topic-delete-control-backfill",
            )
            try:
                yield
            finally:
                topic_control_backfill.cancel()
                try:
                    await topic_control_backfill
                except asyncio.CancelledError:
                    pass
                await worker_runtime.stop()
                await telegram.aclose()

    app.router.lifespan_context = lifecycle
    return app


async def _run_topic_control_backfill(telegram_ui: TelegramMailBotUi) -> None:
    """Gradually upgrade historical Topic messages while the app is online."""

    while True:
        try:
            attempted = await telegram_ui.backfill_topic_delete_controls()
        except asyncio.CancelledError:
            raise
        except Exception:
            attempted = 0
        await asyncio.sleep(10 if attempted else 60)


def _install_worker_readiness_route(app) -> None:
    """Replace factory readiness with the process-owned worker health contract."""
    async def ready() -> JSONResponse:
        runtime = getattr(app.state, "worker_runtime", None)
        if runtime is None:
            return JSONResponse({"status": "not_ready", "reason": "worker_not_started"}, status_code=503)
        is_ready, payload = runtime.readiness()
        return JSONResponse(payload, status_code=200 if is_ready else 503)

    route = APIRoute("/health/ready", ready, methods=["GET"], include_in_schema=False)
    for index, existing in enumerate(app.router.routes):
        if getattr(existing, "path", None) == "/health/ready":
            app.router.routes[index] = route
            return
    # Keep readiness before the SPA catch-all if an alternate API factory omitted it.
    app.router.routes.insert(0, route)


def _webhook_url(settings: Settings) -> str:
    """Derive the canonical webhook route from the public Mini App origin."""
    if not settings.web_base_url:
        raise ValueError("WEB_BASE_URL is required")
    return f"{settings.web_base_url.rstrip('/')}/api/v1/telegram/webhook"


def main() -> None:
    """Run the single-process HTTP server used by local development and containers."""
    port = int(os.getenv("PORT", "8080"))
    uvicorn.run(
        "app.main:create_app",
        factory=True,
        host="0.0.0.0",
        port=port,
        proxy_headers=True,
        forwarded_allow_ips="*",
    )


if __name__ == "__main__":
    main()
