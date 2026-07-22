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
import pycountry
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from psycopg.errors import UniqueViolation
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

ALLOWED_GUILD_ID = 1519693560268455990
TEAM_SIZE = 5
EMPTY_SLOT_ID = 0
FRIEND_CHANNEL_NAME = "friends"
TEAM_CHANNEL_NAME = "teams"
CAPTAIN_ROLE_NAME = "Captain"
SUBSTITUTE_ROLE_NAME = "Substitute"
THREAD_AUTO_ARCHIVE_MINUTES = 10080
TEAM_PLAYER_COLUMNS = (
    "player1_id",
    "player2_id",
    "player3_id",
    "player4_id",
    "player5_id",
)

# TODO: Needs to be replaced with team images
# TEAM_PLACEHOLDER_IMAGE_URL = "https://i.imgur.com/XwQTC7b.png"

pool = ConnectionPool(
    DATABASE_URL,
    min_size=1,
    max_size=5,
    max_lifetime=1800,
    max_idle=300,
    check=ConnectionPool.check_connection,
    kwargs={"row_factory": dict_row}
)

pool.wait()

scheduled_tasks = []

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

    with pool.connection() as conn:
        with conn.cursor() as cur:
            if queries.check_if_tournament_started(cur):
                return

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

    row : DictRow
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                SELECT team_creation_time, captain_vote_time, tournament_start_time FROM key_value
                """
            )
            row = cur.fetchone()


    # needs to be changed
    teamTime : datetime = row["team_creation_time"]
    captainTime : datetime = row["captain_vote_time"]
    tournamentTime : datetime = row["tournament_start_time"]
    for task in scheduled_tasks:
        task.cancel()

    scheduled_tasks.clear()

    scheduled_tasks.append(asyncio.create_task(team_creation_run_at(teamTime)))
    scheduled_tasks.append(asyncio.create_task(captain_vote_run_at(captainTime)))
    scheduled_tasks.append(asyncio.create_task(start_tournament(tournamentTime)))

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
        with pool.connection() as conn:
            with conn.cursor() as cur:
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
        await interaction.user.add_roles(substitute_role)

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
                            "You can't sign out if you are already part of a team. If you want to sign out, you have to leave the team"
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
    await interaction.user.remove_roles(substitute_role)
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

@bot.tree.command(
    name="user_profile",
    description="Shows a user profile card, including team info if applicable.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
@app_commands.describe(user="Whose profile to show (defaults to yourself)")
async def user_profile(interaction: discord.Interaction, user: discord.Member | None = None):
    target = user or interaction.user

    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, _ = queries.select_player_with_discord_id(cur, target.id)

            friend_player = None
            if player is not None and player["friend_code"] is not None:
                cur.execute(
                    """--sql
                    SELECT * FROM players
                    WHERE friend_code = %s AND discord_id != %s
                    """,
                    (player["friend_code"], player["discord_id"]),
                )
                friend_player = cur.fetchone()

            cur.execute(
                """--sql
                SELECT * FROM teams
                WHERE %s IN (player1_id, player2_id, player3_id, player4_id, player5_id)
                """,
                (target.id,),
            )
            team_row = cur.fetchone()

    friend_member = (
        await get_or_fetch_member(interaction.guild, friend_player["discord_id"])
        if friend_player
        else None
    )
    captain_member = (
        await get_or_fetch_member(interaction.guild, team_row["captain_id"])
        if team_row and team_row["captain_id"]
        else None
    )

    embed = await build_user_profile_embed(target, player, friend_member, team_row, captain_member)
    view = ProfileView(
        member=target,
        team_channel_id=team_row["team_channel_id"] if team_row else None,
    )

    await interaction.response.send_message(embed=embed, view=view, ephemeral=True)

@bot.tree.command(
    name="team_profile",
    description="Shows a team profile card.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def team_profile(interaction: discord.Interaction, team_role: discord.Role):

    guild = interaction.guild

    members_rows : list[tuple[int, DictRow | None]] = []


    if team_role is None:
        await interaction.response.send_message("No role found", ephemeral=True)
        return
    
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                SELECT * FROM teams
                WHERE team_role_id = %s
                """,
                (team_role.id,),
            )
            team_row : DictRow | None = cur.fetchone()
            if team_row is not None:
                for column in TEAM_PLAYER_COLUMNS:
                    user_id : int = team_row[column]

                    if user_id is None or user_id == EMPTY_SLOT_ID:
                        continue

                    row, _ = queries.select_player_with_discord_id(cur, user_id) 
                    member_row = (user_id, row)
                    members_rows.append(member_row)

    if team_row is None:
        await interaction.response.send_message("No team with this role was found", ephemeral=True)
        return

    resolved_members: list[tuple[discord.Member, DictRow | None]] = []
    for player_id, player_row in members_rows:
        try:
            resolved_member = await get_or_fetch_member(interaction.guild, player_id)
        except discord.NotFound:
            continue
        resolved_members.append((resolved_member, player_row))

    embeds = await build_team_profile_embeds(guild, team_row, resolved_members)
    view = TeamView(
        team_channel_id=team_row["team_channel_id"] if team_row else None,
        members=[member for member, _ in resolved_members]
    )

    await interaction.response.send_message(embeds=embeds, view=view, ephemeral=True)


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
        await interaction.response.send_message(output, ephemeral=True)
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
        await interaction.response.send_message(output, ephemeral=True)
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

