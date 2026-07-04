import os
import discord
import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import textwrap
import uuid

import Queries
from Queries import QueryErrors
from keep_alive import keep_alive

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

pool = ConnectionPool(
    "postgresql://neondb_owner:npg_rSwaRGpoA3j9@ep-green-darkness-aszevld8-pooler.c-4.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row},
)

keep_alive()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

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
    
    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = Queries.setInPlayers(cur, discordID, "username_steam", steam_username)

            if (error == QueryErrors.PLAYER_NOT_FOUND):
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return

            if (error == QueryErrors.UNKNOWN_ERROR):
                await interaction.response.send_message("An unknown error has been found.")
                return
            
            if (error == QueryErrors.PARAMETER_NOT_FOUND):
                await interaction.response.send_message("The parameter username_steam has not been found as a column.")
                return
            
            await interaction.response.send_message(f"steam username was updated to {steam_username}.",ephemeral=True)


@bot.tree.command(name="change_substitute", description="Allows you either enlist or unlist as a substitute")
async def change_substitute(
    interaction: discord.Interaction, 
    be_substitute: bool
):
    discordID = interaction.user.id

    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = Queries.setInPlayers(cur, discordID, "is_substitute", be_substitute)

            if (error == QueryErrors.PLAYER_NOT_FOUND):
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return

            if (error == QueryErrors.UNKNOWN_ERROR):
                await interaction.response.send_message("An unknown error has been found.")
                return
            
            if (error == QueryErrors.PARAMETER_NOT_FOUND):
                await interaction.response.send_message("The parameter is_substitute has not been found as a column.")
                return
            
            textMessage = "You are now part of the substitute team." if be_substitute else "You are no longer part of the substitute team"
            await interaction.response.send_message(textMessage, ephemeral=True)

