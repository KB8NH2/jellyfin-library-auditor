#!/usr/bin/python3
"""CLI to rename one series/season's episode titles to TheTVDB's aired (or DVD) order.

This is the sibling tool to audit.py's ``aired_dvd_order_mismatch`` check:
where that check only reports an episode whose Jellyfin title doesn't match
TheTVDB at its (season, episode) position, this module actually renames it.
For every episode currently in the given season, it looks up TheTVDB's
episode at that same (season, episode) position - aired order by default,
DVD order with --dvd-order - and overwrites the episode's Name with that
title, leaving Overview, episode/season numbers, and every other field
untouched.

Whether a rename is even needed is decided with audit.titles_match(), the
same lenient comparison the audit check itself uses (punctuation, articles,
accents, US/UK spelling, roman-numeral part suffixes, and more are all
treated as equivalent) - an episode already reading the same as TheTVDB
under those rules is left alone rather than being rewritten to TheTVDB's
exact spelling for no practical benefit. Before an actual rename, the
episode's current Name is backed up into OriginalTitle, the same convention
apply_dvd_metadata.py uses, so the pre-rename title isn't lost. It does not
contain audit logic or report formatting.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import logging
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audit import titles_match
from config import ConfigError
from config import get_config
from jellyfin import EpisodeSummary
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from transfer_metadata import NON_EDITABLE_ITEM_FIELDS
from transfer_metadata import REQUIRED_NON_EMPTY_FIELDS
from tvdb import TvdbClient
from tvdb import TvdbEpisode
from tvdb import TvdbEpisodeCache
from tvdb import TvdbError


LOGGER = logging.getLogger("apply_episode_titles")

# Append-only record of every apply attempt, mirroring
# apply_dvd_metadata.py's DVD_METADATA_LOG_FILE convention.
EPISODE_TITLES_LOG_FILE = Path("episode_titles_apply.log")

# Fields this tool ever writes, and therefore diffs/locks. OriginalTitle only
# ever changes as a side effect of a Name change (see
# build_title_merged_item_dto), never on its own.
METADATA_FIELDS = ("Name", "OriginalTitle")


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
    with EPISODE_TITLES_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} apply_episode_titles: {message}\n")


# Jellyfin deserializes LockedFields into its own MetadataField enum
# (Cast, Genres, ProductionLocations, Studios, Tags, Name, Overview,
# Runtime, OfficialRating) - OriginalTitle is not a member. Sending any
# value outside that set fails the *entire* update with a 400, not just
# that one entry, so only ever lock fields known to be valid there.
LOCKABLE_METADATA_FIELDS = frozenset({"Name"})


def _lock_changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: dict[str, Any],
    changed_fields: list[str],
) -> None:
    """Add every changed, lockable field to the item's LockedFields, in place.

    This is the same thing Jellyfin's own "Edit Metadata" dialog does when a
    field is changed by hand. Without it, a library with TheTVDB's internet
    metadata provider enabled treats Name as provider-owned and its next
    scheduled/on-demand metadata refresh silently overwrites the rename
    again, even though the API write itself succeeded.
    """
    lockable_changed_fields = [
        field for field in changed_fields if field in LOCKABLE_METADATA_FIELDS
    ]
    if not lockable_changed_fields:
        return
    existing_locked_fields = destination_dto.get("LockedFields") or []
    merged_dto["LockedFields"] = list(
        dict.fromkeys([*existing_locked_fields, *lockable_changed_fields])
    )


def build_title_merged_item_dto(
    destination_dto: Mapping[str, Any],
    target_name: str,
) -> dict[str, Any]:
    """Return the destination episode document with its Name renamed.

    Mirrors transfer_metadata.build_merged_item_dto: starts from a full copy
    of the destination document minus NON_EDITABLE_ITEM_FIELDS. Before
    overwriting Name, the episode's current Name is copied into
    OriginalTitle, the same backup convention apply_dvd_metadata.py uses -
    this does mean a genuine original-language title already stored in
    OriginalTitle is overwritten, since this tool repurposes that field as
    its own undo backup.

    Args:
        destination_dto: Full episode item document read from Jellyfin.
        target_name: TheTVDB title to rename this episode to.

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }
    merged_dto["OriginalTitle"] = destination_dto.get("Name")
    merged_dto["Name"] = target_name

    _lock_changed_fields(destination_dto, merged_dto, ["OriginalTitle", "Name"])
    return merged_dto


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return tuple(
        (field, destination_dto.get(field), merged_dto.get(field))
        for field in METADATA_FIELDS
        if destination_dto.get(field) != merged_dto.get(field)
    )