@bot.tree.command(
    name="tournament_link",
    description="Gets the link to the tournament",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def tournament_link(
    interaction: discord.Interaction,
):
    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                """--sql
                SELECT "tournament_url" FROM key_value
                """
                )
                row = cur.fetchone()
    await interaction.response.send_message(f"https://challonge.com/{row["tournament_url"]}")

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_select_players",
    description="Randomly select participants from message reactions.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
@app_commands.describe(
    duration="Example: 5m, 1h30m, 2d4h10s",
    members="Number of participants to select",
    text="Title of the selection"
)
async def admin_select_players(
    interaction: discord.Interaction,
    duration: str,
    members: app_commands.Range[int, 1, TEAM_SIZE],
    text: str,
    role: discord.Role = None
):
    if role is None:
        role = discord.utils.get(interaction.guild.roles, name=SUBSTITUTE_ROLE_NAME)

    try:
        duration_seconds = parse_duration(duration)
    except ValueError:
        await interaction.response.send_message(
            "Invalid duration format.",
            ephemeral=True
        )
        return

    embed = discord.Embed(
        title=text.upper(),
        color=discord.Color.blurple()
    )

    embed.description = (
        f"**Spots:** {members}\n\n"
        f"React with any emoji to participate.\n\n"
        f"**Time Remaining:** `{duration}`"
    )

    embed.set_footer(
        text="Each user is counted only once."
    )

    await interaction.response.send_message(
        content=role.mention,
        embed=embed,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )

    message = await interaction.original_response()

    remaining = duration_seconds

    while remaining > 0:
        embed.description = (
            f"**Participants:** {members}\n\n"
            f"React with any emoji to participate.\n\n"
            f"**Time Remaining:** `{format_duration(remaining)}`"
        )

        await message.edit(embed=embed)

        sleep_time = min(1, remaining)
        await asyncio.sleep(sleep_time)
        remaining -= sleep_time

    message = await message.channel.fetch_message(message.id)

    participants = set()

    for reaction in message.reactions:
        async for user in reaction.users():
            if not user.bot:
                participants.add(user.id)

    if not participants:
        await message.reply("No participants joined.")
        return

    winners = random.sample(
        list(participants),
        min(members, len(participants))
    )

    mentions = " ".join(f"<@{user_id}>" for user_id in winners)

    result = discord.Embed(
        title="Selection Complete",
        color=discord.Color.green()
    )

    result.add_field(
        name="Selected Participants",
        value=mentions,
        inline=False
    )

    amountOfWinners = len(winners)

    result.set_footer(
        text=f"Selected {amountOfWinners} out of {len(participants)} participants."
    )

    await message.reply(
        content=mentions,
        embed=result,
        allowed_mentions=discord.AllowedMentions(users=True)
    )
    
    if (amountOfWinners < members):
        await message.channel.send(f"There were not enough participants to fill every spot. There are still {members - amountOfWinners} spots that need to be filled.")

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_start_create_teams", 
    description="Creates the teams for the tournament",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_start_create_teams(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    _, message_text = await start_creating_teams()
    await interaction.followup.send(message_text)


@bot.tree.command(
    name="leave_team",
    description="Leaves your current team. ONLY THE MODERATORS CAN ASSIGN YOU BACK TO THE TEAM.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def leave_team(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    error = await remove_from_team(interaction.user.id)

    if error == RemoveFromTeamOutput.TEAMS_NOT_CREATED:
        await interaction.followup.send("The teams have not been created yet.", ephemeral=True)
    elif error == RemoveFromTeamOutput.PLAYER_NOT_FOUND:
        await interaction.followup.send("You are not part of a team.", ephemeral=True)
    elif error == RemoveFromTeamOutput.NO_ERROR:
        await interaction.followup.send("You successfully left your team. User /sign_out to sign out or /change_substitute to become a substitute", ephemeral=True)
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
    team_role: discord.Role,
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
                    WHERE team_role_id = %s
                    """,
                    (team_role.id,),
                )
                row = cur.fetchone()
                
    if row is None:
        await interaction.response.send_message("No team with this role exists", ephemeral=True)
        return

    if (row["captain_id"]) is not None:
        try:
            await (await get_or_fetch_member(interaction.guild, row["captain_id"])).remove_roles(captain_role)
        except:
            pass

    team_channel_id = row["team_channel_id"]
    team_thread = await get_or_fetch_channel(team_channel_id)

    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    UPDATE teams
                    SET captain_id = %s
                    WHERE team_role_id = %s
                    """,
                    (new_captain.id, team_role.id)
                )

    await team_thread.send(f"{team_role.mention} {new_captain.mention} is now your new team captain")
            
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
    team_role: discord.Role,
    new_team_name: str
):
    if len(new_team_name) < 1 or len(new_team_name) > 32:
        await interaction.response.send_message("The team name is isn't allowed. Please make sure that the team name is between 1 and 32 characters long.", ephemeral=True)
        return

    output = "The team name has been changed."
    thread_id : int | None = None
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if queries.check_if_tournament_started(cur):
                    output = "The tournament already started."
                    break
                try:
                    cur.execute(
                        """--sql
                        UPDATE teams
                        SET team_name = %s
                        WHERE team_role_id = %s
                        """,
                        (new_team_name, team_role.id)
                    )
                except UniqueViolation:
                        output = "The name already exsts for another team."
                        break
                
                if cur.rowcount == 0: 
                    output = "There was no team found that uses the mentioned role."
                    break

                cur.execute(
                    """--sql
                    SELECT team_channel_id FROM teams
                    WHERE team_role_id = %s
                    """,
                    (team_role.id,)
                )
                row = cur.fetchone()
                thread_id = row["team_channel_id"]
                if thread_id is None:
                    output = "There was no thread found that is linked to the mentioned role."
    
    
    if thread_id is None:
        await interaction.response.send_message(output, ephemeral=True)
        return
    
    team_thread = await get_or_fetch_channel(thread_id)
    await team_thread.edit(name=new_team_name)
    
    await team_role.edit(name=new_team_name)

    await interaction.response.send_message(output, ephemeral=True)

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


