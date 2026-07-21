from datetime import datetime

from pydantic import BaseModel


class JiraProject(BaseModel):
    user_id: str
    cloud_id: str
    project_key: str
    project_name: str | None = None
    status: str = "active"  # active | disconnected
    sync_status: str = "pending"  # pending | in_progress | done | failed
    sync_error: str | None = None
    last_synced_at: datetime | None = None
    connected_at: datetime
    disconnected_at: datetime | None = None
    updated_at: datetime
