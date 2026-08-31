#!/usr/bin/python3
"""CLI to rename a movie's or episode's title(s) to what their filename implies.

This is the filename-only sibling to apply_episode_titles.py: instead of
renaming toward TheTVDB's aired/DVD-order title, it renames toward the title
media.expected_episode_title_from_filename()/expected_movie_title_from_filename()
derive from the item's own on-disk filename under Jellyfin's naming
convention (``Show S01E02 Episode Title.mkv`` for an episode, ``Movie Name
(Year).mkv`` for a movie). It never contacts TheTVDB or any other internet
metadata provider - the new title comes entirely from the filename already
on disk.

Exactly one target must be given: --movie for a single movie, or
--series-name together with --season-number for every episode in one
season. Whether a rename is even needed is decided with audit.titles_match(),
the same lenient comparison the sibling tools use (punctuation, articles,
accents, US/UK spelling, roman-numeral part suffixes, and more are all
treated as equivalent) - an item already reading the same as its
filename-implied title under those rules is left alone. Before an actual
rename, the item's current Name is backed up into OriginalTitle, the same
convention apply_dvd_metadata.py/apply_episode_titles.py use; see
title_backup.py for the shared rename/backup/restore logic.

--restore reverses a previous rename: it sets each item's Name back to its
own OriginalTitle backup, purely locally - it never inspects the filename
and never contacts TheTVDB. An item with no OriginalTitle backup (never
renamed by this tool) is left alone.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import logging
import shlex
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audit import titles_match
from config import ConfigError
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from media import expected_episode_title_from_filename
from media import expected_movie_title_from_filename
from models import MediaItem
from title_backup import build_title_merged_item_dto
from title_backup import build_title_restore_merged_item_dto
from transfer_metadata import changed_fields
from transfer_metadata import rejected_reason as _rejected_reason


LOGGER = logging.getLogger("apply_titles_from_filename")

# Append-only record of every apply attempt, mirroring
# apply_episode_titles.py's EPISODE_TITLES_LOG_FILE convention.
TITLES_FROM_FILENAME_LOG_FILE = Path("titles_from_filename_apply.log")

# Fields this tool ever writes, and therefore diffs/locks. OriginalTitle only
# ever changes as a side effect of a Name change (see title_backup.py),
# never on its own.
METADATA_FIELDS = ("Name", "OriginalTitle")

# One resolved rename/restore target: the item's id, a display label for log
# output, its already-known current Name, and the title its filename implies
# (None when the filename has no recognizable marker to derive one from).
# Movie and episode lookups both collapse to this same shape so planning and
# applying need only one code path regardless of which target was given.
TitleTarget = tuple[str, str, str, str | None]


def configure_logging() -> None:
    """Configure INFO-level application logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _log_line(message: str, *, error: bool = False) -> None:
    """Emit one line of output to the console and to the log file."""
    if error:
        LOGGER.error(message)
    else:
        print(message)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level = "ERROR" if error else "INFO"
    with TITLES_FROM_FILENAME_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} apply_titles_from_filename: {message}\n")


def _expected_title_for_episode(
    path: Path,
    season_number: int,
    episode_number: int,
) -> str | None:
    """Return the episode title implied by one episode's on-disk filename."""
    item = MediaItem(
        id="",
        title="",
        path=path,
        is_movie=False,
        is_episode=True,
        library="",
        series_name=None,
        season_name=None,
        season_number=season_number,
        episode_number=episode_number,
        year=None,
        runtime_ticks=None,
        image_tags={},
        subtitle_tracks=(),
        audio_tracks=(),
        video_track=None,
    )
    return expected_episode_title_from_filename(item)


