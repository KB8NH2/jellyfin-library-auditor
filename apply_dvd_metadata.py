#!/usr/bin/python3
"""CLI to switch one series' episode metadata between TheTVDB's aired and DVD order.

Defaults to every season the series has; pass --season-number to scope it
to just one.

Some series are organized on disk in TheTVDB's DVD order while Jellyfin's
episode metadata reflects TheTVDB's aired order (or vice versa), which
--check-episode-order can detect but not fix. This module looks up TheTVDB's
DVD-order episode at each of a season's existing (season, episode) positions
and overwrites that episode's Name/Overview with the DVD-order values,
leaving the episode's own season/episode numbers untouched. Before changing
Name, it backs up the episode's current Name into OriginalTitle; --aired
reverses the process, preferring that backup over a fresh TheTVDB aired-order
lookup so an inadvertent reordering can be undone with the exact title the
episode had before. --images additionally replaces each episode's Primary
image with TheTVDB's image for the target-order episode, when TheTVDB has
one - unlike Name, there is no local backup for the pre-change image, so
--aired --images can only restore it when TheTVDB still reports an
aired-order image at that position.

Jellyfin's own assigned TheTVDB id for the series isn't trusted blindly:
when TheTVDB has more than one series entry sharing the exact same name
(e.g. a decades-old show and a from-scratch modern revival, each
numbering their own "Season 1" independently), Jellyfin's automatic
matching has no way to know which one actually explains the local
library - and a wrong match here wouldn't just go uncorrected, it would
actively overwrite episode metadata with some other show's values. See
tvdb_series_resolution.resolve_series_tvdb_id() for how the right one is
picked. It does not contain audit logic or report formatting.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
from datetime import timedelta
import logging
import shlex
import sys
from collections.abc import Mapping
from collections.abc import Sequence
from pathlib import Path
from typing import Any

from audit import is_untranslated_tvdb_title
from config import ConfigError
from config import get_config
from jellyfin import EpisodeSummary
from jellyfin import JellyfinClient
from jellyfin import JellyfinError
from media import expected_episode_numbers_from_text
from transfer_metadata import NON_EDITABLE_ITEM_FIELDS
from transfer_metadata import changed_fields
from transfer_metadata import rejected_reason as _rejected_reason
from tvdb import TvdbClient
from tvdb import TvdbEpisode
from tvdb import TvdbEpisodeCache
from tvdb import TvdbError
from tvdb_series_resolution import resolve_series_tvdb_id


LOGGER = logging.getLogger("apply_dvd_metadata")

# Append-only record of every apply attempt, mirroring
# transfer_metadata.py's METADATA_TRANSFER_LOG_FILE convention.
DVD_METADATA_LOG_FILE = Path("dvd_metadata_apply.log")

# Fields this tool ever writes, and therefore diffs/locks. OriginalTitle only
# ever changes as a side effect of a DVD-order Name change (see
# build_dvd_merged_item_dto), never on its own.
METADATA_FIELDS = ("Name", "Overview", "OriginalTitle")


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
    with DVD_METADATA_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} apply_dvd_metadata: {message}\n")


# Jellyfin deserializes LockedFields into its own MetadataField enum
# (Cast, Genres, ProductionLocations, Studios, Tags, Name, Overview,
# Runtime, OfficialRating) - OriginalTitle is not a member. Sending any
# value outside that set fails the *entire* update with a 400, not just
# that one entry, so only ever lock fields known to be valid there.
LOCKABLE_METADATA_FIELDS = frozenset({"Name", "Overview"})


def _lock_changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: dict[str, Any],
    changed_fields: list[str],
) -> None:
    """Add every changed, lockable field to the item's LockedFields, in place.

    This is the same thing Jellyfin's own "Edit Metadata" dialog does when a
    field is changed by hand. Without it, a library with TheTVDB's internet
    metadata provider enabled treats Name/Overview as provider-owned and its
    next scheduled/on-demand metadata refresh silently overwrites the edit
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


