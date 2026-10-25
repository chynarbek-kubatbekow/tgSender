import asyncio
import contextlib
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.session.aiohttp import AiohttpSession
from aiogram.utils.token import TokenValidationError

from aiohttp import web
from bot.config import load_config
from bot.auth_web import create_app
from bot.store import Database
from bot.security import Vault
from bot.runtime import Runtime


async def main() -> None:
    settings = load_config()
    database = Database(settings.data_dir / 'sender.sqlite3', Vault(settings.encryption_key))
    runtime = Runtime(settings, database)
    runner = web.AppRunner(create_app(runtime), access_log=None)
    reaper = None
    try:
        async with Bot(token=settings.token, session=AiohttpSession(timeout=30)) as bot:
            dispatcher = Dispatcher()
            dispatcher.include_router(runtime.router)
            await runner.setup()
            await web.TCPSite(runner, '0.0.0.0', settings.port).start()
            reaper = asyncio.create_task(runtime.reap())
            await dispatcher.start_polling(
                bot, polling_timeout=10, tasks_concurrency_limit=32, close_bot_session=False,
                allowed_updates=dispatcher.resolve_used_update_types(),
            )
    finally:
        await runner.cleanup()
        if reaper:
            reaper.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reaper
        await runtime.close()
        database.close()


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    try:
        asyncio.run(main())
    except (ValueError, KeyError, TokenValidationError):
        raise SystemExit("Проверьте обязательные настройки в .env или переменных окружения.") from None
    except KeyboardInterrupt:
        pass
    except Exception:
        # Exception text may contain API URLs or user data.
        logging.getLogger(__name__).error("Бот остановлен из-за ошибки запуска или соединения.")
        raise SystemExit(1) from None
