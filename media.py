"""Media and filesystem helper functions for normalized media items.

This module works only with application models and local filesystem state. It
does not talk to the Jellyfin API, generate reports, or apply audit-specific
rules.
"""

from __future__ import annotations

from pathlib import Path
import re

from config import get_config
from models import AudioTrack
from models import MediaItem
from models import SubtitleTrack
from models import VideoTrack


BACKDROP_FILENAMES = (
    "backdrop.jpg",
    "backdrop.png",
    "fanart.jpg",
    "fanart.png",
)
GENERIC_NFO_FILENAMES = (
    "movie.nfo",
    "tvshow.nfo",
    "season.nfo",
)
RELEASE_TAG_TOKEN_PATTERN = re.compile(
    r"(?i)^(?:"
    r"2160p|1080p|720p|480p|4k|"
    r"hdtv|web[- ]?dl|webrip|web|bluray|brrip|bdrip|dvdrip|dvd|hdrip|ntsc|pal|"
    r"repack|proper|remux|extended|unrated|"
    r"x264|x265|h264|h265|hevc|av1|vp9|"
    r"aac|ac3|eac3|ddp\d*|dd\d*|truehd|atmos|dts(?:-?hd|-?ma)?|flac|"
    r"sdr|hdr10?"
    # Named rather than positional so a future alternative added above (like
    # "dts(?:-?hd|-?ma)?") can't silently shift this group's index and break
    # the "-GROUPNAME" suffix check below without any test catching it -
    # exactly what happened here once already.
    r")(?P<group_suffix>-\S+)?$"
)
# Matches the *shape* of a bare release-group name or stray channel-count
# digit (e.g. the "JCH" in "x264 JCH", or the "1" in "DDP5 1"): a single word
# with no attached punctuation, so it's never confused with e.g. the "(1)"
# copy marker. See _trailing_tag_run for how this is used - matching this
# shape alone is never enough on its own to strip a word.
_RELEASE_GROUP_NAME_PATTERN = re.compile(r"^[A-Za-z0-9]+$")
TITLE_STRIP_CHARACTERS = " -_.[]{}:"
PRIMARY_IMAGE_TAG = "Primary"
BACKDROP_IMAGE_TAG = "Backdrop"
THUMB_IMAGE_TAG = "Thumb"
JELLYFIN_IMAGE_TAGS = (
    BACKDROP_IMAGE_TAG,
    PRIMARY_IMAGE_TAG,
    THUMB_IMAGE_TAG,
)


def configured_english_language_codes() -> frozenset[str]:
    """Return the configured set of English language codes."""
    config = get_config()
    return frozenset(config.reporting.english_language_codes)


def _item_directory(item: MediaItem) -> Path:
    """Return the directory containing the media item."""
    return item.path.parent


def _existing_sibling_files(item: MediaItem, filenames: tuple[str, ...]) -> tuple[str, ...]:
    """Return every named sibling file that actually exists, in ``filenames`` order.

    Args:
        item: Media item whose directory should be searched.
        filenames: Candidate filenames to look for.

    Returns:
        The subset of ``filenames`` present in the item's directory.
    """
    item_directory = _item_directory(item)
    if not item_directory.is_dir():
        return ()

    return tuple(
        filename for filename in filenames if (item_directory / filename).is_file()
    )


def _sibling_file_exists(item: MediaItem, filenames: tuple[str, ...]) -> bool:
    """Return whether any of the named sibling files exists.

    Args:
        item: Media item whose directory should be searched.
        filenames: Candidate filenames to look for.

    Returns:
        ``True`` when any matching file exists.
    """
    return bool(_existing_sibling_files(item, filenames))


def _normalize_for_prefix_match(value: str) -> str:
    """Normalize a path string for prefix matching."""
    return value.replace("/", "\\").casefold()


