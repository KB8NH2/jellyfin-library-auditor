"""Audit logic for normalized media items.

This module evaluates :class:`models.MediaItem` objects and returns structured
findings. It operates only on application models and helper functions from
``media.py``.
"""

from __future__ import annotations

from collections.abc import Iterable
from collections.abc import Mapping
import re

from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from media import expected_episode_title_from_filename
from media import expected_episode_title_from_stream_titles
from media import expected_movie_title_from_filename
from media import get_primary_audio_codec
from media import get_video_codec
from media import has_english_subtitles
from media import has_jellyfin_primary_image
from media import local_backdrop_exists
from models import MediaItem
from tvdb import TvdbEpisode


_EPISODE_TITLE_PUNCTUATION_PATTERN = re.compile(
    r"""[,:;!\?'"‘’“”·\-‐‑‒–—*<>|]"""
)
_ROMAN_NUMERAL_PAREN_PATTERN = re.compile(r"\(([IVXLCDMivxlcdm]+)\)")
_ROMAN_NUMERAL_VALUES = {"I": 1, "V": 5, "X": 10, "L": 50, "C": 100, "D": 500, "M": 1000}


def audit_media_item(item: MediaItem) -> tuple[AuditFinding, ...]:
    """Run all media item audits and collect findings.

    Args:
        item: Media item to evaluate.

    Returns:
        A tuple containing every finding produced for the media item.
    """
    audits = (
        missing_english_subtitles,
        missing_backdrop,
        missing_primary_image,
        missing_episode_number,
        unknown_video_codec,
        unknown_audio_codec,
        mismatched_episode_filename_title,
        mismatched_episode_stream_title,
        mismatched_movie_filename_title,
    )
    findings: list[AuditFinding] = []

    for audit in audits:
        finding = audit(item)
        if finding is not None:
            findings.append(finding)

    return tuple(findings)


def audit_library_items(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Run library-level audits that require multiple media items.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number), as fetched for
            :func:`audit_episode_ordering`. When given, missing-season and
            missing-episode detection are checked against each series' full
            TheTVDB season/episode list instead of only gaps between
            locally-present numbers.

    Returns:
        A tuple containing findings derived from gaps across TV episodes.
    """
    items_tuple = tuple(items)
    findings: list[AuditFinding] = []
    findings.extend(missing_tv_series_seasons(items_tuple, aired_positions))
    findings.extend(missing_tv_season_episodes(items_tuple, aired_positions))
    return tuple(findings)


def missing_english_subtitles(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no configured English subtitles exist.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when English subtitles are present.
    """
    if has_english_subtitles(item):
        return None

    return _finding(
        item,
        category=AuditCategory.SUBTITLES,
        severity=AuditSeverity.WARNING,
        check_name="missing_english_subtitles",
        message="No configured English subtitles were found.",
    )


def missing_backdrop(item: MediaItem) -> AuditFinding | None:
    """Return a finding when no local backdrop exists.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when a local backdrop exists.
    """
    if local_backdrop_exists(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_backdrop",
        message="No local backdrop file was found.",
    )


def missing_primary_image(item: MediaItem) -> AuditFinding | None:
    """Return a finding when Jellyfin has no primary image for the item.

    Args:
        item: Media item to evaluate.

    Returns:
        An informational finding, or ``None`` when Jellyfin reports a primary
        image.
    """
    if has_jellyfin_primary_image(item):
        return None

    return _finding(
        item,
        category=AuditCategory.ARTWORK,
        severity=AuditSeverity.INFO,
        check_name="missing_primary_image",
        message="No Jellyfin primary image was found.",
    )


def missing_episode_number(item: MediaItem) -> AuditFinding | None:
    """Return a finding when an episode has no episode number set.

    Unlike missing_tv_season_episodes, which flags numeric gaps between
    episodes that already have numbers, this catches an episode file Jellyfin
    could not assign a number to at all (episode_number is None) - the kind
    of gap apply_episode_numbers.py can fill in from TheTVDB's aired order.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the item is not an episode or
        already has an episode number.
    """
    if not item.is_episode or item.episode_number is not None:
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="missing_episode_number",
        message="No episode number is set.",
    )


