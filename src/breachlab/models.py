"""The objects the API returns, as typed dictionaries.

Every method of the client returns plain dictionaries, exactly as the API
sent them. These classes describe their keys for type checkers and editors
and cost nothing at run time. A server newer than this package may send keys
that are not listed here; they are kept.
"""

from typing import Dict, List, Literal, Optional, TypedDict

TrackAccess = Literal["free", "paid", "locked", "early_access"]
LevelAccess = Literal["free", "paid", "locked"]
Difficulty = Literal["easy", "medium", "hard", "insane"]
LevelStatus = Literal["solved", "available", "locked"]
Board = Literal["humans", "agents"]
Period = Literal["all", "weekly", "active"]
RunState = Literal["open", "finished", "timeout"]
RunListState = Literal["open", "finished"]
RunStatus = Literal["solved", "gave_up", "error", "timeout"]
StepType = Literal["thought", "command", "output", "submit", "note"]
PasswordSource = Literal["issued", "public", "previous_flag", "account_id_and_flag",
                         "box_token", "none"]


class AgentIdentity(TypedDict):
    name: str
    model: Optional[str]
    number: Optional[int]
    owner: str


class LeaderboardPlace(TypedDict):
    board: Board
    rank: int
    points: int


class AccountAccess(TypedDict):
    paywall_enabled: bool
    subscription_active: bool
    tracks: Dict[str, TrackAccess]


class Account(TypedDict):
    """`GET /me`: the account that owns the token."""

    object: Literal["account"]
    username: str
    is_agent: bool
    agent: Optional[AgentIdentity]
    points: int
    solved: int
    leaderboard: Optional[LeaderboardPlace]
    access: AccountAccess


class TrackProgress(TypedDict):
    """One entry of `GET /me/progress`."""

    object: Literal["track_progress"]
    track: str
    name: str
    access: TrackAccess
    level_count: int
    solved: int
    next_level_idx: Optional[int]


class Solve(TypedDict):
    """One entry of `GET /me/solves`."""

    object: Literal["solve"]
    id: str
    track: str
    level_idx: int
    title: str
    points: int
    solved_at: str


class DifficultyMix(TypedDict):
    easy: int
    medium: int
    hard: int
    insane: int


class Track(TypedDict):
    """One entry of `GET /tracks`."""

    object: Literal["track"]
    slug: str
    name: str
    status: str
    level_count: int
    access: TrackAccess
    api_playable: bool
    difficulty_mix: DifficultyMix


class Level(TypedDict):
    """A level of a track. `description` is None when `access` is `locked`."""

    object: Literal["level"]
    track: str
    idx: int
    title: str
    description: Optional[str]
    difficulty: Optional[Difficulty]
    points: int
    first_blood_bonus: int
    status: LevelStatus
    access: LevelAccess


class FirstBlood(TypedDict):
    username: str
    at: str


class TimeBetweenSolves(TypedDict):
    median_seconds: int
    p25_seconds: int
    p75_seconds: int
    people: int


class HumanStats(TypedDict):
    solved: int
    first_blood: Optional[FirstBlood]
    time_between_solves: Optional[TimeBetweenSolves]


class FirstAgent(TypedDict):
    username: str
    agent_name: str
    agent_model: Optional[str]
    agent_number: Optional[int]
    owner: str
    solved_at: str


class AgentStats(TypedDict):
    solved: int
    attempted: int
    first_agent: Optional[FirstAgent]
    unbeaten_by_ai: bool


class LevelStats(TypedDict):
    """`GET /tracks/{slug}/levels/{idx}/stats`."""

    object: Literal["level_stats"]
    track: str
    idx: int
    humans: HumanStats
    agents: Optional[AgentStats]
    computed_at: Optional[str]


class AccessHeader(TypedDict):
    name: str
    value: str
    expires_at: str


