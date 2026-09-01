#!/usr/bin/python3
"""CLI to transfer one media item's metadata from one Jellyfin server to another.

This module is intended to be run directly, typically via a command copied
from the "Mismatched Metadata" comparison report. It does not contain audit
logic, report formatting, or any assumptions beyond the two items it is
pointed at.
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

from config import ConfigError
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError


LOGGER = logging.getLogger("transfer_metadata")

# Written alongside the console output for every transfer attempt, so a
# nightly/unattended --yes run (which nobody is watching a terminal for)
# still leaves a persistent record of what changed and what didn't. Appended
# to, never truncated, so history accumulates across runs.
METADATA_TRANSFER_LOG_FILE = Path("metadata_transfer.log")

TRANSFERABLE_METADATA_FIELDS = (
    "Name",
    "OriginalTitle",
    "Overview",
    "Genres",
    "Tags",
    "Studios",
    "People",
    "ProviderIds",
    "CommunityRating",
    "OfficialRating",
    "PremiereDate",
    "ProductionYear",
    "IndexNumber",
    "ParentIndexNumber",
)

# Fields whose absence from the outgoing payload previously caused Jellyfin
# to clear them on the server - including Path, which turned a real,
# file-backed episode into a pathless "virtual" placeholder that Jellyfin's
# library scanner then deleted outright. transfer_metadata() refuses to send
# an update if any of these come back empty, rather than risk repeating that.
REQUIRED_NON_EMPTY_FIELDS = ("Id", "Path")

# Jellyfin's item-update endpoint replaces an item's metadata wholesale: any
# field omitted from the request body gets cleared server-side, not left
# alone. That makes an allowlist of "editable" fields dangerous - a field
# missing from the allowlist, or absent from the GET response for any
# reason, silently wipes real data (this previously cleared IndexNumber on
# an episode, sending it to the bottom of its season). So the update payload
# starts from the FULL destination document and only removes fields known to
# break the update endpoint's strict deserializer - read-only, computed
# fields that reflect server-managed state rather than editable metadata.
# Trickplay is confirmed via a live 500 crash; the rest are the same
# category of computed/per-user data and are excluded on the same basis.
#
# Image-tag fields (ImageTags, BackdropImageTags, ScreenshotImageTags,
# ImageBlurHashes) are deliberately NOT in this set, even though they are
# also computed/server-managed: omitting them from the payload doesn't just
# fail to deserialize, it reads as "this item now has no custom image",
# which caused Jellyfin to discard the destination's own extracted image
# and fall back to whatever was previously cached (observed reverting a
# freshly re-extracted folder.jpg back to the source server's image after a
# transfer). Since TRANSFERABLE_METADATA_FIELDS never overwrites them with
# the source's values, leaving them out of this set means
# build_merged_item_dto round-trips the destination's own current values
# unchanged instead - carrying them over rather than dropping them.
NON_EDITABLE_ITEM_FIELDS = frozenset(
    {
        "Trickplay",
        "MediaSources",
        "MediaStreams",
        "UserData",
        "RemoteTrailers",
        "Chapters",
        "ExternalUrls",
    }
)

# Jellyfin deserializes LockedFields into its own MetadataField enum (Cast,
# Genres, ProductionLocations, Studios, Tags, Name, Overview, Runtime,
# OfficialRating) - OriginalTitle is not a member. Sending any value outside
# that set fails the *entire* update with a 400, not just that one entry, so
# only ever lock fields known to be valid there. Only Name is ever locked
# here (not Overview/Genres/etc., even though several of them are also
# lockable): Name is the one field a stale post-transfer audit read is known
# to misreport (see jellyfin.py's _resolve_stale_title()), so it's the one
# this module can back up a correctness claim for.
LOCKABLE_METADATA_FIELDS = frozenset({"Name"})


def lock_changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: dict[str, Any],
    changed_fields: list[str],
) -> None:
    """Add every changed, lockable field to the item's LockedFields, in place.

    This is the same thing Jellyfin's own "Edit Metadata" dialog does when a
    field is changed by hand. Without it, a library with an internet
    metadata provider enabled treats Name as provider-owned and its next
    scheduled/on-demand metadata refresh silently overwrites the change
    again, even though the API write itself succeeded. Shared with
    title_backup.py, which every apply_*_titles.py rename tool uses for the
    exact same reason.
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


class CommandLineUsageError(ValueError):
    """Raised when command-line arguments are valid syntactically but unusable."""