def _rejected_reason(merged_dto: Mapping[str, Any]) -> str | None:
    """Return why a merged item document is unsafe to write, or ``None``."""
    missing_required_fields = tuple(
        field for field in REQUIRED_NON_EMPTY_FIELDS if not merged_dto.get(field)
    )
    if not missing_required_fields:
        return None
    return (
        "the episode item is missing required field(s) "
        f"{', '.join(missing_required_fields)}. Sending this update would clear "
        "them on the server instead of leaving them alone."
    )


@dataclass(frozen=True, slots=True)
class EpisodeTitlePlan:
    """A computed, not-yet-applied title rename for one episode.

    Separating planning from applying lets the whole season's renames be
    previewed and confirmed in one batch before anything is written.
    """

    episode_id: str
    position: tuple[int, int]
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


def plan_episode_title_update(
    client: JellyfinClient,
    episode: EpisodeSummary,
    season_number: int,
    target_positions: Mapping[tuple[int, int], TvdbEpisode],
) -> EpisodeTitlePlan:
    """Compute one episode's title rename, without writing anything.

    Fetches the episode's full item document from Jellyfin only when a
    rename actually looks necessary - :func:`audit.titles_match` against the
    episode summary's own Name is enough to rule most already-correct
    episodes out without a network round trip.

    Args:
        client: Client for the server the episode lives on.
        episode: The Jellyfin episode to plan a rename for.
        season_number: The season this episode belongs to.
        target_positions: TheTVDB episodes for this series in the target
            ordering ("official" for aired order, "dvd" with --dvd-order),
            keyed by (season_number, episode_number) as TheTVDB reports them
            for that ordering.

    Returns:
        A plan describing what would change and whether it's safe to apply.
    """
    position = (season_number, episode.episode_number)
    target_episode = target_positions.get(position)

    if target_episode is None or not target_episode.name:
        return EpisodeTitlePlan(
            episode_id=episode.id,
            position=position,
            current_name=episode.name,
            target_name=None,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
            already_matches=False,
        )

    if titles_match(episode.name, target_episode.name):
        return EpisodeTitlePlan(
            episode_id=episode.id,
            position=position,
            current_name=episode.name,
            target_name=target_episode.name,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=False,
            already_matches=True,
        )

    destination_dto = client.get_item(episode.id)
    current_name = str(destination_dto.get("Name", episode.name))

    # The episode summary's Name can be stale (e.g. changed since the season
    # was listed) - re-check against the freshly-fetched live Name before
    # deciding a rename is actually needed.
    if titles_match(current_name, target_episode.name):
        return EpisodeTitlePlan(
            episode_id=episode.id,
            position=position,
            current_name=current_name,
            target_name=target_episode.name,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=False,
            already_matches=True,
        )

    merged_dto = build_title_merged_item_dto(destination_dto, target_episode.name)
    return EpisodeTitlePlan(
        episode_id=episode.id,
        position=position,
        current_name=current_name,
        target_name=target_episode.name,
        merged_dto=merged_dto,
        changes=_changed_fields(destination_dto, merged_dto),
        rejected_reason=_rejected_reason(merged_dto),
        no_target_match=False,
        already_matches=False,
    )


def apply_episode_title_plan(client: JellyfinClient, plan: EpisodeTitlePlan) -> None:
    """Write a previously computed, actionable plan to the server.

    Args:
        client: Client for the server the episode lives on.
        plan: A plan from :func:`plan_episode_title_update` that is actionable.
    """
    if plan.merged_dto is None:
        raise ValueError("Cannot apply a plan with no target-order match.")
    client.update_item(plan.episode_id, plan.merged_dto)


def _verify_applied(client: JellyfinClient, plan: EpisodeTitlePlan) -> tuple[str, ...]:
    """Re-read an item right after writing it and report any field that didn't change.

    A successful HTTP response from update_item only means Jellyfin accepted
    the write, not that the value stuck - a locked/provider-owned field can
    silently keep its old value. Mirrors apply_dvd_metadata.py's identical
    check for the same reason.

    Args:
        client: Client for the server the episode lives on.
        plan: The plan that was just applied.

    Returns:
        The field names that still report their pre-update value.
    """
    current_dto = client.get_item(plan.episode_id)
    return tuple(
        field
        for field, _, expected_value in plan.changes
        if current_dto.get(field) != expected_value
    )


