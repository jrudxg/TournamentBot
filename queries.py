# queries.py
"""Database access helpers for the tournament bot.

All functions take an already-open `psycopg` cursor (row factory:
`dict_row`) and translate raw SQL results into `QueryErrors` outcomes
that the bot layer can react to.
"""

from uuid import UUID

from psycopg import Cursor, sql
from psycopg.rows import DictRow

from enums import QueryErrors


# ============================================================
# Player queries
# ============================================================

def select_player_with_discord_id(
    cur: Cursor[DictRow],
    discord_id: int,
) -> tuple[DictRow | None, QueryErrors]:
    """Fetch a single player row by their Discord ID."""
    cur.execute(
        """--sql
        SELECT * FROM players
        WHERE discord_id = %s
        """,
        (discord_id,),
    )

    row = cur.fetchone()
    if row is None:
        return None, QueryErrors.PLAYER_NOT_FOUND

    return row, QueryErrors.NO_ERROR


def set_in_players(
    cur: Cursor[DictRow],
    discord_id: int,
    parameter: str,
    value,
    player: DictRow | None = None,
) -> QueryErrors:
    """Set a single column for a player.

    Pass `discord_id=0` if `player` is already provided.
    """
    if player is None:
        player, error = select_player_with_discord_id(cur, discord_id)
        if error != QueryErrors.NO_ERROR:
            return error
        if player is None:
            print(
                "Unexpected state in set_in_players: player is None "
                "but error was QueryErrors.NO_ERROR!"
            )
            return QueryErrors.UNKNOWN_ERROR

    if parameter not in player:
        return QueryErrors.PARAMETER_NOT_FOUND

    cur.execute(
        sql.SQL(
            """--sql
            UPDATE players
            SET {column} = %s
            WHERE discord_id = %s
            """
        ).format(column=sql.Identifier(parameter)),
        (value, player["discord_id"]),
    )

    return QueryErrors.NO_ERROR


def check_if_player_has_valid_value(
    cur: Cursor[DictRow],
    discord_id: int,
    parameter: str,
    value,
    player: DictRow | None = None,
) -> tuple[bool, QueryErrors]:
    """Check whether a player's column currently equals `value`.

    Pass `discord_id=0` if `player` is already provided.
    """
    if player is None:
        player, error = select_player_with_discord_id(cur, discord_id)
        if error != QueryErrors.NO_ERROR:
            return False, error
        if player is None:
            print(
                "Unexpected state in check_if_player_has_valid_value: "
                "player is None but error was QueryErrors.NO_ERROR!"
            )
            return False, QueryErrors.UNKNOWN_ERROR

    if parameter not in player:
        return False, QueryErrors.PARAMETER_NOT_FOUND

    return player[parameter] == value, QueryErrors.NO_ERROR


def get_all_player_ids(
    cur: Cursor[DictRow],
    filterFriendsOut: bool = False
) -> list[int]:
    """Return the Discord IDs of all non-substitute players."""

    filterExtension = "" if not filterFriendsOut else \
    """--sql
    AND friend_code IS NULL
    """

    filterText = \
    f"""--sql
    WHERE is_substitute = FALSE
    {filterExtension}
    """

    cur.execute(
        f"""--sql
        SELECT discord_id FROM players
        {filterText}
        """
    )

    return [int(row["discord_id"]) for row in cur.fetchall()]


# ============================================================
# Friendship queries
# ============================================================

def insert_friend_thread(
    cur: Cursor[DictRow],
    discord_thread_id: int,
    friend_code: UUID,
) -> QueryErrors:
    """Link a Discord thread to a friend code, if that code exists."""
    cur.execute(
        """--sql
        INSERT INTO friend_threads (discord_id, friend_code)
        SELECT %s, %s
        WHERE EXISTS (
            SELECT 1 FROM players
            WHERE friend_code = %s
        )
        """,
        (discord_thread_id, friend_code, friend_code),
    )

    if cur.rowcount == 0:
        return QueryErrors.FRIENDCODE_NOT_FOUND
    return QueryErrors.NO_ERROR


def remove_friend_code_and_thread(
    cur: Cursor[DictRow],
    friend_code: UUID,
) -> tuple[int | None, int | None, QueryErrors]:
    """Clear a friend code from all players and delete its thread entry.

    Returns (discord_thread_id, amount_of_players, error).
    """
    cur.execute(
        """--sql
        UPDATE players
        SET friend_code = NULL
        WHERE friend_code = %s
        """,
        (friend_code,),
    )
    amount_of_players = cur.rowcount

    if amount_of_players == 0:
        return None, None, QueryErrors.FRIENDCODE_NOT_FOUND

    cur.execute(
        """--sql
        DELETE FROM friend_threads
        WHERE friend_code = %s
        RETURNING discord_id
        """,
        (friend_code,),
    )
    row = cur.fetchone()

    if row is None:
        return None, None, QueryErrors.UNKNOWN_ERROR

    return row["discord_id"], amount_of_players, QueryErrors.NO_ERROR


# ============================================================
# Team queries
# ============================================================

def check_if_teams_exist(cur: Cursor[DictRow]) -> bool:
    """Return True if at least one team has been created."""
    cur.execute(
        """--sql
        SELECT team_id FROM teams
        LIMIT 1
        """
    )
    return cur.fetchone() is not None

def check_if_tournament_started(cur: Cursor[DictRow]) -> bool:
    """Return True if at tournament_started from key_value is also true."""
    cur.execute(
        """--sql
        SELECT tournament_started FROM key_value
        """
    )
    row = cur.fetchone()
    return bool(row["tournament_started"])

def insert_team(
    cur: Cursor[DictRow],
    team_name: str,
    team_channel_id: int,
    team_role_id: int,
    player_ids: tuple[int, int, int, int, int],
) -> None:
    """Insert a newly created team."""
    cur.execute(
        """--sql
        INSERT INTO teams (
            team_name, team_channel_id, team_role_id,
            player1_id, player2_id, player3_id, player4_id, player5_id
        )
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (team_name, team_channel_id, team_role_id, *player_ids),
    )
