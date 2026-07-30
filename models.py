"""Core normalized data models for Jellyfin Library Auditor.

This module defines application-level media models that are independent of any
specific API payload format. The rest of the application can rely on these
dataclasses instead of passing around raw Jellyfin JSON.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


ENGLISH_LANGUAGE_CODES = frozenset({"", "en", "eng"})
ULTRA_HD_HEIGHT = 2160
FULL_HD_HEIGHT = 1080
HD_HEIGHT = 720
SD_HEIGHT = 480


def _normalize_language(language: str) -> str:
    """Normalize a language code or display value.

    Args:
        language: Raw language text.

    Returns:
        The normalized, lowercase language string.
    """
    return language.strip().lower()


def _format_season_episode(
    season_number: int | None,
    episode_number: int | None,
) -> str | None:
    """Build an SxxExx-style episode label when numbers are available.

    Args:
        season_number: Season number for the media item.
        episode_number: Episode number for the media item.

    Returns:
        A formatted episode label, or ``None`` when both values are missing.
    """
    if season_number is None and episode_number is None:
        return None

    season_text = "S??" if season_number is None else f"S{season_number:02d}"
    episode_text = "E??" if episode_number is None else f"E{episode_number:02d}"
    return f"{season_text}{episode_text}"


@dataclass(frozen=True, slots=True)
class SubtitleTrack:
    """Represents a normalized subtitle track."""

    language: str
    codec: str | None
    is_external: bool
    is_default: bool
    is_forced: bool

    def __post_init__(self) -> None:
        """Normalize language and codec values."""
        object.__setattr__(self, "language", _normalize_language(self.language))
        if self.codec is not None:
            object.__setattr__(self, "codec", self.codec.strip().lower())

    @property
    def is_english(self) -> bool:
        """Return whether this subtitle track should be treated as English."""
        return self.language in ENGLISH_LANGUAGE_CODES


@dataclass(frozen=True, slots=True)
class AudioTrack:
    """Represents a normalized audio track."""

    language: str
    codec: str
    channels: int | None
    title: str | None

    def __post_init__(self) -> None:
        """Normalize audio metadata."""
        object.__setattr__(self, "language", _normalize_language(self.language))
        object.__setattr__(self, "codec", self.codec.strip().lower())
        if self.title is not None:
            object.__setattr__(self, "title", self.title.strip() or None)


@dataclass(frozen=True, slots=True)
class VideoTrack:
    """Represents a normalized primary video track."""

    codec: str
    width: int
    height: int
    bitrate: int | None
    hdr: bool
    video_range: str | None

    def __post_init__(self) -> None:
        """Normalize codec and video range values."""
        object.__setattr__(self, "codec", self.codec.strip().lower())
        if self.video_range is not None:
            object.__setattr__(
                self,
                "video_range",
                self.video_range.strip() or None,
            )


@dataclass(frozen=True, slots=True)
class MediaItem:
    """Represents a normalized media item used across the application."""

    id: str
    title: str
    path: Path
    is_movie: bool
    is_episode: bool
    library: str
    series_name: str | None
    season_name: str | None
    season_number: int | None
    episode_number: int | None
    year: int | None
    runtime_ticks: int | None
    image_tags: dict[str, str]
    subtitle_tracks: tuple[SubtitleTrack, ...]
    audio_tracks: tuple[AudioTrack, ...]
    video_track: VideoTrack | None

    def __post_init__(self) -> None:
        """Normalize path-like and collection fields."""
        object.__setattr__(self, "path", Path(self.path))
        object.__setattr__(self, "title", self.title.strip())
        object.__setattr__(self, "library", self.library.strip())
        object.__setattr__(self, "series_name", self._normalize_optional_text(self.series_name))
        object.__setattr__(self, "season_name", self._normalize_optional_text(self.season_name))
        object.__setattr__(self, "image_tags", dict(self.image_tags))
        object.__setattr__(self, "subtitle_tracks", tuple(self.subtitle_tracks))
        object.__setattr__(self, "audio_tracks", tuple(self.audio_tracks))

    @staticmethod
    def _normalize_optional_text(value: str | None) -> str | None:
        """Normalize optional display text.

        Args:
            value: Input text that may be blank or missing.

        Returns:
            The stripped string, or ``None`` when the value is blank.
        """
        if value is None:
            return None

        normalized_value = value.strip()
        return normalized_value or None

    @property
    def has_subtitles(self) -> bool:
        """Return whether the item has any subtitle tracks."""
        return bool(self.subtitle_tracks)

    @property
    def has_external_subtitles(self) -> bool:
        """Return whether the item has at least one external subtitle track."""
        return any(track.is_external for track in self.subtitle_tracks)

    @property
    def has_embedded_subtitles(self) -> bool:
        """Return whether the item has at least one embedded subtitle track."""
        return any(not track.is_external for track in self.subtitle_tracks)

    @property
    def has_english_subtitles(self) -> bool:
        """Return whether the item has at least one English subtitle track."""
        return any(track.is_english for track in self.subtitle_tracks)

    @property
    def resolution(self) -> str | None:
        """Return a display-friendly vertical resolution label."""
        if self.video_track is None:
            return None

        height = self.video_track.height
        if height >= ULTRA_HD_HEIGHT:
            return "2160p"
        if height >= FULL_HD_HEIGHT:
            return "1080p"
        if height >= HD_HEIGHT:
            return "720p"
        if height >= SD_HEIGHT:
            return "480p"
        return f"{height}p"

    @property
    def display_name(self) -> str:
        """Return a user-facing media item name."""
        if not self.is_episode:
            return self.title

        parts: list[str] = []
        if self.series_name:
            parts.append(self.series_name)
        if self.season_name:
            parts.append(self.season_name)

        episode_label = _format_season_episode(
            season_number=self.season_number,
            episode_number=self.episode_number,
        )
        if episode_label:
            parts.append(episode_label)

        parts.append(self.title)
        return " - ".join(parts)


__all__ = [
    "AudioTrack",
    "ENGLISH_LANGUAGE_CODES",
    "MediaItem",
    "SubtitleTrack",
    "VideoTrack",
]