def build_dvd_merged_item_dto(
    destination_dto: Mapping[str, Any],
    dvd_episodes: tuple[TvdbEpisode, ...],
) -> dict[str, Any]:
    """Return the destination episode document set to its DVD-order values.

    Mirrors transfer_metadata.build_merged_item_dto: starts from a full copy
    of the destination document minus NON_EDITABLE_ITEM_FIELDS, and only
    overwrites a field when TheTVDB actually has a value for it, so a field
    TheTVDB doesn't report doesn't clobber a real value already on the
    destination.

    Before overwriting Name, the episode's current Name is copied into
    OriginalTitle, so --aired can restore the exact pre-edit title later
    instead of relying on a fresh (and possibly since-changed) TheTVDB
    aired-order lookup. This does mean a genuine original-language title
    already stored in OriginalTitle is overwritten - this tool repurposes
    that field specifically as its own undo backup.

    A multi-episode item (see plan_episode_update) is passed every position
    it covers' TvdbEpisode, in order - Name is joined from every position's
    title with " / " and Overview from every position's synopsis with a
    blank line, matching the combined style Jellyfin itself already uses for
    a multi-episode item's own metadata. Each field is only touched when
    TheTVDB actually has it at *every* position in the range - joining from
    only some of them would silently drop the missing position's part of
    the combined value instead of just leaving the field alone. An ordinary
    single-episode item is just the one-element case of the same logic.

    Args:
        destination_dto: Full episode item document read from Jellyfin.
        dvd_episodes: TheTVDB DVD-order episode(s) at this item's
            position(s), one per episode number a multi-episode filename
            marker implies (or just one, for an ordinary single-episode
            item).

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }

    changed_fields: list[str] = []
    current_name = destination_dto.get("Name")
    combined_name = _combined_name(dvd_episodes)
    if combined_name and combined_name != current_name:
        merged_dto["OriginalTitle"] = current_name
        merged_dto["Name"] = combined_name
        changed_fields.extend(("OriginalTitle", "Name"))

    combined_overview = _combined_overview(dvd_episodes)
    if combined_overview is not None and combined_overview != destination_dto.get("Overview"):
        merged_dto["Overview"] = combined_overview
        changed_fields.append("Overview")

    _lock_changed_fields(destination_dto, merged_dto, changed_fields)
    return merged_dto


def _combined_name(episodes: tuple[TvdbEpisode, ...]) -> str | None:
    """Return every episode's title joined with " / ", or ``None`` if any is blank or untranslated.

    A blank (or missing) title at any position means there's nothing usable
    to join there - mirrors the single-episode ``if dvd_episode.name`` check
    this generalizes. A title still in its original, untranslated language
    (see audit.is_untranslated_tvdb_title) is treated the same way - TheTVDB
    silently falls back to a series' original-language name when there's no
    English translation on file, and writing foreign-script text into an
    otherwise-English library's Name would be a worse outcome than leaving
    it unchanged. Overview has no such protection (see _combined_overview) -
    it's prose rather than a short label, and TheTVDB doesn't flag its
    language the same way.
    """
    names = [episode.name for episode in episodes]
    if not all(names):
        return None
    if any(is_untranslated_tvdb_title(name) for name in names):
        return None
    return " / ".join(names)


def _has_untranslated_name(episodes: tuple[TvdbEpisode, ...] | None) -> bool:
    """Return whether any episode has a title, but it's still untranslated.

    Used to report ``EpisodePlan.no_english_title`` - unlike a blank/missing
    title (``no_target_match``, or simply no Overview/image change), this is
    worth calling out specifically: TheTVDB *has* something here, it's just
    not usable for Name.
    """
    if not episodes:
        return False
    return any(episode.name and is_untranslated_tvdb_title(episode.name) for episode in episodes)


def _combined_overview(episodes: tuple[TvdbEpisode, ...]) -> str | None:
    """Return every episode's overview joined with a blank line, or ``None``.

    ``None`` only when any position's overview is genuinely absent (unlike
    _combined_name, an empty-string overview is a real value to write, not
    a missing one - mirrors the single-episode ``if dvd_episode.overview is
    not None`` check this generalizes).
    """
    overviews = [episode.overview for episode in episodes]
    if any(overview is None for overview in overviews):
        return None
    return "\n\n".join(overviews)


def build_aired_restore_merged_item_dto(
    destination_dto: Mapping[str, Any],
    original_title: str | None,
    aired_episodes: tuple[TvdbEpisode, ...] | None,
) -> dict[str, Any]:
    """Return the destination episode document restored toward aired order.

    Prefers the episode's own OriginalTitle - the Name this tool backed up
    there during a previous DVD-order apply - over TheTVDB's aired-order
    title, since it reflects exactly what this item had before being
    changed rather than a fresh (and possibly slightly different) TheTVDB
    lookup. That backup is already the item's own combined title for a
    multi-episode item (see build_dvd_merged_item_dto), so it needs no
    recombining here - only the TheTVDB fallback (used when there's no
    backup at all) does. Overview has no such backup, so it always comes
    from TheTVDB's aired-order data when available, combined the same way
    build_dvd_merged_item_dto combines it.

    Args:
        destination_dto: Full episode item document read from Jellyfin.
        original_title: The episode's current OriginalTitle, or ``None``.
        aired_episodes: TheTVDB aired-order episode(s) at this item's
            position(s), or ``None`` when TheTVDB has nothing there (or, for
            a multi-episode item, not at every position it covers).

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }

    changed_fields: list[str] = []
    target_name = original_title or (_combined_name(aired_episodes) if aired_episodes else None)
    if target_name and target_name != destination_dto.get("Name"):
        merged_dto["Name"] = target_name
        changed_fields.append("Name")

    combined_overview = _combined_overview(aired_episodes) if aired_episodes else None
    if combined_overview is not None and combined_overview != destination_dto.get("Overview"):
        merged_dto["Overview"] = combined_overview
        changed_fields.append("Overview")

    _lock_changed_fields(destination_dto, merged_dto, changed_fields)
    return merged_dto


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return changed_fields(destination_dto, merged_dto, METADATA_FIELDS)


