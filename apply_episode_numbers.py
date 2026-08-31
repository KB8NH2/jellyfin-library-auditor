#!/usr/bin/python3
"""CLI to fill in missing episode numbers for one series/season from TheTVDB.

Jellyfin sometimes cannot parse an episode number out of a file at all (no
recognizable ``SxxExx`` marker in the filename), leaving the episode's
IndexNumber unset entirely - Jellyfin then falls back to using the filename
itself as the episode's Name, which the missing_episode_number audit check
flags. When a series is organized with one descriptively-titled file per
episode instead of ``SxxExx`` naming, that fallback Name is usually the
episode's real title, just not yet tied to a number. This module looks up
TheTVDB's aired-order episode list for the target season, works out which
aired-order numbers are not already used by a numbered episode in that
season, and matches each unnumbered episode to one of those by comparing its
Name against each candidate's title (case/punctuation-insensitively, and
ignoring a leading "The"/"A"/"An" if that's the only difference) -
deliberately not by file order, since on-disk file order does not
necessarily follow aired order. An episode whose title matches none of the
remaining candidates by exact or article-insensitive comparison is offered
as a fuzzy (similarity-ratio) match for the user to confirm interactively
instead of being guessed at automatically; confirming one also overwrites
the episode's Name with TheTVDB's title, since a fuzzy match by definition
means the two titles weren't already equivalent. Every other run of the
match (exact/article-insensitive) leaves Name untouched, and Overview and
every other field are always left untouched (see apply_dvd_metadata.py for
fixing those).

Jellyfin's own assigned TheTVDB id for the series isn't trusted blindly:
when TheTVDB has more than one series entry sharing the exact same name
(e.g. a decades-old show and a from-scratch modern revival, each
numbering their own "Season 1" independently), Jellyfin's automatic
matching has no way to know which one actually explains the local
library - and a wrong match here wouldn't just go uncorrected, it would
actively number (and potentially rename) episodes against some other
show's episode list. See tvdb_series_resolution.resolve_series_tvdb_id()
for how the right one is picked. It does not contain audit logic or
report formatting.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import difflib
import logging
import re
import shlex
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audit import normalized_title
from config import ConfigError
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from jellyfin import SeasonEpisodeSummary
from transfer_metadata import NON_EDITABLE_ITEM_FIELDS
from transfer_metadata import changed_fields
from transfer_metadata import rejected_reason as _rejected_reason
from tvdb import TvdbClient
from tvdb import TvdbEpisode
from tvdb import TvdbEpisodeCache
from tvdb import TvdbError
from tvdb_series_resolution import resolve_series_tvdb_id


LOGGER = logging.getLogger("apply_episode_numbers")

# Append-only record of every apply attempt, mirroring
# transfer_metadata.py's METADATA_TRANSFER_LOG_FILE convention.
EPISODE_NUMBER_APPLY_LOG_FILE = Path("episode_numbers_apply.log")

# The only fields this tool ever writes, and therefore diffs/locks. Name is
# only ever actually set for a user-confirmed fuzzy match (see
# build_episode_number_merged_item_dto), so it diffs as unchanged everywhere
# else.
METADATA_FIELDS = ("IndexNumber", "Name")

# Jellyfin deserializes LockedFields into its own MetadataField enum; Name is
# a member (mirrors apply_dvd_metadata.LOCKABLE_METADATA_FIELDS).
_NAME_LOCK_FIELD = "Name"

_LEADING_ARTICLE_PATTERN = re.compile(r"^(?:the|an?)\s+")

# Below this similarity ratio a fuzzy candidate isn't worth showing the user
# at all - titles that different are essentially never the same episode.
_FUZZY_MATCH_MIN_RATIO = 0.6


def _title_match_keys(title: str) -> tuple[str, str]:
    """Return (strict, loose) normalized comparison keys for a title.

    The loose key additionally drops a leading "The"/"A"/"An", so a file
    titled "The Murdering Cowboy" still matches a TheTVDB episode titled
    "Murdering Cowboy" (and vice versa) when nothing else about the title
    differs - a real, observed discrepancy between on-disk titles and
    TheTVDB's own. The strict key is tried first everywhere this is used, so
    two otherwise-identical titles that differ only by which one has the
    article are always preferred over a same-titled-except-for-an-article
    coincidence.
    """
    strict_key = normalized_title(title)
    return strict_key, _LEADING_ARTICLE_PATTERN.sub("", strict_key)


def _pop_title_match(
    remaining_aired_episodes: list[TvdbEpisode],
    episode_name: str,
) -> TvdbEpisode | None:
    """Return and remove the first remaining aired episode whose title matches.

    Tries every candidate's strict key first (in list order, i.e. ascending
    episode number), only falling back to loose (article-insensitive) keys
    if nothing strict-matched, so an exact title match is always preferred
    over an article-insensitive coincidence.

    Args:
        remaining_aired_episodes: Aired-order episodes still unclaimed by an
            earlier unnumbered episode this run, in ascending episode-number
            order. Mutated in place when a match is found.
        episode_name: The unnumbered Jellyfin episode's Name to match.

    Returns:
        The matched aired-order episode, or ``None`` when no candidate's
        title matches at either key.
    """
    strict_key, loose_key = _title_match_keys(episode_name)

    for index, aired_episode in enumerate(remaining_aired_episodes):
        candidate_strict_key, _ = _title_match_keys(aired_episode.name)
        if candidate_strict_key == strict_key:
            return remaining_aired_episodes.pop(index)

    for index, aired_episode in enumerate(remaining_aired_episodes):
        _, candidate_loose_key = _title_match_keys(aired_episode.name)
        if candidate_loose_key == loose_key:
            return remaining_aired_episodes.pop(index)

    return None


def _best_fuzzy_match(
    remaining_aired_episodes: Sequence[TvdbEpisode],
    episode_name: str,
) -> tuple[TvdbEpisode, float] | None:
    """Return the closest remaining aired episode by title similarity, and its score.

    Only ever consulted after a strict and loose title match have both
    failed. Similarity is measured with difflib's ratio() over each side's
    normalized_title() - the same normalization _title_match_keys uses - so
    two titles differing only by, say, a misspelling or "Missing" vs
    "Missing:" still score highly. The best-scoring candidate is returned
    for the caller to confirm with the user, not applied automatically.

    Args:
        remaining_aired_episodes: Aired-order episodes still unclaimed this
            run, not mutated.
        episode_name: The unnumbered Jellyfin episode's Name to match.

    Returns:
        A ``(candidate, ratio)`` pair for the highest-scoring candidate at
        or above ``_FUZZY_MATCH_MIN_RATIO``, or ``None`` when no remaining
        candidate scores that high.
    """
    target_key = normalized_title(episode_name)
    best: tuple[TvdbEpisode, float] | None = None
    for candidate in remaining_aired_episodes:
        ratio = difflib.SequenceMatcher(
            None, target_key, normalized_title(candidate.name)
        ).ratio()
        if ratio < _FUZZY_MATCH_MIN_RATIO:
            continue
        if best is None or ratio > best[1]:
            best = (candidate, ratio)
    return best


def _confirm_fuzzy_match(episode_label: str, candidate: TvdbEpisode, ratio: float) -> bool:
    """Ask the user whether to accept one fuzzy title match.

    Args:
        episode_label: Display label for the unnumbered Jellyfin episode.
        candidate: The closest-scoring TheTVDB aired-order episode.
        ratio: The candidate's difflib similarity ratio, for display.

    Returns:
        Whether the user accepted the match.
    """
    response = input(
        f"  {episode_label}: no exact title match. Closest TheTVDB title is "
        f'"{candidate.name}" (E{candidate.episode_number:02d}, {ratio:.0%} '
        "similar) - use it? [y/N] "
    ).strip().lower()
    return response in {"y", "yes"}


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
    with EPISODE_NUMBER_APPLY_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} apply_episode_numbers: {message}\n")


def build_episode_number_merged_item_dto(
    destination_dto: Mapping[str, Any],
    episode_number: int,
    *,
    title: str | None = None,
) -> dict[str, Any]:
    """Return the destination episode document with IndexNumber set.

    Mirrors apply_dvd_metadata.build_dvd_merged_item_dto: starts from a full
    copy of the destination document minus NON_EDITABLE_ITEM_FIELDS, so every
    other field round-trips back to the server unchanged.

    Args:
        destination_dto: Full episode item document read from Jellyfin.
        episode_number: The episode number to assign.
        title: When given (only for a user-confirmed fuzzy title match),
            also set as the episode's Name, with Name added to
            LockedFields - the same thing Jellyfin's own "Edit Metadata"
            dialog does when a field is changed by hand - so a library with
            an internet metadata provider enabled doesn't silently revert
            the title on its next scheduled/on-demand refresh.

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    } | {"IndexNumber": episode_number}

    if title is not None:
        merged_dto["Name"] = title
        existing_locked_fields = destination_dto.get("LockedFields") or []
        merged_dto["LockedFields"] = list(
            dict.fromkeys([*existing_locked_fields, _NAME_LOCK_FIELD])
        )

    return merged_dto


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return changed_fields(destination_dto, merged_dto, METADATA_FIELDS)


