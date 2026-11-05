#!/usr/bin/env python3
"""Transfer configured Yandex Music playlists without adding duplicates."""

from __future__ import annotations

import argparse
import csv
import getpass
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from collections import Counter, defaultdict
from contextlib import ExitStack
from datetime import datetime
from pathlib import Path

import requests
from yandex_music.exceptions import YandexMusicError

from apple_music import AppleMusic, fetch_web_token
from models import MigrationError, Playlist, Song
from yandex_music_source import YandexMusic, login

PLAYLIST_MAPPING = (("общий", "all"), ("cry", "cry"), ("get popped", "get popped"))
BASE_DIR = Path(__file__).resolve().parent
SECRETS_PATH = BASE_DIR / ".secrets.json"
DURATION_TOLERANCE_MS = 3000
ARTIST_SEPARATOR = re.compile(r"\s*(?:[,;&]|\b(?:feat|ft|featuring)\.?(?=\s))\s*", re.IGNORECASE)
REPORT_FIELDS = (
    "source_playlist", "target_playlist", "yandex_id", "artist", "title", "status",
    "apple_id", "apple_artist", "apple_title", "reason",
)


def normalize(value: str) -> str:
    """Normalize typography without removing recording-version information."""
    text = unicodedata.normalize("NFKC", value).casefold().replace("ё", "е")
    return " ".join("".join(c if c.isalnum() else " " for c in text).split())


def artist_key(song: Song) -> frozenset[str]:
    return frozenset(
        normalize(part)
        for artist in song.artists
        for part in ARTIST_SEPARATOR.split(artist)
        if normalize(part)
    )


def compatible_rating(left: Song, right: Song) -> bool:
    return left.explicit is None or right.explicit is None or left.explicit == right.explicit


def same_metadata(left: Song, right: Song) -> bool:
    """Compare full titles, artists, known ratings and sufficiently close durations."""
    if not left.title or not right.title or not artist_key(left) or not artist_key(right):
        return False
    if normalize(left.title) != normalize(right.title) or artist_key(left) != artist_key(right):
        return False
    if not compatible_rating(left, right):
        return False
    if left.isrc and right.isrc and left.isrc != right.isrc:
        return False
    # Unknown duration is insufficient evidence when IDs/ISRC do not identify a recording.
    return (
        left.duration_ms is not None
        and right.duration_ms is not None
        and abs(left.duration_ms - right.duration_ms) <= DURATION_TOLERANCE_MS
    )


class DuplicateIndex:
    """Index one destination playlist, never the entire Apple Music library."""

    def __init__(self, songs: list[Song]):
        self.catalog_ids: set[str] = set()
        self.isrc_songs: dict[str, list[Song]] = defaultdict(list)
        self.metadata: dict[tuple[str, frozenset[str]], list[Song]] = defaultdict(list)
        for song in songs:
            self.add(song)

    def add(self, song: Song) -> None:
        self.catalog_ids.update(song.catalog_ids)
        if song.isrc:
            self.isrc_songs[song.isrc].append(song)
        self.metadata[(normalize(song.title), artist_key(song))].append(song)

    def contains(self, song: Song) -> bool:
        if self.catalog_ids.intersection(song.catalog_ids):
            return True
        if song.isrc and any(
            compatible_rating(song, existing) for existing in self.isrc_songs[song.isrc]
        ):
            return True
        return any(
            same_metadata(song, existing)
            for existing in self.metadata[(normalize(song.title), artist_key(song))]
        )


def choose_match(source: Song, candidates: list[Song]) -> tuple[Song | None, str]:
    """Prefer ISRC, otherwise require exact metadata instead of fuzzy guessing."""
    unique = {song.id: song for song in candidates}
    matches = [
        song for song in unique.values()
        if source.isrc and song.isrc == source.isrc and compatible_rating(source, song)
    ]
    if not matches:
        matches = [song for song in unique.values() if same_metadata(source, song)]
    if not matches:
        return None, "not_found"
    if source.album:
        same_album = [song for song in matches if normalize(song.album) == normalize(source.album)]
        if same_album:
            matches = same_album
    recordings = {song.isrc or f"id:{song.id}" for song in matches}
    if len(recordings) > 1:
        return None, "ambiguous"
    return min(
        matches,
        key=lambda song: (
            abs((song.duration_ms or 0) - (source.duration_ms or 0)), song.id,
        ),
    ), "matched"


