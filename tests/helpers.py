"""Shared test fixture builders used across the tests/ package."""

from __future__ import annotations

from pathlib import Path

from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
import config
from config import ProcessingConfig
from config import ServerCollection
from config import ServerConfig
from models import AudioTrack
from models import MediaItem
from models import MediaLibrary
from models import SubtitleTrack
from models import VideoTrack
from results import AuditServerResult
from results import ComparisonSetting
from results import LibraryAuditResult
import tvdb


def _make_library(
    *,
    library_id: str,
    name: str,
    collection_type: str,
    locations: tuple[Path, ...] | None = None,
) -> MediaLibrary:
    return MediaLibrary(
        id=library_id,
        name=name,
        collection_type=collection_type,
        locations=(Path(name),) if locations is None else locations,
    )


def _make_item(
    title: str = "Example Title",
    *,
    item_id: str | None = None,
    path: Path | None = None,
    subtitle_tracks: tuple[SubtitleTrack, ...] = (),
    audio_tracks: tuple[AudioTrack, ...] = (),
    image_tags: dict[str, str] | None = None,
    is_movie: bool = True,
    is_episode: bool = False,
    library: str = "Movies",
    series_name: str | None = None,
    season_name: str | None = None,
    season_number: int | None = None,
    episode_number: int | None = None,
    year: int | None = 2024,
    video_track: VideoTrack | None = None,
) -> MediaItem:
    return MediaItem(
        id=title.casefold().replace(" ", "-") if item_id is None else item_id,
        title=title,
        path=Path(f"{title}.mkv") if path is None else path,
        is_movie=is_movie,
        is_episode=is_episode,
        library=library,
        series_name=series_name,
        season_name=season_name,
        season_number=season_number,
        episode_number=episode_number,
        year=year,
        runtime_ticks=None,
        image_tags={} if image_tags is None else image_tags,
        subtitle_tracks=subtitle_tracks,
        audio_tracks=audio_tracks,
        video_track=video_track,
    )


def _make_finding(
    *,
    category: AuditCategory,
    severity: AuditSeverity,
    title: str,
    message: str | None = None,
    media_item: MediaItem | None = None,
    check_name: str | None = None,
) -> AuditFinding:
    return AuditFinding(
        category=category,
        severity=severity,
        check_name=check_name or f"{category.value}_{severity.value}",
        message=message or f"{title} finding",
        media_item=_make_item(title) if media_item is None else media_item,
    )


def _make_comparison_setting(label: str, value: str) -> ComparisonSetting:
    return ComparisonSetting(label=label, value=value)


def _make_app_config(*, tvdb_api_key: str | None = None) -> config.AppConfig:
    return config.AppConfig(
        reporting=config.ReportingConfig(
            media_path_prefix="",
            csv_output=config.CsvOutputConfig(
                movies=Path("movies_report.csv"),
                tv=Path("tv_report.csv"),
            ),
            output=config.ReportOutputConfig(
                audit_csv=Path("audit_report.csv"),
                audit_html=Path("audit_results"),
            ),
            english_language_codes=("en", "eng", ""),
        ),
        processing=ProcessingConfig(enable_movies=True, enable_tv=True),
        servers=ServerCollection(
            default_server="primary",
            servers={
                "primary": ServerConfig(
                    key="primary",
                    name="Primary",
                    url="http://primary:8096",
                    api_key="token",
                ),
            },
        ),
        tvdb=config.TvdbConfig(api_key=tvdb_api_key),
    )


def _make_left_right_app_config(
    *, tvdb_api_key: str | None = None, include_right_server: bool = True
) -> config.AppConfig:
    """Return an AppConfig with a "left" server and, by default, a "right" server.

    Shared by every test class that drives auditor.py's bulk-transfer
    functions, or a standalone apply_*.py tool, against two fake Jellyfin
    servers named "left"/"right" (or just "left", with
    ``include_right_server=False``, for a tool that only ever targets one
    server).
    """
    servers = {
        "left": ServerConfig(key="left", name="Left", url="http://left:8096", api_key="left-token"),
    }
    if include_right_server:
        servers["right"] = ServerConfig(
            key="right", name="Right", url="http://right:8096", api_key="right-token"
        )
    return config.AppConfig(
        reporting=config.ReportingConfig(
            media_path_prefix="",
            csv_output=config.CsvOutputConfig(
                movies=Path("movies_report.csv"),
                tv=Path("tv_report.csv"),
            ),
            output=config.ReportOutputConfig(
                audit_csv=Path("audit_report.csv"),
                audit_html=Path("audit_results"),
            ),
            english_language_codes=("en", "eng", ""),
        ),
        processing=ProcessingConfig(enable_movies=True, enable_tv=True),
        servers=ServerCollection(default_server="left", servers=servers),
        tvdb=config.TvdbConfig(api_key=tvdb_api_key),
    )


def _make_single_library_result(
    items: tuple[MediaItem, ...],
    *,
    library_id: str,
    library_name: str,
    collection_type: str,
    server_name: str | None = None,
    server_key: str | None = None,
    server_url: str | None = None,
    findings: tuple[AuditFinding, ...] = (),
) -> AuditServerResult:
    """Return an AuditServerResult with exactly one library holding ``items``.

    Shared by tests that only need a minimal single-library server result -
    most commonly one side of a comparison/generator.py left/right pair -
    without caring about any other library-level detail.
    """
    return AuditServerResult(
        libraries_audited=1,
        media_items_processed=len(items),
        library_results=(
            LibraryAuditResult(
                library=_make_library(
                    library_id=library_id,
                    name=library_name,
                    collection_type=collection_type,
                ),
                media_items_processed=len(items),
                audited_items=items,
                items_with_english_subtitles=0,
                items_with_local_nfo=0,
                items_with_local_backdrop=0,
                findings=findings,
            ),
        ),
        findings=findings,
        server_name=server_name,
        server_key=server_key,
        server_url=server_url,
    )


def _make_tvdb_episode(
    *,
    episode_id: int = 1,
    season_number: int = 1,
    episode_number: int = 1,
    name: str = "Episode Name",
    overview: str | None = None,
    runtime_minutes: int | None = None,
) -> tvdb.TvdbEpisode:
    return tvdb.TvdbEpisode(
        id=episode_id,
        season_number=season_number,
        episode_number=episode_number,
        name=name,
        overview=overview,
        runtime_minutes=runtime_minutes,
    )


def _make_tvdb_search_result(
    *,
    series_id: str = "1",
    name: str = "Series Name",
    year: str | None = None,
    overview: str | None = None,
) -> tvdb.TvdbSeriesSearchResult:
    return tvdb.TvdbSeriesSearchResult(id=series_id, name=name, year=year, overview=overview)


def _make_empty_comparison_results() -> tuple[AuditServerResult, AuditServerResult]:
    """Return a minimal left/right AuditServerResult pair with no libraries."""
    left_result = AuditServerResult(
        libraries_audited=0,
        media_items_processed=0,
        library_results=(),
        findings=(),
        server_name="Left Server",
        server_key="left",
    )
    right_result = AuditServerResult(
        libraries_audited=0,
        media_items_processed=0,
        library_results=(),
        findings=(),
        server_name="Right Server",
        server_key="right",
    )
    return left_result, right_result
