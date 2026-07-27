"""Tiny HTTP server so hosting platforms (Cloud Run, Koyeb, ...) see a healthy service.

Runs on the same asyncio event loop as the bot — no Flask, no threads.
"""

import logging

from aiohttp import web

log = logging.getLogger(__name__)


async def _index(_request: web.Request) -> web.Response:
    return web.Response(text="Discord Bot is Running")


async def start_health_server(port: int) -> web.AppRunner:
    app = web.Application()
    app.router.add_get("/", _index)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    log.info("Health server listening on :%d", port)
    return runner