def _strip_configured_prefix(path: Path, prefix: str) -> str:
    """Strip a configured display prefix from a path when it matches cleanly.

    Args:
        path: Full media path.
        prefix: Configured path prefix to remove.

    Returns:
        The display path with the configured prefix removed when present.
    """
    full_path = str(path)
    if not prefix:
        return full_path

    normalized_path = _normalize_for_prefix_match(full_path)
    normalized_prefix = _normalize_for_prefix_match(prefix).rstrip("\\")

    if normalized_path == normalized_prefix:
        return ""

    prefix_with_separator = f"{normalized_prefix}\\"
    if normalized_path.startswith(prefix_with_separator):
        stripped_path = full_path[len(prefix.rstrip("\\/")):]
        return stripped_path.lstrip("\\/")

    return full_path


def _track_language_matches(track: SubtitleTrack, language_codes: frozenset[str]) -> bool:
    """Return whether a subtitle track language is in the configured set."""
    return track.language in language_codes


def _has_jellyfin_image_tag(item: MediaItem, tag_name: str) -> bool:
    """Return whether Jellyfin reported a non-empty image tag for the item.

    Args:
        item: Media item to inspect.
        tag_name: Image tag name from Jellyfin.

    Returns:
        ``True`` when the image tag exists and is non-empty.
    """
    tag_value = item.image_tags.get(tag_name)
    if tag_value is None:
        return False

    return bool(tag_value.strip())


# Subtitle helpers


def has_english_subtitles(item: MediaItem) -> bool:
    """Return whether the item has any configured English subtitle tracks.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when at least one subtitle language matches the configured
        English language codes.
    """
    return bool(get_english_subtitle_tracks(item))


def has_external_subtitles(item: MediaItem) -> bool:
    """Return whether the item has any external subtitle tracks.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when at least one subtitle track is external.
    """
    return item.has_external_subtitles


def has_embedded_subtitles(item: MediaItem) -> bool:
    """Return whether the item has any embedded subtitle tracks.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when at least one subtitle track is not external.
    """
    return any(not track.is_external for track in item.subtitle_tracks)


def get_english_subtitle_tracks(item: MediaItem) -> tuple[SubtitleTrack, ...]:
    """Return subtitle tracks whose language matches configured English codes.

    Args:
        item: Media item to inspect.

    Returns:
        A tuple of matching subtitle tracks.
    """
    language_codes = configured_english_language_codes()
    return tuple(
        track
        for track in item.subtitle_tracks
        if _track_language_matches(track, language_codes)
    )


# Artwork helpers


def has_jellyfin_primary_image(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a primary image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Primary`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, PRIMARY_IMAGE_TAG)


def has_jellyfin_backdrop(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a backdrop image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Backdrop`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, BACKDROP_IMAGE_TAG)


def has_jellyfin_thumb(item: MediaItem) -> bool:
    """Return whether Jellyfin reports a thumb image tag for the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a non-empty ``Thumb`` image tag is present.
    """
    return _has_jellyfin_image_tag(item, THUMB_IMAGE_TAG)


def jellyfin_image_types(item: MediaItem) -> tuple[str, ...]:
    """Return the sorted Jellyfin image tag types reported for the item.

    Args:
        item: Media item to inspect.

    Returns:
        A tuple containing each known Jellyfin image tag type whose value exists
        and is non-empty, sorted alphabetically.
    """
    return tuple(
        sorted(
            tag_name
            for tag_name in JELLYFIN_IMAGE_TAGS
            if _has_jellyfin_image_tag(item, tag_name)
        )
    )


def local_backdrop_exists(item: MediaItem) -> bool:
    """Return whether a common local backdrop file exists beside the item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a common backdrop filename exists.
    """
    return _sibling_file_exists(item, BACKDROP_FILENAMES)


# Metadata helpers


def local_nfo_exists(item: MediaItem) -> bool:
    """Return whether a common local NFO file exists beside the media item.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when a known NFO filename exists in the media directory.
    """
    basename_nfo = f"{item.path.stem}.nfo"
    filenames = (*GENERIC_NFO_FILENAMES, basename_nfo)
    return _sibling_file_exists(item, filenames)