def _unnumbered_episode_sort_key(episode: SeasonEpisodeSummary) -> tuple[str, str]:
    """Return a stable display/iteration order for unnumbered episodes.

    Episodes are matched to TheTVDB by title, not by this order, but a
    stable order still matters: it's what the plan list is logged in, and
    it's the tie-breaker when more than one unnumbered episode happens to
    have the same title (whichever sorts first claims the lowest remaining
    TheTVDB episode number among the duplicates).
    """
    return (
        str(episode.path).casefold() if episode.path is not None else "",
        episode.name.casefold(),
    )


@dataclass(frozen=True, slots=True)
class EpisodeNumberPlan:
    """A computed, not-yet-applied episode-number update for one episode.

    Separating planning from applying lets the whole season's changes be
    previewed and confirmed in one batch before anything is written.
    """

    episode_id: str
    current_label: str
    assigned_number: int | None
    merged_dto: dict[str, Any] | None
    changes: tuple[tuple[str, Any, Any], ...]
    rejected_reason: str | None
    no_target_match: bool

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
        return not self.no_target_match and not self.is_rejected and self.has_changes


def plan_episode_number_update(
    client: JellyfinClient,
    episode: SeasonEpisodeSummary,
    aired_episode: TvdbEpisode | None,
    *,
    apply_title: bool = False,
) -> EpisodeNumberPlan:
    """Compute one episode's number assignment, without writing anything.

    Args:
        client: Client for the server the episode lives on.
        episode: The unnumbered Jellyfin episode to plan an update for.
        aired_episode: The TheTVDB aired-order episode whose title matched
            this episode's Name, to assign this episode's number from, or
            ``None`` when no unused aired-order episode has a matching
            title.
        apply_title: When True (only ever set for a user-confirmed fuzzy
            title match), also overwrite the episode's Name with
            ``aired_episode.name``. Left False for an exact or
            article-insensitive match, since those titles are already
            equivalent and shouldn't be reformatted to TheTVDB's exact
            punctuation/casing.

    Returns:
        A plan describing what would change and whether it's safe to apply.
    """
    current_label = episode.path.name if episode.path is not None else episode.name

    if aired_episode is None:
        return EpisodeNumberPlan(
            episode_id=episode.id,
            current_label=current_label,
            assigned_number=None,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
        )

    destination_dto = client.get_item(episode.id)
    merged_dto = build_episode_number_merged_item_dto(
        destination_dto,
        aired_episode.episode_number,
        title=aired_episode.name if apply_title else None,
    )

    return EpisodeNumberPlan(
        episode_id=episode.id,
        current_label=current_label,
        assigned_number=aired_episode.episode_number,
        merged_dto=merged_dto,
        changes=_changed_fields(destination_dto, merged_dto),
        rejected_reason=_rejected_reason(merged_dto),
        no_target_match=False,
    )