def configure_logging() -> None:
    """Configure INFO-level application logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _log_line(message: str, *, error: bool = False) -> None:
    """Emit one line of transfer output to the console and to the log file.

    Console output stays exactly as before (``print`` for normal lines,
    the logger for errors, matching whatever handler ``configure_logging``
    set up) - this only adds a persistent, append-only copy in
    ``METADATA_TRANSFER_LOG_FILE`` so the same information survives an
    unattended run nobody watched the console for.
    """
    if error:
        LOGGER.error(message)
    else:
        print(message)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level = "ERROR" if error else "INFO"
    with METADATA_TRANSFER_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} transfer_metadata: {message}\n")


def build_merged_item_dto(
    source_dto: Mapping[str, Any],
    destination_dto: Mapping[str, Any],
) -> dict[str, Any]:
    """Return the destination item document with transferable fields overwritten.

    Starts from a full copy of the destination document, minus the fields in
    ``NON_EDITABLE_ITEM_FIELDS``, so every field Jellyfin doesn't explicitly
    reject is preserved exactly as the destination server already has it
    (see ``NON_EDITABLE_ITEM_FIELDS``'s docstring note on why an allowlist is
    unsafe here). Only fields in ``TRANSFERABLE_METADATA_FIELDS`` are
    overwritten, and only when the source actually has a non-null value for
    them - Jellyfin often returns an explicit ``null`` for fields an item
    simply doesn't have set (e.g. an episode with no standalone
    ProductionYear), and a null from the source must not clobber a real
    value already present on the destination.

    When Name actually changes, it's added to the destination item's
    LockedFields, the same convention the apply_*_titles.py rename tools
    use - see lock_changed_fields()'s docstring for why. Without it, a
    subsequent audit's whole-library listing can keep showing the
    destination's pre-transfer title for a while even though the transfer
    succeeded and Jellyfin's own UI already shows the new one.

    Args:
        source_dto: Full item document read from the source server.
        destination_dto: Full item document read from the destination server.

    Returns:
        A new item document ready to send back to the destination server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }
    for field in TRANSFERABLE_METADATA_FIELDS:
        source_value = source_dto.get(field)
        if source_value is not None:
            merged_dto[field] = source_value

    if merged_dto.get("Name") != destination_dto.get("Name"):
        lock_changed_fields(destination_dto, merged_dto, ["Name"])

    return merged_dto


def changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
    fields: Sequence[str],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each of ``fields`` that will change.

    Shared by every apply_*.py tool's own plan-building step: each merges a
    destination document and wants to know which of its own writable fields
    actually differ, just against a different field list (this module's own
    TRANSFERABLE_METADATA_FIELDS, or another tool's own METADATA_FIELDS).
    """
    return tuple(
        (field, destination_dto.get(field), merged_dto.get(field))
        for field in fields
        if destination_dto.get(field) != merged_dto.get(field)
    )


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return changed_fields(destination_dto, merged_dto, TRANSFERABLE_METADATA_FIELDS)


def rejected_reason(merged_dto: Mapping[str, Any]) -> str | None:
    """Return why a merged episode document is unsafe to write, or ``None``.

    Shared by apply_tvdb_metadata.py and apply_episode_numbers.py - each
    computes this identically for its own
    from-scratch merged episode document. Kept separate from this module's
    own inline rejection check in plan_transfer(), which is worded for a
    generic "destination item" rather than specifically an episode.
    """
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


def _skipped_null_source_fields(
    source_dto: Mapping[str, Any],
    destination_dto: Mapping[str, Any],
) -> tuple[str, ...]:
    """Return transferable fields the source has no value for.

    Jellyfin returns an explicit ``null`` (or omits the key) for a field an
    item doesn't have set. ``build_merged_item_dto`` treats that as "nothing
    to copy" and keeps the destination's existing value rather than
    clobbering it - which is safe, but silent. This surfaces those cases so
    a field that looks like it "didn't transfer" can be told apart from one
    that already matched: if it shows up here, the source server itself has
    no value for that field on this item.
    """
    return tuple(
        field
        for field in TRANSFERABLE_METADATA_FIELDS
        if source_dto.get(field) is None and destination_dto.get(field) is not None
    )


@dataclass(frozen=True, slots=True)
class TransferPlan:
    """A computed, not-yet-applied metadata transfer for one item pair.

    Separating planning (read both items, compute the merge, decide whether
    it's safe) from applying (the actual write) lets callers preview a
    transfer - or many transfers - before committing to any of them.
    """

    from_item_id: str
    to_item_id: str
    source_name: str
    destination_name: str
    merged_dto: dict[str, Any]
    changes: tuple[tuple[str, Any, Any], ...]
    skipped_fields: tuple[str, ...]
    rejected_reason: str | None

    @property
    def has_changes(self) -> bool:
        """Return whether applying this plan would change anything."""
        return bool(self.changes)

    @property
    def is_rejected(self) -> bool:
        """Return whether this plan failed the pre-write safety check."""
        return self.rejected_reason is not None


def plan_transfer(
    from_client: JellyfinClient,
    to_client: JellyfinClient,
    from_item_id: str,
    to_item_id: str,
) -> TransferPlan:
    """Fetch both items and compute their merge, without writing anything.

    Args:
        from_client: Client for the source server.
        to_client: Client for the destination server.
        from_item_id: Jellyfin item identifier on the source server.
        to_item_id: Jellyfin item identifier on the destination server.

    Returns:
        A plan describing what would change and whether it's safe to apply.
    """
    source_dto = from_client.get_item(from_item_id)
    destination_dto = to_client.get_item(to_item_id)
    merged_dto = build_merged_item_dto(source_dto, destination_dto)

    missing_required_fields = tuple(
        field for field in REQUIRED_NON_EMPTY_FIELDS if not merged_dto.get(field)
    )
    rejected_reason = (
        "the destination item is missing required field(s) "
        f"{', '.join(missing_required_fields)}. Sending this update would clear "
        "them on the server instead of leaving them alone. This usually means "
        "Jellyfin's response for this item didn't include those fields."
        if missing_required_fields
        else None
    )

    return TransferPlan(
        from_item_id=from_item_id,
        to_item_id=to_item_id,
        source_name=str(source_dto.get("Name", from_item_id)),
        destination_name=str(destination_dto.get("Name", to_item_id)),
        merged_dto=merged_dto,
        changes=_changed_fields(destination_dto, merged_dto),
        skipped_fields=_skipped_null_source_fields(source_dto, destination_dto),
        rejected_reason=rejected_reason,
    )


def apply_transfer(to_client: JellyfinClient, plan: TransferPlan) -> None:
    """Write a previously computed, accepted plan to the destination server.

    Args:
        to_client: Client for the destination server.
        plan: A plan from :func:`plan_transfer` that is not rejected.
    """
    to_client.update_item(plan.to_item_id, plan.merged_dto)


def transfer_metadata(
    *,
    from_server_key: str,
    from_item_id: str,
    to_server_key: str,
    to_item_id: str,
    assume_yes: bool,
) -> int:
    """Transfer one item's metadata from one Jellyfin server to another.

    Args:
        from_server_key: Configured source server key from servers.toml.
        from_item_id: Jellyfin item identifier on the source server.
        to_server_key: Configured destination server key from servers.toml.
        to_item_id: Jellyfin item identifier on the destination server.
        assume_yes: Skip the interactive confirmation prompt when ``True``.

    Returns:
        A process exit code: ``0`` on success, ``1`` on failure or when the
        user declines confirmation, ``2`` on a usage/configuration error.
    """
    config = get_config()
    try:
        from_server = config.servers.get(from_server_key)
        to_server = config.servers.get(to_server_key)
    except ConfigError as error:
        _log_line(str(error), error=True)
        return 2

    try:
        with JellyfinClient(from_server) as from_client, JellyfinClient(to_server) as to_client:
            plan = plan_transfer(from_client, to_client, from_item_id, to_item_id)

            _log_line(f"Transfer metadata: {from_server.name} -> {to_server.name}")
            _log_line(f"  Source item:      {plan.source_name!r} ({from_item_id})")
            _log_line(f"  Destination item: {plan.destination_name!r} ({to_item_id})")

            if plan.is_rejected:
                _log_line(f"Refusing to update {to_item_id}: {plan.rejected_reason}", error=True)
                return 1

            if not plan.has_changes:
                _log_line("No transferable fields differ. Nothing to do.")
                if plan.skipped_fields:
                    _log_line(
                        "  Note: the source server has no value for these fields, so "
                        f"the destination's existing value was kept: {', '.join(plan.skipped_fields)}"
                    )
                return 0

            _log_line("  Fields that will change:")
            for field, old_value, new_value in plan.changes:
                _log_line(f"    {field}: {old_value!r} -> {new_value!r}")
            if plan.skipped_fields:
                _log_line(
                    "  Note: the source server has no value for these fields, so "
                    f"the destination's existing value was kept: {', '.join(plan.skipped_fields)}"
                )

            if not assume_yes:
                response = input("Proceed with metadata transfer? [y/N] ").strip().lower()
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            apply_transfer(to_client, plan)
            _log_line("Metadata transfer complete.")
    except JellyfinError as error:
        _log_line(str(error), error=True)
        return 1

    return 0


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the transfer_metadata entrypoint."""
    parser = argparse.ArgumentParser(
        prog="transfer_metadata",
        description=(
            "Copy one item's metadata (title, overview, genres, provider IDs, "
            "etc.) from one Jellyfin server to another, excluding image data."
        ),
        exit_on_error=False,
    )
    parser.add_argument(
        "--from-server",
        required=True,
        metavar="SERVER",
        help="Configured source server key from servers.toml.",
    )
    parser.add_argument(
        "--from-item",
        required=True,
        metavar="ITEM_ID",
        help="Jellyfin item identifier on the source server.",
    )
    parser.add_argument(
        "--to-server",
        required=True,
        metavar="SERVER",
        help="Configured destination server key from servers.toml.",
    )
    parser.add_argument(
        "--to-item",
        required=True,
        metavar="ITEM_ID",
        help="Jellyfin item identifier on the destination server.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and transfer immediately.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the metadata transfer workflow and return an exit code."""
    configure_logging()
    parser = _build_argument_parser()
    _log_line(f"Command: {parser.prog} {shlex.join(argv if argv is not None else sys.argv[1:])}")

    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        LOGGER.error("%s", error)
        return 2

    return transfer_metadata(
        from_server_key=args.from_server,
        from_item_id=args.from_item,
        to_server_key=args.to_server,
        to_item_id=args.to_item,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    raise SystemExit(main())