def expected_episode_title_from_filename(item: MediaItem) -> str | None:
    """Return the episode title implied by the filename's Jellyfin naming.

    Jellyfin's TV naming convention places the episode title after the
    ``SxxExx`` season/episode marker, e.g. ``Show S01E02 Episode Title.mkv``.
    Multi-episode files spanning a range, e.g. ``Show S01E02-E03 Title.mkv``,
    are also recognized so the trailing ``-Exx`` segment is not mistaken for
    part of the title. This locates the marker using the item's known season
    and starting episode numbers and returns the text that follows it, with
    common release tags (resolution, codec, source) and separators trimmed
    away.

    Args:
        item: Media item to inspect.

    Returns:
        The episode title segment implied by the filename, or ``None`` when
        the filename has no recognizable ``SxxExx`` marker or no title text
        follows it.
    """
    if not item.is_episode or item.season_number is None or item.episode_number is None:
        return None

    return _expected_episode_title_from_text(
        item.path.stem, item.season_number, item.episode_number
    )


def expected_episode_title_from_stream_titles(item: MediaItem) -> str | None:
    """Return the episode title implied by an embedded video/audio stream title.

    Tools like mkvmerge often set a stream's ``Title`` to the original
    scene-release filename at rip time, and that title survives even after
    the container file itself gets renamed to fit Jellyfin's naming
    convention. So a rip that was mislabeled at organize time can still carry
    its true episode identity here, in a place a filename-only check can't
    see - the (renamed) filename and Jellyfin's metadata can otherwise agree
    with each other while both being wrong. This checks the primary video
    track first, then each audio track in order, since either can carry the
    original title independently of the other.

    Args:
        item: Media item to inspect.

    Returns:
        The episode title segment implied by the first track whose title
        contains a marker matching the item's known season and starting
        episode numbers, or ``None`` when no track has one.
    """
    if not item.is_episode or item.season_number is None or item.episode_number is None:
        return None

    candidate_titles = [
        track.title for track in (item.video_track,) if track is not None
    ]
    candidate_titles.extend(track.title for track in item.audio_tracks)

    for candidate_title in candidate_titles:
        if not candidate_title:
            continue
        expected_title = _expected_episode_title_from_text(
            candidate_title, item.season_number, item.episode_number
        )
        if expected_title is not None:
            return expected_title

    return None


def _expected_episode_title_from_text(
    text: str,
    season_number: int,
    episode_number: int,
) -> str | None:
    """Return the episode title implied by a filename-style text fragment.

    Args:
        text: Filename-like text to search, e.g. a filename stem or an
            embedded stream title.
        season_number: The item's known season number.
        episode_number: The item's known starting episode number.

    Returns:
        The episode title segment implied by the text, or ``None`` when the
        text has no recognizable ``SxxExx`` marker or no title text follows
        it.
    """
    marker_pattern = re.compile(
        rf"(?i)s0*{season_number}e0*{episode_number}(?:-?e0*\d+)*(?!\d)"
    )
    marker_match = marker_pattern.search(text)
    if marker_match is None:
        return None

    remainder = text[marker_match.end() :].replace("_", " ").replace(".", " ")
    remainder = _strip_trailing_release_tags(remainder)

    remainder = remainder.strip(TITLE_STRIP_CHARACTERS)
    remainder = re.sub(r"\s+", " ", remainder).strip()
    return remainder or None


def _strip_wrapping_parens(word: str) -> str:
    """Return ``word`` with one layer of wrapping parentheses removed.

    Used only to decide whether a word is a release tag (e.g. a
    parenthesized tag group like "(1080p AV1)" splits into the words
    "(1080p" and "AV1)") - the original word, parentheses included, is what
    actually gets kept or dropped from the title.
    """
    core = word
    if core.startswith("("):
        core = core[1:]
    if core.endswith(")"):
        core = core[:-1]
    return core


def _is_separator_word(word: str) -> bool:
    """Return whether ``word`` is made entirely of separator punctuation."""
    return bool(word) and all(character in TITLE_STRIP_CHARACTERS for character in word)