@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_set_team_picture",
    description="Sets the team picture",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_set_team_picture(
    interaction: discord.Interaction,
    team_role: discord.Role,
    team_picture: str
):
    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    UPDATE teams
                    SET team_picture = %s
                    WHERE team_role_id = %s
                    """,
                    (team_picture, team_role.id)
                )
                row_count = cur.rowcount

    if row_count == 0:
        await interaction.response.send_message("No team has been found that uses this role.", ephemeral=True)
        return
    await interaction.response.send_message("The team picture has been changed. Please check with /team_profile if the image works correctly.", ephemeral=True)


@has_captain_role()
@bot.tree.command(
    name="captain_set_team_picture",
    description="Sets the team picture",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def captain_set_team_picture(
    interaction: discord.Interaction,
    team_picture: str
):
    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    UPDATE teams
                    SET team_picture = %s
                    WHERE captain_id = %s
                    """,
                    (team_picture, interaction.user.id)
                )
                row_count = cur.rowcount

    if row_count == 0:
        await interaction.response.send_message("An unknown error occured", ephemeral=True)
        return
    await interaction.response.send_message("The team picture has been changed. Please check with /team_profile if the image works correctly.")

                

@has_captain_role()
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

    output = "The team name has been changed."
    channel_id : int | None = None
    role_id    : int | None = None
    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if queries.check_if_tournament_started(cur):
                    output = "The tournament already started."
                    role_id = 0
                    break

                try: 
                    cur.execute(
                        f"""--sql
                        UPDATE teams
                        SET team_name = %s
                        WHERE captain_id = %s
                        """,
                        (new_team_name, interaction.user.id)
                    )
                except UniqueViolation:
                    output = "The name already exsts for another team."
                    break

                if cur.rowcount == 0:
                    output = "There was no team found that uses the mentioned thread."
                    break

                cur.execute(
                    """--sql
                    SELECT team_role_id, team_channel_id FROM teams
                    WHERE captain_id = %s
                    """,
                    (interaction.user.id,)
                )
                row = cur.fetchone()

                channel_id : int = row["team_channel_id"]

                role_id : int = row["team_role_id"]
                if role_id is None:
                    output = "There was no role found that is linked to the mentioned thread."

    if channel_id is None or role_id is None:
        await interaction.response.send_message(output, ephemeral=True)
        return

    thread = await get_or_fetch_channel(channel_id)
    await thread.edit(name=new_team_name)

    role = await get_or_fetch_role(interaction.guild, roleID)
    await role.edit(name=new_team_name)

    await interaction.response.send_message(output, ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_remove_user_from_team", 
    description="Removes a player from a team. It is advisable to let the player know why the removal happened.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_remove_user_from_team(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer(ephemeral=True)
    error = await remove_from_team(user.id)

    if error == RemoveFromTeamOutput.TEAMS_NOT_CREATED:
        await interaction.followup.send("The teams have not been created yet.", ephemeral=True)
    elif error == RemoveFromTeamOutput.PLAYER_NOT_FOUND:
        await interaction.followup.send("The player is not part of any team.", ephemeral=True)
    elif error == RemoveFromTeamOutput.NO_ERROR:
        await interaction.followup.send(
            "You successfully removed the player from the team.", ephemeral=True
        )
    else:
        await interaction.followup.send("An unknown error has been found.", ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_delete_team", 
    description="Deletes a team completely.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_delete_team(
    interaction: discord.Interaction,
    role: discord.Role
):
    tournament_started = False

    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:
                if queries.check_if_tournament_started(cur):
                    tournament_started = True
                    break


                cur.execute(
                    """--sql
                    SELECT team_channel_id, captain_id FROM teams
                    WHERE team_role_id = %s
                    """,
                    (role.id,)
                )
                row = cur.fetchone()

    if row["captain_id"] is not None:
        try:
            user = await get_or_fetch_member(interaction.guild, row["captain_id"])
            captain_role = discord.utils.get(interaction.guild.roles, name=CAPTAIN_ROLE_NAME)
            await user.remove_roles(captain_role)
        except: pass

    if tournament_started:
        await interaction.response.send_message("The tournament already started.", ephemeral=True)
        return

    if row is None:
        await interaction.response.send_message("There was no team found with the specific role", ephemeral=True)
        return

    try:
        await role.delete()
    except: 
        pass

    thread_id = row["team_channel_id"]

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                DELETE FROM teams
                WHERE team_channel_id = %s
                """,
                (thread_id,)
            )

            cur.execute(
               """--sql
                DELETE FROM captain_poll_finish_times
                WHERE channel_discord_id = %s
                """,
                (thread_id,) 
            )
            
    await interaction.response.send_message("The team has successfully been deleted.", ephemeral=True)
    await (await get_or_fetch_channel(thread_id)).delete()

def is_server_owner():
    async def predicate(interaction: discord.Interaction) -> bool:
        return (
            interaction.guild is not None
            and interaction.user.id == interaction.guild.owner_id
        )

    return app_commands.check(predicate)

@is_server_owner()
@bot.tree.command(
    name="owner_insert_teams_in_tournament", 
    description="Starts the tournament.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def owner_insert_teams_in_tournament(
    interaction: discord.Interaction
):
    await interaction.response.defer(ephemeral=True)
    insertTeamsIntoTournamentTable()
    await interaction.followup.send("Teams have successfully been created", ephemeral=True)


@is_server_owner()
@bot.tree.command(
    name="owner_reset_all", 
    description="Deletes a team completely.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_reset_all(
    interaction: discord.Interaction,
):
    await interaction.response.defer(ephemeral=True)

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

    await interaction.followup.send("All things are now resetted", ephemeral=True)

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(
    name="admin_fill_team_with_user", 
    description="Fills a team with a user",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def admin_fill_team_with_user(
    interaction: discord.Interaction,
    user: discord.User,
    team_role: discord.Role,
):
    await interaction.response.defer(ephemeral=True)

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
                    WHERE team_role_id = %s
                    """,
                    (team_role.id,),
                )
                row = cur.fetchone()
                if row is None:
                    output = "No team with this role exists"
                    break

                for column in TEAM_PLAYER_COLUMNS:
                    if row[column] != EMPTY_SLOT_ID:
                        continue

                    cur.execute(
                        f"""--sql
                        UPDATE teams
                        SET {column} = %s
                        WHERE team_role_id = %s
                        """,
                        (user.id, team_role.id),
                    )
                    break
                else:
                    output = "The team is already full"
                    break

                queries.set_in_players(cur, 0, "is_substitute", False, player)
                team_name = row['team_name']

    if player["is_substitute"]:
        substitute_role = discord.utils.get(interaction.guild.roles, name=SUBSTITUTE_ROLE_NAME)
        if substitute_role is None: return
        await user.remove_roles(substitute_role)


    if output != "":
        await interaction.followup.send(output, ephemeral=True)
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
    team_role: discord.Role
):
    row_count = 0
    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                UPDATE teams
                SET captain_id = NULL
                WHERE team_role_id = %s
                """,
                (team_role.id,)
            )
            row_count = cur.rowcount

            if row_count > 0:
                cur.execute(
                    """--sql
                    SELECT team_channel_id FROM teams
                    WHERE team_role_id = %s 
                    """,
                    (team_role.id,)  
                )
                row = cur.fetchone()

    if row_count == 0:
        await interaction.response.send_message("There was no team found that uses the mentioned role.", ephemeral=True)
        return

    await interaction.response.send_message(await start_captain_vote(row["team_channel_id"]), ephemeral=True)
    

@has_captain_role()
@bot.tree.command(
    name="captain_start_team_captain_vote", 
    description="Starts a captain vote in a team manually.",
    guild=discord.Object(id=ALLOWED_GUILD_ID)
)
async def captain_start_team_captain_vote(
    interaction: discord.Interaction,
):
    channel_id : int | None = None

    for _ in (True,):
        with pool.connection() as conn:
            with conn.cursor() as cur:

                cur.execute(
                    f"""--sql
                    SELECT team_channel_id FROM teams
                    WHERE captain_id = %s
                    """,
                    (interaction.user.id,)
                )

                row = cur.fetchone()
                channel_id = row["team_channel_id"]

                cur.execute(
                    """--sql
                    UPDATE teams
                    SET captain_id = NULL
                    WHERE captain_id = %s
                    """,
                    (interaction.user.id,)
                )
                if cur.rowcount == 0:
                    break
    
    if channel_id == None:
        await interaction.response.send_message("There was no team found that uses the mentioned thread.", ephemeral=True)

    await interaction.response.send_message(await start_captain_vote(channel_id), ephemeral=True)

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
            await interaction.response.send_message(output, ephemeral=True)
            return

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


async def build_user_profile_embed(
    member: discord.Member,
    player: DictRow | None,
    friend_member: discord.Member | None,
    team_row: DictRow | None,
    captain_member: discord.Member | None,
) -> discord.Embed:
    flag, _ = get_member_country(member)
    signed_in = player is not None
    role_category = get_role_category(player)

    title = f"{flag} {member.display_name}" if flag else member.display_name
    steam_name = player["username_steam"] if player else None
    all_roles = [role.name for role in member.roles if role.name != "@everyone"]

    embed = discord.Embed(
        title=title,
        color=discord.Color.green() if signed_in else discord.Color.red(),
    )
    embed.set_thumbnail(url=member.display_avatar.url)

    embed.description = (
        f"**Steam:** {steam_name if steam_name else '*No Steam name set*'}\n\n"
        f"**Status:** {'signed in' if signed_in else 'signed out'}\n\n"
        f"**Role:** {role_category}\n\n"
        f"**Friend:** {friend_member.mention if friend_member else '*No Friend*'}\n\n"
        f"**Team:** {team_row['team_name'] if team_row else '*No Team*'} | "
        f"**Captain:** {captain_member.mention if captain_member else '*No Captain*'}\n\n"
        f"**Roles:** {' | '.join(all_roles) if all_roles else '—'}"
    )
    return embed


async def build_team_profile_embeds(
    guild: discord.Guild,
    team_row: DictRow,
    member_rows: list[tuple[discord.Member, DictRow | None]],
) -> list[discord.Embed]:
    """Returns a list of embeds: one header embed for the team, plus one
    embed per filled team slot. The flag is plain text inside the
    author name — it is never part of a button."""
    captain_member = None
    if team_row["captain_id"] is not None:
        captain_member = await get_or_fetch_member(guild, team_row["captain_id"])
    captain_flag = get_member_country(captain_member)[0] if captain_member else None

    header = discord.Embed(
        title=team_row["team_name"],
        description=(
            f"**Captain:** "
            f"{(captain_flag + ' ') if captain_flag else ''}"
            f"{captain_member.mention if captain_member else '*No Captain*'}"
        ),
        color=discord.Color.blurple(),
    )
    header.set_thumbnail(url=team_row["team_picture"])

    embeds = [header]

    for member, player in member_rows:
        flag, _ = get_member_country(member)
        steam_name = player["username_steam"] if player else None

        member_embed = discord.Embed(
            description=f"**Steam:** {steam_name if steam_name else '*No Steam name set*'}",
            color=discord.Color.blurple(),
        )
        member_embed.set_author(
            name=f"{member.display_name} {flag}" if flag else member.display_name,
            icon_url=member.display_avatar.url,
        )
        embeds.append(member_embed)

    return embeds


async def show_member_profile(interaction: discord.Interaction, member: discord.Member):
    """Edits the current message to show `member`'s profile card."""
    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, _ = queries.select_player_with_discord_id(cur, member.id)
            friend_player = None

            if player is not None and player["friend_code"] is not None:
                cur.execute(
                    """--sql
                    SELECT * FROM players
                    WHERE friend_code = %s AND discord_id != %s
                    """,
                    (player["friend_code"], player["discord_id"]),
                )
            friend_player = cur.fetchone()

            cur.execute(
                """--sql
                SELECT * FROM teams
                WHERE %s IN (player1_id, player2_id, player3_id, player4_id, player5_id)
                """,
                (member.id,),
            )
            team_row = cur.fetchone()

    friend_member = (
        await get_or_fetch_member(interaction.guild, friend_player["discord_id"])
        if friend_player
        else None
    )
    captain_member = (
        await get_or_fetch_member(interaction.guild, team_row["captain_id"])
        if team_row and team_row["captain_id"]
        else None
    )

    embed = await build_user_profile_embed(member, player, friend_member, team_row, captain_member)
    view = ProfileView(
        member=member,
        team_channel_id=team_row["team_channel_id"] if team_row else None,
    )
    await interaction.response.edit_message(embeds=[embed], view=view)