def unknown_video_codec(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the primary video codec is missing or unknown.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the video codec is known.
    """
    codec = get_video_codec(item)
    if codec not in {None, "unknown"}:
        return None

    return _finding(
        item,
        category=AuditCategory.VIDEO,
        severity=AuditSeverity.WARNING,
        check_name="unknown_video_codec",
        message="The primary video codec is missing or unknown.",
    )


def unknown_audio_codec(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the primary audio codec is missing.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when an audio codec exists.
    """
    if get_primary_audio_codec(item) is not None:
        return None

    return _finding(
        item,
        category=AuditCategory.AUDIO,
        severity=AuditSeverity.WARNING,
        check_name="unknown_audio_codec",
        message="No primary audio codec was found.",
    )


def mismatched_episode_filename_title(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the filename implies a different episode title.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the filename has no discernible
        episode title, or its implied title matches the metadata title.
    """
    expected_title = expected_episode_title_from_filename(item)
    if expected_title is None:
        return None

    if normalized_title(expected_title) == normalized_title(item.title):
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="mismatched_episode_filename_title",
        message=(
            f'Filename suggests episode title "{expected_title}" but metadata '
            f'title is "{item.title}".'
        ),
    )


def mismatched_episode_stream_title(item: MediaItem) -> AuditFinding | None:
    """Return a finding when an embedded stream title implies a different title.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when no video/audio track has a
        discernible episode title, or its implied title matches the metadata
        title.
    """
    expected_title = expected_episode_title_from_stream_titles(item)
    if expected_title is None:
        return None

    if normalized_title(expected_title) == normalized_title(item.title):
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="mismatched_episode_stream_title",
        message=(
            f'An embedded stream title suggests episode title "{expected_title}" but '
            f'metadata title is "{item.title}".'
        ),
    )


def mismatched_movie_filename_title(item: MediaItem) -> AuditFinding | None:
    """Return a finding when the filename implies a different movie title.

    Args:
        item: Media item to evaluate.

    Returns:
        A warning finding, or ``None`` when the filename has no discernible
        movie title, or its implied title matches the metadata title.
    """
    expected_title = expected_movie_title_from_filename(item)
    if expected_title is None:
        return None

    if normalized_title(expected_title) == normalized_title(item.title):
        return None

    return _finding(
        item,
        category=AuditCategory.METADATA,
        severity=AuditSeverity.WARNING,
        check_name="mismatched_movie_filename_title",
        message=(
            f'Filename suggests movie title "{expected_title}" but metadata '
            f'title is "{item.title}".'
        ),
    )


def normalized_title(value: str) -> str:
    """Return a normalized title for filename/metadata comparison.

    Periods are treated as word separators (like filename extraction does for
    dot-delimited release names) rather than deleted outright, so abbreviated
    titles such as "S.W.A.T." compare equal to their filename counterpart
    instead of collapsing into a run-together "swat". Parenthesized roman
    numerals (e.g. "(I)") are converted to their arabic-numeral equivalent
    (e.g. "(1)") since Jellyfin metadata and filenames disagree on which form
    to use for disambiguating same-titled entries. "&" is treated the same as
    "and", and "+" the same as "/", since Jellyfin sometimes converts between
    these when deriving filenames from metadata. The single-character
    ellipsis ("…") is treated the same as three literal periods ("..."),
    since Jellyfin metadata and filenames disagree on which form to use.
    """
    normalized_value = _ROMAN_NUMERAL_PAREN_PATTERN.sub(_roman_numeral_paren_to_arabic, value)
    normalized_value = normalized_value.replace("…", "...")
    normalized_value = normalized_value.replace("&", " and ")
    normalized_value = normalized_value.replace("+", "/")
    normalized_value = normalized_value.replace(".", " ")
    normalized_value = _EPISODE_TITLE_PUNCTUATION_PATTERN.sub("", normalized_value)
    normalized_value = re.sub(r"\s+", " ", normalized_value)
    return normalized_value.strip().casefold()


def _roman_numeral_paren_to_arabic(match: re.Match[str]) -> str:
    """Return an arabic-numeral parenthetical for a matched roman numeral."""
    numeral_value = _roman_numeral_to_int(match.group(1))
    if numeral_value is None:
        return match.group(0)
    return f"({numeral_value})"


def _roman_numeral_to_int(numeral: str) -> int | None:
    """Return the integer value of a roman numeral, or ``None`` when invalid."""
    total = 0
    previous_value = 0
    for character in reversed(numeral.upper()):
        value = _ROMAN_NUMERAL_VALUES.get(character)
        if value is None:
            return None
        if value < previous_value:
            total -= value
        else:
            total += value
            previous_value = value
    return total or None


def missing_tv_series_seasons(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Return findings for series with missing numbered seasons.

    Without TheTVDB data, a series' missing seasons can only be inferred from
    gaps between the lowest and highest season numbers present locally -
    there's no way to tell whether seasons are missing after the last one on
    disk. When ``aired_positions`` has an entry for a series, the set of
    season numbers found there is used instead, so seasons missing after the
    last local one (e.g. only seasons 1-2 exist locally but TheTVDB lists
    1-4) are caught too, not just internal gaps.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number), as fetched for
            :func:`audit_episode_ordering`. When omitted, or when a series
            has no matching TVDB data, only internal gaps between
            locally-present season numbers are reported.

    Returns:
        One finding per TV series with missing numbered seasons.
    """
    series_items: dict[str, list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number <= 0:
            continue
        series_items.setdefault(item.series_name, []).append(item)

    findings: list[AuditFinding] = []
    for series_name, grouped_items in sorted(series_items.items(), key=lambda entry: entry[0].casefold()):
        season_numbers = {item.season_number for item in grouped_items if item.season_number is not None}
        tvdb_season_numbers = _tvdb_series_season_numbers(aired_positions, series_name)
        missing_numbers = _missing_numbers(season_numbers, tvdb_season_numbers)
        if not missing_numbers:
            continue
        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="missing_seasons",
                message=f"Missing seasons: {_format_missing_numbers(missing_numbers)}.",
            )
        )
    return tuple(findings)


def missing_tv_season_episodes(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]] | None = None,
) -> tuple[AuditFinding, ...]:
    """Return findings for seasons with missing numbered episodes.

    Without TheTVDB data, a season's missing episodes can only be inferred
    from gaps between the lowest and highest episode numbers present locally
    - there's no way to tell whether episodes are missing after the last one
    on disk. When ``aired_positions`` has an entry for a series' season, its
    full TheTVDB episode list is used instead, so episodes missing after the
    last local one (e.g. only 1-8 exist locally but TheTVDB lists 1-10) are
    caught too, not just internal gaps.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number), as fetched for
            :func:`audit_episode_ordering`. When omitted, or when a series
            or season has no matching TVDB data, only internal gaps between
            locally-present episode numbers are reported.

    Returns:
        One finding per TV season with missing numbered episodes.
    """
    season_items: dict[tuple[str, int], list[MediaItem]] = {}
    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.season_number <= 0:
            continue
        if item.episode_number is None or item.episode_number <= 0:
            continue
        season_items.setdefault((item.series_name, item.season_number), []).append(item)

    findings: list[AuditFinding] = []
    for (series_name, season_number), grouped_items in sorted(
        season_items.items(),
        key=lambda entry: (entry[0][0].casefold(), entry[0][1]),
    ):
        episode_numbers = {
            item.episode_number for item in grouped_items if item.episode_number is not None
        }
        tvdb_episode_numbers = _tvdb_season_episode_numbers(aired_positions, series_name, season_number)
        missing_numbers = _missing_numbers(episode_numbers, tvdb_episode_numbers)
        if not missing_numbers:
            continue
        representative = min(grouped_items, key=_episode_sort_key)
        findings.append(
            _finding(
                representative,
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="missing_episodes",
                message=f"Missing episodes: {_format_missing_numbers(missing_numbers)}.",
            )
        )
    return tuple(findings)


