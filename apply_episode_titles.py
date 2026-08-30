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
apply_dvd_metadata.py uses, so the pre-rename title isn't lost.

--restore reverses a previous rename: it sets each episode's Name back to
its own OriginalTitle backup, purely locally - it needs no TheTVDB api_key
and never contacts TheTVDB. An episode with no OriginalTitle backup (never
renamed by this tool) is left alone.

Jellyfin's own assigned TheTVDB id for the series isn't trusted blindly:
when TheTVDB has more than one series entry sharing the exact same name
(e.g. a decades-old show and a from-scratch modern revival, each
numbering their own "Season 1" independently), Jellyfin's automatic
matching has no way to know which one actually explains the local
library - and a wrong match here wouldn't just go uncorrected, it would
actively rename episodes to some other show's titles. See
resolve_series_tvdb_id() for how the right one is picked. It does not
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

# Bounds worst-case TheTVDB calls for a generically-named series without
# likely missing the real match - TheTVDB ranks search results
# most-relevant first. Mirrors auditor.py's identical cap for the same
# search-then-score-by-local-episode-overlap approach.
_MAX_TVDB_SEARCH_CANDIDATES = 5

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


def build_title_restore_merged_item_dto(
    destination_dto: Mapping[str, Any],
    original_title: str,
) -> dict[str, Any]:
    """Return the destination episode document with its Name restored from OriginalTitle.

    Sets Name back to the episode's own OriginalTitle - the backup
    build_title_merged_item_dto writes there before an earlier rename -
    undoing that rename. OriginalTitle itself is left untouched: there is
    nothing further to preserve once Name is already back to what it held
    before.

    Args:
        destination_dto: Full episode item document read from Jellyfin.
        original_title: The episode's own OriginalTitle backup value.

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }
    merged_dto["Name"] = original_title

    _lock_changed_fields(destination_dto, merged_dto, ["Name"])
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


def plan_episode_title_restore(
    client: JellyfinClient,
    episode: EpisodeSummary,
    season_number: int,
) -> EpisodeTitlePlan:
    """Compute one episode's title restore from its own OriginalTitle backup.

    Unlike plan_episode_title_update(), this needs no TheTVDB data at all -
    OriginalTitle is this tool's own backup, written the last time this
    episode's title was changed by a (non-restore) run, so restoring from it
    is a purely local operation.

    Args:
        client: Client for the server the episode lives on.
        episode: The Jellyfin episode to plan a restore for.
        season_number: The season this episode belongs to.

    Returns:
        A plan describing what would change and whether it's safe to apply.
        ``no_target_match`` is set when the episode has no OriginalTitle
        backup to restore from.
    """
    position = (season_number, episode.episode_number)
    destination_dto = client.get_item(episode.id)
    current_name = str(destination_dto.get("Name", episode.name))
    original_title = destination_dto.get("OriginalTitle") or None

    if not original_title:
        return EpisodeTitlePlan(
            episode_id=episode.id,
            position=position,
            current_name=current_name,
            target_name=None,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
            already_matches=False,
        )

    if original_title == current_name:
        return EpisodeTitlePlan(
            episode_id=episode.id,
            position=position,
            current_name=current_name,
            target_name=original_title,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=False,
            already_matches=True,
        )

    merged_dto = build_title_restore_merged_item_dto(destination_dto, original_title)
    return EpisodeTitlePlan(
        episode_id=episode.id,
        position=position,
        current_name=current_name,
        target_name=original_title,
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


def _describe_plan(plan: EpisodeTitlePlan, *, order_label: str, restore: bool = False) -> None:
    """Log one episode's planned outcome."""
    label = _format_position(plan.position)
    if plan.no_target_match:
        if restore:
            _log_line(f"  {label}: no OriginalTitle backup to restore from - skipped.")
        else:
            _log_line(f"  {label}: no TheTVDB {order_label} match at this position - skipped.")
        return
    if plan.is_rejected:
        _log_line(f"  {label}: rejected: {plan.rejected_reason}", error=True)
        return
    if plan.already_matches:
        if restore:
            _log_line(f"  {label}: already matches its backed-up title {plan.target_name!r}.")
        else:
            _log_line(f"  {label}: already matches {order_label} title {plan.target_name!r}.")
        return
    for field, old_value, new_value in plan.changes:
        _log_line(f"  {label} {field}: {old_value!r} -> {new_value!r}")


