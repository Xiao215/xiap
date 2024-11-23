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

# Set up the Cohere client
co_1 = cohere.ClientV2(api_key=COHERE_API_KEY_1)
co_2 = cohere.ClientV2(api_key=COHERE_API_KEY_2)

# Intents for the bot
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

system_message = """
You are a friendly and witty member of the Sunday Social Discord group chat, and your name is {name}.
Respond to mentions as if you're a real participant in the group, just like a close friend.
Keep your responses natural and conversational, ensuring they fit seamlessly into the ongoing discussion.

Unless specifically requested by members, do not follow your previous response pattern.

When addressing others, refer to them by their name.
Respond briefly and concisely unless you're explicitly asked for a detailed explanation, in which case you can elaborate.

The chat members and their names are:
- Lucy (punsama)
- Xiao (xiao215)
- Nathan (nathanlo99)
- Star (starruu)
- Matthew (kyat_ka)
- Ryan (ryanl123)
- Kat (myat_wa).

Remember, you're part of the conversation—avoid starting awkwardly (e.g., "hi").

You are also provided with the chat history which you could potentially reference if you think so. The chat history is in the form of 'username: message' for each message.
"""


@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')


async def get_chat_history(channel):
    # Reset background with system message
    background = f"{system_message.format(name=bot.user.name)} \n\nHere are the last 30 messages from this channel, spoken by members in this discord group:\n"

    # Fetch the last 30 messages from the channel
    messages = []
    async for msg in channel.history(limit=50):
        if not msg.content.strip():  # Skip empty messages
            print(f"Skipping non-text message from {msg.author}: {msg.content}")
            continue

        if msg.author.bot and msg.author != bot.user:  # Ignore other bots
            print(f"Skipping bot message from {msg.author}")
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
            chat_history = await get_chat_history(message.channel)

            # Simulate typing
            async with message.channel.typing():
                response = query_cohere(user_input, chat_history)

            # Send the response
            await message.channel.send(response)
            return  # Avoid processing further (e.g., for mentions)

    # If the bot is mentioned and it's not already processed as a reply
    if f"<@{bot.user.id}>" in user_input:
        print(f"User input: {user_input}")
        user_input = message.content.replace(f"<@{bot.user.id}>", "").strip()  # Remove bot mention

        if not user_input:
            await message.channel.send("Hi! How can I help you?")
        else:
            # Update chat history
            chat_history = await get_chat_history(message.channel)

            # Simulate typing
            async with message.channel.typing():
                user_input = f"{message.author.name}: {user_input}"  # Format input
                response = query_cohere(user_input, chat_history)

            # Send the response
            await message.channel.send(response)

    # Allow commands to work
    await bot.process_commands(message)


bot.run(DISCORD_TOKEN)