async def show_team_profile(interaction: discord.Interaction, team_channel_id: int):
    """Edits the current message to show the team's profile card."""
    team_row: DictRow | None = None
    member_rows: list[tuple[int, DictRow | None]] = []

    with pool.connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """--sql
                SELECT * FROM teams WHERE team_channel_id = %s
                """,
                (team_channel_id,),
            )
            team_row = cur.fetchone()

            if team_row is not None:
                for column in TEAM_PLAYER_COLUMNS:
                    player_id = team_row[column]
                    if player_id is None or player_id == EMPTY_SLOT_ID:
                        continue
                    player_row, _ = queries.select_player_with_discord_id(cur, player_id)
                    member_rows.append((player_id, player_row))

    if team_row is None:
        await interaction.response.send_message(
            "This team no longer exists.", ephemeral=True
        )
        return

    resolved_members: list[tuple[discord.Member, DictRow | None]] = []
    for player_id, player_row in member_rows:
        try:
            resolved_member = await get_or_fetch_member(interaction.guild, player_id)
        except discord.NotFound:
            continue
        resolved_members.append((resolved_member, player_row))

    embeds = await build_team_profile_embeds(interaction.guild, team_row, resolved_members)
    view = TeamView(
        team_channel_id=team_channel_id,
        members=[member for member, _ in resolved_members],
    )
    await interaction.response.edit_message(embeds=embeds, view=view)