def _unmatched_position_count(
    local_positions: frozenset[tuple[int, int]],
    candidate_positions: Mapping[tuple[int, int], TvdbEpisode],
) -> int:
    """Return how many local positions a candidate's episode list doesn't cover."""
    return sum(1 for position in local_positions if position not in candidate_positions)


def resolve_series_tvdb_id(
    client: JellyfinClient,
    tvdb_client: TvdbClient,
    series_name: str,
    series_id: str,
    assigned_tvdb_id: str | None,
) -> str | None:
    """Return the TheTVDB series id that best explains this series' local episodes.

    Jellyfin's own assigned TheTVDB id for a series can itself be the wrong
    one - TheTVDB sometimes has more than one series entry sharing an exact
    name (e.g. a decades-old show and a from-scratch modern revival, each
    independently numbering their own "Season 1"), and Jellyfin's automatic
    matching has no way to know which one actually explains a given local
    library's episodes. Blindly trusting the assigned id here would mean a
    wrong match doesn't just go uncorrected, it gets used to actively
    rename episodes to some *other* show's titles.

    This searches TheTVDB by name for up to ``_MAX_TVDB_SEARCH_CANDIDATES``
    same-named candidates, adds the assigned id itself if it isn't already
    among them, fetches each candidate's aired-order episode list, and picks
    whichever one's positions best overlap this series' full local
    (season, episode) set - across every season, not just the one being
    renamed, since a wrong id can still coincidentally explain a single
    season while failing everywhere else. Aired order is used for this
    comparison regardless of which ordering the caller ultimately wants
    titles from, since it's the ordering most likely to be fully populated
    for the genuinely correct series.

    Args:
        client: Client for the server the series lives on.
        tvdb_client: TheTVDB client to search and fetch candidate episode
            lists with.
        series_name: Series display name, used for the TheTVDB search.
        series_id: Jellyfin Series item identifier, to read local episode
            positions from.
        assigned_tvdb_id: The TheTVDB id Jellyfin currently has assigned to
            this series, if any - always considered as a candidate even
            when TheTVDB's search doesn't itself surface it.

    Returns:
        The best-fitting TheTVDB id, or ``assigned_tvdb_id`` unchanged when
        there's nothing to compare against (no local episodes at all, or
        the search fails) or no other candidate beats it. ``None`` only
        when there's no assigned id and no candidate was found at all.
    """
    local_positions = client.get_series_episode_positions(series_id)
    if not local_positions:
        return assigned_tvdb_id

    candidate_ids: list[str] = [assigned_tvdb_id] if assigned_tvdb_id is not None else []

    try:
        search_results = tvdb_client.search_series(series_name)
    except TvdbError as error:
        LOGGER.warning("Skipping TheTVDB series search for %r: %s", series_name, error)
        search_results = ()

    considered = 0
    for result in search_results:
        if result.id in candidate_ids:
            continue
        if considered >= _MAX_TVDB_SEARCH_CANDIDATES:
            break
        considered += 1
        candidate_ids.append(result.id)

    if not candidate_ids:
        return None
    if len(candidate_ids) == 1:
        return candidate_ids[0]

    best_id = candidate_ids[0]
    best_unmatched = None
    for candidate_id in candidate_ids:
        try:
            episodes = tvdb_client.get_series_episodes(
                candidate_id, "official", series_name=series_name
            )
        except TvdbError as error:
            LOGGER.warning(
                "Skipping TheTVDB candidate %s for %r: %s", candidate_id, series_name, error
            )
            continue
        candidate_positions = {
            (episode.season_number, episode.episode_number): episode for episode in episodes
        }
        unmatched = _unmatched_position_count(local_positions, candidate_positions)
        if best_unmatched is None or unmatched < best_unmatched:
            best_unmatched = unmatched
            best_id = candidate_id

    return best_id