def _trailing_tag_run(
    words: list[str],
    *,
    wildcard_budget: int = 1,
) -> tuple[int, int, bool]:
    """Scan ``words`` from the end for a run of recognized release tags.

    A separator-only word (e.g. a lone "-" left over from a dash-delimited
    filename) and a layer of wrapping parentheses around a tag word (e.g.
    "(1080p" / "AV1)" from a parenthesized tag group like "(1080p AV1)")
    are transparent: neither counts as a tag itself, but neither stops the
    run either, so a technical-info suffix like "(1080p AV1) - SDR" is
    recognized as a whole even though half of it isn't a bare tag word.

    A release group name is sometimes its own bare trailing word instead of
    hyphenated onto the last tag (e.g. "x264 JCH" instead of "x264-JCH"), and
    a channel-count suffix like "5.1" can itself split into two words ("5"
    and "1") once dots become spaces, stranding a bare digit in the middle
    of otherwise-genuine release info (e.g. "... DDP5 1 x264-NTb"). Since
    such words can't be enumerated the way tags can, up to ``wildcard_budget``
    of them may be skipped over - but only when doing so is later justified
    by finding at least one more real tag further back; a bare word that
    isn't followed (further back) by anything recognizable is left alone; a
    lone bare trailing word is exactly as likely to be a real title's last
    word (e.g. "Spider in the Web") as it is release info, so this alone
    never causes a title's actual final word(s) to be misread as junk.

    Args:
        words: Words to scan from the end.
        wildcard_budget: How many unrecognized bare words may still be
            skipped over in this scan (and any it recurses into).

    Returns:
        A tuple of (how many trailing words were consumed by the run,
        counting transparent and wildcard words; how many of those were
        genuine tag matches; whether any tag match carried a "-GROUPNAME"
        suffix).
    """
    consumed = 0
    real_tag_count = 0
    has_group_suffix = False
    index = len(words) - 1

    while index >= 0:
        word = words[index]

        if _is_separator_word(word):
            consumed += 1
            index -= 1
            continue

        match = RELEASE_TAG_TOKEN_PATTERN.match(_strip_wrapping_parens(word))
        if match is not None:
            consumed += 1
            real_tag_count += 1
            if match.group("group_suffix"):
                has_group_suffix = True
            index -= 1
            continue

        if wildcard_budget > 0 and _RELEASE_GROUP_NAME_PATTERN.match(word):
            further_consumed, further_real, further_suffix = _trailing_tag_run(
                words[:index], wildcard_budget=wildcard_budget - 1
            )
            if further_real > 0:
                return (
                    consumed + 1 + further_consumed,
                    real_tag_count + further_real,
                    has_group_suffix or further_suffix,
                )

        break

    return consumed, real_tag_count, has_group_suffix


def _strip_trailing_release_tags(remainder: str) -> str:
    """Return ``remainder`` with a genuine trailing release-tag run removed.

    A release tag word (e.g. "WEB", "1080p") is also a plausible word
    anywhere in an actual episode title (e.g. "Spider in the Web", or
    "Curious George, Web Master + The Big Sleepy"), so this only strips tag
    words that form a contiguous run at the very end of the remainder - real
    release info always sits immediately before the file extension, never
    buried mid-title. Words are matched whole (optionally with a
    "-GROUPNAME" release-group suffix chained onto the last tag, e.g.
    "x264-GROUP") rather than as a substring search, so a title word that
    merely contains a tag-like substring is never mistaken for one. See
    :func:`_trailing_tag_run` for how a parenthesized tag group, a stray
    separator, and a bare release-group-name-shaped word are also handled
    within that run.

    A trailing run consisting of just one genuine tag match (no attached
    release-group suffix) is still left alone even though it matches, since
    that single word is equally plausible as the last word of a real title
    (e.g. "Spider in the Web", "The Web (1)" - the latter's "(1)" copy
    marker doesn't itself look like a tag, so the run there is empty and
    nothing is stripped at all). A run of two or more tag words, or a single
    tag word with a release-group suffix attached, is unambiguous release
    info and gets stripped.
    """
    words = remainder.split()

    consumed, real_tag_count, has_group_suffix = _trailing_tag_run(words)

    if real_tag_count == 0:
        return remainder
    if real_tag_count == 1 and not has_group_suffix:
        return remainder

    kept_words = words[: len(words) - consumed]
    return " ".join(kept_words)


