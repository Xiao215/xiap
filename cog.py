import discord
from discord import app_commands
from discord.ext import commands
from firebase_admin import firestore

class CommandCog(commands.Cog):
    def __init__(self, db: firestore.Client):
        self.db = db

    @app_commands.command(name="rps")
    @app_commands.choices(choices=[
        app_commands.Choice(name="Rock", value="rock"),
        app_commands.Choice(name="Paper", value="paper"),
        app_commands.Choice(name="Scissors", value="scissors"),
        ])
    async def rps(self, i: discord.Interaction, choices: app_commands.Choice[str]):
        if (choices.value == 'rock'):
            counter = 'paper'
        elif (choices.value == 'paper'):
            counter = 'scissors'
        else:
            counter = 'rock'

    @app_commands.command(name="subscribe", description="Subscribe this channel for the newsletter")
    async def subscribe(self, interaction: discord.Interaction):
        channel_id = str(interaction.channel.id)
        doc_ref = self.db.collection("subscriptions").document(channel_id)
        doc = doc_ref.get()
        if doc.exists:
            await interaction.response.send_message("This channel is already subscribed!")
        else:
            doc_ref.set({
                "subscribed": True,
                "timestamp": firestore.SERVER_TIMESTAMP  # records the time of subscription
            })
            await interaction.response.send_message("This channel has been subscribed!")

    @app_commands.command(name="unsubscribe", description="Unsubscribe this channel from the newsletter")
    async def unsubscribe(self, interaction: discord.Interaction):
        channel_id = str(interaction.channel.id)
        doc_ref = self.db.collection("subscriptions").document(channel_id)
        doc = doc_ref.get()
        if not doc.exists:
            await interaction.response.send_message("This channel is not subscribed!")
        else:
            doc_ref.delete()
            await interaction.response.send_message("This channel has been unsubscribed!")