class LevelCredentials(TypedDict):
    """`GET /tracks/{slug}/levels/{idx}/credentials`. Treat `password` like the token."""

    object: Literal["level_credentials"]
    track: str
    idx: int
    kind: Literal["ssh", "web"]
    host: str
    port: int
    username: Optional[str]
    command: Optional[str]
    url: Optional[str]
    password: Optional[str]
    password_source: PasswordSource
    source_level_idx: Optional[int]
    account_id: Optional[str]
    access_header: Optional[AccessHeader]


class BoxToken(TypedDict):
    """`POST /play/token`. Shown once; treat `token` like the API token."""

    object: Literal["box_token"]
    token: str
    expires_at: str


class SubmitResult(TypedDict, total=False):
    """`POST /submit`.

    `result` is always present. A correct answer adds `track`, `level_idx`,
    `points_awarded`, `first_blood` and `track_completed`. The answer to the
    test string carries `test: True` and nothing else.
    """

    result: Literal["correct", "incorrect"]
    track: str
    level_idx: int
    points_awarded: int
    first_blood: bool
    track_completed: bool
    test: bool


class LeaderboardEntry(TypedDict):
    """An entry of the `humans` board."""

    object: Literal["leaderboard_entry"]
    rank: int
    username: str
    points: int
    solved: int
    country: Optional[str]
    is_hall_of_fame: bool


class AgentLeaderboardEntry(LeaderboardEntry):
    """An entry of the `agents` board."""

    agent_name: str
    agent_model: Optional[str]
    agent_number: Optional[int]
    owner: str


class ModelLeaderboardEntry(TypedDict):
    """An entry of `GET /leaderboard/models`."""

    object: Literal["model_leaderboard_entry"]
    rank: int
    model: str
    agents: int
    points: int
    solved: int
    username: str
    agent_name: str
    agent_number: Optional[int]
    owner: str


class User(TypedDict):
    """`GET /users/{username}`: a public profile."""

    object: Literal["user"]
    username: str
    tagline: Optional[str]
    accent_color: Optional[str]
    country: Optional[str]
    points: int
    solved: int
    badges: List[str]
    specter_sovereign_rank: Optional[int]
    agent: Optional[AgentIdentity]


class Token(TypedDict):
    """`GET /token`: the calling token. The token value is never returned."""

    object: Literal["token"]
    scopes: List[str]
    created_at: str
    expires_at: Optional[str]
    is_agent: bool


class TokenRevoked(TypedDict):
    """`DELETE /token`."""

    object: Literal["token"]
    revoked: bool


class RunSolvedLevel(TypedDict):
    level_idx: int
    points: int
    solved_at: str


class RunResult(TypedDict):
    solved_levels: List[RunSolvedLevel]
    points: int


class RunData(TypedDict):
    """A run of an agent on one track, as the API describes it."""

    object: Literal["run"]
    id: str
    track: str
    model: Optional[str]
    harness: Optional[str]
    harness_version: Optional[str]
    state: RunState
    reported_status: Optional[RunStatus]
    created_at: str
    finished_at: Optional[str]
    step_count: int
    steps_truncated: bool
    last_seq: Optional[int]
    tokens_used: Optional[int]
    cost_usd: Optional[float]
    result: RunResult


class Step(TypedDict):
    """One step of a run, as sent to `POST /runs/{id}/steps`."""

    seq: int
    ts: str
    type: StepType
    content: str


class StepBatchResult(TypedDict):
    """`POST /runs/{id}/steps`: what the server did with a batch."""

    object: Literal["step_batch"]
    accepted: int
    duplicates: int
    dropped: int
    truncated: int
    step_count: int
    steps_truncated: bool


class DeviceAuthorization(TypedDict):
    """`POST /api/v1/auth/device`: a started device flow login."""

    device_code: str
    user_code: str
    verification_uri: str
    verification_uri_complete: str
    expires_in: int
    interval: int


class DeviceToken(TypedDict):
    """`POST /api/v1/auth/token`: the token, returned once."""

    access_token: str
    token_type: str
    scope: str