def run_apply_episode_titles(
    *,
    series_name: str,
    season_number: int,
    server_key: str | None,
    library_name: str | None,
    assume_yes: bool,
    use_dvd_order: bool = False,
    restore: bool = False,
) -> int:
    """Rename one series/season's episode titles to TheTVDB's aired/DVD order,
    or restore them from each episode's own OriginalTitle backup.

    Args:
        series_name: Series display name to match in Jellyfin.
        season_number: Season number to update.
        server_key: Configured server key from servers.toml, or ``None`` to
            use servers.toml's default_server.
        library_name: Library name to restrict the series search to, or
            ``None`` to search every TV library.
        assume_yes: Skip the interactive confirmation prompt when ``True``.
        use_dvd_order: Rename toward TheTVDB's DVD order instead of aired
            order (the default). Ignored when ``restore`` is ``True``.
        restore: Restore each episode's Name from its own OriginalTitle
            backup instead of renaming toward TheTVDB - a purely local
            operation that needs no TheTVDB api_key and never contacts
            TheTVDB. An episode with no OriginalTitle backup is left alone.

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

    if not restore and not app_config.tvdb.api_key:
        _log_line(
            "apply_episode_titles requires api_key to be set in the [tvdb] "
            "table of servers.toml.",
            error=True,
        )
        return 2

    order_label = "DVD-order" if use_dvd_order else "aired-order"
    season_type = "dvd" if use_dvd_order else "official"
    verb = "restore" if restore else "rename"
    past_participle = "restored" if restore else "renamed"

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
            LOGGER.debug(
                "Matched series %r -> item %s in library %r (Jellyfin-assigned TheTVDB id %s).",
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

            if restore:
                plans = []
                for episode in episodes:
                    LOGGER.debug(
                        "Checking %s %r (item %s)...",
                        _format_position((season_number, episode.episode_number)),
                        episode.name,
                        episode.id,
                    )
                    plans.append(plan_episode_title_restore(client, episode, season_number))
                plans = tuple(plans)
            else:
                with TvdbClient(
                    app_config.tvdb.api_key,
                    cache=TvdbEpisodeCache(ttl=timedelta(days=app_config.tvdb.cache_ttl_days)),
                ) as tvdb_client:
                    LOGGER.debug(
                        "Resolving the TheTVDB series id that best explains %r's local episodes...",
                        series_name,
                    )
                    tvdb_id = resolve_series_tvdb_id(
                        client, tvdb_client, series_name, match.series_id, match.tvdb_id
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
                        "Fetching TheTVDB %s-order episodes for series id %s...",
                        season_type,
                        tvdb_id,
                    )
                    target_episodes = tvdb_client.get_series_episodes(
                        tvdb_id, season_type, series_name=series_name
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

            if restore:
                _log_line(
                    f"Episode titles restore: {series_name!r} "
                    f"season {season_number} on {server.name}"
                )
            else:
                _log_line(
                    f"Episode titles rename ({order_label}): {series_name!r} "
                    f"season {season_number} on {server.name}"
                )
            for plan in plans:
                _describe_plan(plan, order_label=order_label, restore=restore)

            actionable_plans = tuple(plan for plan in plans if plan.is_actionable)
            rejected_plans = tuple(plan for plan in plans if plan.is_rejected)

            if not actionable_plans:
                _log_line("Nothing to do.")
                return 1 if rejected_plans else 0

            if not assume_yes:
                if restore:
                    prompt = (
                        f"Restore {len(actionable_plans)} episode(s) to their "
                        "backed-up title? [y/N] "
                    )
                else:
                    prompt = (
                        f"Rename {len(actionable_plans)} episode(s) to their "
                        f"{order_label} title? [y/N] "
                    )
                response = input(prompt).strip().lower()
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
                    _log_line(f"  {_format_position(plan.position)}: {past_participle}.")

            already_matching_count = sum(1 for plan in plans if plan.already_matches)
            no_match_count = sum(1 for plan in plans if plan.no_target_match)
            no_match_label = "no OriginalTitle backup" if restore else f"no {order_label} match"
            _log_line(
                f"Episode titles {verb} complete: {len(actionable_plans) - failed} {past_participle}, "
                f"{failed} failed, {len(rejected_plans)} rejected, "
                f"{already_matching_count} already matching, "
                f"{no_match_count} with {no_match_label}."
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
            "current Name is backed up into OriginalTitle. --restore reverses "
            "this: it sets each episode's Name back to its own OriginalTitle "
            "backup, purely locally - it needs no TheTVDB api_key and never "
            "contacts TheTVDB."
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
        "--restore",
        action="store_true",
        help=(
            "Restore each episode's Name from its own OriginalTitle backup - "
            "the title backed up there before an earlier rename - instead of "
            "renaming toward TheTVDB. Needs no TheTVDB api_key and never "
            "contacts TheTVDB. An episode with no OriginalTitle backup is left "
            "alone. Cannot be combined with --dvd-order."
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

    if args.restore and args.dvd_order:
        LOGGER.error("--dvd-order cannot be used with --restore.")
        return 2

    return run_apply_episode_titles(
        series_name=args.series_name,
        season_number=args.season_number,
        server_key=args.server,
        library_name=args.library,
        assume_yes=args.yes,
        use_dvd_order=args.dvd_order,
        restore=args.restore,
    )


if __name__ == "__main__":
    raise SystemExit(main())
