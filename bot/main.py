import asyncio
import logging

from aiogram import Bot, Dispatcher
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ParseMode

from bot.config import Config, load_config
from bot.db import Database
from bot.handlers import routers
from bot.middlewares import DbSessionMiddleware
from bot.services import FormulaRenderer, GeminiClient


def configure_logging() -> None:
    logging.basicConfig(level=logging.INFO)


def create_database(config: Config) -> Database:
    return Database(config.database_url)


def create_llm_client(config: Config) -> GeminiClient:
    return GeminiClient(
        config.gemini_api_key,
        config.gemini_endpoint,
        config.gemini_model,
        config.gemini_ssl_verify,
        config.gemini_status_endpoint_template,
    )


def create_bot(token: str) -> Bot:
    return Bot(
        token=token,
        default=DefaultBotProperties(parse_mode=ParseMode.HTML),
    )


def create_dispatcher(db: Database, llm: GeminiClient, renderer: FormulaRenderer) -> Dispatcher:
    dispatcher = Dispatcher()
    dispatcher.update.middleware(DbSessionMiddleware(db, llm, renderer))
    for router in routers:
        dispatcher.include_router(router)
    return dispatcher


async def run() -> None:
    configure_logging()

    config = load_config()
    db = create_database(config)
    llm = create_llm_client(config)
    renderer = FormulaRenderer()
    bot = create_bot(config.bot_token)
    dispatcher = create_dispatcher(db, llm, renderer)

    await db.connect()
    try:
        await dispatcher.start_polling(bot)
    finally:
        await db.close()
        await bot.session.close()


def main() -> None:
    asyncio.run(run())


if __name__ == "__main__":
    main()