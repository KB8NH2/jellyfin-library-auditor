"""Search tvdb_cache.json for series by title and show their cached episode positions.

Prints, for each cached TheTVDB series whose name contains the given search
text (case-insensitive), the exact data :func:`audit.mismatched_tvdb_series`
uses to decide whether a series is matched to the wrong TheTVDB entry: its
aired-order and DVD-order episode lists, grouped by season and episode.
Useful for manually checking why a series was (or wasn't) flagged, or what
TheTVDB thinks a series' season/episode numbering looks like, without making
any network calls.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

DEFAULT_CACHE_PATH = Path("tvdb_cache.json")
SEASON_TYPE_LABELS = {"official": "Aired order", "dvd": "DVD order"}


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("title", help="Series title (or substring) to search for, case-insensitive")
    parser.add_argument(
        "--cache",
        type=Path,
        default=DEFAULT_CACHE_PATH,
        help=f"Path to the TheTVDB cache file (default: {DEFAULT_CACHE_PATH})",
    )
    return parser.parse_args(argv)


def load_cache(cache_path: Path) -> dict[str, Any]:
    try:
        raw_document = json.loads(cache_path.read_text(encoding="utf-8"))
    except OSError as error:
        raise SystemExit(f"Could not read {cache_path}: {error}")
    except ValueError as error:
        raise SystemExit(f"{cache_path} is not valid JSON: {error}")

    if not isinstance(raw_document, dict):
        raise SystemExit(f"{cache_path} does not contain a TheTVDB cache document.")
    return raw_document


def matching_series(raw_document: dict[str, Any], title: str) -> list[tuple[str, str, dict[str, Any]]]:
    """Return (series_id, name, series_document) for every cached series matching ``title``."""
    raw_series = raw_document.get("series")
    if not isinstance(raw_series, dict):
        return []

    needle = title.casefold()
    matches: list[tuple[str, str, dict[str, Any]]] = []
    for series_id, series_document in raw_series.items():
        if not isinstance(series_document, dict):
            continue
        name = series_document.get("name")
        if not isinstance(name, str) or needle not in name.casefold():
            continue
        matches.append((series_id, name, series_document))

    matches.sort(key=lambda entry: (entry[1].casefold(), entry[0]))
    return matches


def print_series(series_id: str, name: str, series_document: dict[str, Any]) -> None:
    print(f"{name} (TheTVDB id {series_id})")
    print("=" * (len(name) + len(series_id) + 16))

    for season_type in ("official", "dvd"):
        entry = series_document.get(season_type)
        label = SEASON_TYPE_LABELS[season_type]
        if not isinstance(entry, dict):
            print(f"\n{label}: not cached")
            continue

        episodes = entry.get("episodes")
        fetched_at = entry.get("fetched_at", "unknown")
        if not isinstance(episodes, list) or not episodes:
            print(f"\n{label}: cached (fetched {fetched_at}), but no episodes")
            continue

        print(f"\n{label} (fetched {fetched_at}):")
        by_season: dict[Any, list[dict[str, Any]]] = {}
        for episode in episodes:
            if not isinstance(episode, dict):
                continue
            by_season.setdefault(episode.get("season_number"), []).append(episode)

        for season_number in sorted(by_season, key=lambda value: (value is None, value)):
            season_episodes = sorted(
                by_season[season_number],
                key=lambda episode: (episode.get("episode_number") is None, episode.get("episode_number")),
            )
            print(f"  Season {season_number}:")
            for episode in season_episodes:
                episode_number = episode.get("episode_number")
                episode_name = episode.get("name") or "(untitled)"
                position = f"S{season_number:02d}E{episode_number:02d}" if isinstance(
                    season_number, int
                ) and isinstance(episode_number, int) else f"S{season_number}E{episode_number}"
                print(f"    {position}  {episode_name}")
    print()


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    raw_document = load_cache(args.cache)

    matches = matching_series(raw_document, args.title)
    if not matches:
        print(f"No cached series match {args.title!r} in {args.cache}.")
        return

    print(f"{len(matches)} series match {args.title!r} in {args.cache}:\n")
    for series_id, name, series_document in matches:
        print_series(series_id, name, series_document)


if __name__ == "__main__":
    main()
