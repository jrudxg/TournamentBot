# enums.py
"""Shared enums used across the tournament bot modules."""

from enum import Enum, auto


class QueryErrors(Enum):
    """Outcome of a database query helper function."""
    NO_ERROR = auto()
    PLAYER_NOT_FOUND = auto()
    PARAMETER_NOT_FOUND = auto()
    FRIENDCODE_NOT_FOUND = auto()
    UNKNOWN_ERROR = auto()


class CreateTeamsOutput(Enum):
    """Outcome of the `start_creating_teams` process."""
    NO_TEAM_CHANNEL = auto()
    NO_ERROR = auto()


class RemoveFromTeamOutput(Enum):
    """Outcome of removing a player from their team."""
    TEAMS_NOT_CREATED = auto()
    PLAYER_NOT_FOUND = auto()
    UNKNOWN_ERROR = auto()
    NO_ERROR = auto()