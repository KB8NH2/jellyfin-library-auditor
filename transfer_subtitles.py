#!/usr/bin/python3
"""Transfer one media item's English subtitle track from one server to another.

This module is intended to be called from the bulk ``--transfer-subtitles``
comparison flow in ``auditor.py``, but it can also be run directly for one
item pair - useful to isolate whether a bulk run's target resolution picked
the right destination item, independent of the transfer mechanism itself. It
does not contain audit logic, report formatting, or any assumptions beyond
the two items it is pointed at.

Subtitles are read from and written to the Jellyfin API's video-streaming and
subtitle-upload endpoints, never the filesystem directly - so this works
regardless of whether the source's subtitle file lives next to the media
file or in Jellyfin's own internal metadata cache
(``/var/lib/jellyfin/metadata/library/...``), which a plain rsync of the
media directories would miss entirely.
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
from media import configured_english_language_codes


LOGGER = logging.getLogger("transfer_subtitles")

# Mirrors metadata_transfer.log / image_transfer.log's role for their own
# scripts: a persistent, append-only record of every subtitle transfer
# attempt, so an unattended run still leaves an audit trail even if nobody
# watched the console.
SUBTITLE_TRANSFER_LOG_FILE = Path("subtitle_transfer.log")

# Requested unconditionally regardless of the source track's own codec -
# Jellyfin transcodes text-based subtitle formats (ASS/SSA, VTT, etc.) to SRT
# on the fly when streaming, so one format request covers any text-based
# source track without needing to branch on its codec.
SUBTITLE_TRANSFER_FORMAT = "srt"


@dataclass(frozen=True, slots=True)
class SubtitleTransferPlan:
    """A computed, not-yet-applied subtitle transfer for one item pair.

    Separating planning (read the source subtitle, decide whether there's
    anything to send) from applying (the actual upload) lets callers preview
    or skip a transfer without ever touching the destination server.
    """

    from_item_id: str
    to_item_id: str
    language: str
    subtitle_format: str
    is_forced: bool
    is_hearing_impaired: bool
    track_description: str
    subtitle_bytes: bytes | None

    @property
    def has_subtitle(self) -> bool:
        """Return whether the source has subtitle data to send."""
        return self.subtitle_bytes is not None


def _find_source_subtitle_stream(source_dto: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the first English, text-based subtitle stream on an item, if any.

    Matches the same criterion :func:`media.has_english_subtitles` uses to
    flag a subtitle difference in the comparison report, so a bulk
    ``--transfer-subtitles`` run only ever acts on items that report already
    flags. Streams that aren't text-based (e.g. PGS/VobSub bitmap subtitles)
    are skipped - Jellyfin can't convert those to SRT for the streaming
    endpoint this module downloads from.

    Args:
        source_dto: Full item document read from the source server.

    Returns:
        The raw Jellyfin media stream object, or ``None`` when no matching
        track exists.
    """
    english_codes = configured_english_language_codes()
    media_streams = source_dto.get("MediaStreams")
    if not isinstance(media_streams, list):
        return None

    for stream in media_streams:
        if not isinstance(stream, Mapping):
            continue
        if str(stream.get("Type", "")).strip().lower() != "subtitle":
            continue
        language = str(stream.get("Language") or "").strip().lower()
        if language not in english_codes:
            continue
        if not stream.get("IsTextSubtitleStream", False):
            continue
        return dict(stream)

    return None


def _source_media_source_id(source_dto: Mapping[str, Any], from_item_id: str) -> str:
    """Return the media source id a subtitle stream belongs to.

    Falls back to the item id itself, which is what Jellyfin uses as the
    default media source id for ordinary, single-version local files - the
    only case this application's libraries need to support.
    """
    media_sources = source_dto.get("MediaSources")
    if isinstance(media_sources, list) and media_sources:
        first_source = media_sources[0]
        if isinstance(first_source, Mapping):
            source_id = first_source.get("Id")
            if source_id:
                return str(source_id)
    return from_item_id


