import os
import discord
import psycopg
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

from keep_alive import keep_alive

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

conn = psycopg.connect(
    "postgresql://neondb_owner:npg_rSwaRGpoA3j9@ep-green-darkness-aszevld8-pooler.c-4.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require"
)

keep_alive()

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(command_prefix="!", intents=intents)

@bot.event
async def on_ready():
    await bot.tree.sync()
    print(f"{bot.user} is online!")

@bot.event
async def on_message(msg):
    if msg.author.id != bot.user.id:
        await msg.channel.send(f"Interesting message, {msg.author.mention}")

@bot.tree.command(name="greet", description="Sends a greeting to the user")
async def greet(interaction: discord.Interaction):
    username = interaction.user.mention
    await interaction.response.send_message(f"Hello there, {username}")

@bot.tree.command(name="sign in", description="Signs you into the tournament as a player (not watcher)")
async def signIn(
    interaction: discord.Interaction, 
    steam_username: str, 
    be_substitute : bool
):
    discordID = interaction.user.id

    try: 
        cur = conn.cursor()
        cur.execute(
            """
            INSERT INTO players (DiscordID, UserNameSteam, IsSubstitute)
            VALUES (%s, %s, %s)
            """,
            (discordID, steam_username, be_substitute)
        )
        conn.commit()
        
    except psycopg.Error as e:
        conn.rollback()
        await interaction.response.send_message("The input data was invalid. Make sure that the Steam name is valid.", ephemeral=True)
        return
    finally:
        cur.close()

    await interaction.response.send_message("You are now signed in.", ephemeral=True)

bot.run(TOKEN)