def _expected_title_for_movie(path: Path, year: int) -> str | None:
    """Return the movie title implied by one movie's on-disk filename."""
    item = MediaItem(
        id="",
        title="",
        path=path,
        is_movie=True,
        is_episode=False,
        library="",
        series_name=None,
        season_name=None,
        season_number=None,
        episode_number=None,
        year=year,
        runtime_ticks=None,
        image_tags={},
        subtitle_tracks=(),
        audio_tracks=(),
        video_track=None,
    )
    return expected_movie_title_from_filename(item)


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return changed_fields(destination_dto, merged_dto, METADATA_FIELDS)


@dataclass(frozen=True, slots=True)
class FilenameTitlePlan:
    """A computed, not-yet-applied title rename for one movie or episode.

    Separating planning from applying lets every target be previewed and
    confirmed in one batch before anything is written.
    """

    item_id: str
    label: str
    current_name: str
    target_name: str | None
    merged_dto: dict[str, Any] | None
    changes: tuple[tuple[str, Any, Any], ...]
    rejected_reason: str | None
    no_target_match: bool
    already_matches: bool

    @property
    def has_changes(self) -> bool:
        """Return whether applying this plan would change anything."""
        return bool(self.changes)

    @property
    def is_rejected(self) -> bool:
        """Return whether this plan failed the pre-write safety check."""
        return self.rejected_reason is not None

    @property
    def is_actionable(self) -> bool:
        """Return whether this plan should actually be applied."""
        return (
            not self.no_target_match
            and not self.already_matches
            and not self.is_rejected
            and self.has_changes
        )


def plan_filename_title_update(
    client: JellyfinClient,
    item_id: str,
    label: str,
    known_name: str,
    target_name: str | None,
) -> FilenameTitlePlan:
    """Compute one item's title rename toward its filename-implied title.

    Args:
        client: Client for the server the item lives on.
        item_id: Jellyfin item identifier to plan a rename for.
        label: Display label for this item, used in log output.
        known_name: The item's already-known current Name, used to skip a
            network round trip when it's obviously already correct.
        target_name: The title implied by the item's filename, or ``None``
            when the filename has no recognizable marker to derive one from.

    Returns:
        A plan describing what would change and whether it's safe to apply.
    """
    if target_name is None:
        return FilenameTitlePlan(
            item_id=item_id,
            label=label,
            current_name=known_name,
            target_name=None,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
            already_matches=False,
        )

    if titles_match(known_name, target_name):
        return FilenameTitlePlan(
            item_id=item_id,
            label=label,
            current_name=known_name,
            target_name=target_name,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=False,
            already_matches=True,
        )

    destination_dto = client.get_item(item_id)
    current_name = str(destination_dto.get("Name", known_name))

    # known_name can be stale (e.g. changed since the item was listed) -
    # re-check against the freshly-fetched live Name before deciding a
    # rename is actually needed.
    if titles_match(current_name, target_name):
        return FilenameTitlePlan(
            item_id=item_id,
            label=label,
            current_name=current_name,
            target_name=target_name,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=False,
            already_matches=True,
        )

    merged_dto = build_title_merged_item_dto(destination_dto, target_name)
    return FilenameTitlePlan(
        item_id=item_id,
        label=label,
        current_name=current_name,
        target_name=target_name,
        merged_dto=merged_dto,
        changes=_changed_fields(destination_dto, merged_dto),
        rejected_reason=_rejected_reason(merged_dto),
        no_target_match=False,
        already_matches=False,
    )


