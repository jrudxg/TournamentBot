# Queries.py
from psycopg import Cursor, sql
from psycopg.rows import DictRow
from enum import Enum, auto
from uuid import UUID

class QueryErrors(Enum):
    NO_ERROR = auto()
    PLAYER_NOT_FOUND = auto()
    PARAMETER_NOT_FOUND = auto()
    FRIENDCODE_NOT_FOUND = auto()
    UNKNOWN_ERROR = auto()

# set discordID to 0 if player is not None
def setInPlayers(
    cur : Cursor[DictRow], 
    discordID : int, 
    parameter : str, 
    value, 
    player : DictRow | None = None 
):
    if player is None:
        player, error = selectPlayerWithDiscordID(cur, discordID)
        if (error != QueryErrors.NO_ERROR): return error
        if player is None: 
            print("There was an error where player was none in setInPlayers but the error was QueryErrors.NO_ERROR!")
            return QueryErrors.UNKNOWN_ERROR
        
    if parameter not in player:
        return  QueryErrors.PARAMETER_NOT_FOUND
    
    cur.execute(
        sql.SQL("""--sql
        UPDATE players
        SET {column} = %s
        WHERE discord_id = %s
        """).format(column=sql.Identifier(parameter)),
        (value, player["discord_id"])
    )

    return QueryErrors.NO_ERROR

# set discordID to 0 if player is not None
def checkIfPlayerHasValidValue(
    cur : Cursor[DictRow],
    discordID : int,
    parameter : str,
    value,
    player : DictRow | None = None 
): 
    if player is None:
        player, error = selectPlayerWithDiscordID(cur, discordID)
        if (error != QueryErrors.NO_ERROR): return False, error
        if player is None: 
            print("There was an error where player was none in setInPlayers but the error was QueryErrors.NO_ERROR!")
            return False, QueryErrors.UNKNOWN_ERROR
    
    if parameter not in player: return False, QueryErrors.PARAMETER_NOT_FOUND

    if player[parameter] == value:
        return True, QueryErrors.NO_ERROR
    else:
        return False, QueryErrors.NO_ERROR



def selectPlayerWithDiscordID(
    cur : Cursor[DictRow], 
    discordID : int
): 
    cur.execute(
        """--sql
        SELECT * FROM players
        WHERE discord_id = %s
        """,
        (discordID,)
    )

    row = cur.fetchone()

    if row is None:
        return None, QueryErrors.PLAYER_NOT_FOUND
    
    return row, QueryErrors.NO_ERROR

# make sure that the discordThreadID actually exists because only the friendCode is tracked
def insertFriendThread(
    cur: Cursor[DictRow],
    discordThreadID: int,
    friendCode: UUID
):
    cur.execute(
        """--sql
        INSERT INTO friend_threads (discord_id, friend_code)
        SELECT %s, %s
        WHERE EXISTS (
            SELECT 1 FROM players
            WHERE friend_code = %s
        )
        """,
        (discordThreadID, friendCode, friendCode)
    )
    if cur.rowcount == 0:
        return QueryErrors.FRIENDCODE_NOT_FOUND
    return QueryErrors.NO_ERROR
    
def removeFriendCodeAndThread(
    cur: Cursor[DictRow],
    friend_code : UUID
):
    cur.execute(
        """--sql
        UPDATE players
        SET friend_code = NULL
        WHERE friend_code = %s
        """,
        (friend_code,)
    )
    amountOfPlayers = cur.rowcount

    if (amountOfPlayers == 0):
        return None, None, QueryErrors.FRIENDCODE_NOT_FOUND
    cur.execute(
        """--sql
        DELETE FROM friend_threads
        WHERE friend_code = %s
        RETURNING discord_id
        """,
        (friend_code,)
    )
    row = cur.fetchone()

    if (row is None): return None, None, QueryErrors.UNKNOWN_ERROR

    return row["discord_id"], amountOfPlayers, QueryErrors.NO_ERROR

# ignores substitutes
def getAllPlayerIDs(
    cur: Cursor[DictRow]
):
    cur.execute(
        """--sql
        SELECT discord_id FROM players
        WHERE is_substitute = FALSE
        """
    )

    playerIDs: list[int] = [int(row["discord_id"]) for row in cur.fetchall()]
    
    return playerIDs

def insertTeam(
    cur: Cursor[DictRow],
    teamName: str,
    teamChannelID : int,
    teamRoleID : int,
    playerIDs: tuple[int, int, int, int, int]
):
    cur.execute(
        """--sql
        INSERT INTO teams (team_name, team_channel_id, team_role_id, player1_id, player2_id, player3_id, player4_id, player5_id)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s)
        """,
        (teamName, teamChannelID, teamRoleID, *playerIDs)
    )

def checkIfTeamsExist(
    cur: Cursor[DictRow]
):
    cur.execute(
        """--sql
        SELECT team_id FROM teams
        LIMIT 1
        """
    )
    return cur.rowcount != 0