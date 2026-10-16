from pydantic import BaseModel


class LinkPreview(BaseModel):
    """What's known about a link; any field may be missing, and the card falls back to the address."""

    url: str
    title: str | None = None
    description: str | None = None
    site_name: str | None = None
    icon_url: str | None = None
    image_url: str | None = None