def plan_filename_title_restore(
    client: JellyfinClient,
    item_id: str,
    label: str,
    known_name: str,
) -> FilenameTitlePlan:
    """Compute one item's title restore from its own OriginalTitle backup.

    Unlike plan_filename_title_update(), this needs no filename inspection
    at all - OriginalTitle is this tool's own backup, written the last time
    this item's title was changed by a (non-restore) run, so restoring from
    it is a purely local operation.

    Args:
        client: Client for the server the item lives on.
        item_id: Jellyfin item identifier to plan a restore for.
        label: Display label for this item, used in log output.
        known_name: The item's already-known current Name, used as a
            fallback if the freshly-fetched document has none.

    Returns:
        A plan describing what would change and whether it's safe to apply.
        ``no_target_match`` is set when the item has no OriginalTitle backup
        to restore from.
    """
    destination_dto = client.get_item(item_id)
    current_name = str(destination_dto.get("Name", known_name))
    original_title = destination_dto.get("OriginalTitle") or None

    if not original_title:
        return FilenameTitlePlan(
            item_id=item_id,
            label=label,
            current_name=current_name,
            target_name=None,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
            already_matches=False,
        )

    if original_title == current_name:
        return FilenameTitlePlan(
            item_id=item_id,
            label=label,
            current_name=current_name,
            target_name=original_title,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=False,
            already_matches=True,
        )

    merged_dto = build_title_restore_merged_item_dto(destination_dto, original_title)
    return FilenameTitlePlan(
        item_id=item_id,
        label=label,
        current_name=current_name,
        target_name=original_title,
        merged_dto=merged_dto,
        changes=_changed_fields(destination_dto, merged_dto),
        rejected_reason=_rejected_reason(merged_dto),
        no_target_match=False,
        already_matches=False,
    )


def apply_filename_title_plan(client: JellyfinClient, plan: FilenameTitlePlan) -> None:
    """Write a previously computed, actionable plan to the server.

    Args:
        client: Client for the server the item lives on.
        plan: A plan from plan_filename_title_update()/plan_filename_title_restore()
            that is actionable.
    """
    if plan.merged_dto is None:
        raise ValueError("Cannot apply a plan with no target title.")
    client.update_item(plan.item_id, plan.merged_dto)


def _verify_applied(client: JellyfinClient, plan: FilenameTitlePlan) -> tuple[str, ...]:
    """Re-read an item right after writing it and report any field that didn't change.

    A successful HTTP response from update_item only means Jellyfin accepted
    the write, not that the value stuck - a locked/provider-owned field can
    silently keep its old value. Mirrors apply_episode_titles.py's identical
    check for the same reason.

    Args:
        client: Client for the server the item lives on.
        plan: The plan that was just applied.

    Returns:
        The field names that still report their pre-update value.
    """
    current_dto = client.get_item(plan.item_id)
    return tuple(
        field
        for field, _, expected_value in plan.changes
        if current_dto.get(field) != expected_value
    )


def _describe_plan(plan: FilenameTitlePlan, *, restore: bool = False) -> None:
    """Log one item's planned outcome."""
    if plan.no_target_match:
        if restore:
            _log_line(f"  {plan.label}: no OriginalTitle backup to restore from - skipped.")
        else:
            _log_line(
                f"  {plan.label}: filename has no recognizable title marker - skipped."
            )
        return
    if plan.is_rejected:
        _log_line(f"  {plan.label}: rejected: {plan.rejected_reason}", error=True)
        return
    if plan.already_matches:
        if restore:
            _log_line(f"  {plan.label}: already matches its backed-up title {plan.target_name!r}.")
        else:
            _log_line(
                f"  {plan.label}: already matches its filename-implied title "
                f"{plan.target_name!r}."
            )
        return
    for field, old_value, new_value in plan.changes:
        _log_line(f"  {plan.label} {field}: {old_value!r} -> {new_value!r}")


def _resolve_movie_targets(
    client: JellyfinClient,
    movie_name: str,
    library_name: str | None,
    path_filter: str | None,
    server_name: str,
) -> tuple[TitleTarget, ...] | None:
    """Resolve --movie to the one Jellyfin item it names, with its filename-implied title.

    Returns:
        A one-element tuple of the resolved target, or ``None`` when the
        movie could not be uniquely resolved (already logged in that case).
    """
    matches = client.find_movie(movie_name, library_name=library_name, path_filter=path_filter)
    if not matches:
        _log_line(f"No movie named {movie_name!r} was found on {server_name}.", error=True)
        return None
    if len(matches) > 1:
        library_names = ", ".join(sorted(match.library_name for match in matches))
        _log_line(
            f"Movie name {movie_name!r} matches items in more than one "
            f"library ({library_names}) on {server_name}. Use --library and/or "
            "--path to disambiguate.",
            error=True,
        )
        return None

    match = matches[0]
    if match.path is None or match.year is None:
        _log_line(
            f"{movie_name!r} in library {match.library_name!r} is missing a file path "
            "or release year, so no filename-implied title can be computed.",
            error=True,
        )
        return None

    target_name = _expected_title_for_movie(match.path, match.year)
    return ((match.movie_id, match.name, match.name, target_name),)