def find_playlist(playlists: list[Playlist], name: str, service: str) -> Playlist | None:
    matches = [playlist for playlist in playlists if playlist.name.strip().casefold() == name.casefold()]
    if len(matches) > 1:
        raise MigrationError(
            f'{service}: несколько плейлистов с названием «{name}». '
            "Переименуйте лишние плейлисты, чтобы название было уникальным."
        )
    return matches[0] if matches else None


def prepare(yandex: YandexMusic, apple: AppleMusic) -> list[tuple[str, str, Playlist | None, list[Song], list[Song]]]:
    """Read every source and destination before allowing the first mutation."""
    sources = yandex.playlists()
    targets = apple.playlists()
    selected = []
    for source_name, target_name in PLAYLIST_MAPPING:
        source = find_playlist(sources, source_name, "Яндекс Музыка")
        if source is None:
            available = ", ".join(playlist.name for playlist in sources)
            raise MigrationError(
                f'Яндекс Музыка: не найден ваш плейлист «{source_name}». Доступны: {available}'
            )
        target = find_playlist(targets, target_name, "Apple Music")
        if target is not None and not target.editable:
            raise MigrationError(f'Apple Music: плейлист «{target_name}» недоступен для изменения.')
        selected.append((source_name, target_name, source, target))
    return [
        (
            source_name, target_name, target,
            yandex.tracks(source.id), apple.tracks(target.id) if target else [],
        )
        for source_name, target_name, source, target in selected
    ]


