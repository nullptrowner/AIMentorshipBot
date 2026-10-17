import argparse
import asyncio
import signal

import uvicorn
from redis.asyncio import Redis

from premium_bot.application.admin import seed
from premium_bot.config import settings
from premium_bot.db import database
from premium_bot.observability import configure_logging


async def run(command: str) -> None:
    cfg = settings()
    configure_logging(cfg.log_level)
    if command == "seed":
        sessions, engine = database(cfg.database_url)
        async with sessions.begin() as s:
            await seed(s, cfg)
        await engine.dispose()
    elif command == "worker-health":
        client = Redis.from_url(cfg.redis_url)
        value = await client.get("worker:heartbeat")
        await client.aclose()
        if value is None:
            raise SystemExit(1)
    else:
        cfg.require_production()
        from premium_bot.runtime import Runtime

        rt = Runtime.create(cfg)
        if command == "webhook":
            try:
                await rt.bot.set_webhook(
                    cfg.public_url + "/webhooks/telegram",
                    secret_token=cfg.telegram_secret.get_secret_value(),
                    allowed_updates=["message", "callback_query", "pre_checkout_query"],
                    drop_pending_updates=False,
                )
                print("Telegram webhook configured; pending updates preserved.")
            finally:
                await rt.close()
        elif command == "worker":
            from premium_bot.workers.queue import worker

            stop = asyncio.Event()
            loop = asyncio.get_running_loop()
            for sig in (signal.SIGINT, signal.SIGTERM):
                loop.add_signal_handler(sig, stop.set)
            try:
                await worker(rt, stop)
            finally:
                await rt.close()
        elif command == "serve":
            from premium_bot.api import create_app

            app = create_app(rt)
            server = uvicorn.Server(
                uvicorn.Config(
                    app,
                    host="0.0.0.0",
                    port=8000,
                    access_log=False,
                    log_level=cfg.log_level.lower(),
                )
            )
            await server.serve()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["seed", "serve", "worker", "webhook", "worker-health"])
    asyncio.run(run(parser.parse_args().command))


if __name__ == "__main__":
    main()
