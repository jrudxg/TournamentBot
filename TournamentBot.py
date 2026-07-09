import discord
import psycopg
from psycopg_pool import ConnectionPool
from psycopg.rows import dict_row
from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv
import textwrap
import uuid
import re
import os
import random


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
    bot.add_dynamic_items(acceptFriendshipInviteView)
    await bot.tree.sync()
    print(f"{bot.user} is online!")

@bot.tree.command(name="change_steam_username", description="Changes your Steam username")
async def change_steam_username(
    interaction: discord.Interaction, 
    steam_username: str, 
):
    discordID = interaction.user.id
    
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if len(steam_username.strip()) == 0: 
                await interaction.response.send_message("Your Steam username can't be 0 characters long.", ephemeral=True)
                return
            if len(steam_username.strip()) > 32:
                await interaction.response.send_message("Your Steam username can't be longer than 32 characters.", ephemeral=True)
                return

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
            
            await interaction.response.send_message(f"Steam username was updated to {steam_username}.",ephemeral=True)


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
            canSignOut, error = Queries.checkIfPlayerHasValidValue(cur, discordID, "friend_code", None)
            if not canSignOut: 
                await interaction.response.send_message("You can't sign out if you have a you have a frindship or a friendship request. " \
                                                        "If you want to sign out, you have to cancel the other friendship (request).", ephemeral=True)
                return

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

@bot.event
async def on_member_remove(member : discord.Member):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, error = Queries.selectPlayerWithDiscordID(cur, member.id)

            if player is None or error == QueryErrors.PLAYER_NOT_FOUND:
                return
            
            if player["friend_code"] is not None:
                discordThreadID, amountOfPlayers, error = Queries.removeFriendCodeAndThread(cur, player["friend_code"])

                if (discordThreadID == None):
                    return
                
                try:
                    thread = await bot.fetch_channel(discordThreadID)
                except discord.NotFound:
                    thread = None
                except discord.Forbidden:
                    thread = None  # oder loggen

                if not isinstance(thread, discord.Thread):
                    return

                if (amountOfPlayers == 1):
                    await thread.delete()

                if (amountOfPlayers == 2):
                    # user already left the chat because he's not in the server
                    # await thread.remove_user(interaction.user)

                    await thread.send(f"{member.display_name} left server and therefore the friendship. You are now currently in no friendship. Please leave the thread manually.")
            
            cur.execute(
                """--sql
                DELETE FROM players
                WHERE discord_id = %s
                """,
                (member.id,)
            )


@bot.tree.command(name="leave_friendship", description="Leaves the current friendship or cancels the current request")
async def leave_friendship(
    interaction: discord.Interaction
):
    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, error = Queries.selectPlayerWithDiscordID(cur, interaction.user.id)
            if player is None:
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return
            
            if player["friend_code"] is None: 
                await interaction.response.send_message("You currently don't have a friendship and also don't have a pending request.", ephemeral=True)
                return

            discordThreadID, amountOfPlayers, error = Queries.removeFriendCodeAndThread(cur, player["friend_code"])
            if (discordThreadID == None):
                await interaction.response.send_message("An unknown error has been found.")
                return
            
            try:
                thread = await bot.fetch_channel(discordThreadID)
            except discord.NotFound:
                thread = None
            except discord.Forbidden:
                thread = None

            if not isinstance(thread, discord.Thread):
                await interaction.response.send_message("You are in no friendship.", ephemeral=True)
                return

            if (amountOfPlayers == 1):
                await thread.delete()

            if (amountOfPlayers == 2):
                await thread.remove_user(interaction.user)
                await thread.send(f"{interaction.user.display_name} left the friendship. You are now currently in no friendship. Please leave the thread manually.")

            await interaction.response.send_message("You succesfully left the friendship", ephemeral=True)


@bot.tree.command(name="send_friendship_invite", description="Sends a friendship request to the other player so you")
async def send_friendship_invite(
    interaction: discord.Interaction,
    user: discord.Member
):
    if (user == interaction.user):
        await interaction.response.send_message("You can't send a friendship invite to yourself", ephemeral=True)
        return
    
    friend_code = uuid.uuid4()

    friendChannel : discord.TextChannel = None

    channels = interaction.guild.text_channels
    for channel in channels:
        if channel.name == "friends":
            friendChannel = channel
            break

    # discord doesn't allow 0 as the channel id
    if (friendChannel is None):
        await interaction.response.send_message(f"<@{interaction.guild.owner_id} make sure that there's a \"friends\" channel in your discord. Else the bot can't create threads for the friends function")
        return

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

    thread = await friendChannel.create_thread(
        name                    = f"friend request from {interaction.user.display_name}",
        type                    = discord.ChannelType.private_thread,
        reason                  = "friend request",
        invitable               = False, 
        auto_archive_duration   = 10080
    )
    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = Queries.insertFriendThread(cur, thread.id, friend_code)
            if (error == QueryErrors.FRIENDCODE_NOT_FOUND):
                await interaction.response.send_message("The parameter friend_code has not been found as a column.")
                return

    await thread.add_user(interaction.user)
    await thread.add_user(user)

    await thread.send(
        content= textwrap.dedent(f"""
            {user.mention}
            {interaction.user.mention} has sent you a friend request.
            Do you want to accept or decline this request?
        """),
        view = buildFriendshipView(interaction.user.id, user.id, friend_code)
    )

    await interaction.response.send_message(f"friend request has been created. Look at {thread.mention}", ephemeral=True)