class ProfileView(discord.ui.View):
    """Profile card for a single member. Anyone in the server may browse
    it — clicking is not restricted to whoever ran the command."""

    def __init__(self, *, member: discord.Member, team_channel_id: int | None):
        super().__init__(timeout=180)
        self.member = member
        self.team_channel_id = team_channel_id

        if team_channel_id is None:
            self.view_team.disabled = True
            self.view_team.label = "No Team"

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True

    @discord.ui.button(label="View Team", style=discord.ButtonStyle.primary, emoji="🛡️")
    async def view_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await show_team_profile(interaction, self.team_channel_id)


class MemberProfileButton(discord.ui.Button):
    """One button per team slot, jumping straight to that member's
    profile card. The country flag is intentionally NOT used here —
    it stays plain text inside the embed. This button uses a neutral
    person icon instead."""

    def __init__(self, member: discord.Member):
        super().__init__(
            label=member.display_name[:80],
            style=discord.ButtonStyle.secondary,
            emoji="👤",
        )
        self.member = member

    async def callback(self, interaction: discord.Interaction):
        await show_member_profile(interaction, self.member)


class TeamView(discord.ui.View):
    """Team card with exactly one button per filled slot, opening that
    member's profile. Anyone may click."""

    def __init__(self, *, team_channel_id: int, members: list[discord.Member]):
        super().__init__(timeout=180)
        self.team_channel_id = team_channel_id

        for member in members:
            self.add_item(MemberProfileButton(member))

    async def on_timeout(self) -> None:
        for item in self.children:
            item.disabled = True