@dataclass(frozen=True, slots=True)
class EpisodePlan:
    """A computed, not-yet-applied metadata update for one episode.

    Separating planning from applying lets the whole season's changes be
    previewed and confirmed in one batch before anything is written.
    """

    episode_id: str
    position: tuple[int, int]
    current_name: str
    merged_dto: dict[str, Any] | None
    changes: tuple[tuple[str, Any, Any], ...]
    rejected_reason: str | None
    no_target_match: bool
    no_english_title: bool = False
    image_bytes: bytes | None = None
    image_content_type: str | None = None
    previous_primary_image_tag: str | None = None

    @property
    def has_changes(self) -> bool:
        """Return whether applying this plan would change anything."""
        return bool(self.changes)

    @property
    def has_image_change(self) -> bool:
        """Return whether this plan has a Primary image ready to upload."""
        return self.image_bytes is not None

    @property
    def is_rejected(self) -> bool:
        """Return whether this plan failed the pre-write safety check."""
        return self.rejected_reason is not None

    @property
    def is_actionable(self) -> bool:
        """Return whether this plan should actually be applied."""
        return (
            not self.no_target_match
            and not self.is_rejected
            and (self.has_changes or self.has_image_change)
        )




def plan_episode_update(
    client: JellyfinClient,
    episode: EpisodeSummary,
    season_number: int,
    target_positions: Mapping[tuple[int, int], TvdbEpisode],
    *,
    restore_aired: bool,
    images: bool = False,
    tvdb_client: TvdbClient | None = None,
) -> EpisodePlan:
    """Compute one episode's metadata update, without writing anything.

    Args:
        client: Client for the server the episode lives on.
        episode: The Jellyfin episode to plan an update for.
        season_number: The season this episode belongs to.
        target_positions: TheTVDB episodes for this series in the target
            ordering ("dvd" when applying, "official" when restoring with
            --aired), keyed by (season_number, episode_number) as TheTVDB
            reports them for that ordering.
        restore_aired: Restore toward aired order (preferring the episode's
            own OriginalTitle backup) instead of applying DVD order.
        images: Also plan replacing the episode's Primary image with
            TheTVDB's image for the target-order episode at this position,
            when TheTVDB has one. Runs independently of whether Name/Overview
            change this run, so it also fixes an episode's image after a
            previous apply already corrected its title.
        tvdb_client: TheTVDB client to download the target episode's image
            with. Required when ``images`` is ``True``.

    Returns:
        A plan describing what would change and whether it's safe to apply.
        ``no_english_title`` is set when TheTVDB's Name at this position
        (any position, for a multi-episode item) is present but still in
        its original, untranslated language (see
        audit.is_untranslated_tvdb_title) - Name is left unchanged in that
        case, same protection :func:`apply_episode_titles.plan_episode_title_update`
        gives its own renames. Unlike that sibling tool, this one still
        updates Overview/image independently even when Name is blocked this
        way - ``no_english_title`` doesn't affect ``is_actionable`` here,
        since a real Overview or image change can still be worth applying
        on its own.
    """
    position = (season_number, episode.episode_number)
    episode_numbers = (
        expected_episode_numbers_from_text(episode.path.stem, season_number, episode.episode_number)
        if episode.path is not None
        else None
    ) or (episode.episode_number,)

    target_episodes: tuple[TvdbEpisode, ...] | None = tuple(
        target_positions[(season_number, episode_number)]
        for episode_number in episode_numbers
        if (season_number, episode_number) in target_positions
    )
    if len(target_episodes) != len(episode_numbers):
        # Either no TheTVDB data at all, or (for a multi-episode item) data
        # for only some of the positions the filename covers - a partial
        # combined title/overview built from some-have/some-don't would be
        # guessing, so this is a hard skip either way, same as a plain
        # missing single-episode match.
        target_episodes = None
    # A multi-episode item's own image slot is singular - there's no way to
    # combine two images into one Primary image, so this uses the range's
    # first position, the same one Jellyfin's own episode_number reflects.
    target_episode = target_episodes[0] if target_episodes else None

    if not restore_aired and target_episodes is None:
        # DVD mode has no fallback data source - OriginalTitle only ever
        # holds a backup made during a *previous* DVD apply - so missing
        # TheTVDB DVD-order data at this position (or, for a multi-episode
        # item, any position it covers) is always a hard skip, without
        # needing to fetch the item at all.
        return EpisodePlan(
            episode_id=episode.id,
            position=position,
            current_name=episode.name,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
        )

    destination_dto = client.get_item(episode.id)

    if restore_aired:
        original_title = destination_dto.get("OriginalTitle") or None
        if target_episodes is None and original_title is None:
            return EpisodePlan(
                episode_id=episode.id,
                position=position,
                current_name=str(destination_dto.get("Name", episode.name)),
                merged_dto=None,
                changes=(),
                rejected_reason=None,
                no_target_match=True,
            )
        # TheTVDB's language only matters here when there's no OriginalTitle
        # backup to prefer instead - a backup already restores Name without
        # ever consulting TheTVDB, so its language is never in question.
        no_english_title = original_title is None and _has_untranslated_name(target_episodes)
        merged_dto = build_aired_restore_merged_item_dto(
            destination_dto, original_title, target_episodes
        )
    else:
        no_english_title = _has_untranslated_name(target_episodes)
        merged_dto = build_dvd_merged_item_dto(destination_dto, target_episodes)

    image_bytes: bytes | None = None
    image_content_type: str | None = None
    previous_primary_image_tag = destination_dto.get("ImageTags", {}).get("Primary")
    if images and target_episode is not None and target_episode.image_url:
        assert tvdb_client is not None
        try:
            image_bytes, image_content_type = tvdb_client.download_image(
                target_episode.image_url
            )
        except TvdbError as error:
            _log_line(
                f"  {_format_position(position)}: failed to download TheTVDB "
                f"image, leaving the Primary image unchanged: {error}",
                error=True,
            )

    return EpisodePlan(
        episode_id=episode.id,
        position=position,
        current_name=str(destination_dto.get("Name", episode.name)),
        merged_dto=merged_dto,
        changes=_changed_fields(destination_dto, merged_dto),
        rejected_reason=_rejected_reason(merged_dto),
        no_target_match=False,
        no_english_title=no_english_title,
        image_bytes=image_bytes,
        image_content_type=image_content_type,
        previous_primary_image_tag=previous_primary_image_tag,
    )