def migrate(yandex: YandexMusic, apple: AppleMusic, *, dry_run: bool, report_path: Path) -> Counter:
    """Append missing songs in source order and flush a report after every decision."""
    prepared = prepare(yandex, apple)
    total: Counter = Counter()
    with report_path.open("x", encoding="utf-8-sig", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=REPORT_FIELDS)
        writer.writeheader()
        report.flush()
        for source_name, target_name, target, source_songs, existing in prepared:
            print(f'\n«{source_name}» → «{target_name}»: источник {len(source_songs)}, в Apple {len(existing)}')
            index = DuplicateIndex(existing)
            counts: Counter = Counter()
            for number, song in enumerate(source_songs, 1):
                row = {
                    "source_playlist": source_name, "target_playlist": target_name,
                    "yandex_id": song.id, "artist": ", ".join(song.artists), "title": song.title,
                }
                match = None
                try:
                    if index.contains(song):
                        status, reason = "already_present", "Уже есть в этом плейлисте"
                    elif not song.title or not artist_key(song):
                        status, reason = "invalid_metadata", "Нет названия или исполнителя в Яндекс Музыке"
                    else:
                        match, result = choose_match(song, apple.candidates(song))
                        if match is None:
                            status = result
                            reason = (
                                "Несколько разных записей, автоматический выбор небезопасен"
                                if result == "ambiguous" else
                                "Нет уверенного совпадения в каталоге региона Apple Music"
                            )
                        elif index.contains(match):
                            status, reason = "already_present", "Совпадение уже есть в этом плейлисте"
                        elif dry_run:
                            status, reason = "would_add", "Будет добавлен при запуске без --dry-run"
                            index.add(match)
                            index.add(song)
                        else:
                            if target is None:
                                target = apple.create_playlist(target_name)
                            apple.add_tracks(target.id, [match.id])
                            status, reason = "added", "Apple Music подтвердил добавление"
                            index.add(match)
                            index.add(song)
                except (MigrationError, requests.RequestException) as error:
                    row.update(status="error", reason="Запрос не завершён. Подробности в терминале")
                    writer.writerow(row)
                    report.flush()
                    if isinstance(error, MigrationError):
                        raise
                    raise MigrationError(
                        "Сетевая ошибка. Если выполнялось добавление, результат может быть неизвестен. "
                        "Дождитесь синхронизации Apple Music и проверьте плейлист перед повторным запуском."
                    ) from None
                row.update(status=status, reason=reason)
                if match is not None:
                    row.update(apple_id=match.id, apple_artist=", ".join(match.artists), apple_title=match.title)
                writer.writerow(row)
                report.flush()
                counts[status] += 1
                print(f"  [{number}/{len(source_songs)}] {status}: {', '.join(song.artists)} — {song.title}")
            total.update(counts)
            print("  Итог: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"\nОтчёт: {report_path.resolve()}")
    return total


def save_playlist_songs_to_library(
    apple: AppleMusic, *, dry_run: bool, report_path: Path,
) -> Counter:
    """Save target playlist songs and verify asynchronous library additions."""
    playlists = apple.playlists()
    selected = [
        (name, find_playlist(playlists, name, "Apple Music")) for _, name in PLAYLIST_MAPPING
    ]
    entries = [
        (name, song)
        for name, playlist in selected if playlist is not None
        for song in apple.tracks(playlist.id)
    ]
    print("\nСохраняю песни из целевых плейлистов в медиатеку Apple Music.")
    existing = apple.library_songs()
    saved = DuplicateIndex(existing)
    planned = DuplicateIndex(existing)
    counts: Counter = Counter()
    pending: list[tuple[Song, dict]] = []
    fields = ("target_playlist", "apple_id", "artist", "title", "status")
    with report_path.open("x", encoding="utf-8-sig", newline="") as report:
        writer = csv.DictWriter(report, fieldnames=fields)
        writer.writeheader()
        for name, song in entries:
            row = {
                "target_playlist": name,
                "apple_id": song.catalog_ids[0] if song.catalog_ids else "",
                "artist": ", ".join(song.artists), "title": song.title,
            }
            if planned.contains(song):
                status = "already_in_library" if saved.contains(song) else "already_scheduled"
            elif not song.catalog_ids:
                status = "no_catalog_id"
            elif dry_run:
                status = "library_would_add"
                planned.add(song)
            else:
                pending.append((song, row))
                planned.add(song)
                continue
            row["status"] = status
            counts[status] += 1
            writer.writerow(row)
        report.flush()
        for offset in range(0, len(pending), 100):
            batch = pending[offset:offset + 100]
            try:
                apple.add_to_library([song.catalog_ids[0] for song, _ in batch])
            except MigrationError:
                for _, row in batch:
                    writer.writerow({**row, "status": "error"})
                report.flush()
                print(f"Отчёт медиатеки: {report_path.resolve()}")
                raise
            for _, row in batch:
                writer.writerow({**row, "status": "library_add_requested"})
                counts["library_add_requested"] += 1
            report.flush()
            print(f"Принято для добавления в медиатеку: {min(offset + len(batch), len(pending))}/{len(pending)}")
    if pending:
        for delay in (2, 5, 10, 20):
            time.sleep(delay)
            current = DuplicateIndex(apple.library_songs())
            missing = [song for song, _ in pending if not current.contains(song)]
            if not missing:
                print(f"Проверено в медиатеке: {len(pending)} добавленных песен.")
                break
        else:
            raise MigrationError(
                f"Apple принял добавление, но {len(missing)} песен ещё не видны в медиатеке. "
                "Дождитесь синхронизации и выполните save-library: сначала состав будет прочитан заново."
            )
    print("Медиатека: " + ", ".join(f"{key}={value}" for key, value in sorted(counts.items())))
    print(f"Отчёт медиатеки: {report_path.resolve()}")
    return counts


def load_secrets() -> dict[str, str]:
    if not SECRETS_PATH.exists():
        return {}
    try:
        data = json.loads(SECRETS_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, UnicodeDecodeError):
        raise MigrationError("Повреждён .secrets.json. Повторите команды авторизации.") from None
    if not isinstance(data, dict) or any(not isinstance(value, str) for value in data.values()):
        raise MigrationError("Неверный формат .secrets.json. Повторите команды авторизации.")
    return data


def save_secret(key: str, value: str) -> None:
    data = load_secrets()
    data[key] = value
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="w", encoding="utf-8", dir=BASE_DIR, prefix=".secrets-", suffix=".tmp", delete=False,
        ) as handle:
            temporary = Path(handle.name)
            json.dump(data, handle, ensure_ascii=False, indent=2)
        temporary.chmod(0o600)
        temporary.replace(SECRETS_PATH)
    finally:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
    print("Доступ сохранён локально в .secrets.json. Не передавайте этот файл другим людям.")


def credential(environment: str, key: str, command: str) -> str:
    value = (os.environ.get(environment) or load_secrets().get(key, "")).strip()
    if not value:
        raise MigrationError(f"Нет доступа к сервису. Выполните: uv run python transfer_music.py {command}")
    if "\n" in value or "\r" in value:
        raise MigrationError(f"{environment}: токен должен занимать одну строку.")
    return value


def connect_apple(user_token: str) -> AppleMusic:
    return AppleMusic(os.environ.get("APPLE_MUSIC_BEARER_TOKEN", "").strip() or fetch_web_token(), user_token)


