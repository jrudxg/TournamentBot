# tournament_bot.py
"""Discord bot that manages sign-ups, friend groups, and teams
for a tournament.
"""

import os
import random
import re
import textwrap
import uuid

import discord
import psycopg
from discord import app_commands
from discord.ext import commands
from dotenv import load_dotenv
from psycopg.rows import DictRow, dict_row
from psycopg_pool import ConnectionPool

import queries
from enums import CreateTeamsOutput, QueryErrors, RemoveFromTeamOutput
from keep_alive import keep_alive

# ============================================================
# Configuration & Setup
# ============================================================

load_dotenv()
TOKEN = os.getenv("DISCORD_TOKEN")

TEAM_SIZE = 5
EMPTY_SLOT_ID = 0
FRIEND_CHANNEL_NAME = "friends"
TEAM_CHANNEL_NAME = "teams"
THREAD_AUTO_ARCHIVE_MINUTES = 10080
TEAM_PLAYER_COLUMNS = (
    "player1_id",
    "player2_id",
    "player3_id",
    "player4_id",
    "player5_id",
)

pool = ConnectionPool(
    "postgresql://neondb_owner:npg_rSwaRGpoA3j9@ep-green-darkness-aszevld8-pooler"
    ".c-4.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require",
    min_size=1,
    max_size=5,
    kwargs={"row_factory": dict_row},
)

keep_alive()

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# Bot Events
# ============================================================

@bot.event
async def on_ready():
    bot.add_dynamic_items(AcceptFriendshipInviteView)
    await bot.tree.sync()
    print(f"{bot.user} is online!")


