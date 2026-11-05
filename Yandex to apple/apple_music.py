"""Read and modify Apple Music through its unofficial web session API."""

import re
import time
from dataclasses import replace
from urllib.parse import quote, urljoin, urlsplit

import requests

from models import MigrationError, Playlist, Song

API_ORIGIN = "https://amp-api.music.apple.com"
WEB_ORIGIN = "https://music.apple.com"
TIMEOUT = (10, 30)
BATCH_SIZE = 100
REQUEST_INTERVAL = 1.5
_UNCERTAIN = (
    " Результат записи может быть неопределённым; автоматического повтора нет. "
    "Перед повторным запуском проверьте плейлист в Apple Music, дождитесь "
    "синхронизации и заново прочитайте его содержимое."
)


def _fixed_url(value: str, origin: str, base: str) -> str:
    url = urljoin(base, value)
    parts = urlsplit(url)
    if (
        parts.scheme != "https"
        or parts.netloc != urlsplit(origin).netloc
        or parts.fragment
        or parts.username
        or parts.password
    ):
        raise MigrationError("Apple Music вернул небезопасный адрес; запрос остановлен.")
    return url


def fetch_web_token() -> str:
    """Fetch the public web player bearer without using user credentials."""
    session = requests.Session()
    try:
        def get_public(path: str) -> str:
            url = _fixed_url(path, WEB_ORIGIN, WEB_ORIGIN + "/")
            for _ in range(6):
                try:
                    response = session.get(url, timeout=TIMEOUT, allow_redirects=False)
                except requests.RequestException:
                    raise MigrationError(
                        "Не удалось открыть сайт Apple Music. Проверьте сеть и повторите вход."
                    ) from None
                if response.status_code in (301, 302, 303, 307, 308):
                    location = response.headers.get("Location")
                    if not location:
                        break
                    url = _fixed_url(location, WEB_ORIGIN, url)
                    continue
                if response.status_code != 200:
                    raise MigrationError(
                        f"Сайт Apple Music недоступен (HTTP {response.status_code}). "
                        "Попробуйте позднее или задайте APPLE_MUSIC_BEARER_TOKEN вручную."
                    )
                return response.text
            raise MigrationError("Сайт Apple Music вернул некорректное перенаправление.")

        homepage = get_public("/")
        asset = re.search(r'''["'](/assets/index[~-][^/"'\s]+\.js)["']''', homepage)
        if not asset:
            raise MigrationError(
                "Не найден JavaScript Apple Music: структура сайта изменилась. "
                "Задайте актуальный APPLE_MUSIC_BEARER_TOKEN вручную."
            )
        javascript = get_public(asset.group(1))
        token = re.search(
            r'''["'](eyJ[A-Za-z0-9_-]+\.eyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+)["']''',
            javascript,
        )
        if not token:
            raise MigrationError(
                "Не найден веб-токен Apple Music: структура сайта изменилась. "
                "Задайте актуальный APPLE_MUSIC_BEARER_TOKEN вручную."
            )
        return token.group(1)
    finally:
        session.close()


def _resources(payload: dict) -> list[dict]:
    data = payload.get("data")
    if not isinstance(data, list) or any(not isinstance(item, dict) for item in data):
        raise MigrationError("Apple Music вернул неполные данные; перенос остановлен.")
    return data