def apply_episode_plan(client: JellyfinClient, plan: EpisodePlan) -> None:
    """Write a previously computed, actionable plan to the server.

    Args:
        client: Client for the server the episode lives on.
        plan: A plan from :func:`plan_episode_update` that is actionable.
    """
    if plan.has_changes:
        if plan.merged_dto is None:
            raise ValueError("Cannot apply a plan with no target-order match.")
        client.update_item(plan.episode_id, plan.merged_dto)
    if plan.has_image_change:
        assert plan.image_bytes is not None and plan.image_content_type is not None
        client.upload_item_image(
            plan.episode_id, "Primary", plan.image_bytes, plan.image_content_type
        )


def _verify_applied(client: JellyfinClient, plan: EpisodePlan) -> tuple[str, ...]:
    """Re-read an item right after writing it and report any field that didn't change.

    A successful HTTP response from update_item only means Jellyfin accepted
    the write, not that the value stuck - a locked/provider-owned field can
    silently keep its old value. Mirrors transfer_images.py re-reading
    ImageTags after an upload for the same reason.

    Args:
        client: Client for the server the episode lives on.
        plan: The plan that was just applied.

    Returns:
        The field names that still report their pre-update value.
    """
    current_dto = client.get_item(plan.episode_id)
    stale_fields = [
        field
        for field, _, expected_value in plan.changes
        if current_dto.get(field) != expected_value
    ]
    if plan.has_image_change:
        new_primary_image_tag = current_dto.get("ImageTags", {}).get("Primary")
        if (
            new_primary_image_tag is None
            or new_primary_image_tag == plan.previous_primary_image_tag
        ):
            stale_fields.append("PrimaryImage")
    return tuple(stale_fields)


