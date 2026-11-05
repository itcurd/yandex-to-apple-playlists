"""Song and playlist metadata used by the service adapters."""

from dataclasses import dataclass


class MigrationError(RuntimeError):
    """Report a migration failure without exposing credentials."""


@dataclass(frozen=True)
class Song:
    id: str
    title: str
    artists: tuple[str, ...]
    duration_ms: int | None = None
    isrc: str = ""
    album: str = ""
    explicit: bool | None = None
    catalog_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class Playlist:
    id: str
    name: str
    editable: bool = True
