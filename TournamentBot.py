# tournament_bot.py
"""Discord bot that manages sign-ups, friend groups, and teams
for a tournament.
"""

import os
import random
import re
import textwrap
import uuid
import challonge

import discord
import psycopg
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

import json
import asyncio
import queries
from datetime import datetime, timezone, timedelta
from enums import CreateTeamsOutput, QueryErrors, RemoveFromTeamOutput
from collections import defaultdict
from keep_alive import keep_alive

# ============================================================
# Configuration & Setup
# ============================================================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
CHALLONGE_API_KEY = os.getenv("CHALLONGE_API_KEY")
CHALLONGE_USER = os.getenv("CHALLONGE_USER")
TOURNAMENT_ID = os.getenv("TOURNAMENT_ID")

ALLOWED_GUILD_ID = 1519693560268455990
TEAM_SIZE = 5
EMPTY_SLOT_ID = 0
FRIEND_CHANNEL_NAME = "friends"
TEAM_CHANNEL_NAME = "teams"
CAPTAIN_ROLE_NAME = "captain"
SUBSTITUTE_ROLE_NAME = "substitute"
THREAD_AUTO_ARCHIVE_MINUTES = 10080
TEAM_PLAYER_COLUMNS = (
    "player1_id",
    "player2_id",
    "player3_id",
    "player4_id",
    "player5_id",
)

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row}
)

keep_alive()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="/", intents=intents)


async def get_or_fetch_guild(guild_id: int) -> discord.Guild | None:
    return bot.get_guild(guild_id) or await bot.fetch_guild(guild_id)

async def get_or_fetch_channel(channel_id: int) -> discord.abc.GuildChannel | discord.Thread | None:
    return bot.get_channel(channel_id) or await bot.fetch_channel(channel_id)

async def get_or_fetch_user(user_id: int) -> discord.User | None:
    return bot.get_user(user_id) or await bot.fetch_user(user_id)

async def get_or_fetch_member(guild: discord.Guild, member_id: int) -> discord.Member | None:
    return guild.get_member(member_id) or await guild.fetch_member(member_id)

async def get_or_fetch_role(guild: discord.Guild, role_id: int) -> discord.Role | None:
    return guild.get_role(role_id) or await guild.fetch_role(role_id)

async def get_or_fetch_message(guild: discord.Guild, role_id: int) -> discord.Role | None:
    return guild.get_role(role_id) or await guild.fetch_role(role_id)



async def captain_vote_run_at(target_time: datetime):
    now = datetime.now(timezone.utc)
    delay = (target_time - now).total_seconds()

    if delay > 0:
        await asyncio.sleep(delay)

    await start_captain_vote_everywhere()

async def team_creation_run_at(target_time: datetime):
    now = datetime.now(timezone.utc)
    delay = (target_time - now).total_seconds()

    if delay > 0:
        await asyncio.sleep(delay)

    await start_creating_teams()
    
async def start_tournament(target_time: datetime):
    now = datetime.now(timezone.utc)
    delay = (target_time - now).total_seconds()

    if delay > 0:
        await asyncio.sleep(delay)

    insertTeamsIntoTournamentTable()

# ============================================================
# Bot Events
# ============================================================

@bot.event
async def on_ready():
    bot.add_dynamic_items(AcceptFriendshipInviteView)
    try:
        synced = await bot.tree.sync(guild=discord.Object(id=ALLOWED_GUILD_ID))
        print(f"Synced {len(synced)} commands to guild {ALLOWED_GUILD_ID}", flush=True)
    except Exception as e:
        print(f"Sync failed: {e}", flush=True)

    print(f"{bot.user} is online!", flush=True)

    # needs to be changed
    teamTime= datetime(2026, 12, 31, 0, tzinfo=timezone.utc)
    captainTime = datetime(2027, 1, 2, 0, tzinfo=timezone.utc)
    asyncio.create_task(team_creation_run_at(teamTime))
    asyncio.create_task(captain_vote_run_at(captainTime))

    bot.add_dynamic_items(AcceptFriendshipInviteView)

    load_captain_polls()
    
@bot.event
async def on_message(message):
    pass


@bot.event
async def on_member_remove(member: discord.Member):
    """When a member leaves the server: dissolve their friendship (if
    any), remove them from `players`, and remove them from their team.
    """

    player : DictRow
    with pool.connection() as conn:
        with conn.cursor() as cur:
            output, error = queries.select_player_with_discord_id(cur, member.id)

            if output is None or error == QueryErrors.PLAYER_NOT_FOUND:
                return
            player = output
            
            cur.execute(
                """--sql
                DELETE FROM players
                WHERE discord_id = %s
                """,
                (member.id,),
            )

    await remove_from_team(member.id)

    if player["friend_code"] is not None:
        discord_thread_id, amount_of_players, error = (
            queries.remove_friend_code_and_thread(cur, player["friend_code"])
        )

        if discord_thread_id is None:
            return

        try:
            thread = await get_or_fetch_channel(discord_thread_id)
        except (discord.NotFound, discord.Forbidden):
            thread = None

        if isinstance(thread, discord.Thread):
            if amount_of_players == 1:
                await thread.delete()
            elif amount_of_players == 2:
                # The user already left the thread automatically
                # because they left the server.
                await thread.send(
                    f"{member.display_name} left the server and therefore the "
                    "friendship. You are now in no friendship. Please leave "
                    "the thread manually."
                )


# ============================================================
# Player Sign-up Commands
# ============================================================

