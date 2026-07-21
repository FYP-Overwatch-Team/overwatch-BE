from datetime import datetime

from pydantic import BaseModel


class OAuthToken(BaseModel):
    """Stored third-party OAuth credentials. Token values are Fernet-encrypted at rest."""

    user_id: str
    provider: str  # "github" | "jira"
    access_token_encrypted: str
    refresh_token_encrypted: str | None = None
    expires_at: datetime | None = None
    status: str = "active"  # "active" | "needs_reauth"
    cloud_id: str | None = None  # Jira only
    updated_at: datetime