@bot.event
async def on_member_remove(member: discord.Member):
    """When a member leaves the server: dissolve their friendship (if
    any), remove them from `players`, and remove them from their team.
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, error = queries.select_player_with_discord_id(cur, member.id)

            if player is None or error == QueryErrors.PLAYER_NOT_FOUND:
                return

            if player["friend_code"] is not None:
                discord_thread_id, amount_of_players, error = (
                    queries.remove_friend_code_and_thread(cur, player["friend_code"])
                )

                if discord_thread_id is None:
                    return

                try:
                    thread = await bot.fetch_channel(discord_thread_id)
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

            cur.execute(
                """--sql
                DELETE FROM players
                WHERE discord_id = %s
                """,
                (member.id,),
            )

            await remove_from_team(member.id, cur)


# ============================================================
# Player Sign-up Commands
# ============================================================

@bot.tree.command(name="sign_in", description="Signs you into the tournament as a player (not watcher)")
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

    with pool.connection() as conn:
        with conn.cursor() as cur:
            if queries.check_if_teams_exist(cur) and not be_substitute:
                await interaction.response.send_message(
                    "You can only sign up as a substitute because the teams have "
                    "already been created.",
                    ephemeral=True,
                )
                return

            _, error = queries.select_player_with_discord_id(cur, discord_id)

            if error == QueryErrors.UNKNOWN_ERROR:
                await interaction.response.send_message("An unknown error has been found.")
                return

            if error != QueryErrors.PLAYER_NOT_FOUND:
                await interaction.response.send_message("You are already signed up.", ephemeral=True)
                return

            cur.execute(
                """--sql
                INSERT INTO players (discord_id, username_steam, is_substitute)
                VALUES (%s, %s, %s)
                ON CONFLICT (discord_id) DO NOTHING
                """,
                (discord_id, steam_username, be_substitute),
            )
            if cur.rowcount == 0:
                await interaction.response.send_message("You are already signed up.", ephemeral=True)
                return

            await interaction.response.send_message("You are now signed in.", ephemeral=True)


@bot.tree.command(name="sign_out", description="Signs you out of the tournament as a player (not watcher)")
async def sign_out(interaction: discord.Interaction):
    """Sign-out requirements:
    - no friendship
    - if not a substitute: teams must not have been created yet
    - already signed in
    """
    discord_id = interaction.user.id

    with pool.connection() as conn:
        with conn.cursor() as cur:
            can_sign_out, _ = queries.check_if_player_has_valid_value(
                cur, discord_id, "friend_code", None
            )
            if not can_sign_out:
                await interaction.response.send_message(
                    "You can't sign out if you have a friendship or a friendship "
                    "request. If you want to sign out, you have to cancel the other "
                    "friendship (request).",
                    ephemeral=True,
                )
                return

            if queries.check_if_teams_exist(cur):
                player, _ = queries.select_player_with_discord_id(cur, discord_id)
                if player is not None and not player["is_substitute"]:
                    await interaction.response.send_message(
                        "You can't sign out if you are already part of a team. "
                        "If you want to sign out, you have to cancel the other "
                        "friendship (request).",
                        ephemeral=True,
                    )
                    return

            cur.execute(
                """--sql
                DELETE FROM players
                WHERE discord_id = %s
                """,
                (discord_id,),
            )
            if cur.rowcount == 0:
                await interaction.response.send_message(
                    "You are currently not signed in.", ephemeral=True
                )
                return

            await interaction.response.send_message("You are now signed out.", ephemeral=True)


@bot.tree.command(name="change_steam_username", description="Changes your Steam username")
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

    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = queries.set_in_players(cur, discord_id, "username_steam", steam_username)

            if error == QueryErrors.PLAYER_NOT_FOUND:
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return
            if error == QueryErrors.UNKNOWN_ERROR:
                await interaction.response.send_message("An unknown error has been found.")
                return
            if error == QueryErrors.PARAMETER_NOT_FOUND:
                await interaction.response.send_message(
                    "The parameter username_steam has not been found as a column."
                )
                return

            await interaction.response.send_message(
                f"Steam username was updated to {steam_username}.", ephemeral=True
            )


@bot.tree.command(name="change_substitute", description="Allows you to either enlist or unlist as a substitute")
async def change_substitute(interaction: discord.Interaction, be_substitute: bool):
    """Requirements:
    - teams must not have been created yet
    - no friendship
    """
    discord_id = interaction.user.id

    with pool.connection() as conn:
        with conn.cursor() as cur:
            if queries.check_if_teams_exist(cur):
                await interaction.response.send_message(
                    "You can't change your substitute status anymore because the "
                    "teams have already been created.",
                    ephemeral=True,
                )
                return

            can_change_substitute, error = queries.check_if_player_has_valid_value(
                cur, discord_id, "friend_code", None
            )
            if error == QueryErrors.PLAYER_NOT_FOUND:
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return
            if not can_change_substitute:
                await interaction.response.send_message(
                    "You can't change your substitute status if you have a "
                    "friendship or a friendship request.",
                    ephemeral=True,
                )
                return

            error = queries.set_in_players(cur, discord_id, "is_substitute", be_substitute)

            if error == QueryErrors.PLAYER_NOT_FOUND:
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return
            if error == QueryErrors.UNKNOWN_ERROR:
                await interaction.response.send_message("An unknown error has been found.")
                return
            if error == QueryErrors.PARAMETER_NOT_FOUND:
                await interaction.response.send_message(
                    "The parameter is_substitute has not been found as a column."
                )
                return

            message = (
                "You are now part of the substitute team."
                if be_substitute
                else "You are no longer part of the substitute team."
            )
            await interaction.response.send_message(message, ephemeral=True)


# ============================================================
# Friendship Commands
# ============================================================

@bot.tree.command(
    name="send_friendship_invite",
    description="Sends a friendship request to the other player so you",
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

    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, error = queries.select_player_with_discord_id(cur, interaction.user.id)

            if error == QueryErrors.PLAYER_NOT_FOUND or player is None:
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return

            if "is_substitute" not in player:
                await interaction.response.send_message(
                    "The parameter is_substitute has not been found as a column."
                )
                return
            if player["is_substitute"]:
                await interaction.response.send_message(
                    "You can't create a friend request if you are part of the "
                    "substitute team.",
                    ephemeral=True,
                )
                return

            if "friend_code" not in player:
                await interaction.response.send_message(
                    "The parameter friend_code has not been found as a column."
                )
                return
            if player["friend_code"] is not None:
                await interaction.response.send_message(
                    "You already sent out a friendship request or are part of a "
                    "friend group. If you want to send out this request, you have "
                    "to cancel the other friendship (request)",
                    ephemeral=True,
                )
                return

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
                await interaction.response.send_message(
                    "You already sent out a friendship request or are part of a "
                    "friend group. If you want to send out this request, you have "
                    "to cancel the other friendship (request)",
                    ephemeral=True,
                )
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

    await interaction.response.send_message(
        f"Friend request has been created. Look at {thread.mention}", ephemeral=True
    )


@bot.tree.command(
    name="leave_friendship",
    description="Leaves the current friendship or cancels the current request",
)
async def leave_friendship(interaction: discord.Interaction):
    """Requirements:
    - signed in
    - has a friendship (or pending request)
    """
    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, _ = queries.select_player_with_discord_id(cur, interaction.user.id)
            if player is None:
                await interaction.response.send_message("You are not signed in.", ephemeral=True)
                return

            if player["friend_code"] is None:
                await interaction.response.send_message(
                    "You currently don't have a friendship and also don't have a "
                    "pending request.",
                    ephemeral=True,
                )
                return

            discord_thread_id, amount_of_players, _ = queries.remove_friend_code_and_thread(
                cur, player["friend_code"]
            )
            if discord_thread_id is None:
                await interaction.response.send_message("An unknown error has been found.")
                return

            try:
                thread = await bot.fetch_channel(discord_thread_id)
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
                    f"{interaction.user.display_name} left the friendship. You are "
                    "now in no friendship. Please leave the thread manually."
                )

            await interaction.response.send_message(
                "You successfully left the friendship", ephemeral=True
            )


# ============================================================
# Team Management Commands (Admin)
# ============================================================

@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="start_create_teams", description="Creates the teams for the tournament")
async def start_create_teams(interaction: discord.Interaction):
    await interaction.response.defer()
    _, message_text = await start_creating_teams(interaction.guild)
    await interaction.followup.send(message_text)


@bot.tree.command(
    name="leave_team",
    description="Leaves your current team. ONLY THE MODERATORS CAN ASSIGN YOU BACK TO THE TEAM.",
)
async def leave_team(interaction: discord.Interaction):
    await interaction.response.defer()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = await remove_from_team(interaction.user.id, cur)

            if error is RemoveFromTeamOutput.TEAMS_NOT_CREATED:
                await interaction.followup.send("The teams have not been created yet.", ephemeral=True)
            elif error is RemoveFromTeamOutput.PLAYER_NOT_FOUND:
                await interaction.followup.send("You are not part of a team.", ephemeral=True)
            elif error is RemoveFromTeamOutput.NO_ERROR:
                await interaction.followup.send("You successfully left your team.", ephemeral=True)
            else:
                await interaction.followup.send("An unknown error has been found.")


@app_commands.checks.has_permissions(administrator=True)
@bot.tree.command(name="remove_from_team", description="Removes a player from a team.")
async def remove_from_team_command(interaction: discord.Interaction, user: discord.User):
    await interaction.response.defer()
    with pool.connection() as conn:
        with conn.cursor() as cur:
            error = await remove_from_team(user.id, cur)

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
@bot.tree.command(name="fill_team_with_user", description="Fills a team with a user")
async def fill_team_with_user(
    interaction: discord.Interaction,
    user: discord.User,
    team_thread: discord.Thread,
):
    await interaction.response.defer()

    with pool.connection() as conn:
        with conn.cursor() as cur:
            player, _ = queries.select_player_with_discord_id(cur, user.id)
            if player is None:
                await interaction.followup.send("The user is not signed in", ephemeral=True)
                return

            if not queries.check_if_teams_exist(cur):
                await interaction.followup.send("There are currently no teams existing", ephemeral=True)
                return

            cur.execute(
                """--sql
                SELECT * FROM teams
                WHERE %s IN (player1_id, player2_id, player3_id, player4_id, player5_id)
                """,
                (user.id,),
            )
            if cur.fetchone() is not None:
                await interaction.followup.send("User is already in a team.", ephemeral=True)
                return

            cur.execute(
                """--sql
                SELECT * FROM teams
                WHERE team_channel_id = %s
                """,
                (team_thread.id,),
            )
            row = cur.fetchone()
            if row is None:
                await interaction.followup.send("Team with this thread does not exist")
                return

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
                queries.set_in_players(cur, 0, "is_substitute", False, player)
                await interaction.followup.send(
                    f"{user.display_name} is now successfully part of the team "
                    f"{row['team_name']}",
                    ephemeral=True,
                )
                return

            await interaction.followup.send("The team is already full", ephemeral=True)


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
        with pool.connection() as conn:
            with conn.cursor() as cur:
                player, error = queries.select_player_with_discord_id(cur, self.receiver_id)

                if error == QueryErrors.PLAYER_NOT_FOUND or player is None:
                    await interaction.response.send_message("You are not signed in.", ephemeral=True)
                    return

                if player["friend_code"] is not None:
                    await interaction.response.send_message(
                        "You already accepted the friend request (or are in "
                        "another friendship).",
                        ephemeral=True,
                    )
                    return

                if player["is_substitute"]:
                    await interaction.response.send_message(
                        "You can't accept a friend request as a substitute.", ephemeral=True
                    )
                    return

                queries.set_in_players(cur, 0, "friend_code", self.friend_code, player)

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
            ) or await interaction.guild.fetch_member(self.receiver_id)
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

async def start_creating_teams(guild: discord.Guild) -> tuple[CreateTeamsOutput, str]:
    """Randomly distribute all signed-up players into teams of
    `TEAM_SIZE`, creating a role and a private thread per team.
    """
    teams_channel = discord.utils.get(guild.text_channels, name=TEAM_CHANNEL_NAME)

    if teams_channel is None:
        return (
            CreateTeamsOutput.NO_TEAM_CHANNEL,
            f"<@{guild.owner_id}> make sure that there's a \"{TEAM_CHANNEL_NAME}\" "
            "channel in your discord. Else the bot can't create threads for the "
            "friends function",
        )

    with pool.connection() as conn:
        with conn.cursor() as cur:
            player_ids = queries.get_all_player_ids(cur)
            random.shuffle(player_ids)

            players_without_full_team = len(player_ids) % TEAM_SIZE
            missing_players = (TEAM_SIZE - players_without_full_team) % TEAM_SIZE
            amount_of_teams = (len(player_ids) + missing_players) // TEAM_SIZE

            player_ids.extend([EMPTY_SLOT_ID] * missing_players)

            if missing_players != 0:
                await teams_channel.send(
                    f"There are {missing_players} missing players that need to be "
                    f"filled in team{amount_of_teams}"
                )

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
                        continue
                    try:
                        member = await guild.fetch_member(player_id)
                    except discord.NotFound:
                        players[i] = EMPTY_SLOT_ID
                        await teams_channel.send(
                            f"{team_name.capitalize()} needs a new member because "
                            f"<@{player_id}> is not in the server anymore."
                        )
                        continue

                    await member.add_roles(role)
                    await thread.add_user(member)

                await thread.send(f"Welcome {team_name}")
                queries.insert_team(cur, team_name, thread.id, role.id, players)

    return CreateTeamsOutput.NO_ERROR, f"All {amount_of_teams} teams have been created."


async def remove_from_team(
    user_id: int,
    cur: psycopg.Cursor[DictRow],
) -> RemoveFromTeamOutput:
    """Remove a player from their team (if any), clean up their role,
    and clear the captain slot if they were the captain.
    """
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

    thread = await bot.fetch_channel(row["team_channel_id"])
    if not isinstance(thread, discord.Thread):
        return RemoveFromTeamOutput.UNKNOWN_ERROR
    guild = thread.guild

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

        await thread.remove_user(await bot.fetch_user(user_id))
        role = await guild.fetch_role(row["team_role_id"])
        try:
            member = await guild.fetch_member(user_id)
            await member.remove_roles(role)
        except discord.NotFound:
            pass

        break

    if row["captain_id"] == user_id:
        cur.execute(
            """--sql
            UPDATE teams
            SET captain_id = NULL
            WHERE captain_id = %s
            """,
            (user_id,),
        )
        await thread.send(f"<@&{row['team_role_id']}> There is currently no captain")

    await thread.parent.send(
        f"{row['team_name']} needs a new member because <@{user_id}> is not in the "
        "team anymore."
    )
    return RemoveFromTeamOutput.NO_ERROR


bot.run(TOKEN)