def _resolve_episode_targets(
    client: JellyfinClient,
    series_name: str,
    season_number: int,
    library_name: str | None,
    path_filter: str | None,
    server_name: str,
) -> tuple[TitleTarget, ...] | None:
    """Resolve --series-name/--season-number to every episode in that season.

    Returns:
        One resolved target per episode in the season (possibly empty when
        the season has no episodes), or ``None`` when the series could not
        be uniquely resolved (already logged in that case).
    """
    matches = client.find_series(series_name, library_name=library_name, path_filter=path_filter)
    if not matches:
        _log_line(f"No series named {series_name!r} was found on {server_name}.", error=True)
        return None
    if len(matches) > 1:
        library_names = ", ".join(sorted(match.library_name for match in matches))
        _log_line(
            f"Series name {series_name!r} matches shows in more than one "
            f"library ({library_names}) on {server_name}. Use --library and/or "
            "--path to disambiguate.",
            error=True,
        )
        return None

    match = matches[0]
    episodes = client.get_series_season_episodes_all(match.series_id, season_number)

    targets: list[TitleTarget] = []
    for episode in episodes:
        if episode.episode_number is not None:
            label = f"S{season_number:02d}E{episode.episode_number:02d}"
        else:
            label = episode.name

        if episode.path is None or episode.episode_number is None:
            target_name = None
        else:
            target_name = _expected_title_for_episode(
                episode.path, season_number, episode.episode_number
            )
        targets.append((episode.id, label, episode.name, target_name))

    return tuple(targets)