def expected_movie_title_from_filename(item: MediaItem) -> str | None:
    """Return the movie title implied by the filename's Jellyfin naming.

    Jellyfin's movie naming convention is ``Movie Name (Year)``, optionally
    followed by an edition, extra, or release-tag suffix, e.g.
    ``Movie Name (Year) - Director's Cut.mkv``. Dot-delimited release names
    that omit the parentheses (e.g. ``Movie.Name.Year.1080p.mkv``) are also
    recognized. Jellyfin strips the year (and anything after it) out of the
    title itself, so this locates the year using the item's known release
    year and returns the text that precedes it, with separators trimmed away.
    A parenthesized year is preferred over a bare one so titles that happen to
    contain a number matching the release year (e.g. "Fantasia 2000 (2000)")
    aren't truncated at the in-title occurrence.

    Args:
        item: Media item to inspect.

    Returns:
        The movie title segment implied by the filename, or ``None`` when the
        filename has no recognizable year marker or no title text precedes
        it.
    """
    if not item.is_movie or item.year is None:
        return None

    stem = item.path.stem
    strict_year_pattern = re.compile(rf"\(\s*{item.year}\s*\)")
    loose_year_pattern = re.compile(rf"(?<!\d){item.year}(?!\d)")
    year_match = strict_year_pattern.search(stem) or loose_year_pattern.search(stem)
    if year_match is None:
        return None

    base = stem[: year_match.start()].replace("_", " ").replace(".", " ")
    base = base.strip(TITLE_STRIP_CHARACTERS)
    base = re.sub(r"\s+", " ", base).strip()
    return base or None


# Video helpers


def get_video_codec(item: MediaItem) -> str | None:
    """Return the normalized primary video codec for the item.

    Args:
        item: Media item to inspect.

    Returns:
        The primary video codec, or ``None`` when no video track exists.
    """
    video_track: VideoTrack | None = item.video_track
    if video_track is None:
        return None

    return video_track.codec


def is_hdr(item: MediaItem) -> bool:
    """Return whether the item's primary video track is HDR.

    Args:
        item: Media item to inspect.

    Returns:
        ``True`` when the primary video track is HDR.
    """
    video_track = item.video_track
    if video_track is None:
        return False

    return video_track.hdr


def get_resolution(item: MediaItem) -> str | None:
    """Return the display-friendly video resolution for the item.

    Args:
        item: Media item to inspect.

    Returns:
        A resolution label such as ``"2160p"`` or ``None`` when unavailable.
    """
    return item.resolution


# Audio helpers


def get_primary_audio_codec(item: MediaItem) -> str | None:
    """Return the codec of the first available audio track.

    Args:
        item: Media item to inspect.

    Returns:
        The primary audio codec, or ``None`` when no audio track exists.
    """
    primary_audio_track: AudioTrack | None = next(iter(item.audio_tracks), None)
    if primary_audio_track is None:
        return None

    return primary_audio_track.codec


def get_audio_languages(item: MediaItem) -> tuple[str, ...]:
    """Return distinct audio languages in first-seen order.

    Args:
        item: Media item to inspect.

    Returns:
        A tuple of normalized audio language codes.
    """
    seen_languages: set[str] = set()
    languages: list[str] = []

    for track in item.audio_tracks:
        if track.language in seen_languages:
            continue

        seen_languages.add(track.language)
        languages.append(track.language)

    return tuple(languages)


# Filesystem helpers


def get_display_path(item: MediaItem) -> str:
    """Return the media path with the configured display prefix removed.

    Args:
        item: Media item to format.

    Returns:
        The media path with ``REPORT_MEDIA_PATH_PREFIX`` removed from the start
        when present.
    """
    media_path_prefix = get_config().reporting.media_path_prefix
    return _strip_configured_prefix(item.path, media_path_prefix)


def media_files(item: MediaItem) -> tuple[Path, ...]:
    """Return media-adjacent files that share the same basename.

    Args:
        item: Media item to inspect.

    Returns:
        All files in the item's directory whose names match the media basename,
        sorted alphabetically.
    """
    item_directory = _item_directory(item)
    if not item_directory.is_dir():
        return ()

    basename = item.path.stem
    basename_prefix = f"{basename}"

    matching_files = [
        path
        for path in item_directory.iterdir()
        if path.is_file()
        and (path.name == basename or path.name.startswith(basename_prefix))
    ]
    matching_files.sort(key=lambda path: path.name.casefold())
    return tuple(matching_files)