@bot.tree.command(
    name="sign_in", 
    description="Signs you into the tournament as a player (not watcher)",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def sign_in(
    interaction: discord.Interaction,
    steam_username: str,
    be_substitute: bool,
):
    """Sign-in requirements:
    - Steam username between 1 and 32 characters
    - if not `be_substitute`: teams must not have been created yet
    - not already signed in
    """
    discord_id = interaction.user.id
    steam_username = steam_username.strip()

    if not steam_username:
        await interaction.response.send_message(
            "The input data was invalid. Make sure that your Steam name is not empty.",
            ephemeral=True,
        )
        return
    if len(steam_username) > 32:
        await interaction.response.send_message(
            "The input data was invalid. Make sure that your Steam name is not bigger "
            "than 32 characters.",
            ephemeral=True,
        )
        return
    
    output = ("", False)
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if queries.check_if_teams_exist(cur) and not be_substitute:
                    output = ("You can only sign up as a substitute because the teams have already been created.", True)
                    break

                _, error = queries.select_player_with_discord_id(cur, discord_id)

                if error == QueryErrors.UNKNOWN_ERROR:
                    output = ("An unknown error has been found.", False)
                    break

                if error != QueryErrors.PLAYER_NOT_FOUND:
                    output = ("You are already signed up.", True)
                    break

                cur.execute(
                    """--sql
                    INSERT INTO players (discord_id, username_steam, is_substitute)
                    VALUES (%s, %s, %s)
                    ON CONFLICT (discord_id) DO NOTHING
                    """,
                    (discord_id, steam_username, be_substitute),
                )
                if cur.rowcount == 0:
                    output = ("You are already signed up.", True)
                    break
                
    if output[0] != "":
        await interaction.response.send_message(output[0],ephemeral=output[1])
        return

            
    if be_substitute:
        substitute_role = discord.utils.get(interaction.guild.roles, name=SUBSTITUTE_ROLE_NAME)
        if substitute_role is None: return
        await interaction.user.add_roles(await get_or_fetch_role(interaction.guild, substitute_role))

    await interaction.response.send_message("You are now signed in.", ephemeral=True)


@bot.tree.command(
    name="sign_out", 
    description="Signs you out of the tournament as a player (not watcher)",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def sign_out(interaction: discord.Interaction):
    """Sign-out requirements:
    - no friendship
    - if not a substitute: teams must not have been created yet
    - already signed in
    """
    discord_id = interaction.user.id

    output = ""
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:

                can_sign_out, _ = queries.check_if_player_has_valid_value(
                    cur, discord_id, "friend_code", None
                )

                if not can_sign_out:
                    output = (
                        "You can't sign out if you have a friendship or a friendship "
                        "request. If you want to sign out, you have to cancel the other "
                        "friendship (request)."
                    )
                    break

                if queries.check_if_teams_exist(cur):
                    player, _ = queries.select_player_with_discord_id(cur, discord_id)
                    
                    if player is not None and not player["is_substitute"]:
                        output = (
                            "You can't sign out if you are already part of a team. "
                            "If you want to sign out, you have to cancel the other "
                            "friendship (request)."
                        )
                        break

                cur.execute(
                    """--sql
                    DELETE FROM players
                    WHERE discord_id = %s
                    """,
                    (discord_id,),
                )
                if cur.rowcount == 0:
                    output = "You are currently not signed in."
                    break
            
    if output != "":
        await interaction.response.send_message(output, ephemeral=True)
        return

    substitute_role = discord.utils.get(interaction.guild.roles, name=SUBSTITUTE_ROLE_NAME)
    if substitute_role is None: return
    await interaction.user.remove_roles(await get_or_fetch_role(interaction.guild, substitute_role))
    await interaction.response.send_message("You are now signed out.", ephemeral=True)


@bot.tree.command(
    name="change_steam_username", 
    description="Changes your Steam username",
    guild=discord.Object(id=ALLOWED_GUILD_ID)    
)
async def change_steam_username(interaction: discord.Interaction, steam_username: str):
    """Requirements:
    - Steam username between 1 and 32 characters
    - signed in
    """
    discord_id = interaction.user.id
    stripped_username = steam_username.strip()

    if len(stripped_username) == 0:
        await interaction.response.send_message(
            "Your Steam username can't be 0 characters long.", ephemeral=True
        )
        return
    if len(stripped_username) > 32:
        await interaction.response.send_message(
            "Your Steam username can't be longer than 32 characters.", ephemeral=True
        )
        return
    
    output = f"Steam username was updated to {steam_username}."
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                error = queries.set_in_players(cur, discord_id, "username_steam", steam_username)

                if error == QueryErrors.PLAYER_NOT_FOUND:
                    output = "You are not signed in."
                    break

    await interaction.response.send_message(output, ephemeral=True)


@bot.tree.command(
    name="change_substitute", 
    description="Allows you to either enlist or unlist as a substitute",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def change_substitute(interaction: discord.Interaction, be_substitute: bool):
    """Requirements:
    - teams must not have been created yet
    - no friendship
    """
    discord_id = interaction.user.id

    output = ""

    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if queries.check_if_teams_exist(cur):
                    output = (
                        "You can't change your substitute status anymore because the "
                        "teams have already been created."
                    )
                    break

                can_change_substitute, error = queries.check_if_player_has_valid_value(
                    cur, discord_id, "friend_code", None
                )
                if error == QueryErrors.PLAYER_NOT_FOUND:
                    output = "You are not signed in."
                    break

                if not can_change_substitute:
                    output = (
                        "You can't change your substitute status if you have a "
                        "friendship or a friendship request."
                    )
                    break

                error = queries.set_in_players(cur, discord_id, "is_substitute", be_substitute)

                if error == QueryErrors.PLAYER_NOT_FOUND:
                    output = "You are not signed in."
                    break

    if (output != ""):
        await interaction.response.send_message(output, ephemeral=True)
        return

    message = (
        "You are now part of the substitute team."
        if be_substitute
        else "You are no longer part of the substitute team."
    )

    await interaction.response.send_message(message, ephemeral=True)

    substitute_role = discord.utils.get(interaction.guild.roles, name=SUBSTITUTE_ROLE_NAME)
    if substitute_role is None: return

    if be_substitute: await interaction.user.add_roles(substitute_role)
    else: await interaction.user.remove_roles(substitute_role)


# ============================================================
# Friendship Commands
# ============================================================

@bot.tree.command(
    name="send_friendship_invite",
    description="Sends a friendship request to the other player so you",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def send_friendship_invite(interaction: discord.Interaction, user: discord.Member):
    if user == interaction.user:
        await interaction.response.send_message(
            "You can't send a friendship invite to yourself", ephemeral=True
        )
        return

    friend_code = uuid.uuid4()
    friend_channel = discord.utils.get(interaction.guild.text_channels, name=FRIEND_CHANNEL_NAME)

    if friend_channel is None:
        await interaction.response.send_message(
            f"<@{interaction.guild.owner_id}> make sure that there's a "
            f'"{FRIEND_CHANNEL_NAME}" channel in your discord. Else the bot can\'t '
            "create threads for the friends function"
        )
        return
    
    output = ""
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                teams_exist = queries.check_if_teams_exist(cur)
                if teams_exist:
                    output = "You can't send out a friendship invite if the teams have already been created."
                    break


                player, error = queries.select_player_with_discord_id(cur, interaction.user.id)

                if error == QueryErrors.PLAYER_NOT_FOUND or player is None:
                    output = "You are not signed in."
                    break
                
                if player["is_substitute"]:
                    output = "You can't create a friend request if you are part of the substitute team."
                    break

                if player["friend_code"] is not None:
                    output = (
                        "You already sent out a friendship request or are part of a "
                        "friend group. If you want to send out this request, you have "
                        "to cancel the other friendship (request)"
                    )
                    break

                cur.execute(
                    """--sql
                    UPDATE players
                    SET friend_code = %s
                    WHERE discord_id = %s
                    AND friend_code IS NULL
                    AND is_substitute = FALSE
                    """,
                    (friend_code, interaction.user.id),
                )
                if cur.rowcount == 0:
                    output = (
                        "You already sent out a friendship request or are part of a "
                        "friend group. If you want to send out this request, you have "
                        "to cancel the other friendship (request)"
                    )
                    break
    if output != "":
        interaction.response.send_message(output, ephemeral=True)
        return

    try:
        thread = await friend_channel.create_thread(
            name=f"friend request from {interaction.user.display_name}",
            type=discord.ChannelType.private_thread,
            reason="friend request",
            invitable=False,
            auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
        )
    except discord.HTTPException:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                queries.set_in_players(cur, interaction.user.id, "friend_code", None)

        await interaction.response.send_message(
            "The thread couldn't be created. Please try again.", ephemeral=True
        )
        return

    error : QueryErrors = QueryErrors.NO_ERROR
    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = queries.insert_friend_thread(cur, thread.id, friend_code)

    if error == QueryErrors.FRIENDCODE_NOT_FOUND:
        await interaction.response.send_message(
            "The parameter friend_code has not been found as a column."
        )
        return

    await thread.add_user(interaction.user)
    await thread.add_user(user)

    await thread.send(
        content=textwrap.dedent(
            f"""
                {user.mention}
                {interaction.user.mention} has sent you a friend request.
                Do you want to accept or decline this request?
            """
        ),
        view=build_friendship_view(interaction.user.id, user.id, friend_code),
    )

    await interaction.response.send_message(f"Friend request has been created. Look at {thread.mention}", ephemeral=True)


@bot.tree.command(
    name="leave_friendship",
    description="Leaves the current friendship or cancels the current request",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def leave_friendship(interaction: discord.Interaction):
    """Requirements:
    - signed in
    - has a friendship (or pending request)
    """

    output = ""
    discord_thread_id = 0
    amount_of_players = 0

    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                player, _ = queries.select_player_with_discord_id(cur, interaction.user.id)
                if player is None:
                    output = "You are not signed in."
                    break

                if player["friend_code"] is None:
                    output = (
                        "You currently don't have a friendship and also don't have a "
                        "pending request."
                    )
                    break

                discord_thread_id, amount_of_players, _ = queries.remove_friend_code_and_thread(
                    cur, player["friend_code"]
                )

    if output != "":
        interaction.response.send_message(output, ephemeral=True)
        return
    
    if discord_thread_id is None:
        await interaction.response.send_message("An unknown error has been found.")
        return

    try:
        thread = await get_or_fetch_channel(discord_thread_id)
    except (discord.NotFound, discord.Forbidden):
        thread = None

    if not isinstance(thread, discord.Thread):
        await interaction.response.send_message("You are in no friendship.", ephemeral=True)
        return

    if amount_of_players == 1:
        await thread.delete()
    elif amount_of_players == 2:
        await thread.remove_user(interaction.user)
        await thread.send(
            f"{interaction.user.display_name} left the friendship. You are now in no friendship. Please leave the thread manually."
        )

    await interaction.response.send_message("You successfully left the friendship", ephemeral=True)


# ============================================================
# Team Management Commands
# ============================================================

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_start_create_teams", 
    description="Creates the teams for the tournament",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_start_create_teams(interaction: discord.Interaction):
    await interaction.response.defer()
    _, message_text = await start_creating_teams()
    await interaction.followup.send(message_text)


@bot.tree.command(
    name="leave_team",
    description="Leaves your current team. ONLY THE MODERATORS CAN ASSIGN YOU BACK TO THE TEAM.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def leave_team(interaction: discord.Interaction):
    await interaction.response.defer()
    error = await remove_from_team(interaction.user.id)

    if error is RemoveFromTeamOutput.TEAMS_NOT_CREATED:
        await interaction.followup.send("The teams have not been created yet.", ephemeral=True)
    elif error is RemoveFromTeamOutput.PLAYER_NOT_FOUND:
        await interaction.followup.send("You are not part of a team.", ephemeral=True)
    elif error is RemoveFromTeamOutput.NO_ERROR:
        await interaction.followup.send("You successfully left your team.", ephemeral=True)
    else:
        await interaction.followup.send("An unknown error has been found.")

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_set_captain",
    description="Sets the captain in a team",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_set_captain(
    interaction: discord.Interaction,
    team_thread: discord.Thread,
    new_captain: discord.Member
):
    
    captain_role = discord.utils.get(interaction.guild.roles, name=CAPTAIN_ROLE_NAME)
    if captain_role is None: return

    row : DictRow

    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    SELECT * FROM teams
                    WHERE team_channel_id = %s
                    """,
                    (team_thread.id,),
                )
                row = cur.fetchone()
                
    if row is None:
        await interaction.response.send_message("No team with this thread exists", ephemeral=True)
        return

    if (row["captain_id"]) is not None:
        try:
            await (await get_or_fetch_member(interaction.guild, row["captain_id"])).remove_roles(captain_role)
        except:
            pass

    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    UPDATE teams
                    SET captain_id = %s
                    WHERE team_channel_id = %s
                    """,
                    (new_captain.id, team_thread.id)
                )

    await team_thread.send(f"<@&{row['team_role_id']}> {new_captain.mention} is now your new team captain")
            
    await new_captain.add_roles(captain_role)
    await interaction.response.send_message("New captain has been set", ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_set_teamname_from_team",
    description="Sets the teamname",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_set_teamname_from_team(
    interaction: discord.Interaction,
    team_thread: discord.Thread,
    new_team_name: str
):
    if len(new_team_name) < 1 or len(new_team_name) > 32:
        await interaction.response.send_message("The team name is isn't allowed. Please make sure that the team name is between 1 and 32 characters long.", ephemeral=True)
        return

    roleID : int | None = None
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""--sql
                    UPDATE teams
                    SET team_name = %s
                    WHERE team_channel_id = %s
                    """,
                    (new_team_name, team_thread.id)
                )
                if cur.rowcount == 0: break

                cur.execute(
                    f"""--sql
                    SELECT team_role_id FROM teams
                    WHERE team_channel_id = %s
                    """,
                    (team_thread.id,)
                )
                row = cur.fetchone()
                roleID = row["team_role_id"]
                if roleID is not None:
                    roleID = int(roleID)

    
    if roleID is None:
        await interaction.response.send_message("There was no team found that uses the mentioned thread.", ephemeral=True)
        return
    await interaction.response.send_message("The team name has been changed.", ephemeral=True)

    await team_thread.edit(name=new_team_name)
    
    role = await get_or_fetch_role(interaction.guild, roleID)
    role.edit(name=new_team_name)

def has_captain_role():
    async def predicate(interaction: discord.Interaction) -> bool:
        captain_role = None

        if interaction.guild is not None:
            captain_role = discord.utils.get(interaction.guild.roles, name=CAPTAIN_ROLE_NAME)
        return (
            interaction.guild is not None
            and captain_role is not None
            and interaction.user in captain_role.members
        )

    return app_commands.check(predicate)


@has_captain_role
@bot.tree.command(
    name="captain_set_teamname",
    description="Sets the teamname",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def captain_set_teamname(
    interaction: discord.Interaction,
    new_team_name: str
):
    if len(new_team_name) < 1 or len(new_team_name) > 32:
        await interaction.response.send_message("The team name is isn't allowed. Please make sure that the team name is between 1 and 32 characters long.", ephemeral=True)
        return

    channelID : int | None = None
    roleID    : int | None = None
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""--sql
                    UPDATE teams
                    SET team_name = %s
                    WHERE captain_id = %s
                    """,
                    (new_team_name, interaction.user.id)
                )
                if cur.rowcount == 0:
                    break

                cur.execute(
                    f"""--sql
                    SELECT team_role_id, team_channel_id FROM teams
                    WHERE captain_id = %s
                    """,
                    (interaction.user.id,)
                )
                row = cur.fetchone()

                channelID = row["team_channel_id"]
                if channelID is not None:
                    channelID = int(channelID)

                roleID = row["team_role_id"]
                if roleID is not None:
                    channelID = int(channelID)

    if channelID == None or roleID is None:
        await interaction.response.send_message("There was no team found that uses the mentioned thread.", ephemeral=True)
        return
    
    interaction.response.send_message("The team name has been changed.", ephemeral=True)

    thread = await get_or_fetch_channel(channelID)
    await thread.edit(name=new_team_name)

    role = await get_or_fetch_role(interaction.guild, roleID)
    await role.edit(name=new_team_name)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_remove_user_from_team", 
    description="Removes a player from a team.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_remove_user_from_team(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()
    error = await remove_from_team(user.id)

    if error is RemoveFromTeamOutput.TEAMS_NOT_CREATED:
        await interaction.followup.send("The teams have not been created yet.", ephemeral=True)
    elif error is RemoveFromTeamOutput.PLAYER_NOT_FOUND:
        await interaction.followup.send("The player is not part of any team.", ephemeral=True)
    elif error is RemoveFromTeamOutput.NO_ERROR:
        await interaction.followup.send(
            "You successfully removed the player from the team.", ephemeral=True
        )
    else:
        await interaction.followup.send("An unknown error has been found.")

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_delete_team", 
    description="Deletes a team completely.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_delete_team(
    interaction: discord.Interaction,
    thread: discord.Thread
):
    
    row : DictRow

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                SELECT team_role_id FROM teams
                WHERE team_channel_id = %s
                """,
                (thread.id,)
            )
            row = cur.fetchone()

    if row is None:
        await interaction.response.send_message("There was no team found with the specific thread", ephemeral=True)
    
    role = await get_or_fetch_role(interaction.guild, row["team_role_id"])
    if role is None: return
    await role.delete()


    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                DELETE FROM teams
                WHERE team_channel_id = %s
                """,
                (thread.id)
            )

            cur.execute(
               """--sql
                DELETE FROM captain_poll_finish_times
                WHERE channel_discord_id = %s
                """,
                (thread.id) 
            )
            
    await thread.delete()

def is_server_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and interaction.user.id == interaction.guild.owner_id
        )

    return app_commands.check(predicate)

@is_server_owner
@bot.tree.command(
    name="owner_insert_teams_in_tournament", 
    description="Starts the tournament.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def owner_insert_teams_in_tournament(
    interaction: discord.Interaction
):
    interaction.response.defer()
    insertTeamsIntoTournamentTable()
    interaction.followup.send("Teams have successfully been created", ephemeral=True)


@is_server_owner()
@bot.tree.command(
    name="owner_reset_all", 
    description="Deletes a team completely.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_reset_all(
    interaction: discord.Interaction,
):
    interaction.response.defer()

    friend_channel = discord.utils.get(interaction.guild.text_channels, name=FRIEND_CHANNEL_NAME)
    if friend_channel is not None: 
        for thread in friend_channel.threads: await thread.delete()

    team_channel = discord.utils.get(interaction.guild.text_channels, name=TEAM_CHANNEL_NAME)
    if team_channel is not None:
        for thread in team_channel.threads: await thread.delete()


    for role_name in [SUBSTITUTE_ROLE_NAME, CAPTAIN_ROLE_NAME]:
        role = discord.utils.get(interaction.guild.roles, name=role_name)
        if role is None:
            continue

        role_data = {
            "name": role.name,
            "permissions": role.permissions,
            "colour": role.colour,
            "secondary_colour": role.secondary_colour,
            "tertiary_colour": role.tertiary_colour,
            "hoist": role.hoist,
            "display_icon": role.display_icon,
            "mentionable": role.mentionable,
            "position": role.position
        }
        await role.delete()
        new_role = await interaction.guild.create_role(
            name=role_data["name"],
            permissions=role_data["permissions"],
            colour=role_data["colour"],
            secondary_colour=role_data["secondary_colour"],
            tertiary_colour=role_data["tertiary_colour"],
            hoist=role_data["hoist"],
            display_icon=role_data["display_icon"],
            mentionable=role_data["mentionable"]
        )

        await new_role.edit(position=role_data["position"])

    substitute_role = discord.utils.get(interaction.guild.roles, name=SUBSTITUTE_ROLE_NAME)
    if substitute_role is not None:
        for member in substitute_role.members:
            await member.remove_roles(substitute_role)
    
    captain_role = discord.utils.get(interaction.guild.roles, name=CAPTAIN_ROLE_NAME)
    if captain_role is not None:
        for member in captain_role.members:
            await member.remove_roles(captain_role)

    rows : list[DictRow]
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute("TRUNCATE captain_poll_finish_times")
            cur.execute("TRUNCATE friend_threads")
            cur.execute("TRUNCATE players")

            cur.execute("SELECT team_role_id FROM teams")
            rows = cur.fetchall()

            cur.execute("TRUNCATE teams")

    for row in rows:
        role = await get_or_fetch_role(interaction.guild, row["team_role_id"])
        await role.delete()

    interaction.followup.send("All things are now resetted", ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_fill_team_with_user", 
    description="Fills a team with a user",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_fill_team_with_user(
    interaction: discord.Interaction,
    user: discord.User,
    team_thread: discord.Thread,
):
    await interaction.response.defer()

    output = ""
    team_name = ""

    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                player, _ = queries.select_player_with_discord_id(cur, user.id)
                if player is None:
                    output = "The user is not signed in"
                    break

                if not queries.check_if_teams_exist(cur):
                    output = "There are currently no teams existing"
                    break

                cur.execute(
                    """--sql
                    SELECT * FROM teams
                    WHERE %s IN (player1_id, player2_id, player3_id, player4_id, player5_id)
                    """,
                    (user.id,),
                )
                if cur.fetchone() is not None:
                    output = "User is already in a team."
                    break

                cur.execute(
                    """--sql
                    SELECT * FROM teams
                    WHERE team_channel_id = %s
                    """,
                    (team_thread.id,),
                )
                row = cur.fetchone()
                if row is None:
                    output = "No team with this thread exists"
                    break

                for column in TEAM_PLAYER_COLUMNS:
                    if row[column] != EMPTY_SLOT_ID:
                        continue

                    cur.execute(
                        f"""--sql
                        UPDATE teams
                        SET {column} = %s
                        WHERE team_channel_id = %s
                        """,
                        (user.id, team_thread.id),
                    )
                    break
                else:
                    output = "The team is already full"
                    break

                queries.set_in_players(cur, 0, "is_substitute", False, player)
                team_name = row['team_name']

    if output != "":
        interaction.followup.send(output, ephemeral=True)
        return

    await interaction.followup.send(
        f"{user.display_name} is now successfully part of the team "
        f"{team_name}",
        ephemeral=True,
    )
        
@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_start_captain_vote", 
    description="Starts a captain vote in a team manually.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_start_captain_vote(
    interaction: discord.Interaction,
    team_thread: discord.Thread
):
    
    row_count = 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""--sql
                UPDATE teams
                SET captain_id = NULL
                WHERE team_channel_id = %s
                """,
                (team_thread.id,)
            )
            row_count = cur.row_count

    if row_count == 0:
        await interaction.response.send_message("There was no team found that uses the mentioned thread.", ephemeral=True)
        return

    await interaction.response.send_message("The poll will now be created")
    await start_captain_vote(team_thread.id)

@has_captain_role
@bot.tree.command(
    name="captain_start_team_captain_vote", 
    description="Starts a captain vote in a team manually.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def captain_start_team_captain_vote(
    interaction: discord.Interaction,
):
    channelID : int | None = None
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    f"""--sql
                    UPDATE teams
                    SET captain_id = NULL
                    WHERE captain_id = %s
                    """,
                    (interaction.user.id,)
                )
                if cur.rowcount == 0:
                    break

                cur.execute(
                    f"""--sql
                    SELECT team_channel_id FROM teams
                    WHERE captain_id = %s
                    """,
                    (interaction.user.id,)
                )

                row = cur.fetchone()
                channelID = row["team_channel_id"]

                if channelID is not None:
                    channelID = int(channelID)
    
    if channelID == None:
        await interaction.response.send_message("There was no team found that uses the mentioned thread.", ephemeral=True)

    await interaction.response.send_message("The poll will now be created")
    await start_captain_vote()

# ============================================================
# UI Components
# ============================================================

class AcceptFriendshipInviteView(
    discord.ui.DynamicItem[discord.ui.Button],
    template=r"friend:(?P<action>accept|deny):(?P<sender>\d+):(?P<receiver>\d+):(?P<code>[0-9a-fA-F-]+)",
):
    """Persistent accept/deny button pair for friendship invites."""

    def __init__(self, action: str, sender_id: int, receiver_id: int, friend_code: uuid.UUID):
        super().__init__(
            discord.ui.Button(
                label="accept request" if action == "accept" else "deny request",
                style=discord.ButtonStyle.success if action == "accept" else discord.ButtonStyle.danger,
                custom_id=f"friend:{action}:{sender_id}:{receiver_id}:{friend_code}",
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
            uuid.UUID(match["code"]),
        )

    async def callback(self, interaction: discord.Interaction):
        if interaction.user.id != self.receiver_id:
            await interaction.response.send_message(
                "You don't have the rights to interact with these buttons. These "
                "buttons are for the player you invited.",
                ephemeral=True,
            )
            return

        if self.action == "accept":
            await self._accept(interaction)
        else:
            await self._deny(interaction)

    async def _accept(self, interaction: discord.Interaction):

        output = ""
        for _ in (True,):
            with pool.connection() as conn:
                with conn.cursor() as cur:
                    player, error = queries.select_player_with_discord_id(cur, self.receiver_id)

                    if error == QueryErrors.PLAYER_NOT_FOUND or player is None:
                        output = "You are not signed in."
                        break

                    if player["friend_code"] is not None:
                        output = "You already accepted the friend request (or are in another friendship)."
                        break

                    if player["is_substitute"]:
                        output = "You can't accept a friend request as a substitute."
                        break

                    queries.set_in_players(cur, 0, "friend_code", self.friend_code, player)

        if output != "":
            interaction.response.send_message(output, ephemeral=True)

        await interaction.channel.edit(name="friend group")
        await interaction.response.edit_message(view=None)
        await interaction.followup.send("Friend request accepted.")

    async def _deny(self, interaction: discord.Interaction):
        thread = interaction.channel
        if not isinstance(thread, discord.Thread):
            await interaction.response.send_message(
                "An unknown error has been found. Channel is not a thread."
            )
            return

        discord_thread_id
        with pool.connection() as conn:
            with conn.cursor() as cur:
                discord_thread_id, _, _ = queries.remove_friend_code_and_thread(
                    cur, self.friend_code
                )
        if discord_thread_id is None:
            await interaction.response.send_message(
                "This request was already handled or no longer exists.",
                ephemeral=True,
            )
            return

        await interaction.response.edit_message(view=None)

        try:
            receiver = interaction.guild.get_member(
                self.receiver_id
            ) or await get_or_fetch_member(interaction.guild, self.receiver_id)
            await thread.remove_user(receiver)
        except discord.NotFound:
            pass

        await interaction.followup.send("Friend request denied. Please leave the thread manually.")


def build_friendship_view(
    sender_id: int, receiver_id: int, friend_code: uuid.UUID
) -> discord.ui.View:
    """Build the accept/deny button view for a friendship invite thread."""
    view = discord.ui.View(timeout=None)
    view.add_item(AcceptFriendshipInviteView("accept", sender_id, receiver_id, friend_code))
    view.add_item(AcceptFriendshipInviteView("deny", sender_id, receiver_id, friend_code))
    return view


# ============================================================
# Helper Functions
# ============================================================

async def start_captain_vote_everywhere():
    guild = await get_or_fetch_guild(ALLOWED_GUILD_ID)
    
    all_threads : discord.Thread

    team_channel = discord.utils.get(guild.text_channels, name=TEAM_CHANNEL_NAME)
    if team_channel is None: return

    all_threads = team_channel.threads

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"""--sql
                UPDATE teams
                SET captain_id = NULL
                """
            )

    for thread in all_threads:
        await start_captain_vote(thread.id)

async def start_creating_teams() -> tuple[CreateTeamsOutput, str]:
    """Randomly distribute all signed-up players into teams of
    `TEAM_SIZE`, creating a role and a private thread per team.
    """

    guild = await get_or_fetch_guild(ALLOWED_GUILD_ID)


    teams_channel = discord.utils.get(guild.text_channels, name=TEAM_CHANNEL_NAME)

    if teams_channel is None:
        return (
            CreateTeamsOutput.NO_TEAM_CHANNEL,
            f"<@{guild.owner_id}> make sure that there's a \"{TEAM_CHANNEL_NAME}\" "
            "channel in your discord. Else the bot can't create threads for the "
            "friends function",
        )

    rows : list[DictRow] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if queries.check_if_teams_exist(cur):
                return (
                    CreateTeamsOutput.TEAMS_ALREADY_CREATED,
                    "The teams have already been created."
                )

            player_ids_singels = queries.get_all_player_ids(cur, filterFriendsOut=True)
            
            cur.execute(
                """--sql
                SELECT discord_id, friend_code FROM players
                WHERE is_substitute = FALSE
                AND friend_code IS NOT NULL 
                """
            )
            rows = cur.fetchall()

    player_pairs = defaultdict(list)

    for row in rows:
        player_pairs[row["friend_code"]].append(row["discord_id"])

    player_pairs = [
        pair_player__ids
        for pair_player__ids in player_pairs.values()
        if len(pair_player__ids) == 2
    ]

    random.shuffle(player_pairs)
    random.shuffle(player_ids_singels)

    player_ids = []

    pair_index = 0
    single_index = 0

    amount_of_teams = 0

    while pair_index < len(player_pairs) or single_index < len(player_ids_singels):

        group = []
        
        if pair_index < len(player_pairs):
            group.extend(player_pairs[pair_index])
            pair_index += 1

        while len(group) < 5 and single_index < len(player_ids_singels):
            group.append(player_ids_singels[single_index])
            single_index += 1

        if len(group) == 0:
            break

        group.extend([EMPTY_SLOT_ID] * (TEAM_SIZE - len(group))) 

        amount_of_teams += 1
        player_ids.extend(group)

    missing_players = 0

    players_list : list[list] = []
    team_names   : list[str] = []
    thread_ids   : list[int] = []
    role_ids     : list[int] = []

    for team_number in range(amount_of_teams):
        team_name = f"team{team_number + 1}"
        players = player_ids[team_number * TEAM_SIZE : (team_number + 1) * TEAM_SIZE]

        role = await guild.create_role(name=team_name, mentionable=True)
        thread = await teams_channel.create_thread(
            name=team_name,
            type=discord.ChannelType.private_thread,
            reason="team creation",
            invitable=False,
            auto_archive_duration=THREAD_AUTO_ARCHIVE_MINUTES,
        )

        for i, player_id in enumerate(players):
            if player_id == EMPTY_SLOT_ID:

                await teams_channel.send(f"{team_name} needs a new member because the team is not full.")

                missing_players += 1
                continue
            try:
                member = await get_or_fetch_member(guild, player_id)
            except discord.NotFound:
                players[i] = EMPTY_SLOT_ID
                await teams_channel.send(f"{team_name} needs a new member because <@{player_id}> is not in the server anymore.")

                missing_players += 1
                continue

            await member.add_roles(role)
            await thread.add_user(member)

        await thread.send(f"Welcome {team_name}")


        team_names.append(team_name)
        thread_ids.append(thread.id)
        role_ids.append(role.id)
        players_list.append(players)


    with pool.connection() as conn:
        with conn.cursor() as cur:
            for team_name, thread_id, role_id, players in zip(team_names, thread_ids, role_ids, players_list):
                queries.insert_team(cur, team_name, thread_id, role_id, players)

    if missing_players != 0:
                await teams_channel.send(
                    f"There are {missing_players} missing players that need to be "
                    f"filled in team{amount_of_teams}"
                )

    return CreateTeamsOutput.NO_ERROR, f"All {amount_of_teams} teams have been created."


async def remove_from_team(
    user_id: int
) -> RemoveFromTeamOutput:
    """Remove a player from their team (if any), clean up their role,
    and clear the captain slot if they were the captain.
    """

    row : DictRow
    with pool.connection() as conn:
        with conn.cursor() as cur:
            if not queries.check_if_teams_exist(cur):
                return RemoveFromTeamOutput.TEAMS_NOT_CREATED

            cur.execute(
                """--sql
                SELECT * FROM teams
                WHERE %s IN (
                    captain_id, player1_id, player2_id, player3_id, player4_id, player5_id
                )
                """,
                (user_id,),
            )
            row = cur.fetchone()
            if row is None:
                return RemoveFromTeamOutput.PLAYER_NOT_FOUND

            for column in TEAM_PLAYER_COLUMNS:
                if row[column] != user_id:
                    continue

                cur.execute(
                    f"""--sql
                    UPDATE teams
                    SET {column} = %s
                    WHERE team_id = %s
                    """,
                    (EMPTY_SLOT_ID, row["team_id"]),
                )
                break
    thread = await get_or_fetch_channel(row["team_channel_id"])
    if not isinstance(thread, discord.Thread):
        return RemoveFromTeamOutput.UNKNOWN_ERROR
    guild = thread.guild

    try:
        member = await guild.fetch_member(user_id)
        await thread.remove_user(await get_or_fetch_member(guild, user_id))
        teamRole = await get_or_fetch_role(guild, row["team_role_id"])
        captain_role = discord.utils.get(guild.roles, name=CAPTAIN_ROLE_NAME)

        await member.remove_roles(teamRole)
        if (captain_role is not None): 
            await member.remove_roles(captain_role)
    except discord.NotFound:
        pass

    if row["captain_id"] == user_id:
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    UPDATE teams
                    SET captain_id = NULL
                    WHERE captain_id = %s
                    """,
                    (user_id,)
                )
        
        await thread.send(f"<@&{row['team_role_id']}> there is currently no captain")
        await start_captain_vote(thread.id)

    await thread.parent.send(f"{row['team_name']} needs a new member because {(await get_or_fetch_user(user_id)).display_name} is not in the team anymore.")
    return RemoveFromTeamOutput.NO_ERROR

async def start_captain_vote(
    team_channel_id: int
):
    
    thread = await get_or_fetch_channel(team_channel_id)
    if not isinstance(thread, discord.Thread):
        return

    playerNames = list(["","","","",""])
    playerIDs   = list([0,0,0,0,0])

    pollMessage = "@here who do you want to have as your captain?"

    row : DictRow
    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    SELECT * FROM teams
                    WHERE team_channel_id = %s
                    """,
                    (team_channel_id,)
                )
                row = cur.fetchone()

                if row is None: return

                if (row["team_role_id"] is not None):
                    pollMessage = f"<@&{row['team_role_id']}> who do you want to have as your captain?"

                if row["captain_id"] is not None:
                    cur.execute(
                        """--sql
                        UPDATE teams
                        SET captain_id = NULL
                        WHERE captain_id = %s
                        """,
                        (row["captain_id"],)
                    )
                
    for i, column in enumerate(TEAM_PLAYER_COLUMNS):
        id = row[column]
        if id == 0: continue
        try:
            user = await get_or_fetch_member(thread.guild, id)
            if user is None: continue
            username = f"{user.display_name} ({user.name})"
            playerNames[i] = username
            playerIDs[i] = id
        except discord.HTTPException:
            pass

    filteredPlayerNames = list(filter(None, playerNames))
    filteredPlayerIDs = list(filter(None, playerIDs))

    poll = discord.Poll(
        question = pollMessage,
        duration = 24
    ) 

    for playerName in filteredPlayerNames:
        poll.add_answer(text=playerName)

    players = {
        memberName: memberID
        for memberName, memberID in zip(filteredPlayerNames, filteredPlayerIDs)
    }

    message = await thread.send(poll=poll)
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                INSERT INTO captain_poll_finish_times(
                    finish_time,
                    poll_discord_id,
                    channel_discord_id,
                    players
                )
                VALUES (%s, %s, %s, %s)
                """,
                (poll.expires_at, message.id, thread.id, json.dumps(players, skipkeys=True))
            )

