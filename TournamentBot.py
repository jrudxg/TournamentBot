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

@bot.tree.command(name="sign_in", description="Signs you into the tournament as a player (not watcher)")
async def sign_in(
    interaction: discord.Interaction, 
    steam_username: str, 
    be_substitute : bool
):
    discordID = interaction.user.id

    try: 
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO players (DiscordID, UserNameSteam, IsSubstitute)
                VALUES (%s, %s, %s)
                """,
                (discordID, steam_username, be_substitute)
            )
            
        conn.commit()

    except psycopg.errors.UniqueViolation:
        conn.rollback()
        await interaction.response.send_message(
            "You are already signed up.",
            ephemeral=True
        )

    except psycopg.errors.CheckViolation:
        conn.rollback()
        await interaction.response.send_message("The input data was invalid. Make sure that the Steam name is valid.", ephemeral=True)

    except psycopg.Error:
        conn.rollback()
        await interaction.response.send_message(
            "An internal database error occurred.",
            ephemeral=True
        )
        raise
        
    else:
        await interaction.response.send_message("You are now signed in.", ephemeral=True)

bot.run(TOKEN)
