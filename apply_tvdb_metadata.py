#!/usr/bin/python3
"""CLI to apply TheTVDB metadata to one series' episodes: aired order, DVD order, or restore.

Exactly one of --aired, --dvd, or --restore selects the mode:

--aired and --dvd each overwrite Name and Overview with TheTVDB's aired-order
or DVD-order values (respectively) at each episode's existing (season,
episode) position, via a fresh TheTVDB lookup - useful for a series stored on
disk in one ordering but currently labeled with the other's titles.

--restore undoes a previous --dvd (or --aired) apply: it prefers each
episode's own OriginalTitle backup for Name - the title this tool backed up
there the last time it changed Name - over a fresh TheTVDB lookup, since the
backup reflects exactly what the episode had before being changed. When
there's no backup at all, it falls back to a fresh TheTVDB aired-order
lookup, the same as --aired would use. Overview has no local backup, so it
always comes from TheTVDB's aired-order data when available, regardless of
where Name came from. Because of this, --restore also needs a TheTVDB
api_key and does contact TheTVDB, whenever there's actually something to
restore or sync.

Defaults to every season the series has; pass --season-number to scope it
to just one.

Episode/season numbers are never touched, so this only corrects what an
episode is called and described as, not where it lives. --images
additionally replaces each episode's Primary image with TheTVDB's image for
the target-order episode, when TheTVDB has one, independently of whether
Name/Overview change this run - unlike Name, there is no local backup for
the pre-change image, so --restore --images can only restore it when
TheTVDB still reports an aired-order image at that position.

Whether Name is even worth rewriting is decided with audit.titles_match(),
the same lenient comparison audit.py's aired_dvd_order_mismatch check uses
(punctuation, articles, accents, US/UK spelling, and more are all treated as
equivalent) - an episode already reading the same as the target title under
those rules is left alone rather than being rewritten to TheTVDB's exact
spelling for no practical benefit. Overview has no such lenient comparison -
it's prose, not a short label, so it's compared for exact equality.

A candidate title still in its original, untranslated language (see
audit.is_untranslated_tvdb_title) is never written to Name, even though
TheTVDB reports a name there - there's no separate flag in TheTVDB's
response saying a title fell back to the original language, and writing
foreign-script text into an otherwise-English library's metadata would be a
worse outcome than leaving Name alone. This never blocks Overview/image,
which are independent of Name's source in every mode.

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
from typing import Literal

from audit import is_untranslated_tvdb_title
from audit import titles_match
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


LOGGER = logging.getLogger("apply_tvdb_metadata")

Mode = Literal["aired", "dvd", "restore"]

# Append-only record of every apply attempt, mirroring
# transfer_metadata.py's METADATA_TRANSFER_LOG_FILE convention.
METADATA_LOG_FILE = Path("tvdb_metadata_apply.log")

# Fields this tool ever writes, and therefore diffs/locks. OriginalTitle only
# ever changes as a side effect of a Name change (see build_merged_item_dto),
# never on its own.
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
    with METADATA_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} apply_tvdb_metadata: {message}\n")


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


def build_merged_item_dto(
    destination_dto: Mapping[str, Any],
    *,
    target_name: str | None,
    target_overview: str | None,
) -> dict[str, Any]:
    """Return the destination episode document set to its target Name/Overview.

    Mirrors transfer_metadata.build_merged_item_dto: starts from a full copy
    of the destination document minus NON_EDITABLE_ITEM_FIELDS, and only
    overwrites a field when a target value was actually resolved for it, so
    a field with no target value doesn't clobber a real value already on the
    destination.

    Name is only rewritten when it doesn't already read the same as
    ``target_name`` under audit.titles_match()'s lenient rules - an episode
    already equivalent to the target title is left alone rather than
    rewritten to TheTVDB's exact spelling for no practical benefit. Before
    overwriting Name, the episode's current Name is copied into
    OriginalTitle, so a later --restore can recover the exact pre-edit title
    instead of relying on a fresh (and possibly since-changed) TheTVDB
    lookup. This does mean a genuine original-language title already stored
    in OriginalTitle is overwritten - this tool repurposes that field
    specifically as its own undo backup.

    Overview has no lenient comparison - it's prose, not a short label - so
    it's only rewritten when it's genuinely different by exact comparison.

    Args:
        destination_dto: Full episode item document read from Jellyfin.
        target_name: The title to rename toward, or ``None`` when nothing
            usable was resolved for this position (already excludes a
            blank/untranslated title or an incomplete multi-episode range -
            see plan_episode_update).
        target_overview: The overview to sync toward, or ``None`` when
            nothing usable was resolved for this position.

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
    if target_name and not titles_match(target_name, str(current_name or "")):
        merged_dto["OriginalTitle"] = current_name
        merged_dto["Name"] = target_name
        changed_fields.extend(("OriginalTitle", "Name"))

    if target_overview is not None and target_overview != destination_dto.get("Overview"):
        merged_dto["Overview"] = target_overview
        changed_fields.append("Overview")

    _lock_changed_fields(destination_dto, merged_dto, changed_fields)
    return merged_dto


