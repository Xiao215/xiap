import os
import random
import discord
from discord.ext import commands
from dotenv import load_dotenv
import cohere
from cog import CommandCog
import firebase_admin
from firebase_admin import firestore


# Load environment variables from the .env file
load_dotenv()
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
COHERE_API_KEY_1 = os.getenv("COHERE_API_KEY_1")
COHERE_API_KEY_2 = os.getenv("COHERE_API_KEY_2")
COHERE_API_KEY_3 = os.getenv("COHERE_API_KEY_3")


class XiapBot(commands.Bot):
    """
    A Discord bot that interacts with users using Cohere's chat model.
    It caches custom emojis per guild and uses a custom system prompt
    to guide its responses.
    """

    def __init__(self, command_prefix: str, intents: discord.Intents):
        super().__init__(command_prefix=command_prefix, intents=intents)

        # Cache for custom emojis by guild id.
        self.custom_emojis_cache = {}

        # Initialize Cohere clients with multiple API keys.
        self.cohere_clients = [
            cohere.ClientV2(api_key=COHERE_API_KEY_1),
            cohere.ClientV2(api_key=COHERE_API_KEY_2),
            cohere.ClientV2(api_key=COHERE_API_KEY_3),
        ]

        # Define the system message template.
        self.system_message = """
        You are a friendly, witty, and conversational member of the Sunday Social Discord group chat. Your name is {name}, and you actively engage with others like a close friend.

        ### Guidelines for Behavior:
        1. **Tone**:
        - Be warm, friendly, and natural.
        - Use wit and humor sparingly to add personality but ensure it fits the context.

        2. **Custom Emoji Usage (VERY IMPORTANT)**:
            - Always include emojis from the list below when they fit the context of your response:
                {emojis}
            - You **must** integrate at least one emoji in every response unless it feels completely inappropriate.
            - For example:
                - If someone mentions food, use food-related emojis.
                - If someone is joking, use emojis to emphasize the humor.
            - Note that the emoji is in the format of "<:name:emoji_id>" and you can include the entire thing with brackets in your response.

        3. **Engagement**:
        - Respond directly to mentions or replies.
        - Address users by their names to show familiarity.
        - Use emojis as a way to reflect emotions, humor, or excitement.

        4. **Response Style**:
        - Be concise unless explicitly asked to elaborate.
        - Always make your responses conversational and context-aware. Avoid sounding robotic or repetitive.
        - Do not ignore the custom emoji list. Even brief responses should try to include an emoji.

        5. **Chat Context**:
        - Use the provided chat history to craft relevant and engaging replies:
            - Example format: `username: message`
        - Reference previous conversations when it makes sense.

        ### Important Notes:
        - Avoid starting messages awkwardly (e.g., avoid "Hi" or "Hello" as standalone replies).
        - Always strive to make your responses relevant to the conversation and group dynamics.
        """

        app = firebase_admin.initialize_app()
        self.db = firestore.client()

    # async def setup_hook(self):
    #     # Load the subscription cog (which adds the slash commands).
    #     await self.add_cog(CommandCog(self.db))
    #     # Sync application (slash) commands with Discord.
    #     await self.tree.sync()

    async def on_ready(self):
        """
        Called when the bot has successfully connected to Discord.
        Caches custom emojis for each guild.
        """
        self.custom_emojis_cache.clear()
        for guild in self.guilds:
            print(f"Caching emojis for guild: {guild.name}")
            self.custom_emojis_cache[guild.id] = {emoji.name: emoji for emoji in guild.emojis}
        print(f"We have logged in as {self.user}")

    async def get_chat_history(self, guild_id: int, channel: discord.TextChannel) -> list[dict]:
        """
        Fetches the chat history from a given channel and formats it together
        with the system prompt.

        Args:
            guild_id (int): The guild's unique identifier.
            channel (discord.TextChannel): The channel from which to fetch messages.

        Returns:
            List[dict]: A list containing a single dictionary with role "system" and the formatted chat history.
        """
        # Build a string of custom emoji info for the system prompt.
        emojis_info = [
            f"name: {name} emoji_id: {emoji.id}"
            for name, emoji in self.custom_emojis_cache.get(guild_id, {}).items()
        ]
        emojis_str = "\n".join(emojis_info)
        background = self.system_message.format(name=self.user.name, emojis=emojis_str)
        background += "\n\nHere are the last 30 messages from this channel, spoken by members in this discord group:\n"

        # Retrieve messages (ignoring empty messages and non-user messages).
        messages = []
        async for msg in channel.history(limit=50):
            if not msg.content.strip():
                continue
            if msg.author.bot and msg.author != self.user:
                continue
            # Replace bot mentions with the bot's name for clarity.
            msg.content = msg.content.replace(f"<@{self.user.id}>", f"@{self.user.name}")
            messages.append(msg)

        # Order messages chronologically.
        messages.reverse()
        for msg in messages:
            background += f"{msg.author}: {msg.content}\n"
        return [{"role": "system", "content": background}]

    def query_cohere(self, prompt: str, chat_history: list[dict], model: str = "command-r-plus-08-2024") -> str:
        """
        Queries Cohere's chat model with the provided prompt and chat history.

        Args:
            prompt (str): The user's input or formatted message.
            chat_history (List[dict]): The conversation history including the system prompt.
            model (str, optional): The model name to use. Defaults to "command-r-plus-08-2024".

        Returns:
            str: The generated response from Cohere.
        """
        try:
            # Add the user's prompt to the chat history.
            chat_history.append({"role": "user", "content": prompt})

            # Randomly select one of the available Cohere clients.
            client = random.choice(self.cohere_clients)
            res = client.chat(
                model=model,
                messages=chat_history,
            )

            # Extract the response text.
            if res.message and res.message.content:
                response = res.message.content[0].text
            else:
                response = "I'm sorry, I couldn't generate a response."
            # Append the assistant's response to the chat history.
            chat_history.append({"role": "assistant", "content": response})
            return response
        except Exception as e:
            return f"Error: {e}"

    async def on_message(self, message: discord.Message):
        """
        Processes incoming messages. If the bot is mentioned or replied to,
        it builds the chat history, queries Cohere for a response, and sends it.
        """
        # Ignore the bot's own messages.
        if message.author == self.user:
            return

        user_input = message.content

        # Check if the message is a reply to the bot.
        if message.reference and message.reference.resolved:
            replied_message = message.reference.resolved
            if replied_message.author == self.user:
                # Format the input when replying to one of the bot's messages.
                referenced_content = replied_message.content
                user_input = message.content.replace(f"<@{self.user.id}>", "").strip()
                user_input = (
                    f"{message.author.name} replies to '{referenced_content}' from {self.user.name} "
                    f"with '{user_input}'"
                )
                chat_history = await self.get_chat_history(message.guild.id, message.channel)
                async with message.channel.typing():
                    response = self.query_cohere(user_input, chat_history)
                await message.channel.send(response)
                return  # Prevent further processing

        # Process messages where the bot is mentioned.
        if f"<@{self.user.id}>" in user_input:
            user_input = message.content.replace(f"<@{self.user.id}>", "").strip()
            if not user_input:
                await message.channel.send("What do you mean?")
            else:
                chat_history = await self.get_chat_history(message.guild.id, message.channel)
                async with message.channel.typing():
                    formatted_input = f"{message.author.name}: {user_input}"
                    response = self.query_cohere(formatted_input, chat_history)
                await message.channel.send(response)

        # Allow command processing to continue.
        await self.process_commands(message)


def start_bot():
    # Define the bot's intents.
    intents = discord.Intents.default()
    intents.messages = True
    intents.message_content = True

    # Instantiate and run the bot.
    bot = XiapBot(command_prefix="!", intents=intents)
    bot.run(DISCORD_TOKEN)

if __name__ == "__main__":
    start_bot()