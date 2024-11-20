import os
import discord
from discord.ext import commands
from dotenv import load_dotenv
import cohere

# Load environment variables from the .env file
load_dotenv()

DISCORD_TOKEN = os.getenv('DISCORD_TOKEN')
COHERE_API_KEY = os.getenv('COHERE_API_KEY')  # Store your Cohere API key in the .env file

# Set up the Cohere client
co = cohere.ClientV2(api_key=COHERE_API_KEY)

# Intents for the bot
intents = discord.Intents.default()
intents.messages = True
intents.message_content = True

bot = commands.Bot(command_prefix='!', intents=intents)

system_message = f"You are a kind and funny member with name {bot.user} in this discord group chat. The chat is called Sunday Social, and you are a real member of it. Just answer like a friend to whoever mentions you. Remember this is in the middle of a conversation. Also, some name and username of the members in the chat are: Lucy (punsama), Xiao (xiao215), Nathan (nathanlo99), Star (starruu), Matthew (kyat_ka), Ryan (ryanl123) and Kat (myat_wa)."


@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')


async def get_chat_history(channel):
    # Reset background with system message
    background = f"{system_message} \n\nHere are the last 30 messages from this channel, spoken by members in this discord group:\n"

    # Fetch the last 30 messages from the channel
    messages = []
    async for msg in channel.history(limit=50):
        if not msg.content.strip():  # Skip empty messages
            print(f"Skipping non-text message from {msg.author}: {msg.content}")
            continue

        if msg.author.bot and msg.author != bot.user:  # Ignore other bots
            print(f"Skipping bot message from {msg.author}")
            continue

        messages.append(msg)

    # Reverse messages to chronological order
    messages.reverse()

    # Add messages to the background
    for msg in messages:
        background += f"{msg.author}: {msg.content}\n"
    print(background)
    return [{"role": "system", "content": background}]  # Reset with system message


# Function to query Cohere's chat model
def query_cohere(prompt, chat_history, model="command-r-plus-08-2024"):
    try:
        # Add the user's message to the chat history
        chat_history.append({"role": "user", "content": prompt})

        # Query Cohere with the conversation history
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

    # If the bot is mentioned
    if bot.user in message.mentions:
        user_input = message.content.replace(f"<@{bot.user.id}>", "").strip()  # Remove bot mention

        if not user_input:
            await message.channel.send("Hi! How can I help you?")
        else:
            # Update chat history with the latest messages
            chat_history = await get_chat_history(message.channel)

            # Simulate typing
            async with message.channel.typing():
                response = query_cohere(user_input, chat_history)

            # Send the response
            await message.channel.send(response)

    await bot.process_commands(message)  # Allow commands to work


bot.run(DISCORD_TOKEN)