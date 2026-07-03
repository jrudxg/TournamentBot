import os
import discord
import psycopg
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import textwrap
import uuid

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

@bot.tree.command(name="change_steam_username", description="Changes your steam username")
async def change_steam_username(
    interaction: discord.Interaction, 
    steam_username: str, 
):
    discordID = interaction.user.id

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE players
            SET username_steam = %s
            WHERE discord_id = %s
            """,
            (steam_username, discordID)
        )
        if cur.rowcount == 0:
            await interaction.response.send_message("You are not signed in", ephemeral=True)
        else:
            conn.commit()
            await interaction.response.send_message(f"steam username was updated to {steam_username}.",ephemeral=True)


@bot.tree.command(name="change_substitute", description="Allows you either enlist or unlist as a substitute")
async def change_substitute(
    interaction: discord.Interaction, 
    be_substitute: bool
):
    discordID = interaction.user.id

    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE players
            SET is_substitute = %s
            WHERE discord_id = %s
            """,
            (be_substitute, discordID)
        )
        if cur.rowcount == 0:
            await interaction.response.send_message("You are not signed in", ephemeral=True)
        else:
            conn.commit()
            textMessage = "You are now part of the substitute team." if be_substitute else "You are no longer part of the substitute team"
            await interaction.response.send_message(textMessage, ephemeral=True)

    


@bot.tree.command(name="sign_out", description="Signs you out of the tournament as a player (not watcher)")
async def sign_out(
    interaction: discord.Interaction
):
    discordID = interaction.user.id

    with conn.cursor() as cur:
        cur.execute(
            """
            DELETE FROM players
            WHERE discord_id = %s
            """,
            (discordID,)
        )
        if cur.rowcount == 0:
            await interaction.response.send_message("You are are currently not signed in.", ephemeral=True)
        else:
            conn.commit()
            await interaction.response.send_message("You are now signed out.", ephemeral=True)


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
                INSERT INTO players (discord_id, username_steam, is_substitute)
                VALUES (%s, %s, %s)
                """,
                (discordID, steam_username, be_substitute)
            )

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
        conn.commit()
        await interaction.response.send_message("You are now signed in.", ephemeral=True)

@bot.tree.command(name="send_friendship_invite", description="Sends a friendship request to the other player so you")
async def send_friendship_invite(
    interaction: discord.Interaction,
    user: discord.Member
):
    
    friend_code = uuid.uuid4()

    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT is_substitute
            FROM players
            WHERE discord_id = %s
            """,
            (interaction.user.id,)
        )
        row = cur.fetchone()
        if row is not None:
            is_substitute = row[0]
            if is_substitute:
                await interaction.response.send_message("You can't create a friend request if you are part of the substitute team.")
                return

        cur.execute(
            """
            UPDATE players
            SET friend_code = %s
            WHERE discord_id = %s
            AND friend_code IS NULL
            """,
            (friend_code, interaction.user.id)
        )
        if cur.rowcount == 0:
            await interaction.response.send_message("You are either not signed in or already send out an unanswered friendship request. If you have another friendship request, make sure to cancel that one.", ephemeral=True)
            return

        conn.commit()
    
    friendChannel : discord.TextChannel = None

    channels = interaction.guild.text_channels
    for channel in channels:
        if channel.name == "friends":
            friendChannel = channel
            break

    # discord doesn't allow 0 as the channel id
    if (friendChannel is None):
        await interaction.response.send_message(f"{interaction.guild.owner.mention} make sure that there's a friend channel in your discord. Else the bot can't create threads for the friends function")
        return

    thread = await friendChannel.create_thread(
        name                    = f"friend request from {interaction.user.display_name}",
        type                    = discord.ChannelType.private_thread,
        reason                  = "friend request",
        invitable               = False, 
        auto_archive_duration   = 10080
    )

    await thread.add_user(interaction.user)
    await thread.add_user(user)

    await thread.send(
        content= textwrap.dedent(f"""
            {user.mention}
            {interaction.user.mention} has sent you a friend request.
            Do you want to accept or decline this request?
        """),
        view = acceptFriendshipInviteView(sender_id=interaction.user.id, receiver_id=user.id, friend_code=friend_code)
    )

    await interaction.response.send_message(f"friend request has been created. Look at {thread.mention}", ephemeral=True)


class acceptFriendshipInviteView(discord.ui.View):
    def __init__(self, sender_id: int, receiver_id: int, friend_code : uuid.UUID):
        super().__init__(timeout=None)
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.friend_code = friend_code

    # the thread, this view is in, will be automatically be deleted if the sender will cancel the request
    @discord.ui.button(
        label="accept request",
        style=discord.ButtonStyle.success
    )
    async def accept(self, interaction: discord.Interaction, button: discord.ui.Button):
        if (interaction.user.id != self.receiver_id):
            await interaction.response.send_message("You don't have the rights to interact with these buttons. These buttons are for the player, you invited.")
            return

        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT is_substitute
                FROM players
                WHERE discord_id = %s
                """,
                (interaction.user.id,)
            )
            row = cur.fetchone()
            if row is not None:
                is_substitute = row[0]
                if is_substitute:
                    await interaction.response.send_message("You can't create a friend request if you are part of the substitute team.")
                    return

            cur.execute(
                """
                UPDATE players
                SET friend_code = %s
                WHERE discord_id = %s
                AND friend_code IS NULL
                """,
                (self.friend_code, self.receiver_id)
            )
            if cur.rowcount == 0:
                await interaction.response.send_message("You are not signed in", ephemeral=True)
                return
        
            conn.commit()

        await interaction.response.send_message(
            "Friend request accepted."
        )


    @discord.ui.button(
        label="deny request",
        style=discord.ButtonStyle.danger
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):

        if (interaction.user.id != self.receiver_id):
            await interaction.response.send_message("You don't have the rights to interact with these buttons. These buttons are for the player, you invited.")
            return

        with conn.cursor() as cur:
            cur.execute(
                """
                UPDATE players
                SET friend_code = NULL
                WHERE discord_id = %s
                AND friend_code IS NULL
                """,
                (self.sender_id,)
            )
            conn.commit()

        await interaction.response.send_message(
            "Friend request denied. Please leave the thread manually."
        )



bot.run(TOKEN)