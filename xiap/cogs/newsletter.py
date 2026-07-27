"""Channel newsletter subscriptions backed by Firestore.

This cog only loads if Firebase credentials are available (GOOGLE_APPLICATION_CREDENTIALS
or ambient GCP credentials) — the rest of the bot works fine without it.
"""

import logging

import discord
from discord import app_commands
from discord.ext import commands

log = logging.getLogger(__name__)


class NewsletterCog(commands.Cog):
    def __init__(self, db) -> None:
        self.db = db

    @app_commands.command(name="subscribe", description="Subscribe this channel to the newsletter")
    async def subscribe(self, interaction: discord.Interaction) -> None:
        from firebase_admin import firestore

        doc_ref = self.db.collection("subscriptions").document(str(interaction.channel.id))
        if doc_ref.get().exists:
            await interaction.response.send_message("This channel is already subscribed!")
        else:
            doc_ref.set({"subscribed": True, "timestamp": firestore.SERVER_TIMESTAMP})
            await interaction.response.send_message("This channel has been subscribed! 📬")

    @app_commands.command(name="unsubscribe", description="Unsubscribe this channel from the newsletter")
    async def unsubscribe(self, interaction: discord.Interaction) -> None:
        doc_ref = self.db.collection("subscriptions").document(str(interaction.channel.id))
        if not doc_ref.get().exists:
            await interaction.response.send_message("This channel is not subscribed!")
        else:
            doc_ref.delete()
            await interaction.response.send_message("This channel has been unsubscribed! 👋")


async def setup(bot: commands.Bot) -> None:
    try:
        import firebase_admin
        from firebase_admin import firestore

        if not firebase_admin._apps:
            firebase_admin.initialize_app()
        db = firestore.client()
    except Exception as e:
        log.warning("Firebase unavailable, newsletter commands disabled: %s", e)
        return
    await bot.add_cog(NewsletterCog(db))