# ============================================================
# Helper Functions
# ============================================================

def parse_duration(duration: str) -> int:
    matches = re.findall(r"^(?:(\d+)d)?(?:(\d+)h)?(?:(\d+)m)?(?:(\d+)s)?$", duration.lower())

    if not matches:
        raise ValueError("Invalid duration.")

    total = 0

    units = {
        "d": 86400,
        "h": 3600,
        "m": 60,
        "s": 1
    }

    for value, unit in matches:
        total += int(value) * units[unit]

    if total <= 0:
        raise ValueError("Duration must be greater than 0.")

    return total

def format_duration(seconds: int) -> str:
    days, seconds = divmod(seconds, 86400)
    hours, seconds = divmod(seconds, 3600)
    minutes, seconds = divmod(seconds, 60)

    parts = []

    if days:
        parts.append(f"{days}d")
    if hours:
        parts.append(f"{hours}h")
    if minutes:
        parts.append(f"{minutes}m")
    if seconds or not parts:
        parts.append(f"{seconds}s")

    return " ".join(parts)

def get_role_category(player: DictRow | None) -> str:
    """Player | Substitute | Viewer, based on sign-in state."""
    if player is None:
        return "Viewer"
    return "Substitute" if player["is_substitute"] else "Player"

def get_member_country(member: discord.Member) -> tuple[str | None, str | None]:
    """Returns (flag_emoji, role_name) for the first country role a member
    has, or (None, None) if they have none."""
    for role in member.roles:
        flag = role_name_to_flag_emoji(role.name)
        if flag is not None:
            return flag, role.name
    return None, None

