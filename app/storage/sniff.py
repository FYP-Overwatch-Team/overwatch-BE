"""What a file really is, from its first bytes, so a renamed file can't pass as another kind."""

#: bytes read from the start of a file to tell what it is
HEAD_BYTES = 4096


def image_type(data: bytes) -> str | None:
    """The image type (`image/png`...) the bytes start like; None for anything else."""
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if data.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if data[:6] in (b"GIF87a", b"GIF89a"):
        return "image/gif"
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return "image/webp"
    return None


def is_pdf(data: bytes) -> bool:
    return data.startswith(b"%PDF-")


def is_zip(data: bytes) -> bool:
    """docx, xlsx and pptx are zip files."""
    return data.startswith(b"PK\x03\x04")


def is_text(data: bytes) -> bool:
    """UTF-8 text with no NUL bytes; a character cut off where the read stopped is fine."""
    if b"\x00" in data:
        return False
    try:
        data.decode("utf-8")
    except UnicodeDecodeError as error:
        return len(data) == HEAD_BYTES and error.start >= len(data) - 3
    return True
