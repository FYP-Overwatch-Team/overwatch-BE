from datetime import datetime

from pydantic import BaseModel

from app.core.constants import KeepProfile


class MergeProfile(BaseModel):
    """One side of a merge, as the person confirming it sees it."""

    username: str | None
    name: str | None
    avatar_url: str | None
    email: str | None
    github_login: str | None
    created_at: datetime | None


class MergeMoves(BaseModel):
    """What the other account brings: memberships and what it created."""

    workspaces: int
    repos: int
    documents: int
    work_items: int
    comments: int
    chats: int


class MergePreview(BaseModel):
    current: MergeProfile
    other: MergeProfile
    proof: str
    moves: MergeMoves
    # the other account's personal workspace, renamed and kept as an ordinary one; null when it has none
    personal_workspace_name: str | None
    # sign-in methods that block the merge until one account removes theirs
    conflicts: list[str]


class ConfirmMerge(BaseModel):
    keep_profile: KeepProfile = KeepProfile.CURRENT