NON_COUNTRY_ROLE_NAMES = {
    "@everyone",
    CAPTAIN_ROLE_NAME.lower(),
    SUBSTITUTE_ROLE_NAME.lower(),
}
TEAM_ROLE_NAME_PATTERN = re.compile(r"^team\d+$", re.IGNORECASE)

COUNTRY_ROLE_OVERRIDES: dict[str, str] = {
    "uk": "GB",
    "united kingdom": "GB",
    "great britain": "GB",
    "britain": "GB",
    "england": "GB",
    "vatican": "VA",
    "vatican city": "VA",
    "usa": "US",
    "us": "US",
    "america": "US",
    "united states": "US",
    "south korea": "KR",
    "north korea": "KP",
    "russia": "RU",
    "ivory coast": "CI",
    "czech republic": "CZ",
    "czechia": "CZ",
    "netherlands": "NL",
    "holland": "NL",
    "laos": "LA",
    "syria": "SY",
    "iran": "IR",
    "bolivia": "BO",
    "venezuela": "VE",
    "moldova": "MD",
    "macedonia": "MK",
    "north macedonia": "MK",
}


def _alpha2_to_flag_emoji(alpha_2: str) -> str:
    """Turns an ISO 3166-1 alpha-2 code (e.g. 'DE') into a flag emoji (🇩🇪)."""
    return "".join(chr(0x1F1E6 + ord(char) - ord("A")) for char in alpha_2.upper())


