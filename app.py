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

system_message = "You are a kinda and funny member in this discord group chat."

chat_history = [{"role": "system", "content": system_message}]


@bot.event
async def on_ready():
    print(f'We have logged in as {bot.user}')

# Function to query Cohere's chat model
def query_cohere(prompt, model="command-r-plus-08-2024"):
    global chat_history
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

    if bot.user in message.mentions:  # Check if the bot is mentioned
        user_input = message.content.replace(f"<@{bot.user.id}>", "").strip()  # Remove bot mention

        if not user_input:
            await message.channel.send("Hi! How can I help you?")
        else:
            async with message.channel.typing():  # Simulate typing
                response = query_cohere(user_input)

            # Send the response
            await message.channel.send(response)

    await bot.process_commands(message)  # Allow commands to work

bot.run(DISCORD_TOKEN)