def load_captain_polls():

    polls : list[DictRow] = []
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                SELECT finish_time, poll_discord_id, channel_discord_id, players
                FROM captain_poll_finish_times
                """
            )

            polls = cur.fetchall()

    for poll in polls:
        asyncio.create_task(wait_for_poll_finish(poll))
                    
async def wait_for_poll_finish(poll : DictRow):

    delay = (poll["finish_time"] - datetime.now(timezone.utc)).total_seconds()

    if delay > 0:
        await asyncio.sleep(delay)

    await handle_finished_poll(
        poll["poll_discord_id"],
        poll["channel_discord_id"],
        poll["players"]
    )

async def handle_finished_poll(
    poll_discord_id : int,
    channel_discord_id : int,
    players : dict[str, int]
):
    channel = bot.get_channel(channel_discord_id)

    if channel is None:
        return

    message = await get_or_fetch_message(guild, poll_discord_id)

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                DELETE FROM captain_poll_finish_times
                WHERE poll_discord_id = %s
                """,
                (poll_discord_id,)
            )
    poll = message.poll
    if not isinstance(poll, discord.Poll): return
    
    if not poll.is_finalised:
        await poll.end()

    answers = poll.answers

    sorted_answers = sorted(
        enumerate(answers),
        key = lambda answer: (-answer[1].vote_count, answer[0]),
    )

    guild = await get_or_fetch_guild(ALLOWED_GUILD_ID)
    captain_role = discord.utils.get(guild.roles, name=CAPTAIN_ROLE_NAME)

    for sorted_answer in sorted_answers:
        playerID  = players[sorted_answer[1].text]
        try:
            member = await get_or_fetch_member(guild, playerID)
        except:
            continue

        await member.add_roles(captain_role)
    
        row_count = 0
        with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    UPDATE teams
                    SET captain_id = %s
                    WHERE team_channel_id = %s
                    """,
                    (playerID, channel_discord_id)
                )
                row_count = cur.rowcount

        if cur.rowcount == 0:
            await member.remove_roles(captain_role)
            continue

        break

def insertTeamsIntoTournamentTable():

    names : list[str] = []

    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    SELECT team_name FROM teams
                    """
                )

                rows = cur.fetchall()
                names = [row["team_name"] for row in rows]

    with challonge.Client(user=CHALLONGE_USER, api_key=CHALLONGE_API_KEY, timezone="UTC") as client:
        tournament =  client.tournaments.show(TOURNAMENT_ID)
        client.participants.bulk_add(tournament.id, names)
        client.tournaments.start(tournament.id)


bot.run(TOKEN)