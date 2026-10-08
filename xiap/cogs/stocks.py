"""/stock — price chart for a ticker (default: SPY, the S&P 500 ETF).

Daily price data comes from Yahoo Finance's public chart API (no API key);
the chart is rendered with matplotlib and attached as an image.
"""

import asyncio
import io
import logging
from datetime import UTC, date, datetime

import aiohttp
import discord
import matplotlib.dates as mdates
from discord import app_commands
from discord.ext import commands
from matplotlib.figure import Figure

log = logging.getLogger(__name__)

YAHOO_URL = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}?range={range}&interval=1d"
USER_AGENT = "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7)"

GREEN = "#2ecc71"
RED = "#e74c3c"
BACKGROUND = "#2b2d31"  # Discord's dark theme


class StockCog(commands.Cog):
    def __init__(self) -> None:
        self.http: aiohttp.ClientSession | None = None

    async def cog_load(self) -> None:
        self.http = aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=30))

    async def cog_unload(self) -> None:
        if self.http:
            await self.http.close()

    @app_commands.command(name="stock", description="Price chart for a stock (default: SPY, the S&P 500)")
    @app_commands.describe(
        symbol="Ticker symbol, e.g. AAPL, TSLA, NVDA (default: SPY)",
        period="Time range for the chart (default: 6 months)",
    )
    @app_commands.choices(period=[
        app_commands.Choice(name="1 month", value="1mo"),
        app_commands.Choice(name="3 months", value="3mo"),
        app_commands.Choice(name="6 months", value="6mo"),
        app_commands.Choice(name="1 year", value="1y"),
        app_commands.Choice(name="5 years", value="5y"),
    ])
    async def stock(
        self,
        interaction: discord.Interaction,
        symbol: str = "SPY",
        period: app_commands.Choice[str] | None = None,
    ) -> None:
        await interaction.response.defer()
        yahoo_range = period.value if period else "6mo"
        period_name = period.name if period else "6 months"
        ticker = symbol.strip().upper()

        try:
            window = await self._fetch(ticker, yahoo_range)
        except Exception:
            log.exception("Stock fetch failed for %s", ticker)
            window = []
        if len(window) < 2:
            await interaction.followup.send(
                f"Couldn't find price data for `{ticker}` 😕 — is the ticker right?"
            )
            return

        first, last = window[0][1], window[-1][1]
        change = last - first
        pct = change / first * 100
        up = change >= 0

        buf = await asyncio.to_thread(self._render, ticker, window, up)

        embed = discord.Embed(
            title=f"{'📈' if up else '📉'} {ticker} — ${last:,.2f}",
            description=f"{'▲' if up else '▼'} {change:+,.2f} ({pct:+.2f}%) over {period_name}",
            color=discord.Color.green() if up else discord.Color.red(),
        )
        embed.set_image(url="attachment://chart.png")
        embed.set_footer(text=f"Daily close · data: Yahoo Finance · {window[-1][0].isoformat()}")
        await interaction.followup.send(embed=embed, file=discord.File(buf, "chart.png"))

    async def _fetch(self, ticker: str, yahoo_range: str) -> list[tuple[date, float]]:
        url = YAHOO_URL.format(symbol=ticker, range=yahoo_range)
        async with self.http.get(url, headers={"User-Agent": USER_AGENT}) as resp:
            data = await resp.json()

        result = (data.get("chart") or {}).get("result")
        if not result:
            return []
        timestamps = result[0].get("timestamp") or []
        closes = result[0]["indicators"]["quote"][0].get("close") or []
        return [
            (datetime.fromtimestamp(ts, tz=UTC).date(), c)
            for ts, c in zip(timestamps, closes, strict=False)
            if c is not None
        ]

    @staticmethod
    def _render(ticker: str, window: list[tuple[date, float]], up: bool) -> io.BytesIO:
        dates = [d for d, _ in window]
        closes = [c for _, c in window]
        color = GREEN if up else RED

        # A bare Figure, not pyplot: this runs in a worker thread and pyplot's global state isn't thread-safe.
        fig = Figure(figsize=(8, 4), dpi=150, facecolor=BACKGROUND)
        ax = fig.subplots()
        ax.set_facecolor(BACKGROUND)
        ax.plot(dates, closes, color=color, linewidth=1.8)
        ax.fill_between(dates, closes, min(closes), color=color, alpha=0.15)
        ax.grid(color="#4e5058", linewidth=0.5, alpha=0.5)
        ax.tick_params(colors="#b5bac1", labelsize=9)
        for spine in ax.spines.values():
            spine.set_visible(False)
        ax.xaxis.set_major_formatter(mdates.ConciseDateFormatter(ax.xaxis.get_major_locator()))
        ax.margins(x=0.01)
        fig.tight_layout(pad=1.2)

        buf = io.BytesIO()
        fig.savefig(buf, format="png", facecolor=fig.get_facecolor())
        buf.seek(0)
        return buf


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(StockCog())