def role_name_to_flag_emoji(role_name: str) -> str | None:
    """Resolves a Discord role name (English country name, e.g. 'Germany',
    'UK', 'Vatican') to its flag emoji. Returns None if the role name does
    not look like a country.
    """
    key = role_name.strip().lower()

    if not key or key in NON_COUNTRY_ROLE_NAMES or TEAM_ROLE_NAME_PATTERN.match(key):
        return None

    alpha_2 = COUNTRY_ROLE_OVERRIDES.get(key)

    if alpha_2 is None:
        try:
            country = pycountry.countries.get(name=role_name)
            if country is None:
                country = pycountry.countries.get(official_name=role_name)
            if country is None:
                if len(key) < 4:
                    return None
                candidates = pycountry.countries.search_fuzzy(role_name)
                match = candidates[0]
                names = " ".join(
                    filter(
                        None,
                        [
                            getattr(match, "name", ""),
                            getattr(match, "official_name", ""),
                            getattr(match, "common_name", ""),
                        ],
                    )
                ).lower()
                if not re.search(rf"\b{re.escape(key)}\b", names):
                    return None
                country = match
        except LookupError:
            return None

        alpha_2 = country.alpha_2

    return _alpha2_to_flag_emoji(alpha_2)


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
        return "No thread exists for the team"

    playerNames = list(["","","","",""])
    playerIDs   = list([0,0,0,0,0])

    message = "@here"
    pollMessage = "Who do you want to have as your captain?"

    row : DictRow
    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    SELECT poll_id FROM captain_poll_finish_times
                    WHERE channel_discord_id = %s
                    """,
                    (team_channel_id,)
                )
                if cur.fetchone()["poll_id"] is None:
                    return "A poll already exists."


                cur.execute(
                    """--sql
                    SELECT * FROM teams
                    WHERE team_channel_id = %s
                    """,
                    (team_channel_id,)
                )
                row = cur.fetchone()

                if row["team_id"] is None: return "No team could be found with this thread."

                if (row["team_role_id"] is not None):
                    message = f"<@&{row['team_role_id']}>"
                    pollMessage = f"Who do you want to have as your captain?"

                if row["captain_id"] is not None:
                    cur.execute(
                        """--sql
                        UPDATE teams
                        SET captain_id = NULL
                        WHERE captain_id = %s
                        """,
                        (row["captain_id"],)
                    )
    if row["captain_id"] is not None:
        member = await get_or_fetch_member(thread.guild, row["captain_id"])
        captain_role = discord.utils.get(thread.guild.roles, name=CAPTAIN_ROLE_NAME)
        member.remove_roles(captain_role)
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
        duration = timedelta(hours=24)
    ) 

    for playerName in filteredPlayerNames:
        poll.add_answer(text=playerName)

    players = {
        memberName: memberID
        for memberName, memberID in zip(filteredPlayerNames, filteredPlayerIDs)
    }

    message = await thread.send(
        content=message,
        poll=poll,
        allowed_mentions=discord.AllowedMentions(roles=True)
    )
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
    return "The poll has successfully been created"

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
        scheduled_tasks.append(asyncio.create_task(wait_for_poll_finish(poll)))
                    
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

    message = await get_or_fetch_message(channel.guild, poll_discord_id)

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

        if row_count == 0:
            await member.remove_roles(captain_role)
            continue

        break

def insertTeamsIntoTournamentTable():

    names : list[str] = []
    tournamentURL : str

    with pool.connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    """--sql
                    SELECT team_name FROM teams
                    """
                )
                rows = cur.fetchall()
                names = [row["team_name"] for row in rows]

                cur.execute(
                    """--sql
                    SELECT tournament_url FROM key_value
                    """
                )
                row = cur.fetchone()
                tournamentURL = row["tournament_url"]

                cur.execute(
                    """--sql
                    UPDATE key_value
                    SET tournament_started = TRUE
                    """
                )

    with challonge.Client(user=CHALLONGE_USER, api_key=CHALLONGE_API_KEY, timezone="UTC") as client:
        tournament =  client.tournaments.show(tournamentURL)
        client.participants.bulk_add(tournament.id, names)
        client.participants.randomize(tournament.id)
        client.tournaments.start(tournament.id)


bot.run(TOKEN)