def apply_episode_number_plan(client: JellyfinClient, plan: EpisodeNumberPlan) -> None:
    """Write a previously computed, actionable plan to the server.

    Args:
        client: Client for the server the episode lives on.
        plan: A plan from :func:`plan_episode_number_update` that is
            actionable.
    """
    if plan.merged_dto is None:
        raise ValueError("Cannot apply a plan with no target-order match.")
    client.update_item(plan.episode_id, plan.merged_dto)


def _verify_applied(client: JellyfinClient, plan: EpisodeNumberPlan) -> tuple[str, ...]:
    """Re-read an item right after writing it and report any field that didn't change.

    A successful HTTP response from update_item only means Jellyfin accepted
    the write, not that the value stuck - a locked/provider-owned field can
    silently keep its old value. Mirrors apply_dvd_metadata._verify_applied.

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


def _describe_plan(plan: EpisodeNumberPlan) -> None:
    """Log one episode's planned outcome."""
    label = plan.current_label
    if plan.no_target_match:
        _log_line(
            f"  {label}: no unused TheTVDB aired-order episode title matches "
            "this episode's name - skipped."
        )
        return
    if plan.is_rejected:
        _log_line(f"  {label}: rejected: {plan.rejected_reason}", error=True)
        return
    if not plan.has_changes:
        _log_line(f"  {label}: already numbered E{plan.assigned_number:02d}.")
        return
    for field, old_value, new_value in plan.changes:
        _log_line(f"  {label} {field}: {old_value!r} -> {new_value!r}")


