"""Read owned Yandex playlists without dropping unavailable source entries.

Playlist IDs are internal ``owner_uid:kind`` strings, not public playlist URLs.
The released yandex-music 3.2.2 client exposes the complete playlist through
users_playlists; its fetch_tracks shortcut returns the same shortened entries.
"""

import re
import sys

from yandex_music import Client, DeviceCode, Track
from yandex_music.exceptions import (
    DeviceAuthError,
    NetworkError,
    NotFoundError,
    UnauthorizedError,
    YandexMusicError,
)

from models import MigrationError, Playlist, Song

_BATCH_SIZE = 100


def _api_error(error: YandexMusicError, action: str) -> MigrationError:
    if isinstance(error, UnauthorizedError):
        detail = "Доступ отклонён. Выполните auth-yandex заново и проверьте аккаунт."
    elif isinstance(error, DeviceAuthError):
        detail = "Вход не подтверждён или код истёк. Повторите auth-yandex."
    elif isinstance(error, NotFoundError):
        detail = "Данные не найдены. Проверьте аккаунт и доступность плейлиста."
    elif isinstance(error, NetworkError):
        detail = "Проверьте сеть и доступность Яндекс Музыки; повторите команду позже."
    else:
        detail = "Сервис вернул ошибку. Проверьте доступ к Яндекс Музыке и повторите команду."
    return MigrationError(f"Яндекс Музыка: {action}. {detail}")


def login() -> str:
    """Display the device verification code and return the access token privately."""
    def show_code(code: DeviceCode) -> None:
        print(f"Откройте {code.verification_url} и введите код: {code.user_code}", file=sys.stderr)

    try:
        token = Client().device_auth(on_code=show_code)
    except YandexMusicError as error:
        raise _api_error(error, "не удалось выполнить вход") from None
    if not token.access_token:
        raise MigrationError("Яндекс Музыка не вернула токен. Повторите auth-yandex.")
    return token.access_token


def _value(track: Track | None, fallback: Track | None, name: str):
    value = getattr(track, name, None)
    return getattr(fallback, name, None) if value is None or value == "" or value == [] else value


def _song(track_id: str, track: Track | None, fallback: Track | None = None) -> Song:
    title = _value(track, fallback, "title") or ""
    metadata = _value(track, fallback, "meta_data")
    version = _value(track, fallback, "version") or getattr(metadata, "version", None)
    if title and version:
        title = f"{title} ({version})"
    artists = tuple(
        artist.name for artist in (_value(track, fallback, "artists") or []) if artist.name
    )
    if not artists:
        artist = _value(track, fallback, "ugc_artist_name")
        artists = (artist,) if artist else ()
    albums = _value(track, fallback, "albums") or []
    album = (albums[0].title or "") if albums else (getattr(metadata, "album", None) or "")
    explicit = _value(track, fallback, "explicit")
    if explicit is None and _value(track, fallback, "content_warning") == "explicit":
        explicit = True
    return Song(
        id=track_id,
        title=title,
        artists=artists,
        duration_ms=_value(track, fallback, "duration_ms"),
        isrc=re.sub(r"[^A-Z0-9]", "", (_value(track, fallback, "isrc") or "").upper()),
        album=album,
        explicit=explicit,
    )


class YandexMusic:
    """Read the authenticated user's own named playlists."""

    def __init__(self, token: str):
        if not token.strip():
            raise MigrationError("Токен Яндекс Музыки пуст. Выполните auth-yandex.")
        try:
            self._client = Client(token).init()
        except YandexMusicError as error:
            raise _api_error(error, "не удалось проверить аккаунт") from None
        if self._client.account_uid is None:
            raise MigrationError("Яндекс Музыка не вернула аккаунт. Выполните auth-yandex заново.")

    def playlists(self) -> list[Playlist]:
        """Return all owned named playlists using exact owner and kind identities."""
        try:
            playlists = self._client.users_playlists_list()
        except YandexMusicError as error:
            raise _api_error(error, "не удалось прочитать список плейлистов") from None
        result = []
        for playlist in playlists:
            owner = getattr(playlist.owner, "uid", None)
            if owner is None:
                owner = playlist.uid if playlist.uid is not None else self._client.account_uid
            if str(owner) != str(self._client.account_uid) or not playlist.title:
                continue
            if playlist.kind is None:
                raise MigrationError("Яндекс Музыка вернула плейлист без ID. Повторите команду позже.")
            result.append(Playlist(id=f"{owner}:{playlist.kind}", name=playlist.title))
        return result

    def tracks(self, playlist_id: str) -> list[Song]:
        """Return every playlist entry in order, hydrating only shortened metadata."""
        if not re.fullmatch(r"\d+:\d+", playlist_id):
            raise MigrationError("Некорректный ID плейлиста Яндекс Музыки: ожидается owner_uid:kind.")
        owner, kind = playlist_id.split(":")
        try:
            playlist = self._client.users_playlists(kind, user_id=owner)
        except YandexMusicError as error:
            raise _api_error(error, "не удалось прочитать плейлист") from None
        if playlist is None:
            raise MigrationError("Яндекс Музыка не вернула плейлист. Проверьте аккаунт и его доступность.")
        entries = playlist.tracks or []
        if playlist.track_count is None or playlist.track_count != len(entries):
            raise MigrationError(
                "Яндекс Музыка вернула неполный плейлист: "
                f"заявлено {playlist.track_count}, получено {len(entries)}. "
                "Перенос остановлен; повторите команду позже."
            )

        records = []
        known = {}
        pending = {}
        for entry in entries:
            track = getattr(entry, "track", None)
            track_id = str(entry.id) if entry is not None and entry.id is not None else ""
            album_id = getattr(entry, "album_id", None)
            key = f"{track_id}:{album_id}" if album_id is not None and album_id != "" else track_id
            records.append((track_id, key, track))
            if track is not None and track.title and (track.artists or track.ugc_artist_name):
                known[key] = track
            elif track_id:
                pending[key] = track_id
        missing = [key for key in pending if key not in known]
        for start in range(0, len(missing), _BATCH_SIZE):
            keys = missing[start : start + _BATCH_SIZE]
            try:
                hydrated = self._client.tracks(keys)
            except YandexMusicError as error:
                raise _api_error(error, "не удалось получить метаданные треков") from None
            by_id = {}
            for key in keys:
                by_id.setdefault(pending[key], []).append(key)
            for track in hydrated:
                if track is None or track.id is None:
                    continue
                candidates = by_id.get(str(track.id), [])
                album_keys = {f"{track.id}:{album.id}" for album in track.albums}
                for key in candidates:
                    if key in album_keys or key == str(track.id) or len(candidates) == 1:
                        known[key] = track

        # A missing response is still a source row; the caller reports invalid metadata.
        return [_song(track_id, known.get(key), track) for track_id, key, track in records]