class acceptFriendshipInviteView(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"friend:(?P<action>accept|deny):(?P<sender>\d+):(?P<receiver>\d+):(?P<code>[0-9a-fA-F-]+)"
):
    def __init__(
        self, 
        action: str,
        sender_id: int, 
        receiver_id: int,
        friend_code: uuid.UUID
    ):
        super().__init__(
            discord.ui.Button(
                label="accept request" if action == "accept" else "deny request",
                style=discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.danger,
                custom_id=f"friend:{action}:{sender_id}:{receiver_id}:{friend_code}"
            )
        )
        self.action = action
        self.sender_id = sender_id
        self.receiver_id = receiver_id
        self.friend_code = friend_code

    @classmethod
    async def from_custom_id(cls, interaction: discord.Interaction, item, match: re.Match):
        return cls(
            match["action"],
            int(match["sender"]),
            int(match["receiver"]),
            uuid.UUID(match["code"])
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message("You don't have the rights to interact with these buttons. These buttons are for the player, you invited.", ephemeral=True)
            return

        if self.action == "accept":
            await self._accept(interaction)
        else:
            await self._deny(interaction)

    async def _accept(self, interaction: discord.Interaction):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                player, error = Queries.selectPlayerWithDiscordID(cur, self.receiver_id)

                if error == QueryErrors.PLAYER_NOT_FOUND or player is None:
                    await interaction.response.send_message("You are not signed in.", ephemeral=True)
                    return

                if player["friend_code"] is not None:
                    await interaction.response.send_message("You already accepted the friend request (or are in another friendship).", ephemeral=True)
                    return

                if player["is_substitute"]:
                    await interaction.response.send_message("You can't accept a friend request as a substitute.", ephemeral=True)
                    return

                Queries.setInPlayers(cur, 0, "friend_code", self.friend_code, player)

        await interaction.channel.edit(name="friend group")
        await interaction.response.edit_message(view=None)
        await interaction.followup.send("friend request accepted.")

    async def _deny(self, interaction: discord.Interaction):
        thread = interaction.channel
        if type(thread) is not discord.Thread:
            await interaction.response.send_message("An unknown error has been found. id is not from a thread")
            return

        with pool.connection() as conn:
            with conn.cursor() as cur:
                discordThreadID, amountOfPlayers, error = Queries.removeFriendCodeAndThread(cur, self.friend_code)
                if discordThreadID is None:
                    await interaction.response.send_message("This request was already handled or no longer exists.", ephemeral=True)
                    return
        
        await interaction.response.edit_message(view=None)
                
        try:
            receiver = interaction.guild.get_member(self.receiver_id) or \
                        await interaction.guild.fetch_member(self.receiver_id)
            await thread.remove_user(receiver)
        except discord.NotFound:
            pass
        await interaction.followup.send("friend request denied. Please leave the thread manually.")

def buildFriendshipView(sender_id: int, receiver_id: int, friend_code: uuid.UUID) -> discord.ui.View:
    view = discord.ui.View(timeout=None)
    view.add_item(acceptFriendshipInviteView("accept", sender_id, receiver_id, friend_code))
    view.add_item(acceptFriendshipInviteView("deny", sender_id, receiver_id, friend_code))
    return view

async def start_creating_teams(
    interaction: discord.Interaction
):
    await interaction.response.defer()

    teamsChannel : discord.TextChannel = None

    channels = interaction.guild.text_channels
    for channel in channels:
        if channel.name == "teams":
            teamsChannel = channel
            break

    # discord doesn't allow 0 as the channel id
    if (teamsChannel is None):
        await interaction.followup.send(f"<@{interaction.guild.owner_id}> make sure that there's a \"teams\" channel in your discord. Else the bot can't create threads for the friends function")
        return

    amountOfMissingPlayers = 0
    amountOfTeams = 0
    teamSize = 5

    with pool.connection() as conn:
        with conn.cursor() as cur:
            playerIDs = Queries.getAllPlayerIDs(cur)
            random.shuffle(playerIDs)

            amountOfPlayersWithoutFullTeam = len(playerIDs) % teamSize

            amountOfMissingPlayers = (teamSize - amountOfPlayersWithoutFullTeam) % teamSize
            amountOfTeams = (len(playerIDs) + amountOfMissingPlayers) // teamSize

            playerIDs.extend([0] * amountOfMissingPlayers)

            if (amountOfMissingPlayers != 0):
                await teamsChannel.send(f"There are {amountOfMissingPlayers} missing players that need to be filled in team{amountOfTeams}")

            for teamNumber in range(amountOfTeams):
                teamName = f"team{teamNumber+1}"

                players = tuple(playerIDs[teamNumber*teamSize : (teamNumber+1) * teamSize])

                role = await interaction.guild.create_role(name=teamName,mentionable=True)
                thread = await teamsChannel.create_thread(
                    name=teamName,
                    type                    = discord.ChannelType.private_thread,
                    reason                  = "team creation",
                    invitable               = False, 
                    auto_archive_duration   = 10080
                )
                
                for player in players:
                    if (player == 0): continue
                    try:
                        member = await interaction.guild.fetch_member(player)
                    except discord.NotFound:
                        await teamsChannel.send(f"<@{player}> is not in the server anymore.")
                        continue

                    await member.add_roles(role)
                    await thread.add_user(member)

                await thread.send(f"Welcome {teamName}")

                Queries.insertTeam(cur, teamName, thread.id, players)

    await interaction.followup.send(f"All {amountOfTeams} teams have been created.", ephemeral=True)

                

        

bot.run(TOKEN)