def _tvdb_season_episode_numbers(
    aired_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]] | None,
    series_name: str,
    season_number: int,
) -> frozenset[int] | None:
    """Return TheTVDB's known episode numbers for one series' season, if any."""
    if not aired_positions:
        return None
    series_positions = aired_positions.get(series_name)
    if not series_positions:
        return None
    season_episode_numbers = frozenset(
        episode_number
        for position_season, episode_number in series_positions
        if position_season == season_number
    )
    return season_episode_numbers or None


def _tvdb_series_season_numbers(
    aired_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]] | None,
    series_name: str,
) -> frozenset[int] | None:
    """Return TheTVDB's known season numbers for one series, if any."""
    if not aired_positions:
        return None
    series_positions = aired_positions.get(series_name)
    if not series_positions:
        return None
    series_season_numbers = frozenset(position_season for position_season, _ in series_positions)
    return series_season_numbers or None


def _missing_numbers(
    local_numbers: Iterable[int],
    tvdb_numbers: frozenset[int] | None,
) -> tuple[int, ...]:
    """Return missing numbers (season or episode) for one series or season.

    Without TheTVDB data (``tvdb_numbers`` is ``None``), only gaps between
    the lowest and highest locally-present numbers are reported. With
    TheTVDB data, every TVDB-listed number absent locally is reported,
    including ones after the last local number.
    """
    if tvdb_numbers is None:
        return _missing_sequence_numbers(local_numbers)
    return tuple(sorted(tvdb_numbers - set(local_numbers)))