def _format_position(position: tuple[int, int]) -> str:
    """Return one episode position as an SxxExx label."""
    season_number, episode_number = position
    return f"S{season_number:02d}E{episode_number:02d}"


def _describe_plan(plan: EpisodePlan, *, restore_aired: bool) -> None:
    """Log one episode's planned outcome."""
    label = _format_position(plan.position)
    order_label = "aired order" if restore_aired else "DVD order"
    if plan.no_target_match:
        if restore_aired:
            _log_line(
                f"  {label}: no backup title and no TheTVDB aired-order match "
                "at this position - skipped."
            )
        else:
            _log_line(f"  {label}: no DVD-order match at this position - skipped.")
        return
    if plan.is_rejected:
        _log_line(f"  {label}: rejected: {plan.rejected_reason}", error=True)
        return
    if plan.no_english_title:
        _log_line(
            f"  {label}: TheTVDB has no English title at this position - Name left unchanged."
        )
    if not plan.has_changes and not plan.has_image_change:
        if not plan.no_english_title:
            _log_line(f"  {label}: already matches {order_label}.")
        return
    for field, old_value, new_value in plan.changes:
        _log_line(f"  {label} {field}: {old_value!r} -> {new_value!r}")
    if plan.has_image_change:
        _log_line(
            f"  {label} Primary image: {len(plan.image_bytes)} bytes, "
            f"{plan.image_content_type} (from TheTVDB)"
        )