def _combined_name(episodes: tuple[TvdbEpisode, ...]) -> str | None:
    """Return every episode's title joined with " / ", or ``None`` if any is blank or untranslated.

    A blank (or missing) title at any position means there's nothing usable
    to join there. A title still in its original, untranslated language
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

    ``None`` only when any position's overview is genuinely absent (an
    empty-string overview is a real value to write, not a missing one).
    """
    overviews = [episode.overview for episode in episodes]
    if any(overview is None for overview in overviews):
        return None
    return "\n\n".join(overviews)


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return changed_fields(destination_dto, merged_dto, METADATA_FIELDS)


@dataclass(frozen=True, slots=True)
class EpisodePlan:
    """A computed, not-yet-applied metadata update for one episode.

    Separating planning from applying lets the whole batch's changes be
    previewed and confirmed before anything is written.
    """

    episode_id: str
    position: tuple[int, int]
    current_name: str
    target_name: str | None
    merged_dto: dict[str, Any] | None
    changes: tuple[tuple[str, Any, Any], ...]
    rejected_reason: str | None
    no_target_match: bool
    no_english_title: bool = False
    already_matches: bool = False
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
        """Return whether this plan should actually be applied.

        ``no_english_title`` never blocks this on its own - it only means
        Name was left out of ``merged_dto``, but Overview/image are
        independent of Name's source in every mode, so a real Overview or
        image change can still be worth applying even when Name is blocked
        this way.
        """
        return (
            not self.no_target_match
            and not self.already_matches
            and not self.is_rejected
            and (self.has_changes or self.has_image_change)
        )


