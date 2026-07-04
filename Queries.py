from psycopg import Cursor, sql
from psycopg.rows import DictRow
from enum import Enum, auto

class QueryErrors(Enum):
    NO_ERROR = auto()
    PLAYER_NOT_FOUND = auto()
    PARAMETER_NOT_FOUND = auto()
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