def parser() -> argparse.ArgumentParser:
    result = argparse.ArgumentParser(description="Перенос плейлистов Яндекс Музыки в Apple Music без дублей.")
    result.add_argument("--dry-run", action="store_true", help="Только поиск и отчёт, без изменений Apple Music")
    result.add_argument("--report", type=Path, help="Новый CSV-файл отчёта (существующий файл не перезаписывается)")
    commands = result.add_subparsers(dest="command")
    auth = commands.add_parser("auth-yandex", help="Подключить Яндекс Музыку")
    auth.add_argument("--manual", action="store_true", help="Вставить готовый OAuth-токен скрытым вводом")
    commands.add_parser("auth-apple", help="Подключить Apple Music через cookie веб-плеера")
    commands.add_parser("check", help="Проверить доступ и прочитать настроенные плейлисты без изменений")
    commands.add_parser("save-library", help="Сохранить песни целевых плейлистов в медиатеку")
    return result


def main() -> int:
    arguments = parser().parse_args()
    with ExitStack() as stack:
        if arguments.command == "auth-yandex":
            token = getpass.getpass("OAuth-токен Яндекс Музыки: ").strip() if arguments.manual else login()
            if not token:
                raise MigrationError("Пустой токен. Авторизация не сохранена.")
            yandex = YandexMusic(token)
            print(f"Яндекс Музыка подключена, ваших плейлистов: {len(yandex.playlists())}")
            save_secret("yandex_music_token", token)
            return 0
        if arguments.command == "auth-apple":
            print("Войдите на https://music.apple.com и скопируйте cookie media-user-token (см. README.md).")
            token = getpass.getpass("media-user-token (ввод не отображается): ").strip()
            if not token or "\n" in token or "\r" in token:
                raise MigrationError("Нужен непустой media-user-token одной строкой.")
            apple = connect_apple(token)
            stack.callback(apple.close)
            region = apple.storefront()
            print(f"Apple Music подключена. Регион: {region}, плейлистов: {len(apple.playlists())}")
            save_secret("apple_music_user_token", token)
            return 0
        if arguments.command == "save-library":
            apple = connect_apple(credential("APPLE_MUSIC_USER_TOKEN", "apple_music_user_token", "auth-apple"))
            stack.callback(apple.close)
            print(f"Регион Apple Music: {apple.storefront()}")
            report_path = arguments.report or BASE_DIR / f"migration-report-library-{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
            counts = save_playlist_songs_to_library(apple, dry_run=arguments.dry_run, report_path=report_path)
            return 2 if counts["no_catalog_id"] else 0
        yandex = YandexMusic(credential("YANDEX_MUSIC_TOKEN", "yandex_music_token", "auth-yandex"))
        apple = connect_apple(credential("APPLE_MUSIC_USER_TOKEN", "apple_music_user_token", "auth-apple"))
        stack.callback(apple.close)
        print(f"Регион Apple Music: {apple.storefront()}")
        if arguments.command == "check":
            for source_name, target_name, target, source_songs, existing in prepare(yandex, apple):
                destination = str(len(existing)) if target else "будет создан при первом добавлении"
                print(f"«{source_name}» ({len(source_songs)}) → «{target_name}» ({destination})")
            print("Доступ проверен. Плейлисты не изменены.")
            return 0
        report_path = arguments.report or BASE_DIR / f"migration-report-{datetime.now():%Y%m%d-%H%M%S-%f}.csv"
        if arguments.dry_run:
            print("ПРОБНЫЙ ЗАПУСК: треки и плейлисты Apple Music не изменяются.")
        counts = migrate(yandex, apple, dry_run=arguments.dry_run, report_path=report_path)
        if not arguments.dry_run:
            library_report = report_path.with_name(report_path.stem + "-library.csv")
            counts.update(save_playlist_songs_to_library(apple, dry_run=False, report_path=library_report))
        return 2 if any(counts[key] for key in ("not_found", "ambiguous", "invalid_metadata", "no_catalog_id")) else 0


def entrypoint() -> int:
    try:
        return main()
    except KeyboardInterrupt:
        print("\nОстановлено. Если был запрос добавления, дождитесь синхронизации Apple Music перед повтором.", file=sys.stderr)
        return 130
    except MigrationError as error:
        print(f"Ошибка: {error}", file=sys.stderr)
    except YandexMusicError:
        print("Ошибка Яндекс Музыки. Проверьте сеть и повторите auth-yandex. Данные аккаунта не выводятся.", file=sys.stderr)
    except requests.RequestException:
        print("Сетевая ошибка. Проверьте соединение и доступность сервисов. Токены не выводятся.", file=sys.stderr)
    except FileExistsError:
        print("Файл отчёта уже существует. Укажите новое имя через --report.", file=sys.stderr)
    except OSError:
        print("Не удалось прочитать или сохранить локальный файл. Проверьте путь и права доступа.", file=sys.stderr)
    return 1


if __name__ == "__main__":
    sys.exit(entrypoint())
