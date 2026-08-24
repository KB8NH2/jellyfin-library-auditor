#!/usr/bin/python3
"""Transfer one media item's cached Jellyfin image from one server to another.

This module is intended to be called from the bulk ``--transfer-images``
comparison flow in ``auditor.py``, but it can also be run directly for one
item and image type - useful to isolate whether a bulk run's target
resolution picked the right destination item, independent of the transfer
mechanism itself. It does not contain audit logic, report formatting, or any
assumptions beyond the two items and image type it is pointed at.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass
from datetime import datetime
import logging
from collections.abc import Sequence
from pathlib import Path

from config import ConfigError
from config import get_config
from jellyfin import JellyfinClient
from jellyfin import JellyfinError


LOGGER = logging.getLogger("transfer_images")

# Mirrors metadata_transfer.log's role for transfer_metadata.py: a persistent,
# append-only record of every image transfer attempt, so an unattended run
# still leaves an audit trail even if nobody watched the console.
IMAGE_TRANSFER_LOG_FILE = Path("image_transfer.log")

IMAGE_TYPES = ("Primary", "Backdrop", "Thumb")


@dataclass(frozen=True, slots=True)
class ImageTransferPlan:
    """A computed, not-yet-applied image transfer for one item pair and image type.

    Separating planning (read the source image, decide whether there's
    anything to send) from applying (the actual upload) lets callers preview
    or skip a transfer without ever touching the destination server.
    """

    from_item_id: str
    to_item_id: str
    image_type: str
    image_bytes: bytes | None
    content_type: str | None

    @property
    def has_image(self) -> bool:
        """Return whether the source has an image of this type to send."""
        return self.image_bytes is not None


def plan_image_transfer(
    from_client: JellyfinClient,
    to_client: JellyfinClient,
    from_item_id: str,
    to_item_id: str,
    image_type: str,
) -> ImageTransferPlan:
    """Fetch the source item's image, without writing anything.

    Args:
        from_client: Client for the source server.
        to_client: Client for the destination server (unused for now, but
            accepted for symmetry with :func:`transfer_metadata.plan_transfer`
            and so a future destination-aware check doesn't change callers).
        from_item_id: Jellyfin item identifier on the source server.
        to_item_id: Jellyfin item identifier on the destination server.
        image_type: Jellyfin image type to transfer (e.g. ``"Primary"``).

    Returns:
        A plan describing the image that would be sent, if any.
    """
    del to_client
    image = from_client.get_item_image(from_item_id, image_type)
    image_bytes, content_type = image if image is not None else (None, None)

    return ImageTransferPlan(
        from_item_id=from_item_id,
        to_item_id=to_item_id,
        image_type=image_type,
        image_bytes=image_bytes,
        content_type=content_type,
    )


def apply_image_transfer(to_client: JellyfinClient, plan: ImageTransferPlan) -> None:
    """Write a previously computed plan's image to the destination server.

    Args:
        to_client: Client for the destination server.
        plan: A plan from :func:`plan_image_transfer` with ``has_image`` true.
    """
    assert plan.image_bytes is not None and plan.content_type is not None
    to_client.upload_item_image(
        plan.to_item_id, plan.image_type, plan.image_bytes, plan.content_type
    )


def configure_logging() -> None:
    """Configure INFO-level application logging with timestamps."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


def _log_line(message: str, *, error: bool = False) -> None:
    """Emit one line of transfer output to the console and to the log file."""
    if error:
        LOGGER.error(message)
    else:
        print(message)

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    level = "ERROR" if error else "INFO"
    with IMAGE_TRANSFER_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} transfer_images: {message}\n")


def transfer_image(
    *,
    from_server_key: str,
    from_item_id: str,
    to_server_key: str,
    to_item_id: str,
    image_type: str,
    assume_yes: bool,
) -> int:
    """Transfer one item's cached image from one Jellyfin server to another.

    Prints the destination item's name before writing anything, and
    re-reads its ``ImageTags`` immediately after the upload, so a mismatched
    or silently-ignored write is visible in the output rather than assumed
    to have worked just because the HTTP request succeeded.

    Args:
        from_server_key: Configured source server key from servers.toml.
        from_item_id: Jellyfin item identifier on the source server.
        to_server_key: Configured destination server key from servers.toml.
        to_item_id: Jellyfin item identifier on the destination server.
        image_type: Jellyfin image type to transfer (e.g. ``"Primary"``).
        assume_yes: Skip the interactive confirmation prompt when ``True``.

    Returns:
        A process exit code: ``0`` on success or when there was nothing to
        transfer, ``1`` on failure or when the user declines confirmation,
        ``2`` on a usage/configuration error.
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
            source_item = from_client.get_item(from_item_id)
            destination_item = to_client.get_item(to_item_id)

            _log_line(f"Transfer {image_type} image: {from_server.name} -> {to_server.name}")
            _log_line(f"  Source item:      {source_item.get('Name', from_item_id)!r} ({from_item_id})")
            _log_line(f"  Destination item: {destination_item.get('Name', to_item_id)!r} ({to_item_id})")

            plan = plan_image_transfer(from_client, to_client, from_item_id, to_item_id, image_type)
            if not plan.has_image:
                _log_line(f"Source has no {image_type} image. Nothing to do.")
                return 0

            _log_line(
                f"  Source {image_type} image: {len(plan.image_bytes)} bytes, {plan.content_type}"
            )

            if not assume_yes:
                response = input(f"Proceed with {image_type} image transfer? [y/N] ").strip().lower()
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            apply_image_transfer(to_client, plan)

            updated_item = to_client.get_item(to_item_id)
            new_tag = updated_item.get("ImageTags", {}).get(image_type)
            _log_line(
                f"Upload complete. Destination ImageTags[{image_type!r}] is now: {new_tag!r}"
            )
    except JellyfinError as error:
        _log_line(str(error), error=True)
        return 1

    return 0


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the transfer_images entrypoint."""
    parser = argparse.ArgumentParser(
        prog="transfer_images",
        description="Copy one item's cached Jellyfin image from one server to another.",
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
        "--image-type",
        default="Primary",
        choices=IMAGE_TYPES,
        help="Jellyfin image type to transfer. Defaults to Primary.",
    )
    parser.add_argument(
        "--yes",
        action="store_true",
        help="Skip the confirmation prompt and transfer immediately.",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """Run the image transfer workflow and return an exit code."""
    configure_logging()
    parser = _build_argument_parser()

    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        LOGGER.error("%s", error)
        return 2

    return transfer_image(
        from_server_key=args.from_server,
        from_item_id=args.from_item,
        to_server_key=args.to_server,
        to_item_id=args.to_item,
        image_type=args.image_type,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "IMAGE_TYPES",
    "ImageTransferPlan",
    "apply_image_transfer",
    "main",
    "plan_image_transfer",
    "transfer_image",
]