def audit_episode_ordering(
    items: Iterable[MediaItem],
    aired_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]],
    dvd_positions: Mapping[str, Mapping[tuple[int, int], TvdbEpisode]],
) -> tuple[AuditFinding, ...]:
    """Return findings for episodes whose aired/DVD order titles disagree.

    Some series are organized on disk in TheTVDB's DVD order while Jellyfin
    labels files using aired order (or vice versa), so the filename, season
    number, and episode number all line up with Jellyfin's own metadata even
    though the video content is a different episode. Comparing Jellyfin's
    metadata against a single online ordering can't catch that; this compares
    each episode's (season, episode) position against both of TheTVDB's
    orderings and flags positions where they disagree, so the user knows
    exactly which episodes are worth checking by eye.

    Args:
        items: Media items from one audited library.
        aired_positions: TheTVDB aired-order episodes for each series name,
            keyed by (season_number, episode_number).
        dvd_positions: TheTVDB DVD-order episodes for each series name, keyed
            by (season_number, episode_number).

    Returns:
        One finding per episode whose aired-order and DVD-order titles differ
        at its (season, episode) position.
    """
    findings: list[AuditFinding] = []

    for item in items:
        if not item.is_episode or not item.series_name:
            continue
        if item.season_number is None or item.episode_number is None:
            continue

        position = (item.season_number, item.episode_number)
        aired_episode = aired_positions.get(item.series_name, {}).get(position)
        dvd_episode = dvd_positions.get(item.series_name, {}).get(position)
        if aired_episode is None or dvd_episode is None:
            continue

        if normalized_title(aired_episode.name) == normalized_title(dvd_episode.name):
            continue

        findings.append(
            _finding(
                item,
                category=AuditCategory.EPISODE_ORDER,
                severity=AuditSeverity.WARNING,
                check_name="aired_dvd_order_mismatch",
                message=(
                    f'TheTVDB aired order lists S{item.season_number:02d}E{item.episode_number:02d} '
                    f'as "{aired_episode.name}", but DVD order lists a different episode at that '
                    f'position: "{dvd_episode.name}". Verify the video content matches the aired-order '
                    f"title before trusting Jellyfin's metadata."
                ),
            )
        )

    return tuple(findings)


def _missing_sequence_numbers(numbers: Iterable[int]) -> tuple[int, ...]:
    """Return missing integers between the smallest and largest values."""
    sorted_numbers = sorted(set(numbers))
    if len(sorted_numbers) < 2:
        return ()

    missing_numbers: list[int] = []
    for previous, current in zip(sorted_numbers, sorted_numbers[1:]):
        if current - previous <= 1:
            continue
        missing_numbers.extend(range(previous + 1, current))
    return tuple(missing_numbers)


def _format_missing_numbers(numbers: Iterable[int]) -> str:
    """Return a compact string for missing number sequences."""
    sorted_numbers = sorted(set(numbers))
    if not sorted_numbers:
        return ""

    ranges: list[str] = []
    range_start = sorted_numbers[0]
    range_end = sorted_numbers[0]

    for number in sorted_numbers[1:]:
        if number == range_end + 1:
            range_end = number
            continue
        ranges.append(_format_number_range(range_start, range_end))
        range_start = number
        range_end = number

    ranges.append(_format_number_range(range_start, range_end))
    return ", ".join(ranges)


def _format_number_range(start: int, end: int) -> str:
    """Return one display range for missing season or episode numbers."""
    if start == end:
        return str(start)
    return f"{start}-{end}"


def _episode_sort_key(item: MediaItem) -> tuple[str, int, int, str]:
    """Return a stable sort key for episode representative selection."""
    return (
        (item.series_name or "").casefold(),
        item.season_number if item.season_number is not None else -1,
        item.episode_number if item.episode_number is not None else -1,
        item.title.casefold(),
    )


def _finding(
    item: MediaItem,
    *,
    category: AuditCategory,
    severity: AuditSeverity,
    check_name: str,
    message: str,
) -> AuditFinding:
    """Build an audit finding for a media item.

    Args:
        item: Media item associated with the finding.
        category: Finding category.
        severity: Finding severity.
        check_name: Stable audit check name.
        message: Human-readable description.

    Returns:
        A structured audit finding.
    """
    return AuditFinding(
        category=category,
        severity=severity,
        check_name=check_name,
        message=message,
        media_item=item,
    )


__all__ = [
    "AuditCategory",
    "AuditFinding",
    "AuditSeverity",
    "audit_episode_ordering",
    "audit_library_items",
    "audit_media_item",
    "mismatched_episode_filename_title",
    "mismatched_episode_stream_title",
    "mismatched_movie_filename_title",
    "missing_backdrop",
    "missing_english_subtitles",
    "missing_episode_number",
    "missing_tv_season_episodes",
    "missing_tv_series_seasons",
    "missing_primary_image",
    "normalized_title",
    "unknown_audio_codec",
    "unknown_video_codec",
]