def plan_subtitle_transfer(
    from_client: JellyfinClient,
    to_client: JellyfinClient,
    from_item_id: str,
    to_item_id: str,
) -> SubtitleTransferPlan:
    """Fetch the source item's English subtitle track, without writing anything.

    Args:
        from_client: Client for the source server.
        to_client: Client for the destination server (unused for now, but
            accepted for symmetry with
            :func:`transfer_images.plan_image_transfer` and so a future
            destination-aware check doesn't change callers).
        from_item_id: Jellyfin item identifier on the source server.
        to_item_id: Jellyfin item identifier on the destination server.

    Returns:
        A plan describing the subtitle that would be sent, if any.
    """
    del to_client
    source_dto = from_client.get_item(from_item_id)
    stream = _find_source_subtitle_stream(source_dto)
    if stream is None:
        return SubtitleTransferPlan(
            from_item_id=from_item_id,
            to_item_id=to_item_id,
            language="",
            subtitle_format=SUBTITLE_TRANSFER_FORMAT,
            is_forced=False,
            is_hearing_impaired=False,
            track_description="no matching English text subtitle track on the source",
            subtitle_bytes=None,
        )

    language = str(stream.get("Language") or "eng").strip().lower() or "eng"
    track_description = str(
        stream.get("DisplayTitle") or stream.get("Title") or f"{language} subtitle"
    )
    is_forced = bool(stream.get("IsForced", False))
    is_hearing_impaired = bool(stream.get("IsHearingImpaired", False))

    index = stream.get("Index")
    if index is None:
        return SubtitleTransferPlan(
            from_item_id=from_item_id,
            to_item_id=to_item_id,
            language=language,
            subtitle_format=SUBTITLE_TRANSFER_FORMAT,
            is_forced=is_forced,
            is_hearing_impaired=is_hearing_impaired,
            track_description=f"{track_description} (missing stream index)",
            subtitle_bytes=None,
        )

    media_source_id = _source_media_source_id(source_dto, from_item_id)
    subtitle_bytes = from_client.get_item_subtitle(
        from_item_id, media_source_id, int(index), SUBTITLE_TRANSFER_FORMAT
    )

    return SubtitleTransferPlan(
        from_item_id=from_item_id,
        to_item_id=to_item_id,
        language=language,
        subtitle_format=SUBTITLE_TRANSFER_FORMAT,
        is_forced=is_forced,
        is_hearing_impaired=is_hearing_impaired,
        track_description=track_description,
        subtitle_bytes=subtitle_bytes,
    )


def apply_subtitle_transfer(to_client: JellyfinClient, plan: SubtitleTransferPlan) -> None:
    """Write a previously computed plan's subtitle to the destination server.

    Args:
        to_client: Client for the destination server.
        plan: A plan from :func:`plan_subtitle_transfer` with ``has_subtitle`` true.
    """
    assert plan.subtitle_bytes is not None
    to_client.upload_item_subtitle(
        plan.to_item_id,
        language=plan.language,
        subtitle_format=plan.subtitle_format,
        is_forced=plan.is_forced,
        is_hearing_impaired=plan.is_hearing_impaired,
        subtitle_bytes=plan.subtitle_bytes,
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
    with SUBTITLE_TRANSFER_LOG_FILE.open("a", encoding="utf-8") as log_file:
        log_file.write(f"{timestamp} {level} transfer_subtitles: {message}\n")


def transfer_subtitle(
    *,
    from_server_key: str,
    from_item_id: str,
    to_server_key: str,
    to_item_id: str,
    assume_yes: bool,
) -> int:
    """Transfer one item's English subtitle track from one Jellyfin server to another.

    Args:
        from_server_key: Configured source server key from servers.toml.
        from_item_id: Jellyfin item identifier on the source server.
        to_server_key: Configured destination server key from servers.toml.
        to_item_id: Jellyfin item identifier on the destination server.
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

            _log_line(f"Transfer subtitle: {from_server.name} -> {to_server.name}")
            _log_line(f"  Source item:      {source_item.get('Name', from_item_id)!r} ({from_item_id})")
            _log_line(f"  Destination item: {destination_item.get('Name', to_item_id)!r} ({to_item_id})")

            plan = plan_subtitle_transfer(from_client, to_client, from_item_id, to_item_id)
            if not plan.has_subtitle:
                _log_line(f"Nothing to transfer: {plan.track_description}.")
                return 0

            _log_line(
                f"  Source subtitle track: {plan.track_description!r} "
                f"({len(plan.subtitle_bytes)} bytes as .{plan.subtitle_format})"
            )

            if not assume_yes:
                response = input("Proceed with subtitle transfer? [y/N] ").strip().lower()
                if response not in {"y", "yes"}:
                    _log_line("Aborted.")
                    return 1

            apply_subtitle_transfer(to_client, plan)

            updated_item = to_client.get_item(to_item_id)
            updated_streams = updated_item.get("MediaStreams") or []
            subtitle_count = sum(
                1
                for stream in updated_streams
                if isinstance(stream, Mapping) and str(stream.get("Type", "")).lower() == "subtitle"
            )
            _log_line(
                f"Upload complete. Destination now has {subtitle_count} subtitle track(s)."
            )
    except JellyfinError as error:
        _log_line(str(error), error=True)
        return 1

    return 0


def _build_argument_parser() -> argparse.ArgumentParser:
    """Return the command-line parser for the transfer_subtitles entrypoint."""
    parser = argparse.ArgumentParser(
        prog="transfer_subtitles",
        description="Copy one item's English subtitle track from one Jellyfin server to another.",
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
    """Run the subtitle transfer workflow and return an exit code."""
    configure_logging()
    parser = _build_argument_parser()
    _log_line(f"Command: {parser.prog} {shlex.join(argv if argv is not None else sys.argv[1:])}")

    try:
        args = parser.parse_args(argv)
    except argparse.ArgumentError as error:
        LOGGER.error("%s", error)
        return 2

    return transfer_subtitle(
        from_server_key=args.from_server,
        from_item_id=args.from_item,
        to_server_key=args.to_server,
        to_item_id=args.to_item,
        assume_yes=args.yes,
    )


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "SUBTITLE_TRANSFER_FORMAT",
    "SubtitleTransferPlan",
    "apply_subtitle_transfer",
    "main",
    "plan_subtitle_transfer",
    "transfer_subtitle",
]