@bot.tree.command(name="sign_out", description="Signs you out of the tournament as a player (not watcher)")
async def sign_out(
    interaction: discord.Interaction
):
    discordID = interaction.user.id

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                DELETE FROM players
                WHERE discord_id = %s
                """,
                (discordID,)
            )
            if cur.rowcount == 0:
                await interaction.response.send_message("You are are currently not signed in.", ephemeral=True)
            else:
                await interaction.response.send_message("You are now signed out.", ephemeral=True)


@bot.tree.command(name="sign_in", description="Signs you into the tournament as a player (not watcher)")
async def sign_in(
    interaction: discord.Interaction, 
    steam_username: str, 
    be_substitute : bool
):
    discordID = interaction.user.id

    steam_username = steam_username.strip()
    if not steam_username:
        await interaction.response.send_message("The input data was invalid. Make sure that tyour Steam name is not empty.", ephemeral=True)
        return
    if len(steam_username) > 32:
        await interaction.response.send_message("The input data was invalid. Make sure that tyour Steam name is not bigger than 32 characters.", ephemeral=True)
        return
    
    with pool.connection() as conn:
        with conn.cursor() as cur:
            uselessObject, error = Queries.selectPlayerWithDiscordID(cur, discordID)

            if (error == QueryErrors.UNKNOWN_ERROR):
                await interaction.response.send_message("An unknown error has been found.")
                return
           
            if (error != QueryErrors.PLAYER_NOT_FOUND):
                await interaction.response.send_message("You are already signed up.", ephemeral=True)
                return


            cur.execute(
                """--sql
                INSERT INTO players (discord_id, username_steam, is_substitute)
                VALUES (%s, %s, %s)
                """,
                (discordID, steam_username, be_substitute)
            )
            await interaction.response.send_message("You are now signed in.", ephemeral=True)

@bot.tree.command(name="send_friendship_invite", description="Sends a friendship request to the other player so you")
async def send_friendship_invite(
    interaction: discord.Interaction,
    user: discord.Member
):
    
    friend_code = uuid.uuid4()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, error = Queries.selectPlayerWithDiscordID(cur, interaction.user.id)

            if (error == QueryErrors.PLAYER_NOT_FOUND):
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return
            
            if (player is None):
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return
            
            if "is_substitute" not in player:
                await interaction.response.send_message("The parameter is_substitute has not been found as a column.")
                return
            
            if player["is_substitute"]:
                await interaction.response.send_message("You can't create a friend request if you are part of the substitute team.", ephemeral=True)
                return
            
            if "friend_code" not in player:
                await interaction.response.send_message("The parameter friend_code has not been found as a column.")
                return
        
            if player["friend_code"] is not None:
                await interaction.response.send_message(" You already sent out a friendship request or are part of a friend group. " \
                "If you want to send out this request, you have to cancel the other friendship (request)", ephemeral=True)
                return
            
            Queries.setInPlayers(cur, 0, "friend_code", friend_code, player)
    
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
        view = acceptFriendshipInviteView(sender_id=interaction.user.id, receiver_id=user.id, thread_id=thread.id, friend_code=friend_code)
    )

    await interaction.response.send_message(f"friend request has been created. Look at {thread.mention}", ephemeral=True)


class acceptFriendshipInviteView(discord.ui.View):
    def __init__(
            self, 
            sender_id: int, 
            receiver_id: int,
            thread_id : int,
            friend_code : uuid.UUID
    ):
        super().__init__(timeout=None)
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.friend_code = friend_code
        self.thread_id = thread_id

    # the thread, this view is in, will be automatically be deleted if the sender will cancel the request
    @discord.ui.button(
        label="accept request",
        style=discord.ButtonStyle.success
    )
    async def accept(
        self, 
        interaction: discord.Interaction, 
        button: discord.ui.Button
    ):
        if (interaction.user.id != self.receiver_id):
            await interaction.response.send_message("You don't have the rights to interact with these buttons. These buttons are for the player, you invited.", ephemeral=True)
            return
        
        channel = interaction.channel
        if channel is None: return

        channelThread = channel.get_thread(self.thread_id)
        if channelThread is None: return

        if (channelThread.name == "friend group"):
            await interaction.response.send_message("You already accepted the friend request.", ephemeral=True)
            return
        
        with pool.connection() as conn:
            with conn.cursor() as cur:
                player, error = Queries.selectPlayerWithDiscordID(cur, self.receiver_id)

                if (error == QueryErrors.PLAYER_NOT_FOUND):
                    await interaction.response.send_message("You are not signed in.", ephemeral=True)
                    return
                
                if (player is None):
                    await interaction.response.send_message("You are not signed in.", ephemeral=True)
                    return
                
                if "is_substitute" not in player:
                    await interaction.response.send_message("The parameter is_substitute has not been found as a column.")
                    return
                
                if player["is_substitute"]:
                    await interaction.response.send_message("You can't create a friend request if you are part of the substitute team.", ephemeral=True)
                    return
                
                if "friend_code" not in player:
                    await interaction.response.send_message("The parameter friend_code has not been found as a column.")
                    return
            
                if player["friend_code"] is not None:
                    await interaction.response.send_message(" You already sent out a friendship request or are part of a friend group. " \
                    "If you want to send out this request, you have to cancel the other friendship (request)", ephemeral=True)
                    return
                
                Queries.setInPlayers(cur, 0, "friend_code", self.friend_code, player)

        await interaction.channel.get_thread(self.thread_id).edit(name="friend group")
        await interaction.response.send_message(
            "Friend request accepted."
        )


    @discord.ui.button(
        label="deny request",
        style=discord.ButtonStyle.danger
    )
    async def deny(self, interaction: discord.Interaction, button: discord.ui.Button):

        if (interaction.user.id != self.receiver_id):
            await interaction.response.send_message("You don't have the rights to interact with these buttons. These buttons are for the player, you invited.", ephemeral=True)
            return
        
        with pool.connection() as conn:
            with conn.cursor() as cur:
                error = Queries.setInPlayers(cur, self.sender_id, "friend_code", None)
        
        try:
            receiver = interaction.guild.get_member(self.receiver_id) or await interaction.guild.fetch_member(self.receiver_id)
            await interaction.channel.remove_user(receiver)
        except discord.NotFound:
            pass
        await interaction.response.send_message("Friend request denied. Please leave the thread manually.")

bot.run(TOKEN)