def _format_position(position: tuple[int, int]) -> str:
    """Return one episode position as an SxxExx label."""
    season_number, episode_number = position
    return f"S{season_number:02d}E{episode_number:02d}"


def _describe_plan(plan: EpisodeTitlePlan, *, order_label: str) -> None:
    """Log one episode's planned outcome."""
    label = _format_position(plan.position)
    if plan.no_target_match:
        _log_line(f"  {label}: no TheTVDB {order_label} match at this position - skipped.")
        return
    if plan.is_rejected:
        _log_line(f"  {label}: rejected: {plan.rejected_reason}", error=True)
        return
    if plan.already_matches:
        _log_line(f"  {label}: already matches {order_label} title {plan.target_name!r}.")
        return
    for field, old_value, new_value in plan.changes:
        _log_line(f"  {label} {field}: {old_value!r} -> {new_value!r}")


def run_apply_episode_titles(
    *,
    series_name: str,
    season_number: int,
    server_key: str | None,
    library_name: str | None,
    assume_yes: bool,
    use_dvd_order: bool = False,
) -> int:
    """Rename one series/season's episode titles to TheTVDB's aired/DVD order.

    Args:
        series_name: Series display name to match in Jellyfin.
        season_number: Season number to update.
        server_key: Configured server key from servers.toml, or ``None`` to
            use servers.toml's default_server.
        library_name: Library name to restrict the series search to, or
            ``None`` to search every TV library.
        assume_yes: Skip the interactive confirmation prompt when ``True``.
        use_dvd_order: Rename toward TheTVDB's DVD order instead of aired
            order (the default).

    Returns:
        A process exit code: ``0`` on success (including "nothing to do"),
        ``1`` if any item was rejected/failed or the user declined, ``2`` on
        a usage/configuration error.
    """
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

    if not app_config.tvdb.api_key:
        _log_line(
            "apply_episode_titles requires api_key to be set in the [tvdb] "
            "table of servers.toml.",
            error=True,
        )
        return 2

    order_label = "DVD-order" if use_dvd_order else "aired-order"
    season_type = "dvd" if use_dvd_order else "official"

    try:
        with JellyfinClient(server) as client:
            LOGGER.debug("Looking up series %r on %s...", series_name, server.name)
            matches = client.find_series(series_name, library_name=library_name)
            if not matches:
                _log_line(
                    f"No series named {series_name!r} was found on {server.name}.",
                    error=True,
                )
                return 1
            if len(matches) > 1:
                library_names = ", ".join(sorted(match.library_name for match in matches))
                _log_line(
                    f"Series name {series_name!r} matches shows in more than one "
                    f"library ({library_names}) on {server.name}. Use --library "
                    "to disambiguate.",
                    error=True,
                )
                return 1

            match = matches[0]
            if match.tvdb_id is None:
                _log_line(
                    f"{series_name!r} in library {match.library_name!r} has no "
                    "TheTVDB provider id.",
                    error=True,
                )
                return 1

            LOGGER.debug(
                "Matched series %r -> item %s in library %r (TheTVDB id %s).",
                series_name,
                match.series_id,
                match.library_name,
                match.tvdb_id,
            )

            episodes = client.get_series_season_episodes(match.series_id, season_number)
            if not episodes:
                _log_line(
                    f"No episodes found for {series_name!r} season {season_number} "
                    f"in library {match.library_name!r}."
                )
                return 0

            with TvdbClient(
                app_config.tvdb.api_key,
                cache=TvdbEpisodeCache(ttl=timedelta(days=app_config.tvdb.cache_ttl_days)),
            ) as tvdb_client:
                LOGGER.debug(
                    "Fetching TheTVDB %s-order episodes for series id %s...",
                    season_type,
                    match.tvdb_id,
                )
                target_episodes = tvdb_client.get_series_episodes(
                    match.tvdb_id, season_type, series_name=series_name
                )

            target_positions = {
                (target_episode.season_number, target_episode.episode_number): target_episode
                for target_episode in target_episodes
            }

            plans = []
            for episode in episodes:
                LOGGER.debug(
                    "Checking %s %r (item %s)...",
                    _format_position((season_number, episode.episode_number)),
                    episode.name,
                    episode.id,
                )
                plans.append(
                    plan_episode_title_update(client, episode, season_number, target_positions)
                )
            plans = tuple(plans)

            _log_line(
                f"Episode titles rename ({order_label}): {series_name!r} "
                f"season {season_number} on {server.name}"
            )
            for plan in plans:
                _describe_plan(plan, order_label=order_label)

            actionable_plans = tuple(plan for plan in plans if plan.is_actionable)
            rejected_plans = tuple(plan for plan in plans if plan.is_rejected)

            if not actionable_plans:
                _log_line("Nothing to do.")
                return 1 if rejected_plans else 0

            if not assume_yes:
                response = (
                    input(
                        f"Rename {len(actionable_plans)} episode(s) to their "
                        f"{order_label} title? [y/N] "
                    )
                    .strip()
                    .lower()
                )
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            failed = 0
            for plan in actionable_plans:
                LOGGER.debug("Applying %s...", _format_position(plan.position))
                try:
                    apply_episode_title_plan(client, plan)
                except JellyfinError as error:
                    failed += 1
                    _log_line(f"  {_format_position(plan.position)}: failed: {error}", error=True)
                    continue

                LOGGER.debug("Verifying %s...", _format_position(plan.position))
                stale_fields = _verify_applied(client, plan)
                if stale_fields:
                    failed += 1
                    _log_line(
                        f"  {_format_position(plan.position)}: update did not take "
                        f"effect for {', '.join(stale_fields)} - Jellyfin still "
                        "reports the old value(s) right after the write. This "
                        "usually means an internet metadata provider (e.g. TheTVDB) "
                        "is enabled for this library and overwrote the edit again "
                        "on its own refresh.",
                        error=True,
                    )
                else:
                    _log_line(f"  {_format_position(plan.position)}: renamed.")

            already_matching_count = sum(1 for plan in plans if plan.already_matches)
            no_match_count = sum(1 for plan in plans if plan.no_target_match)
            _log_line(
                f"Episode titles rename complete: {len(actionable_plans) - failed} renamed, "
                f"{failed} failed, {len(rejected_plans)} rejected, "
                f"{already_matching_count} already matching, "
                f"{no_match_count} with no {order_label} match."
            )

            return 1 if failed or rejected_plans else 0
    except (JellyfinError, TvdbError) as error:
        _log_line(str(error), error=True)
        return 1


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the apply_episode_titles entrypoint."""
    parser = argparse.ArgumentParser(
        prog="apply_episode_titles",
        description=(
            "Rename one series/season's episode titles to TheTVDB's aired-order "
            "title at each existing season/episode position (--dvd-order for DVD "
            "order instead). An episode already reading the same as TheTVDB's "
            "title is left alone - matching is the same lenient comparison "
            "audit.py's aired_dvd_order_mismatch check uses (punctuation, "
            "articles, accents, US/UK spelling, and more are all treated as "
            "equivalent), not an exact string match. Episode/season numbers and "
            "Overview are never changed. Before an actual rename, the episode's "
            "current Name is backed up into OriginalTitle."
        ),
        exit_on_error=False,
    )
    parser.add_argument(
        "--series-name",
        required=True,
        metavar="NAME",
        help="Series name to match, as it appears in Jellyfin.",
    )
    parser.add_argument(
        "--season-number",
        required=True,
        type=int,
        metavar="N",
        help="Season number to update.",
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
            "Limit the series search to one library, to disambiguate a "
            "series name that matches shows in more than one library."
        ),
    )
    parser.add_argument(
        "--dvd-order",
        action="store_true",
        help="Rename toward TheTVDB's DVD order instead of aired order (the default).",
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
            "checked, calls to Jellyfin and TheTVDB) - console only, never "
            "written to episode_titles_apply.log."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the episode-title rename workflow and return an exit code."""
    configure_logging()
    parser = _build_argument_parser()

    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        LOGGER.error("%s", error)
        return 2

    if args.debug:
        logging.getLogger().setLevel(logging.DEBUG)

    if args.season_number < 0:
        LOGGER.error("--season-number must not be negative.")
        return 2

    return run_apply_episode_titles(
        series_name=args.series_name,
        season_number=args.season_number,
        server_key=args.server,
        library_name=args.library,
        assume_yes=args.yes,
        use_dvd_order=args.dvd_order,
    )


if __name__ == "__main__":
    raise SystemExit(main())