def plan_episode_update(
    client: JellyfinClient,
    episode: EpisodeSummary,
    season_number: int,
    target_positions: Mapping[tuple[int, int], TvdbEpisode],
    *,
    mode: Mode,
    images: bool = False,
    tvdb_client: TvdbClient | None = None,
) -> EpisodePlan:
    """Compute one episode's metadata update, without writing anything.

    A single video file can span more than one episode, e.g.
    ``Show S01E17-E18 Title.mkv`` - Jellyfin's own episode_number for such an
    item is just the range's first episode (17 here), so acting toward only
    that one position's TheTVDB data would silently drop the second
    episode's title/overview instead of producing the combined values a
    multi-episode item's metadata is supposed to read (see
    media.expected_episode_numbers_from_text). Every position the filename
    implies is required to have TheTVDB data before an update is planned at
    all - a partial combination built from only some of the range would be
    guessing.

    Args:
        client: Client for the server the episode lives on.
        episode: The Jellyfin episode to plan an update for.
        season_number: The season this episode belongs to.
        target_positions: TheTVDB episodes for this series in the target
            ordering ("dvd" for --dvd, "official" for --aired/--restore),
            keyed by (season_number, episode_number) as TheTVDB reports them
            for that ordering.
        mode: "aired"/"dvd" always use a fresh TheTVDB lookup for Name.
            "restore" prefers each episode's own OriginalTitle backup for
            Name, falling back to a fresh TheTVDB aired-order lookup when
            there's no backup. Overview/image always come from TheTVDB in
            every mode, independent of where Name came from.
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
        its original, untranslated language - Name is left unchanged in
        that case, but Overview/image can still apply independently, so
        ``no_english_title`` never affects ``is_actionable`` on its own.
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

    if mode != "restore" and target_episodes is None:
        # --aired/--dvd have no fallback data source - OriginalTitle only
        # ever holds a backup made during a *previous* apply - so missing
        # TheTVDB data at this position (or, for a multi-episode item, any
        # position it covers) is always a hard skip, without needing to
        # fetch the item at all.
        return EpisodePlan(
            episode_id=episode.id,
            position=position,
            current_name=episode.name,
            target_name=None,
            merged_dto=None,
            changes=(),
            rejected_reason=None,
            no_target_match=True,
        )

    destination_dto = client.get_item(episode.id)

    if mode == "restore":
        original_title = destination_dto.get("OriginalTitle") or None
        if target_episodes is None and original_title is None:
            return EpisodePlan(
                episode_id=episode.id,
                position=position,
                current_name=str(destination_dto.get("Name", episode.name)),
                target_name=None,
                merged_dto=None,
                changes=(),
                rejected_reason=None,
                no_target_match=True,
            )
        # TheTVDB's language only matters here when there's no OriginalTitle
        # backup to prefer instead - a backup already restores Name without
        # ever consulting TheTVDB, so its language is never in question.
        no_english_title = original_title is None and _has_untranslated_name(target_episodes)
        target_name = original_title or (_combined_name(target_episodes) if target_episodes else None)
    else:
        no_english_title = _has_untranslated_name(target_episodes)
        target_name = _combined_name(target_episodes)

    target_overview = _combined_overview(target_episodes) if target_episodes else None
    merged_dto = build_merged_item_dto(
        destination_dto, target_name=target_name, target_overview=target_overview
    )

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

    changes = _changed_fields(destination_dto, merged_dto)
    return EpisodePlan(
        episode_id=episode.id,
        position=position,
        current_name=str(destination_dto.get("Name", episode.name)),
        target_name=target_name,
        merged_dto=merged_dto,
        changes=changes,
        rejected_reason=_rejected_reason(merged_dto),
        no_target_match=False,
        no_english_title=no_english_title,
        already_matches=not changes and image_bytes is None,
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


def _describe_plan(plan: EpisodePlan, *, mode: Mode) -> None:
    """Log one episode's planned outcome."""
    label = _format_position(plan.position)
    order_label = "aired order" if mode in ("aired", "restore") else "DVD order"
    if plan.no_target_match:
        if mode == "restore":
            _log_line(
                f"  {label}: no backup title and no TheTVDB aired-order match "
                "at this position - skipped."
            )
        else:
            _log_line(f"  {label}: no TheTVDB {order_label} match at this position - skipped.")
        return
    if plan.is_rejected:
        _log_line(f"  {label}: rejected: {plan.rejected_reason}", error=True)
        return
    if plan.no_english_title:
        _log_line(
            f"  {label}: TheTVDB has no English title at this position - Name left unchanged."
        )
    if plan.already_matches:
        _log_line(f"  {label}: already matches {order_label} title {plan.target_name!r}.")
        return
    for field, old_value, new_value in plan.changes:
        _log_line(f"  {label} {field}: {old_value!r} -> {new_value!r}")
    if plan.has_image_change:
        _log_line(
            f"  {label} Primary image: {len(plan.image_bytes)} bytes, "
            f"{plan.image_content_type} (from TheTVDB)"
        )