def run_apply_dvd_metadata(
    *,
    series_name: str,
    season_number: int | None,
    server_key: str | None,
    library_name: str | None,
    path_filter: str | None = None,
    assume_yes: bool,
    restore_aired: bool = False,
    images: bool = False,
) -> int:
    """Switch TheTVDB's aired/DVD-order Name/Overview for one series.

    Args:
        series_name: Series display name to match in Jellyfin.
        season_number: Season number to update, or ``None`` to update every
            season the series has.
        server_key: Configured server key from servers.toml, or ``None`` to
            use servers.toml's default_server.
        library_name: Library name to restrict the series search to, or
            ``None`` to search every TV library.
        path_filter: When given, only a series whose Path contains this text
            (case-insensitively) is considered - disambiguates a series name
            that matches more than one show.
        assume_yes: Skip the interactive confirmation prompt when ``True``.
        restore_aired: Restore toward aired order (preferring each episode's
            own OriginalTitle backup) instead of applying DVD order.
        images: Also replace each episode's Primary image with TheTVDB's
            image for the target-order episode, when TheTVDB has one.

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
            "apply_dvd_metadata requires api_key to be set in the [tvdb] "
            "table of servers.toml.",
            error=True,
        )
        return 2

    order_label = "aired-order" if restore_aired else "DVD-order"
    order_label_title = "Aired-order" if restore_aired else "DVD-order"
    verb = "restore" if restore_aired else "update"
    past_participle = "restored" if restore_aired else "updated"

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

            if season_number is not None:
                season_numbers: tuple[int, ...] = (season_number,)
            else:
                season_numbers = client.get_series_season_numbers(match.series_id)

            episodes_by_season = {
                season: client.get_series_season_episodes(match.series_id, season)
                for season in season_numbers
            }
            if not any(episodes_by_season.values()):
                season_description = (
                    f"season {season_number}" if season_number is not None else "any season"
                )
                _log_line(
                    f"No episodes found for {series_name!r} {season_description} "
                    f"in library {match.library_name!r}."
                )
                return 0

            season_type = "official" if restore_aired else "dvd"
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

                # Image downloads happen here, while tvdb_client's session is
                # still open, mirroring transfer_images.py's plan_image_transfer
                # fetching the source image during planning, before confirmation.
                plans = []
                for season, episodes in episodes_by_season.items():
                    for episode in episodes:
                        LOGGER.debug(
                            "Checking %s %r (item %s)...",
                            _format_position((season, episode.episode_number)),
                            episode.name,
                            episode.id,
                        )
                        plans.append(
                            plan_episode_update(
                                client,
                                episode,
                                season,
                                target_positions,
                                restore_aired=restore_aired,
                                images=images,
                                tvdb_client=tvdb_client if images else None,
                            )
                        )
                plans = tuple(plans)

            season_label = f"season {season_number}" if season_number is not None else "all seasons"
            _log_line(
                f"{order_label_title} metadata {verb}: {series_name!r} "
                f"{season_label} on {server.name}"
            )
            for plan in plans:
                _describe_plan(plan, restore_aired=restore_aired)

            actionable_plans = tuple(plan for plan in plans if plan.is_actionable)
            rejected_plans = tuple(plan for plan in plans if plan.is_rejected)

            if not actionable_plans:
                _log_line("Nothing to do.")
                return 1 if rejected_plans else 0

            if not assume_yes:
                response = input(
                    f"{verb.capitalize()} {len(actionable_plans)} episode(s) to "
                    f"{order_label} metadata? [y/N] "
                ).strip().lower()
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            failed = 0
            for plan in actionable_plans:
                LOGGER.debug("Applying %s...", _format_position(plan.position))
                try:
                    apply_episode_plan(client, plan)
                except JellyfinError as error:
                    failed += 1
                    _log_line(
                        f"  {_format_position(plan.position)}: failed: {error}",
                        error=True,
                    )
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

            no_match_count = sum(1 for plan in plans if plan.no_target_match)
            no_english_title_count = sum(1 for plan in plans if plan.no_english_title)
            _log_line(
                f"{order_label_title} metadata {verb} complete: "
                f"{len(actionable_plans) - failed} {past_participle}, {failed} failed, "
                f"{len(rejected_plans)} rejected, {no_match_count} with no {order_label} match, "
                f"{no_english_title_count} with no English title available (Name left unchanged)."
            )

            return 1 if failed or rejected_plans else 0
    except (JellyfinError, TvdbError) as error:
        _log_line(str(error), error=True)
        return 1


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the apply_dvd_metadata entrypoint."""
    parser = argparse.ArgumentParser(
        prog="apply_dvd_metadata",
        description=(
            "Overwrite one series/season's episode Name and Overview with "
            "TheTVDB's DVD-order values at each existing season/episode "
            "position - for a season stored on disk in DVD order but "
            "currently labeled with aired-order titles. Episode/season "
            "numbers are never changed. Before changing Name, the episode's "
            "current Name is backed up into OriginalTitle; --aired reverses "
            "the process, preferring that backup over a fresh TheTVDB "
            "aired-order lookup so an inadvertent reordering can be undone."
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
        type=int,
        metavar="N",
        help="Season number to update. Every season is updated if omitted.",
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
        "--aired",
        action="store_true",
        help=(
            "Restore toward TheTVDB's aired order instead of applying DVD "
            "order, undoing a previous apply. Prefers each episode's own "
            "OriginalTitle backup over a fresh TheTVDB aired-order lookup "
            "when one is present."
        ),
    )
    parser.add_argument(
        "--images",
        action="store_true",
        help=(
            "Also replace each episode's Primary image with TheTVDB's image "
            "for the target-order episode, when TheTVDB has one. Runs "
            "independently of whether Name/Overview change this run, so it "
            "also fixes an episode's image after a previous apply already "
            "corrected its title. There is no local backup for the "
            "pre-change image, so --aired --images can only restore it when "
            "TheTVDB still reports an aired-order image at that position."
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
            "written to dvd_metadata_apply.log."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the DVD-order metadata apply workflow and return an exit code."""
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

    if args.season_number is not None and args.season_number < 0:
        LOGGER.error("--season-number must not be negative.")
        return 2

    return run_apply_dvd_metadata(
        series_name=args.series_name,
        season_number=args.season_number,
        server_key=args.server,
        library_name=args.library,
        path_filter=args.path,
        assume_yes=args.yes,
        restore_aired=args.aired,
        images=args.images,
    )


if __name__ == "__main__":
    raise SystemExit(main())