def run_apply_titles_from_filename(
    *,
    movie_name: str | None,
    series_name: str | None,
    season_number: int | None,
    server_key: str | None,
    library_name: str | None,
    path_filter: str | None = None,
    assume_yes: bool,
    restore: bool = False,
) -> int:
    """Rename a movie's or a season's episode titles to their filename-implied
    title, or restore them from each item's own OriginalTitle backup.

    Args:
        movie_name: Movie display name to match in Jellyfin. Mutually
            exclusive with series_name/season_number.
        series_name: Series display name to match in Jellyfin. Mutually
            exclusive with movie_name; requires season_number.
        season_number: Season number to update. Requires series_name.
        server_key: Configured server key from servers.toml, or ``None`` to
            use servers.toml's default_server.
        library_name: Library name to restrict the movie/series search to,
            or ``None`` to search every movie/TV library.
        path_filter: When given, only a movie/series whose Path contains
            this text (case-insensitively) is considered - disambiguates a
            name that matches more than one item.
        assume_yes: Skip the interactive confirmation prompt when ``True``.
        restore: Restore each item's Name from its own OriginalTitle backup
            instead of renaming toward its filename-implied title - a purely
            local operation that never inspects the filename and never
            contacts TheTVDB. An item with no OriginalTitle backup is left
            alone.

    Returns:
        A process exit code: ``0`` on success (including "nothing to do"),
        ``1`` if any item was rejected/failed or the user declined, ``2`` on
        a usage/configuration error.

    Raises:
        ValueError: If season_number is given without series_name, or vice
            versa, or if neither movie_name nor series_name is given.
    """
    if movie_name is not None and series_name is not None:
        raise ValueError("movie_name and series_name are mutually exclusive.")
    if movie_name is None and series_name is None:
        raise ValueError("One of movie_name or series_name is required.")
    if series_name is not None and season_number is None:
        raise ValueError("series_name requires season_number.")
    if season_number is not None and series_name is None:
        raise ValueError("season_number requires series_name.")

    app_config = get_config()

    try:
        server = (
            app_config.servers.get(server_key)
            if server_key is not None
            else app_config.servers.get_default()
        )
    except ConfigError as error:
        _log_line(str(error), error=True)
        return 2

    verb = "restore" if restore else "rename"
    past_participle = "restored" if restore else "renamed"

    try:
        with JellyfinClient(server) as client:
            if movie_name is not None:
                LOGGER.debug("Looking up movie %r on %s...", movie_name, server.name)
                targets = _resolve_movie_targets(
                    client, movie_name, library_name, path_filter, server.name
                )
                subject = f"movie {movie_name!r}"
            else:
                LOGGER.debug("Looking up series %r on %s...", series_name, server.name)
                targets = _resolve_episode_targets(
                    client, series_name, season_number, library_name, path_filter, server.name
                )
                subject = f"{series_name!r} season {season_number}"

            if targets is None:
                return 1
            if not targets:
                _log_line(f"No episodes found for {subject} on {server.name}.")
                return 0

            if restore:
                plans = tuple(
                    plan_filename_title_restore(client, item_id, label, known_name)
                    for item_id, label, known_name, _target_name in targets
                )
                _log_line(f"Title restore: {subject} on {server.name}")
            else:
                plans = tuple(
                    plan_filename_title_update(client, item_id, label, known_name, target_name)
                    for item_id, label, known_name, target_name in targets
                )
                _log_line(f"Title rename from filename: {subject} on {server.name}")

            for plan in plans:
                _describe_plan(plan, restore=restore)

            actionable_plans = tuple(plan for plan in plans if plan.is_actionable)
            rejected_plans = tuple(plan for plan in plans if plan.is_rejected)

            if not actionable_plans:
                _log_line("Nothing to do.")
                return 1 if rejected_plans else 0

            if not assume_yes:
                if restore:
                    prompt = (
                        f"Restore {len(actionable_plans)} item(s) to their "
                        "backed-up title? [y/N] "
                    )
                else:
                    prompt = (
                        f"Rename {len(actionable_plans)} item(s) to their "
                        "filename-implied title? [y/N] "
                    )
                response = input(prompt).strip().lower()
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            failed = 0
            for plan in actionable_plans:
                LOGGER.debug("Applying %s...", plan.label)
                try:
                    apply_filename_title_plan(client, plan)
                except JellyfinError as error:
                    failed += 1
                    _log_line(f"  {plan.label}: failed: {error}", error=True)
                    continue

                LOGGER.debug("Verifying %s...", plan.label)
                stale_fields = _verify_applied(client, plan)
                if stale_fields:
                    failed += 1
                    _log_line(
                        f"  {plan.label}: update did not take effect for "
                        f"{', '.join(stale_fields)} - Jellyfin still reports the old "
                        "value(s) right after the write. This usually means an "
                        "internet metadata provider is enabled for this library and "
                        "overwrote the edit again on its own refresh.",
                        error=True,
                    )
                else:
                    _log_line(f"  {plan.label}: {past_participle}.")

            already_matching_count = sum(1 for plan in plans if plan.already_matches)
            no_match_count = sum(1 for plan in plans if plan.no_target_match)
            no_match_label = (
                "no OriginalTitle backup" if restore else "no filename-implied title"
            )
            _log_line(
                f"Title {verb} complete: {len(actionable_plans) - failed} {past_participle}, "
                f"{failed} failed, {len(rejected_plans)} rejected, "
                f"{already_matching_count} already matching, "
                f"{no_match_count} with {no_match_label}."
            )

            return 1 if failed or rejected_plans else 0
    except JellyfinError as error:
        _log_line(str(error), error=True)
        return 1


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the apply_titles_from_filename entrypoint."""
    parser = argparse.ArgumentParser(
        prog="apply_titles_from_filename",
        description=(
            "Rename a movie's or a season's episode titles to the title their "
            "own on-disk filename implies, under Jellyfin's naming convention. "
            "Exactly one target must be given: --movie for a single movie, or "
            "--series-name together with --season-number for every episode in "
            "one season. Never contacts TheTVDB or any other internet metadata "
            "provider. An item already reading the same as its filename-implied "
            "title is left alone - matching is the same lenient comparison "
            "audit.py's checks use (punctuation, articles, accents, US/UK "
            "spelling, and more are all treated as equivalent), not an exact "
            "string match. Before an actual rename, the item's current Name is "
            "backed up into OriginalTitle. --restore reverses this: it sets "
            "each item's Name back to its own OriginalTitle backup, purely "
            "locally - it never inspects the filename and never contacts "
            "TheTVDB."
        ),
        exit_on_error=False,
    )
    parser.add_argument(
        "--movie",
        metavar="TITLE",
        help="Movie title to match, as it appears in Jellyfin. Cannot be combined with --series-name/--season-number.",
    )
    parser.add_argument(
        "--series-name",
        metavar="NAME",
        help="Series name to match, as it appears in Jellyfin. Requires --season-number.",
    )
    parser.add_argument(
        "--season-number",
        type=int,
        metavar="N",
        help="Season number to update. Requires --series-name.",
    )
    parser.add_argument(
        "--server",
        metavar="SERVER",
        help="Configured server key from servers.toml. Defaults to servers.toml's default_server.",
    )
    parser.add_argument(
        "--library",
        metavar="NAME",
        help=(
            "Limit the movie/series search to one library, to disambiguate a "
            "name that matches items in more than one library."
        ),
    )
    parser.add_argument(
        "--path",
        metavar="PARTIAL_PATH",
        help=(
            "Limit the movie/series search to an item whose path contains "
            "this text (case-insensitive), to disambiguate a name that "
            "matches more than one item."
        ),
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help=(
            "Restore each item's Name from its own OriginalTitle backup - the "
            "title backed up there before an earlier rename - instead of "
            "renaming toward its filename-implied title. Never inspects the "
            "filename and never contacts TheTVDB. An item with no OriginalTitle "
            "backup is left alone."
        ),
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and apply immediately.",
    )
    parser.add_argument(
        "--debug",
        action="store_true",
        help=(
            "Print verbose progress to the console (which item is being "
            "checked, calls to Jellyfin) - console only, never written to "
            "titles_from_filename_apply.log."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the filename-title rename workflow and return an exit code."""
    configure_logging()
    parser = _build_argument_parser()
    _log_line(f"Command: {parser.prog} {shlex.join(argv if argv is not None else sys.argv[1:])}")

    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        LOGGER.error("%s", error)
        return 2

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.movie is not None and (args.series_name is not None or args.season_number is not None):
        LOGGER.error("--movie cannot be combined with --series-name/--season-number.")
        return 2
    if args.movie is None and args.series_name is None:
        LOGGER.error("Specify either --movie or --series-name/--season-number.")
        return 2
    if args.series_name is not None and args.season_number is None:
        LOGGER.error("--series-name requires --season-number.")
        return 2
    if args.season_number is not None and args.series_name is None:
        LOGGER.error("--season-number requires --series-name.")
        return 2
    if args.season_number is not None and args.season_number < 0:
        LOGGER.error("--season-number must not be negative.")
        return 2

    return run_apply_titles_from_filename(
        movie_name=args.movie,
        series_name=args.series_name,
        season_number=args.season_number,
        server_key=args.server,
        library_name=args.library,
        path_filter=args.path,
        assume_yes=args.yes,
        restore=args.restore,
    )


if __name__ == "__main__":
    raise SystemExit(main())
