import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import cohere
import random
# Load environment variables from the .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
COHERE_API_KEY_1 = os.getenv('COHERE_API_KEY_1')
COHERE_API_KEY_2 = os.getenv('COHERE_API_KEY_2')
COHERE_API_KEY_3 = os.getenv('COHERE_API_KEY_3')

# Set up the Cohere client
co_1 = cohere.ClientV2(api_key=COHERE_API_KEY_3)
co_2 = cohere.ClientV2(api_key=COHERE_API_KEY_3)

# Intents for the bot
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)
custom_emojis_cache = {}  # Global dictionary to store custom emojis by name


# Here are some custome emoji from this server that you SHOULD include in your response if they are relevant. The emoji below is in the format of "<:name:emoji_id>" and you can include the entire thing with brackets in your response:

system_message = """
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


@bot.event
async def on_ready():
    global custom_emojis_cache
    custom_emojis_cache.clear()  # Clear cache on bot reconnect

    for guild in bot.guilds:  # Iterate through all guilds
        print(f"Caching emojis for guild: {guild.name}")
        custom_emojis_cache[guild.id] = {
            emoji.name: emoji for emoji in guild.emojis}
    print(f"We have logged in as {bot.user}")


async def get_chat_history(guild_id, channel):
    # Reset background with system message
    emojis = [f'name: {name} emoji_id: {emoji.id}' for name,
              emoji in custom_emojis_cache[guild_id].items()]
    emojis = '\n'.join(emojis)
    background = f"{system_message.format(name=bot.user.name, emojis=emojis)} \n\nHere are the last 30 messages from this channel, spoken by members in this discord group:\n"

    # Fetch the last 30 messages from the channel
    messages = []
    async for msg in channel.history(limit=50):
        if not msg.content.strip():  # Skip empty messages
            continue

        if msg.author.bot and msg.author != bot.user:  # Ignore other bots
            continue
        msg.content = msg.content.replace(f"<@{bot.user.id}>", f"@{bot.user.name}")  # Replace bot mention with username
        messages.append(msg)

    # Reverse messages to chronological order
    messages.reverse()

    # Add messages to the background
    for msg in messages:
        background += f"{msg.author}: {msg.content}\n"
    return [{"role": "system", "content": background}]  # Reset with system message


# Function to query Cohere's chat model
def query_cohere(prompt, chat_history, model="command-r-plus-08-2024"):
    try:
        # Add the user's message to the chat history
        chat_history.append({"role": "user", "content": prompt})

        # Query Cohere with the conversation history
        co = co_1 if random.random() < 0.5 else co_2
        res = co.chat(
            model=model,
            messages=chat_history,
        )

        # Extract the assistant's response content
        if res.message and res.message.content:
            response = res.message.content[0].text
        else:
            response = "I'm sorry, I couldn't generate a response."

        # Add the assistant's response to the chat history
        chat_history.append({"role": "assistant", "content": response})
        return response
    except Exception as e:
        return f"Error: {e}"


# Chat interaction
@bot.event
async def on_message(message):
    global chat_history

    # Ignore bot's own messages
    if message.author == bot.user:
        return

    user_input = message.content
    referenced_message = None
    # Check if the message is a reply to the bot
    if message.reference and message.reference.resolved:
        replied_message = message.reference.resolved

        if replied_message.author == bot.user:
            referenced_message = replied_message.content  # Get the content of the referenced message
            user_input = message.content.replace(f"<@{bot.user.id}>", "").strip()
            user_input = f"{message.author.name} replies to '{referenced_message}' from {bot.user.name} with '{user_input}'"  # Format input
            print(f"User input: {user_input}")
            # Update chat history
            chat_history = await get_chat_history(message.guild.id, message.channel)

            # Simulate typing
            async with message.channel.typing():
                response = query_cohere(user_input, chat_history)

            # Send the response
            await message.channel.send(response)
            return  # Avoid processing further (e.g., for mentions)

    # If the bot is mentioned and it's not already processed as a reply
    if f"<@{bot.user.id}>" in user_input:
        user_input = message.content.replace(f"<@{bot.user.id}>", "").strip()  # Remove bot mention

        if not user_input:
            await message.channel.send("What do you mean?")
        else:
            # Update chat history
            chat_history = await get_chat_history(message.guild.id, message.channel)

            # Simulate typing
            async with message.channel.typing():
                user_input = f"{message.author.name}: {user_input}"  # Format input
                response = query_cohere(user_input, chat_history)

            # Send the response
            await message.channel.send(response)

    # Allow commands to work
    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
