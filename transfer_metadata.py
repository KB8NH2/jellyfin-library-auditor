#!/usr/bin/python3
"""CLI to transfer one media item's metadata from one Jellyfin server to another.

This module is intended to be run directly, typically via a command copied
from the "Mismatched Metadata" comparison report. It does not contain audit
logic, report formatting, or any assumptions beyond the two items it is
pointed at.
"""

from __future__ import annotations

import argparse
import logging
from collections.abc import Mapping
from collections.abc import Sequence
from typing import Any

from config import ConfigError
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError


LOGGER = logging.getLogger("transfer_metadata")

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
NON_EDITABLE_ITEM_FIELDS = frozenset(
    {
        "Trickplay",
        "MediaSources",
        "MediaStreams",
        "UserData",
        "RemoteTrailers",
        "Chapters",
        "ExternalUrls",
        "ImageTags",
        "BackdropImageTags",
        "ScreenshotImageTags",
        "ImageBlurHashes",
    }
)


class CommandLineUsageError(ValueError):
    """Raised when command-line arguments are valid syntactically but unusable."""


def configure_logging() -> None:
    """Configure INFO-level application logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


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
    return merged_dto


def _changed_fields(
    destination_dto: Mapping[str, Any],
    merged_dto: Mapping[str, Any],
) -> tuple[tuple[str, Any, Any], ...]:
    """Return (field, old_value, new_value) for each field that will change."""
    return tuple(
        (field, destination_dto.get(field), merged_dto.get(field))
        for field in TRANSFERABLE_METADATA_FIELDS
        if destination_dto.get(field) != merged_dto.get(field)
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
        LOGGER.error("%s", error)
        return 2

    try:
        with JellyfinClient(from_server) as from_client, JellyfinClient(to_server) as to_client:
            source_dto = from_client.get_item(from_item_id)
            destination_dto = to_client.get_item(to_item_id)
            merged_dto = build_merged_item_dto(source_dto, destination_dto)
            missing_required_fields = tuple(
                field for field in REQUIRED_NON_EMPTY_FIELDS if not merged_dto.get(field)
            )
            if missing_required_fields:
                LOGGER.error(
                    "Refusing to update %s: the destination item is missing required "
                    "field(s) %s. Sending this update would clear them on the server "
                    "instead of leaving them alone. This usually means Jellyfin's "
                    "response for this item didn't include those fields.",
                    to_item_id,
                    ", ".join(missing_required_fields),
                )
                return 1

            changes = _changed_fields(destination_dto, merged_dto)
            skipped_fields = _skipped_null_source_fields(source_dto, destination_dto)

            print(f"Transfer metadata: {from_server.name} -> {to_server.name}")
            print(f"  Source item:      {source_dto.get('Name', from_item_id)!r} ({from_item_id})")
            print(f"  Destination item: {destination_dto.get('Name', to_item_id)!r} ({to_item_id})")

            if not changes:
                print("No transferable fields differ. Nothing to do.")
                if skipped_fields:
                    print(
                        "  Note: the source server has no value for these fields, so "
                        f"the destination's existing value was kept: {', '.join(skipped_fields)}"
                    )
                return 0

            print("  Fields that will change:")
            for field, old_value, new_value in changes:
                print(f"    {field}: {old_value!r} -> {new_value!r}")
            if skipped_fields:
                print(
                    "  Note: the source server has no value for these fields, so "
                    f"the destination's existing value was kept: {', '.join(skipped_fields)}"
                )

            if not assume_yes:
                response = input("Proceed with metadata transfer? [y/N] ").strip().lower()
                if response not in {"y", "yes"}:
                    print("Aborted.")
                    return 1

            to_client.update_item(to_item_id, merged_dto)
            print("Metadata transfer complete.")
    except JellyfinError as error:
        LOGGER.error("%s", error)
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