def _song(resource: dict) -> Song:
    identifier = resource.get("id")
    if not isinstance(identifier, str) or not identifier:
        raise MigrationError("Apple Music вернул трек без идентификатора; перенос остановлен.")
    attributes = resource.get("attributes") or {}
    rating = attributes.get("contentRating")
    duration = attributes.get("durationInMillis")
    catalog_ids = []
    if resource.get("type") == "songs":
        catalog_ids.append(identifier)
    elif resource.get("type") == "library-songs":
        catalog_id = (attributes.get("playParams") or {}).get("catalogId")
        if catalog_id:
            catalog_ids.append(str(catalog_id))
        catalog = (resource.get("relationships") or {}).get("catalog") or {}
        for item in catalog.get("data", []):
            if item.get("type") == "songs" and item.get("id"):
                catalog_ids.append(str(item["id"]))
    artist = attributes.get("artistName") or ""
    return Song(
        id=identifier,
        title=attributes.get("name") or "",
        artists=(artist,) if artist else (),
        duration_ms=duration if isinstance(duration, int) and duration > 0 else None,
        isrc=re.sub(r"[^A-Z0-9]", "", str(attributes.get("isrc") or "").upper()),
        album=attributes.get("albumName") or "",
        explicit=True if rating == "explicit" else False if rating == "clean" else None,
        catalog_ids=tuple(dict.fromkeys(catalog_ids)),
    )


