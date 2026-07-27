"""Entry point: run the bot (and its built-in health-check server)."""

import asyncio

from xiap.bot import main

if __name__ == "__main__":
    asyncio.run(main())