def run_apply_episode_numbers(
    *,
    series_name: str,
    season_number: int,
    server_key: str | None,
    library_name: str | None,
    path_filter: str | None = None,
    assume_yes: bool,
) -> int:
    """Fill in missing episode numbers for one series' season from TheTVDB.

    Args:
        series_name: Series display name to match in Jellyfin.
        season_number: Season number to update.
        server_key: Configured server key from servers.toml, or ``None`` to
            use servers.toml's default_server.
        library_name: Library name to restrict the series search to, or
            ``None`` to search every TV library.
        path_filter: When given, only a series whose Path contains this text
            (case-insensitively) is considered - disambiguates a series name
            that matches more than one show.
        assume_yes: Skip the interactive confirmation prompt when ``True``.

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
            "apply_episode_numbers requires api_key to be set in the [tvdb] "
            "table of servers.toml.",
            error=True,
        )
        return 2

    try:
        with JellyfinClient(server) as client:
            LOGGER.debug("Looking up series %r on %s...", series_name, server.name)
            matches = client.find_series(
                series_name, library_name=library_name, path_filter=path_filter
            )
            if not matches:
                _log_line(
                    f"No series named {series_name!r} was found on {server.name}.",
                    error=True,
                )
                return 1
            if len(matches) > 1:
                library_names = ", ".join(
                    sorted(match.library_name for match in matches)
                )
                _log_line(
                    f"Series name {series_name!r} matches shows in more than one "
                    f"library ({library_names}) on {server.name}. Use --library "
                    "and/or --path to disambiguate.",
                    error=True,
                )
                return 1

            match = matches[0]
            LOGGER.debug(
                "Matched series %r -> item %s in library %r (Jellyfin-assigned TheTVDB id %s).",
                series_name,
                match.series_id,
                match.library_name,
                match.tvdb_id,
            )

            episodes = client.get_series_season_episodes_all(match.series_id, season_number)
            if not episodes:
                _log_line(
                    f"No episodes found for {series_name!r} season {season_number} "
                    f"in library {match.library_name!r}."
                )
                return 0

            numbered_episodes = tuple(e for e in episodes if e.episode_number is not None)
            unnumbered_episodes = tuple(
                sorted(
                    (e for e in episodes if e.episode_number is None),
                    key=_unnumbered_episode_sort_key,
                )
            )
            if not unnumbered_episodes:
                _log_line(
                    f"No episodes are missing an episode number for {series_name!r} "
                    f"season {season_number} in library {match.library_name!r}."
                )
                return 0

            with TvdbClient(
                app_config.tvdb.api_key,
                cache=TvdbEpisodeCache(ttl=timedelta(days=app_config.tvdb.cache_ttl_days)),
            ) as tvdb_client:
                LOGGER.debug(
                    "Resolving the TheTVDB series id that best explains %r's local episodes...",
                    series_name,
                )
                tvdb_id = resolve_series_tvdb_id(
                    client, tvdb_client, series_name, match.series_id, match.tvdb_id, logger=LOGGER
                )
                if tvdb_id is None:
                    _log_line(
                        f"{series_name!r} in library {match.library_name!r} has no "
                        "TheTVDB provider id, and no matching TheTVDB series could be "
                        "found by name.",
                        error=True,
                    )
                    return 1
                if tvdb_id != match.tvdb_id:
                    _log_line(
                        f"TheTVDB id {match.tvdb_id!r} assigned in Jellyfin for "
                        f"{series_name!r} doesn't best explain its local episodes across "
                        f"every season - using TheTVDB id {tvdb_id!r} instead."
                    )

                LOGGER.debug(
                    "Fetching TheTVDB aired-order episodes for series id %s...",
                    tvdb_id,
                )
                aired_episodes = tvdb_client.get_series_episodes(
                    tvdb_id, "official", series_name=series_name
                )

            season_aired_episodes = sorted(
                (
                    aired_episode
                    for aired_episode in aired_episodes
                    if aired_episode.season_number == season_number
                ),
                key=lambda aired_episode: aired_episode.episode_number,
            )
            taken_numbers = {episode.episode_number for episode in numbered_episodes}
            available_aired_episodes = [
                aired_episode
                for aired_episode in season_aired_episodes
                if aired_episode.episode_number not in taken_numbers
            ]

            plans: list[EpisodeNumberPlan] = []
            for episode in unnumbered_episodes:
                target = _pop_title_match(available_aired_episodes, episode.name)
                apply_title = False
                if target is None and not assume_yes:
                    fuzzy_match = _best_fuzzy_match(available_aired_episodes, episode.name)
                    if fuzzy_match is not None:
                        candidate, ratio = fuzzy_match
                        current_label = (
                            episode.path.name if episode.path is not None else episode.name
                        )
                        if _confirm_fuzzy_match(current_label, candidate, ratio):
                            available_aired_episodes.remove(candidate)
                            target = candidate
                            apply_title = True
                plans.append(
                    plan_episode_number_update(client, episode, target, apply_title=apply_title)
                )
            plans = tuple(plans)

            _log_line(
                f"Episode numbers assigned from TheTVDB aired order: {series_name!r} "
                f"season {season_number} on {server.name}"
            )
            for plan in plans:
                _describe_plan(plan)

            actionable_plans = tuple(plan for plan in plans if plan.is_actionable)
            rejected_plans = tuple(plan for plan in plans if plan.is_rejected)

            if not actionable_plans:
                _log_line("Nothing to do.")
                return 1 if rejected_plans else 0

            if not assume_yes:
                response = input(
                    f"Assign episode numbers to {len(actionable_plans)} episode(s) "
                    "from TheTVDB aired order? [y/N] "
                ).strip().lower()
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            failed = 0
            for plan in actionable_plans:
                LOGGER.debug("Applying %s...", plan.current_label)
                try:
                    apply_episode_number_plan(client, plan)
                except JellyfinError as error:
                    failed += 1
                    _log_line(f"  {plan.current_label}: failed: {error}", error=True)
                    continue

                LOGGER.debug("Verifying %s...", plan.current_label)
                stale_fields = _verify_applied(client, plan)
                if stale_fields:
                    failed += 1
                    _log_line(
                        f"  {plan.current_label}: update did not take effect for "
                        f"{', '.join(stale_fields)} - Jellyfin still reports the old "
                        "value(s) right after the write. This usually means an "
                        "internet metadata provider (e.g. TheTVDB) is enabled for "
                        "this library and overwrote the edit again on its own "
                        "refresh.",
                        error=True,
                    )
                else:
                    _log_line(
                        f"  {plan.current_label}: numbered E{plan.assigned_number:02d}."
                    )

            no_match_count = sum(1 for plan in plans if plan.no_target_match)
            _log_line(
                "Episode numbers assign complete: "
                f"{len(actionable_plans) - failed} numbered, {failed} failed, "
                f"{len(rejected_plans)} rejected, {no_match_count} with no matching "
                "aired-order title."
            )

            return 1 if failed or rejected_plans else 0
    except (JellyfinError, TvdbError) as error:
        _log_line(str(error), error=True)
        return 1


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the apply_episode_numbers entrypoint."""
    parser = argparse.ArgumentParser(
        prog="apply_episode_numbers",
        description=(
            "Fill in missing episode numbers for one series/season using "
            "TheTVDB's aired order. Episodes with no episode number are "
            "matched by comparing their Jellyfin Name against each unused "
            "aired-order episode's title, and IndexNumber is set from "
            "whichever one matches. When no exact match exists, the closest "
            "title is offered as a fuzzy match to confirm interactively "
            "(skipped entirely with --yes); confirming one also overwrites "
            "Name with TheTVDB's title. Overview and every other field are "
            "always left untouched."
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
        "--path",
        metavar="PARTIAL_PATH",
        help=(
            "Limit the series search to a series whose path contains this "
            "text (case-insensitive), to disambiguate a series name that "
            "matches more than one show."
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
            "checked, calls to Jellyfin and TheTVDB) - console only, never "
            "written to episode_numbers_apply.log."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the episode-number apply workflow and return an exit code."""
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

    if args.season_number < 0:
        LOGGER.error("--season-number must not be negative.")
        return 2

    return run_apply_episode_numbers(
        series_name=args.series_name,
        season_number=args.season_number,
        server_key=args.server,
        library_name=args.library,
        path_filter=args.path,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