def run_apply_tvdb_metadata(
    *,
    series_name: str,
    season_number: int | None,
    server_key: str | None,
    library_name: str | None,
    path_filter: str | None = None,
    assume_yes: bool,
    mode: Mode,
    images: bool = False,
) -> int:
    """Apply TheTVDB's aired/DVD-order (or a restore of) Name/Overview for one series.

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
        mode: "aired" or "dvd" apply that ordering's TheTVDB values via a
            fresh lookup; "restore" prefers each episode's own OriginalTitle
            backup for Name, falling back to a fresh aired-order lookup.
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
            "apply_tvdb_metadata requires api_key to be set in the [tvdb] "
            "table of servers.toml.",
            error=True,
        )
        return 2

    order_label = "aired-order" if mode in ("aired", "restore") else "DVD-order"
    order_label_title = "Aired-order" if mode in ("aired", "restore") else "DVD-order"
    verb = "restore" if mode == "restore" else "update"
    past_participle = "restored" if mode == "restore" else "updated"

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

            season_type = "dvd" if mode == "dvd" else "official"
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
                                mode=mode,
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
                _describe_plan(plan, mode=mode)

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

            already_matching_count = sum(1 for plan in plans if plan.already_matches)
            no_match_count = sum(1 for plan in plans if plan.no_target_match)
            no_english_title_count = sum(1 for plan in plans if plan.no_english_title)
            _log_line(
                f"{order_label_title} metadata {verb} complete: "
                f"{len(actionable_plans) - failed} {past_participle}, {failed} failed, "
                f"{len(rejected_plans)} rejected, {already_matching_count} already matching, "
                f"{no_match_count} with no {order_label} match, "
                f"{no_english_title_count} with no English title available (Name left unchanged)."
            )

            return 1 if failed or rejected_plans else 0
    except (JellyfinError, TvdbError) as error:
        _log_line(str(error), error=True)
        return 1


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the apply_tvdb_metadata entrypoint."""
    parser = argparse.ArgumentParser(
        prog="apply_tvdb_metadata",
        description=(
            "Apply TheTVDB metadata to one series' episodes: --aired or --dvd "
            "overwrite Name and Overview with TheTVDB's aired-order or "
            "DVD-order values (a fresh lookup each time); --restore prefers "
            "each episode's own OriginalTitle backup for Name, falling back "
            "to a fresh TheTVDB aired-order lookup when there's no backup, "
            "and always syncs Overview from TheTVDB's aired-order data too. "
            "Exactly one of --aired/--dvd/--restore is required. Episode/season "
            "numbers are never changed. An episode already reading the same "
            "as the target title is left alone - matching is the same "
            "lenient comparison audit.py's aired_dvd_order_mismatch check "
            "uses (punctuation, articles, accents, US/UK spelling, and more "
            "are all treated as equivalent), not an exact string match. "
            "Before an actual Name change, the episode's current Name is "
            "backed up into OriginalTitle."
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
        help="Apply TheTVDB's aired-order Name/Overview via a fresh lookup.",
    )
    parser.add_argument(
        "--dvd",
        action="store_true",
        help="Apply TheTVDB's DVD-order Name/Overview via a fresh lookup.",
    )
    parser.add_argument(
        "--restore",
        action="store_true",
        help=(
            "Restore toward aired order, preferring each episode's own "
            "OriginalTitle backup for Name over a fresh TheTVDB aired-order "
            "lookup when one is present. Overview always comes from "
            "TheTVDB's aired-order data. Requires a TheTVDB api_key, same "
            "as --aired/--dvd."
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
            "pre-change image, so --restore --images can only restore it "
            "when TheTVDB still reports an aired-order image at that "
            "position."
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
            "written to tvdb_metadata_apply.log."
        ),
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the TheTVDB metadata apply workflow and return an exit code."""
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

    mode_flags = {"aired": args.aired, "dvd": args.dvd, "restore": args.restore}
    selected_modes = [name for name, selected in mode_flags.items() if selected]
    if len(selected_modes) != 1:
        LOGGER.error("Specify exactly one of --aired, --dvd, or --restore.")
        return 2

    return run_apply_tvdb_metadata(
        series_name=args.series_name,
        season_number=args.season_number,
        server_key=args.server,
        library_name=args.library,
        path_filter=args.path,
        assume_yes=args.yes,
        mode=selected_modes[0],
        images=args.images,
    )


if __name__ == "__main__":
    raise SystemExit(main())
