"""hannah-telegram – entry point.

Starts two concurrent components:
  1. HannahClient (gRPC)             – text commands to/from Hannah
  2. HannahBot (python-telegram-bot) – Telegram long-polling

STT, NLU and TTS are handled centrally by Hannah Core via gRPC:
  - Text message     → SubmitText()
  - Voice message    → SubmitVoice() (STT + NLU + TTS in Core)
  - /auto command    → GetCarState()
  - Auto geparkt     → SubscribeEvents(["car.parked"]) push

Usage:
  python main.py [--config path/to/config.yaml]
"""
from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from hannah_telegram.bot import HannahBot
from hannah_telegram.config import load as load_config
from hannah_telegram.grpc_client import HannahClient

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger("hannah_telegram")


async def main(config_path: str) -> None:
    cfg = load_config(config_path)

    if not cfg.telegram_token or cfg.telegram_token == "YOUR_BOT_TOKEN_HERE":
        log.error("telegram_token is not set in %s – aborting", config_path)
        sys.exit(1)

    # --- gRPC client ---
    hannah = HannahClient(cfg.grpc.host, cfg.grpc.port)
    await hannah.connect()

    # --- Bot ---
    bot = HannahBot(
        token=cfg.telegram_token,
        hannah=hannah,
    )
    app = bot.build_app()

    # --- Event stream ---
    async def on_event(event) -> None:
        if event.event_type == "car.parked":
            log.info("car.parked event received – notifying Telegram users")
            await bot.send_car_parked_to_all(event.car_state)
        elif event.event_type == "system.notification":
            log.info("system.notification event received – notifying recipients")
            await bot.send_system_notification(event.system_notification.text)

    async def on_connected(first_connect: bool) -> None:
        text = "Hannah ist bereit ✅" if first_connect else "Hannah Core Verbindung wiederhergestellt ✅"
        log.info("gRPC event stream connected (first=%s)", first_connect)
        await bot.send_status_update(text)

    async def on_disconnected() -> None:
        log.warning("gRPC event stream disconnected")
        await bot.send_status_update("⚠️ Verbindung zu Hannah Core unterbrochen")

    event_task = asyncio.create_task(
        hannah.subscribe_events(
            ["car.parked", "system.notification"],
            on_event,
            on_connected=on_connected,
            on_disconnected=on_disconnected,
        ),
        name="event_stream",
    )

    log.info("hannah-telegram starting (gRPC=%s:%d)", cfg.grpc.host, cfg.grpc.port)

    try:
        await app.initialize()
        await app.start()
        await bot.init_commands()
        await app.updater.start_polling(drop_pending_updates=True)
        log.info("Bot is running. Press Ctrl+C to stop.")
        await asyncio.Event().wait()
    except (KeyboardInterrupt, asyncio.CancelledError):
        pass
    finally:
        log.info("Shutting down…")
        event_task.cancel()
        try:
            await event_task
        except asyncio.CancelledError:
            pass
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        await hannah.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Hannah Telegram Bot")
    parser.add_argument("--config", default="config.yaml", help="Path to config.yaml")
    args = parser.parse_args()
    asyncio.run(main(args.config))