class AppleMusic:
    """Keep credentials on the fixed Apple API origin and never retry writes."""

    def __init__(self, bearer_token: str, user_token: str):
        if not bearer_token.strip() or not user_token.strip():
            raise MigrationError("Нужны оба токена Apple Music. Выполните auth-apple.")
        if any(character in bearer_token + user_token for character in "\r\n"):
            raise MigrationError("Некорректный токен Apple Music. Повторите auth-apple.")
        self.session = requests.Session()
        self.session.headers.update({
            "Authorization": f"Bearer {bearer_token.strip()}",
            "Origin": WEB_ORIGIN,
            "Referer": WEB_ORIGIN + "/",
            "Accept": "application/json",
        })
        self.session.cookies.set(
            "media-user-token", user_token.strip(),
            domain="amp-api.music.apple.com", path="/", secure=True,
        )
        self._storefront: str | None = None
        self._catalog: dict[str, Song | None] = {}
        self._candidate_cache: dict[tuple, tuple[Song, ...]] = {}
        self._next_request_at = 0.0

    def close(self) -> None:
        self.session.close()

    def _request(self, method: str, path: str, *, params=None, json=None) -> dict:
        url = _fixed_url(path, API_ORIGIN, API_ORIGIN + "/")
        if not urlsplit(url).path.startswith("/v1/"):
            raise MigrationError("Apple Music вернул неизвестный API-адрес; запрос остановлен.")
        mutation = method != "GET"
        suffix = _UNCERTAIN if mutation else ""
        for attempt in range(4):
            delay = self._next_request_at - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            try:
                response = self.session.request(
                    method, url, params=params, json=json,
                    timeout=TIMEOUT, allow_redirects=False,
                )
            except requests.RequestException:
                raise MigrationError(
                    "Не удалось получить ответ Apple Music. Проверьте подключение к сети." + suffix
                ) from None
            finally:
                self._next_request_at = time.monotonic() + REQUEST_INTERVAL
            if response.status_code != 429 or mutation or attempt == 3:
                break
            retry_after = response.headers.get("Retry-After", "")
            delay = max(60 * 2 ** attempt, int(retry_after) if retry_after.isdecimal() else 0)
            print(f"Apple Music: ограничение чтения (429), пауза {delay} с.", flush=True)
            time.sleep(delay)
        status = response.status_code
        if status == 401:
            message = "Apple Music отклонил авторизацию (401). Обновите токены через auth-apple."
        elif status == 403:
            message = (
                "Apple Music запретил доступ (403). Проверьте активную подписку, "
                "доступ к медиатеке и повторите auth-apple."
            )
        elif status == 429:
            message = "Apple Music ограничил частоту запросов (429). Подождите и запустите позднее."
        elif not 200 <= status < 300:
            message = f"Ошибка Apple Music (HTTP {status}). Проверьте доступ и повторите позднее."
        else:
            message = ""
        if message:
            raise MigrationError(message + suffix)
        if status == 204 or (status == 202 and method == "POST" and urlsplit(url).path == "/v1/me/library"):
            return {}
        try:
            payload = response.json()
        except ValueError:
            raise MigrationError("Apple Music вернул не JSON; перенос остановлен." + suffix) from None
        if not isinstance(payload, dict) or payload.get("errors"):
            raise MigrationError("Apple Music вернул ошибку; перенос остановлен." + suffix)
        return payload

    def _pages(self, path: str, *, params=None, search: bool = False):
        seen = set()
        url = _fixed_url(path, API_ORIGIN, API_ORIGIN + "/")
        while True:
            if url in seen:
                raise MigrationError("Apple Music повторил страницу; перенос остановлен.")
            seen.add(url)
            payload = self._request("GET", url, params=params)
            if search and "results" in payload:
                payload = payload["results"].get("songs", {"data": []})
            yield from _resources(payload)
            next_page = payload.get("next")
            if not next_page:
                break
            if not isinstance(next_page, str):
                raise MigrationError("Apple Music вернул некорректную следующую страницу.")
            url = _fixed_url(next_page, API_ORIGIN, url)
            params = None

    def storefront(self) -> str:
        if self._storefront is None:
            account = self._request("GET", "/v1/me/account", params={"meta": "subscription"})
            subscription = (account.get("meta") or {}).get("subscription") or {}
            if subscription.get("active") is not True:
                raise MigrationError(
                    "Нет активной подписки Apple Music. Активируйте её и повторите auth-apple."
                )
            storefront = subscription.get("storefront")
            if not isinstance(storefront, str) or not re.fullmatch(r"[A-Za-z]{2}", storefront):
                raise MigrationError("Не удалось определить страну Apple Music. Повторите auth-apple.")
            self._storefront = storefront.lower()
        return self._storefront

    def playlists(self) -> list[Playlist]:
        playlists = []
        for resource in self._pages("/v1/me/library/playlists", params={"limit": 100}):
            attributes = resource.get("attributes") or {}
            if not resource.get("id") or not isinstance(attributes.get("name"), str):
                raise MigrationError("Apple Music вернул неполный плейлист; перенос остановлен.")
            playlists.append(Playlist(
                id=str(resource["id"]), name=attributes["name"],
                editable=attributes.get("canEdit") is True,
            ))
        return playlists

    def _enrich_catalog(self, identifiers: list[str]) -> None:
        missing = [identifier for identifier in dict.fromkeys(identifiers) if identifier not in self._catalog]
        if not missing:
            return
        path = f"/v1/catalog/{self.storefront()}/songs"
        for offset in range(0, len(missing), BATCH_SIZE):
            batch = missing[offset:offset + BATCH_SIZE]
            found = {}
            for resource in self._pages(path, params={"ids": ",".join(batch)}):
                if resource.get("type") == "songs":
                    song = _song(resource)
                    found[song.id] = song
            for identifier in batch:
                self._catalog[identifier] = found.get(identifier)

    def tracks(self, playlist_id: str) -> list[Song]:
        path = f"/v1/me/library/playlists/{quote(playlist_id, safe='')}/tracks"
        resources = list(self._pages(path, params={"limit": 100, "include": "catalog"}))
        songs = [_song(resource) for resource in resources]
        # Playlist relationships can omit catalog identity, including for uploaded songs.
        unresolved = list(dict.fromkeys(
            song.id for song, resource in zip(songs, resources)
            if resource.get("type") == "library-songs" and not song.catalog_ids
        ))
        hydrated = {}
        for offset in range(0, len(unresolved), BATCH_SIZE):
            for resource in self._pages("/v1/me/library/songs", params={
                "ids": ",".join(unresolved[offset:offset + BATCH_SIZE]), "include": "catalog",
            }):
                hydrated[resource["id"]] = resource
        for index, song in enumerate(songs):
            if song.id in hydrated:
                resource = hydrated[song.id]
                merged = dict(resources[index])
                merged["attributes"] = {
                    **(resource.get("attributes") or {}),
                    **(resources[index].get("attributes") or {}),
                }
                merged["attributes"]["playParams"] = {
                    **((resource.get("attributes") or {}).get("playParams") or {}),
                    **((resources[index].get("attributes") or {}).get("playParams") or {}),
                }
                merged["relationships"] = {
                    **(resources[index].get("relationships") or {}),
                    **(resource.get("relationships") or {}),
                }
                songs[index] = _song(merged)
        self._enrich_catalog([identifier for song in songs for identifier in song.catalog_ids])
        result = []
        for song in songs:
            catalog = next((self._catalog.get(identifier) for identifier in song.catalog_ids
                            if self._catalog.get(identifier) is not None), None)
            if catalog is not None:
                song = replace(
                    song, title=song.title or catalog.title, artists=song.artists or catalog.artists,
                    duration_ms=song.duration_ms or catalog.duration_ms,
                    isrc=song.isrc or catalog.isrc, album=song.album or catalog.album,
                    explicit=song.explicit if song.explicit is not None else catalog.explicit,
                )
            result.append(song)
        return result

    def library_songs(self) -> list[Song]:
        """Read saved library songs, not songs appearing only in playlists."""
        return [
            _song(resource)
            for resource in self._pages(
                "/v1/me/library/songs", params={"limit": 100, "include": "catalog"},
            )
        ]

    def add_to_library(self, catalog_ids: list[str]) -> None:
        """Submit catalog songs for asynchronous addition to the saved library."""
        identifiers = list(dict.fromkeys(catalog_ids))
        if any(not identifier.isdecimal() for identifier in identifiers):
            raise MigrationError("Для медиатеки нужны идентификаторы каталога Apple Music.")
        for offset in range(0, len(identifiers), BATCH_SIZE):
            self._request(
                "POST", "/v1/me/library",
                params={"ids[songs]": ",".join(identifiers[offset:offset + BATCH_SIZE])},
            )

    def create_playlist(self, name: str) -> Playlist:
        payload = self._request("POST", "/v1/me/library/playlists", json={"attributes": {"name": name}})
        try:
            resources = _resources(payload)
            if len(resources) != 1 or not resources[0].get("id"):
                raise MigrationError("Apple Music не вернул созданный плейлист.")
        except MigrationError:
            raise MigrationError("Не удалось определить созданный плейлист." + _UNCERTAIN) from None
        attributes = resources[0].get("attributes") or {}
        return Playlist(str(resources[0]["id"]), attributes.get("name") or name,
                        attributes.get("canEdit", True) is True)

    def add_tracks(self, playlist_id: str, catalog_ids: list[str]) -> None:
        if not catalog_ids:
            return
        if any(not identifier.isdecimal() for identifier in catalog_ids):
            raise MigrationError("Для добавления нужны идентификаторы каталога, не медиатеки Apple Music.")
        path = f"/v1/me/library/playlists/{quote(playlist_id, safe='')}/tracks"
        for offset in range(0, len(catalog_ids), BATCH_SIZE):
            self._request("POST", path, json={"data": [
                {"id": identifier, "type": "songs"}
                for identifier in catalog_ids[offset:offset + BATCH_SIZE]
            ]})

    def candidates(self, song: Song) -> list[Song]:
        isrc = re.sub(r"[^A-Z0-9]", "", song.isrc.upper())
        key = (isrc, song.title, song.artists)
        if key in self._candidate_cache:
            return list(self._candidate_cache[key])
        base = f"/v1/catalog/{self.storefront()}"
        found = {}

        def collect(resources):
            for resource in resources:
                attributes = resource.get("attributes") or {}
                play = attributes.get("playParams") or {}
                if (resource.get("type") == "songs" and play.get("id")
                        and play.get("kind") == "song" and not play.get("isLibrary")):
                    candidate = _song(resource)
                    found[candidate.id] = candidate

        if isrc:
            collect(self._pages(base + "/songs", params={"filter[isrc]": isrc}))
        if not found:
            collect(self._pages(base + "/search", search=True, params={
                "types": "songs", "term": " ".join((*song.artists, song.title)), "limit": 25,
            }))
        self._candidate_cache[key] = tuple(found.values())
        return list(found.values())
