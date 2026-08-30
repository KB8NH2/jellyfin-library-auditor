"""Tests for CLI handling and report generation."""

from __future__ import annotations

import base64
import contextlib
import csv
from datetime import datetime
from datetime import timedelta
from datetime import timezone
import io
import json
import logging
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
from unittest.mock import call
from unittest.mock import patch

import apply_dvd_metadata
import apply_episode_numbers
import apply_episode_titles
import compare_csv_files
import auditor
import audit
from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from comparison import write_comparison_reports
from comparison import ImageTransferResult
from comparison import ImageTransferTarget
from comparison import MetadataTransferResult
from comparison import MetadataTransferTarget
from comparison import SubtitleTransferTarget
from comparison import generator as comparison_generator
from comparison.generator import DEFAULT_COMPARISON_OUTPUT_DIR
import config
from config import ProcessingConfig
from config import ServerCollection
from config import ServerConfig
from config import clear_config_cache
import jellyfin
from jellyfin import JellyfinClient
import requests
import media
from models import AudioTrack
from models import MediaItem
from models import MediaLibrary
from models import SubtitleTrack
from models import VideoTrack
import reports
from reports import checks as report_checks
from reports import generator as report_generator
from reports import library as report_library
from results import AuditServerResult
from results import ComparisonSetting
from results import LibraryComparisonSettings
from results import LibraryAuditResult
import transfer_images
import transfer_metadata
import transfer_subtitles
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


class ParseArgumentsTests(unittest.TestCase):
    def test_defaults_write_both_reports_without_filters(self) -> None:
        options = auditor.parse_args([])

        self.assertTrue(options.write_csv)
        self.assertTrue(options.write_html)
        self.assertEqual(options.library_names, ())
        self.assertIsNone(options.categories)
        self.assertIsNone(options.severities)

    def test_explicit_arguments_enable_requested_filters(self) -> None:
        options = auditor.parse_args(
            [
                "--html",
                "--library",
                "Movies",
                "--category",
                "subtitles",
                "--severity",
                "error",
            ]
        )

        self.assertFalse(options.write_csv)
        self.assertTrue(options.write_html)
        self.assertEqual(options.library_names, ("Movies",))
        self.assertEqual(options.categories, frozenset({AuditCategory.SUBTITLES}))
        self.assertEqual(options.severities, frozenset({AuditSeverity.ERROR}))

    def test_compare_argument_is_parsed(self) -> None:
        options = auditor.parse_args(["--server", "mediaserver", "--compare", "backup"])

        self.assertEqual(options.server_key, "mediaserver")
        self.assertEqual(options.compare_server_key, "backup")
        self.assertFalse(options.audit_all)

    def test_bare_compare_argument_uses_auto_compare_sentinel(self) -> None:
        options = auditor.parse_args(["--compare"])

        self.assertIsNone(options.server_key)
        self.assertEqual(options.compare_server_key, auditor.AUTO_COMPARE_SENTINEL)
        self.assertFalse(options.audit_all)

    def test_all_argument_is_parsed(self) -> None:
        options = auditor.parse_args(["--all"])

        self.assertTrue(options.audit_all)
        self.assertIsNone(options.server_key)
        self.assertIsNone(options.compare_server_key)

    def test_transfer_metadata_argument_is_parsed(self) -> None:
        options = auditor.parse_args(["--compare", "backup", "--transfer-metadata"])

        self.assertTrue(options.transfer_metadata)
        self.assertFalse(options.transfer_metadata_dry_run)
        self.assertFalse(options.transfer_metadata_yes)

    def test_transfer_metadata_dry_run_and_yes_are_parsed(self) -> None:
        options = auditor.parse_args(
            ["--compare", "backup", "--transfer-metadata", "--dry-run", "--yes"]
        )

        self.assertTrue(options.transfer_metadata_dry_run)
        self.assertTrue(options.transfer_metadata_yes)

    def test_transfer_metadata_without_compare_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--transfer-metadata"])

    def test_dry_run_without_transfer_metadata_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--compare", "backup", "--dry-run"])

    def test_yes_without_transfer_metadata_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--compare", "backup", "--yes"])

    def test_limit_argument_is_parsed(self) -> None:
        options = auditor.parse_args(
            ["--compare", "backup", "--transfer-metadata", "--limit", "5"]
        )

        self.assertEqual(options.transfer_limit, 5)

    def test_limit_defaults_to_none(self) -> None:
        options = auditor.parse_args(["--compare", "backup", "--transfer-metadata"])

        self.assertIsNone(options.transfer_limit)

    def test_limit_works_with_transfer_images(self) -> None:
        options = auditor.parse_args(
            ["--compare", "backup", "--transfer-images", "--limit", "3"]
        )

        self.assertEqual(options.transfer_limit, 3)

    def test_limit_without_transfer_flag_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--compare", "backup", "--limit", "5"])

    def test_limit_below_one_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(
                ["--compare", "backup", "--transfer-metadata", "--limit", "0"]
            )

    def test_check_episode_order_argument_is_parsed(self) -> None:
        with patch("auditor.get_config", return_value=_make_app_config(tvdb_api_key="secret-key")):
            options = auditor.parse_args(["--check-episode-order"])

        self.assertTrue(options.check_episode_order)

    def test_check_episode_order_defaults_to_false(self) -> None:
        options = auditor.parse_args([])

        self.assertFalse(options.check_episode_order)

    def test_check_episode_order_without_tvdb_api_key_is_rejected(self) -> None:
        with patch("auditor.get_config", return_value=_make_app_config(tvdb_api_key=None)):
            with self.assertRaises(auditor.CommandLineUsageError):
                auditor.parse_args(["--check-episode-order"])

    def test_refresh_tvdb_cache_argument_is_parsed(self) -> None:
        with patch("auditor.get_config", return_value=_make_app_config(tvdb_api_key="secret-key")):
            options = auditor.parse_args(["--check-episode-order", "--refresh-tvdb-cache"])

        self.assertTrue(options.refresh_tvdb_cache)

    def test_refresh_tvdb_cache_defaults_to_false(self) -> None:
        with patch("auditor.get_config", return_value=_make_app_config(tvdb_api_key="secret-key")):
            options = auditor.parse_args(["--check-episode-order"])

        self.assertFalse(options.refresh_tvdb_cache)

    def test_refresh_tvdb_cache_without_check_episode_order_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--refresh-tvdb-cache"])

    def test_verify_argument_is_parsed(self) -> None:
        options = auditor.parse_args(
            ["--compare", "backup", "--transfer-metadata", "--verify"]
        )

        self.assertTrue(options.verify)

    def test_verify_defaults_to_false(self) -> None:
        options = auditor.parse_args(["--compare", "backup", "--transfer-metadata"])

        self.assertFalse(options.verify)

    def test_verify_works_with_transfer_images(self) -> None:
        options = auditor.parse_args(
            ["--compare", "backup", "--transfer-images", "--verify"]
        )

        self.assertTrue(options.verify)

    def test_verify_without_transfer_flag_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--compare", "backup", "--verify"])

    def test_series_name_and_season_number_are_parsed(self) -> None:
        options = auditor.parse_args(
            [
                "--compare",
                "backup",
                "--transfer-metadata",
                "--series-name",
                "Show Name",
                "--season-number",
                "2",
            ]
        )

        self.assertEqual(options.transfer_metadata_series_name, "Show Name")
        self.assertEqual(options.transfer_metadata_season_number, 2)

    def test_series_name_and_season_number_default_to_none(self) -> None:
        options = auditor.parse_args(["--compare", "backup", "--transfer-metadata"])

        self.assertIsNone(options.transfer_metadata_series_name)
        self.assertIsNone(options.transfer_metadata_season_number)

    def test_series_name_without_transfer_metadata_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(["--compare", "backup", "--series-name", "Show Name"])

    def test_season_number_without_series_name_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(
                ["--compare", "backup", "--transfer-metadata", "--season-number", "2"]
            )

    def test_season_number_below_zero_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(
                [
                    "--compare",
                    "backup",
                    "--transfer-metadata",
                    "--series-name",
                    "Show Name",
                    "--season-number",
                    "-1",
                ]
            )

    def test_blank_series_name_is_rejected(self) -> None:
        with self.assertRaises(auditor.CommandLineUsageError):
            auditor.parse_args(
                ["--compare", "backup", "--transfer-metadata", "--series-name", "   "]
            )


class SelectLibrariesTests(unittest.TestCase):
    def test_select_audit_libraries_filters_case_insensitively(self) -> None:
        libraries = (
            _make_library(
                library_id="movies",
                name="Movies",
                collection_type="movies",
            ),
            _make_library(
                library_id="shows",
                name="Shows",
                collection_type="tv",
            ),
            _make_library(
                library_id="music",
                name="Music",
                collection_type="music",
            ),
        )
        processing = ProcessingConfig(enable_movies=True, enable_tv=True)

        selected = auditor._select_audit_libraries(
            libraries,
            processing,
            requested_library_names=("movies",),
        )

        self.assertEqual(tuple(library.name for library in selected), ("Movies",))

    def test_select_audit_libraries_raises_for_unknown_library(self) -> None:
        libraries = (
            _make_library(
                library_id="movies",
                name="Movies",
                collection_type="movies",
            ),
        )
        processing = ProcessingConfig(enable_movies=True, enable_tv=False)

        with self.assertRaises(auditor.CommandLineUsageError):
            auditor._select_audit_libraries(
                libraries,
                processing,
                requested_library_names=("Shows",),
            )


class FilterAuditResultTests(unittest.TestCase):
    def test_filter_audit_result_applies_category_and_severity(self) -> None:
        warning_subtitles = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Warning Subtitles",
        )
        error_subtitles = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.ERROR,
            title="Error Subtitles",
        )
        error_metadata = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.ERROR,
            title="Error Metadata",
        )

        result = auditor.filter_audit_result(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=3,
                library_results=(),
                findings=(
                    warning_subtitles,
                    error_subtitles,
                    error_metadata,
                ),
            ),
            categories=frozenset({AuditCategory.SUBTITLES}),
            severities=frozenset({AuditSeverity.ERROR}),
        )

        self.assertEqual(result.findings, (error_subtitles,))


class LibraryAuditResultTests(unittest.TestCase):
    def test_audit_library_result_tracks_per_library_asset_coverage(self) -> None:
        library = _make_library(
            library_id="movies",
            name="Movies",
            collection_type="movies",
        )

        with TemporaryDirectory() as temp_dir:
            temp_path = Path(temp_dir)
            first_directory = temp_path / "Movie One"
            first_directory.mkdir()
            first_media = first_directory / "Movie One.mkv"
            first_media.write_text("", encoding="utf-8")
            (first_directory / "fanart.jpg").write_text("", encoding="utf-8")
            (first_directory / "Movie One.nfo").write_text("", encoding="utf-8")

            second_directory = temp_path / "Movie Two"
            second_directory.mkdir()
            second_media = second_directory / "Movie Two.mkv"
            second_media.write_text("", encoding="utf-8")

            items = (
                _make_item(
                    "Movie One",
                    path=first_media,
                    subtitle_tracks=(
                        SubtitleTrack(
                            language="eng",
                            codec="srt",
                            is_external=True,
                            is_default=False,
                            is_forced=False,
                        ),
                    ),
                ),
                _make_item("Movie Two", path=second_media),
            )

            class FakeClient:
                def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                    self.last_library_id = library_id
                    return items

            result = auditor._audit_library_result(FakeClient(), library)

        self.assertEqual(result.media_items_processed, 2)
        self.assertEqual(result.items_with_english_subtitles, 1)
        self.assertEqual(result.items_with_local_nfo, 1)
        self.assertEqual(result.items_with_local_backdrop, 1)
        self.assertEqual(result.english_subtitles_percentage, 50.0)
        self.assertEqual(result.local_nfo_percentage, 50.0)
        self.assertEqual(result.local_backdrop_percentage, 50.0)

    def test_audit_library_result_detects_missing_tv_seasons_and_episodes(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Pilot",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                title="...And the Bag's in the River",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=3,
            ),
            _make_item(
                title="No Mas",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_name="Season 3",
                season_number=3,
                episode_number=1,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                self.last_library_id = library_id
                return items

        result = auditor._audit_library_result(FakeClient(), library)

        missing_seasons = [
            finding
            for finding in result.findings
            if finding.check_name == "missing_seasons"
        ]
        missing_episodes = [
            finding
            for finding in result.findings
            if finding.check_name == "missing_episodes"
        ]

        self.assertEqual(len(missing_seasons), 1)
        self.assertEqual(missing_seasons[0].message, "Missing seasons: 2.")
        self.assertEqual(len(missing_episodes), 1)
        self.assertEqual(missing_episodes[0].message, "Missing episodes: 2.")

    def test_audit_library_result_includes_episode_ordering_findings_when_tvdb_client_given(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Local Title",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_number=1,
                episode_number=1,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                self.requested_library_id = library_id
                return {"Breaking Bad": ("12345",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                self.calls.append((series_id, season_type))
                if season_type == "official":
                    return (_make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),)
                return (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)

        fake_client = FakeClient()
        fake_tvdb_client = FakeTvdbClient()

        result = auditor._audit_library_result(
            fake_client, library, tvdb_client=fake_tvdb_client, check_episode_order=True
        )

        check_names = {finding.check_name for finding in result.findings}
        self.assertIn("aired_dvd_order_mismatch", check_names)
        self.assertEqual(fake_client.requested_library_id, "shows")
        self.assertEqual(
            sorted(fake_tvdb_client.calls),
            [("12345", "dvd"), ("12345", "official")],
        )

    def test_audit_library_result_merges_cached_series_id_not_assigned_in_jellyfin(
        self,
    ) -> None:
        """A same-named id only known from the TheTVDB cache still gets merged in.

        Regression test: Jellyfin's Series item was only ever assigned one
        TheTVDB id, but the episode cache also knows of another id recorded
        under the same series name (e.g. left behind by an earlier
        mismatched_tvdb_series suggestion search). That second id's episodes
        must still be merged into the position map the local title is
        checked against - otherwise a show TheTVDB split across ids (like a
        long-running series relaunch) looks like a mismatch even though the
        cache already has the data to explain it.
        """
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Space Babies",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Doctor Who",
                season_number=1,
                episode_number=1,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Doctor Who": ("78804",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                assert name == "Doctor Who"
                return ("999",)

            def get_series_episodes(
                self, series_id: str, season_type: str, *, series_name: str | None = None
            ):
                del season_type
                if series_id == "78804":
                    return (_make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),)
                return (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Space Babies"),
                )

        result = auditor._audit_library_result(
            FakeClient(), library, tvdb_client=FakeTvdbClient(), check_episode_order=True
        )

        check_names = {finding.check_name for finding in result.findings}
        self.assertNotIn("aired_dvd_order_mismatch", check_names)

    def test_audit_library_result_skips_episode_ordering_without_check_episode_order(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Aired Title",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_number=1,
                episode_number=1,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Breaking Bad": ("12345",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def __init__(self) -> None:
                self.calls: list[tuple[str, str]] = []

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                self.calls.append((series_id, season_type))
                return (_make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),)

        fake_tvdb_client = FakeTvdbClient()

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=fake_tvdb_client)

        check_names = {finding.check_name for finding in result.findings}
        self.assertNotIn("aired_dvd_order_mismatch", check_names)
        self.assertEqual(
            sorted(fake_tvdb_client.calls),
            [("12345", "dvd"), ("12345", "official")],
        )

    def test_audit_library_result_flags_missing_trailing_episodes_using_tvdb_data(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Pilot",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                title="Cat's in the Bag...",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_number=1,
                episode_number=2,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Breaking Bad": ("12345",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del series_id
                del season_type
                return (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),
                    _make_tvdb_episode(season_number=1, episode_number=2, name="Cat's in the Bag..."),
                    _make_tvdb_episode(season_number=1, episode_number=3, name="...And the Bag's in the River"),
                )

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        missing_episodes = [
            finding
            for finding in result.findings
            if finding.check_name == "missing_episodes"
        ]

        self.assertEqual(len(missing_episodes), 1)
        self.assertEqual(missing_episodes[0].message, "Missing episodes: 3.")

    def test_audit_library_result_flags_missing_trailing_seasons_using_tvdb_data(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Pilot",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_number=1,
                episode_number=1,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Breaking Bad": ("12345",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del series_id
                del season_type
                return (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),
                    _make_tvdb_episode(season_number=2, episode_number=1, name="Seven Thirty-Seven"),
                )

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        missing_seasons = [
            finding
            for finding in result.findings
            if finding.check_name == "missing_seasons"
        ]

        self.assertEqual(len(missing_seasons), 1)
        self.assertEqual(missing_seasons[0].message, "Missing seasons: 2.")

    def test_audit_library_result_skips_series_when_tvdb_lookup_fails(self) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = (
            _make_item(
                title="Aired Title",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Breaking Bad",
                season_number=1,
                episode_number=1,
            ),
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Breaking Bad": ("12345",)}

        class FailingTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                raise tvdb.TvdbRequestError("boom")

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FailingTvdbClient())

        check_names = {finding.check_name for finding in result.findings}
        self.assertNotIn("aired_dvd_order_mismatch", check_names)

    def _make_mismatched_series_items(self, count: int = 7) -> tuple:
        return tuple(
            _make_item(
                title=f"Episode {number}",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Mismatched Show",
                season_number=1,
                episode_number=number,
            )
            for number in range(1, count + 1)
        )

    def test_audit_library_result_merges_positions_from_multiple_tvdb_ids_sharing_a_name(
        self,
    ) -> None:
        """Regression test: a Jellyfin library can have two distinct Series
        items sharing one display name (e.g. TheTVDB splitting a
        long-running show into a new entry for a later era while an older
        entry keeps the earlier episodes, both still titled the same in
        Jellyfin). Collapsing that to a single TheTVDB id previously meant
        every local episode belonging to whichever id lost the race got
        flagged as a mismatch, and which id "won" wasn't even stable across
        runs."""
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = tuple(
            _make_item(
                title=f"Episode {number}",
                is_movie=False,
                is_episode=True,
                library="TV Shows",
                series_name="Doctor Who",
                season_number=1,
                episode_number=number,
            )
            for number in range(1, 9)
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Doctor Who": ("classic-id", "reboot-id")}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del season_type
                if series_id == "classic-id":
                    return tuple(
                        _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                        for number in range(1, 5)
                    )
                return tuple(
                    _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                    for number in range(5, 9)
                )

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        mismatched = [
            finding for finding in result.findings if finding.check_name == "mismatched_tvdb_series"
        ]
        self.assertEqual(mismatched, [])

    def test_audit_library_result_enriches_mismatched_series_finding_with_a_better_match(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = self._make_mismatched_series_items()

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Mismatched Show": ("wrong-id",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del season_type
                if series_id == "wrong-id":
                    return (
                        _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                    )
                return tuple(
                    _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                    for number in range(1, 8)
                )

            def search_series(self, name: str):
                del name
                return (
                    _make_tvdb_search_result(series_id="wrong-id", name="Mismatched Show"),
                    _make_tvdb_search_result(series_id="right-id", name="Mismatched Show (Right)"),
                )

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        mismatched = [
            finding for finding in result.findings if finding.check_name == "mismatched_tvdb_series"
        ]
        self.assertEqual(len(mismatched), 1)
        self.assertIn(
            "TheTVDB id right-id ('Mismatched Show (Right)') matches these local episodes much "
            "better",
            mismatched[0].message,
        )

    def test_audit_library_result_leaves_mismatched_series_finding_unchanged_without_a_better_match(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = self._make_mismatched_series_items()

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Mismatched Show": ("wrong-id",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del series_id
                del season_type
                return (_make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),)

            def search_series(self, name: str):
                del name
                return (_make_tvdb_search_result(series_id="wrong-id", name="Mismatched Show"),)

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        mismatched = [
            finding for finding in result.findings if finding.check_name == "mismatched_tvdb_series"
        ]
        self.assertEqual(len(mismatched), 1)
        self.assertNotIn("TheTVDB id", mismatched[0].message)

    def test_audit_library_result_leaves_mismatched_series_finding_unchanged_when_search_fails(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = self._make_mismatched_series_items()

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Mismatched Show": ("wrong-id",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del series_id
                del season_type
                return (_make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),)

            def search_series(self, name: str):
                del name
                raise tvdb.TvdbRequestError("boom")

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        mismatched = [
            finding for finding in result.findings if finding.check_name == "mismatched_tvdb_series"
        ]
        self.assertEqual(len(mismatched), 1)
        self.assertNotIn("TheTVDB id", mismatched[0].message)

    def test_audit_library_result_caps_the_number_of_search_candidates_it_fetches(
        self,
    ) -> None:
        library = _make_library(
            library_id="shows",
            name="TV Shows",
            collection_type="tvshows",
        )
        items = self._make_mismatched_series_items()

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return items

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                return {"Mismatched Show": ("wrong-id",)}

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def __init__(self) -> None:
                self.fetched_ids: list[str] = []

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                del season_type
                self.fetched_ids.append(series_id)
                if series_id == "wrong-id":
                    return (
                        _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                    )
                # Every non-"wrong-id" candidate would be a perfect match if fetched,
                # so this proves the cap - not a lack of a real match - is why the
                # 6th candidate's fix never gets suggested.
                return tuple(
                    _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                    for number in range(1, 8)
                )

            def search_series(self, name: str):
                del name
                return (_make_tvdb_search_result(series_id="wrong-id", name="Mismatched Show"),) + tuple(
                    _make_tvdb_search_result(series_id=f"candidate-{index}", name="Mismatched Show")
                    for index in range(1, 7)
                )

        fake_tvdb_client = FakeTvdbClient()
        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=fake_tvdb_client)

        # "wrong-id" fetched aired+dvd (2), plus the 5-candidate cap = 7 episode fetches total.
        self.assertEqual(len(fake_tvdb_client.fetched_ids), 7)
        mismatched = [
            finding for finding in result.findings if finding.check_name == "mismatched_tvdb_series"
        ]
        self.assertEqual(len(mismatched), 1)
        self.assertIn("TheTVDB id candidate-1", mismatched[0].message)

    def test_audit_library_result_skips_tvdb_check_for_movie_libraries(self) -> None:
        library = _make_library(
            library_id="movies",
            name="Movies",
            collection_type="movies",
        )

        class FakeClient:
            def get_library_items(self, library_id: str) -> tuple[MediaItem, ...]:
                return ()

            def get_series_tvdb_ids(self, library_id: str) -> dict[str, tuple[str, ...]]:
                raise AssertionError("should not be called for a movie library")

        class FakeTvdbClient:
            def get_cached_series_ids_by_name(self, name: str) -> tuple[str, ...]:
                return ()

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name: str | None = None):
                raise AssertionError("should not be called for a movie library")

        result = auditor._audit_library_result(FakeClient(), library, tvdb_client=FakeTvdbClient())

        self.assertEqual(result.findings, ())


class AuditServerTests(unittest.TestCase):
    def test_audit_server_includes_configuration_snapshot_for_compare(self) -> None:
        library = _make_library(
            library_id="movies",
            name="Movies",
            collection_type="movies",
        )
        item = _make_item(title="Alien", library="Movies")

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                del args
                del kwargs

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, exc_type, exc_value, traceback) -> None:
                del exc_type
                del exc_value
                del traceback

            def ping(self) -> bool:
                return True

            def get_server_name(self) -> str:
                return "Primary Server"

            def get_libraries(self) -> list[MediaLibrary]:
                return [library]

            def get_library_items(self, library_id: str) -> list[MediaItem]:
                self.last_library_id = library_id
                return [item]

            def get_server_user_experience_settings(
                self,
            ) -> tuple[ComparisonSetting, ...]:
                return (
                    _make_comparison_setting("UI Culture", "en-US"),
                    _make_comparison_setting("Enable Folder View", "Yes"),
                )

            def get_library_user_experience_settings(
                self,
                library_names: tuple[str, ...],
            ) -> tuple[LibraryComparisonSettings, ...]:
                self.last_library_names = library_names
                return (
                    LibraryComparisonSettings(
                        library_name="Movies",
                        settings=(
                            _make_comparison_setting("Locations", "D:\\Media\\Movies"),
                            _make_comparison_setting("Realtime Monitor", "Yes"),
                        ),
                    ),
                )

        fake_config = config.AppConfig(
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
            tvdb=config.TvdbConfig(api_key=None),
        )

        with patch("auditor.get_config", return_value=fake_config):
            with patch("auditor.JellyfinClient", FakeClient):
                with patch("auditor.audit_media_item", return_value=()):
                    result = auditor.audit_server(
                        "primary",
                        include_configuration_snapshot=True,
                    )

        self.assertEqual(result.server_name, "Primary Server")
        self.assertEqual(
            result.server_settings,
            (
                _make_comparison_setting("UI Culture", "en-US"),
                _make_comparison_setting("Enable Folder View", "Yes"),
            ),
        )
        self.assertEqual(
            result.library_settings,
            (
                LibraryComparisonSettings(
                    library_name="Movies",
                    settings=(
                        _make_comparison_setting("Locations", "D:\\Media\\Movies"),
                        _make_comparison_setting("Realtime Monitor", "Yes"),
                    ),
                ),
            ),
        )


class ConfigLoadingTests(unittest.TestCase):
    def test_load_server_collection_reads_servers_toml(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                        "",
                        "[servers.backup]",
                        'name = "Backup"',
                        'url = "http://backup:8096"',
                        'api_key = "def456"',
                    )
                ),
                encoding="utf-8",
            )

            collection = config.load_server_collection(servers_path)

        self.assertIsInstance(collection, ServerCollection)
        self.assertEqual(collection.default_server, "primary")
        self.assertEqual(collection.get_default(), ServerConfig("primary", "Primary", "http://primary:8096", "abc123"))
        self.assertEqual(collection.get("backup").name, "Backup")
        self.assertEqual(
            collection.ordered(),
            (
                ServerConfig("primary", "Primary", "http://primary:8096", "abc123"),
                ServerConfig("backup", "Backup", "http://backup:8096", "def456"),
            ),
        )
        first_server, second_server = collection.first_two()
        self.assertEqual(first_server.key, "primary")
        self.assertEqual(second_server.key, "backup")

    def test_load_tvdb_api_key_reads_tvdb_table(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                        "",
                        "[tvdb]",
                        'api_key = "tvdb-secret"',
                    )
                ),
                encoding="utf-8",
            )

            api_key = config.load_tvdb_api_key(servers_path)

        self.assertEqual(api_key, "tvdb-secret")

    def test_load_tvdb_api_key_returns_none_when_table_missing(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                    )
                ),
                encoding="utf-8",
            )

            api_key = config.load_tvdb_api_key(servers_path)

        self.assertIsNone(api_key)

    def test_load_tvdb_api_key_returns_none_when_key_blank(self) -> None:
        with TemporaryDirectory() as temp_dir:
            servers_path = Path(temp_dir) / "servers.toml"
            servers_path.write_text(
                "\n".join(
                    (
                        'default_server = "primary"',
                        "",
                        "[servers.primary]",
                        'name = "Primary"',
                        'url = "http://primary:8096"',
                        'api_key = "abc123"',
                        "",
                        "[tvdb]",
                        'api_key = "  "',
                    )
                ),
                encoding="utf-8",
            )

            api_key = config.load_tvdb_api_key(servers_path)

        self.assertIsNone(api_key)

    def test_server_collection_first_two_requires_two_servers(self) -> None:
        collection = ServerCollection(
            default_server="primary",
            servers={
                "primary": ServerConfig(
                    key="primary",
                    name="Primary",
                    url="http://primary:8096",
                    api_key="abc123",
                ),
            },
        )

        with self.assertRaises(config.ConfigError):
            collection.first_two()


class AuditFindingsTests(unittest.TestCase):
    def test_audit_media_item_only_returns_actionable_findings(self) -> None:
        item = _make_item(
            title="Example",
            image_tags={},
        )

        findings = audit.audit_media_item(item)
        check_names = {finding.check_name for finding in findings}

        self.assertIn("missing_english_subtitles", check_names)
        self.assertIn("missing_backdrop", check_names)
        self.assertIn("missing_primary_image", check_names)
        self.assertIn("unknown_audio_codec", check_names)
        self.assertIn("unknown_video_codec", check_names)
        self.assertNotIn("missing_nfo", check_names)
        self.assertNotIn("hdr_video", check_names)


class MissingEpisodeNumberTests(unittest.TestCase):
    def test_flags_episode_with_no_episode_number(self) -> None:
        item = _make_item(
            "No Number",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=None,
        )

        finding = audit.missing_episode_number(item)

        self.assertIsNotNone(finding)
        self.assertEqual(finding.check_name, "missing_episode_number")

    def test_no_finding_when_episode_number_is_set(self) -> None:
        item = _make_item(
            "Numbered",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.missing_episode_number(item))

    def test_no_finding_for_movies(self) -> None:
        movie = _make_item("A Movie", is_movie=True, is_episode=False, episode_number=None)

        self.assertIsNone(audit.missing_episode_number(movie))


class MissingTvSeriesSeasonsTests(unittest.TestCase):
    def test_flags_only_internal_gaps_without_tvdb_data(self) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Season 3 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=3,
                episode_number=1,
            ),
        )

        findings = audit.missing_tv_series_seasons(items)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing seasons: 2.")

    def test_no_finding_when_local_seasons_are_the_last_ones_and_no_tvdb_data(self) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Season 2 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=2,
                episode_number=1,
            ),
        )

        findings = audit.missing_tv_series_seasons(items)

        self.assertEqual(findings, ())

    def test_flags_seasons_missing_after_the_last_local_season_using_tvdb_data(self) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Season 2 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=2,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Season 1 Episode"),
                (2, 1): _make_tvdb_episode(season_number=2, episode_number=1, name="Season 2 Episode"),
                (3, 1): _make_tvdb_episode(season_number=3, episode_number=1, name="Season 3 Episode"),
                (4, 1): _make_tvdb_episode(season_number=4, episode_number=1, name="Season 4 Episode"),
            }
        }

        findings = audit.missing_tv_series_seasons(items, aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing seasons: 3-4.")

    def test_no_finding_when_local_seasons_match_tvdb_data_exactly(self) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Season 1 Episode"),
            }
        }

        findings = audit.missing_tv_series_seasons(items, aired_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_season_zero_matches_tvdb_specials(self) -> None:
        items = (
            _make_item(
                "Special",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=0,
                episode_number=1,
            ),
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (0, 1): _make_tvdb_episode(season_number=0, episode_number=1, name="Special"),
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Season 1 Episode"),
            }
        }

        findings = audit.missing_tv_series_seasons(items, aired_positions)

        self.assertEqual(findings, ())

    def test_never_flags_season_zero_as_missing_even_when_tvdb_has_specials_but_none_are_local(
        self,
    ) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (0, 1): _make_tvdb_episode(season_number=0, episode_number=1, name="Special"),
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Season 1 Episode"),
            }
        }

        findings = audit.missing_tv_series_seasons(items, aired_positions)

        self.assertEqual(findings, ())

    def test_ignores_missing_season_zero_but_still_flags_other_missing_seasons(self) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (0, 1): _make_tvdb_episode(season_number=0, episode_number=1, name="Special"),
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Season 1 Episode"),
                (2, 1): _make_tvdb_episode(season_number=2, episode_number=1, name="Season 2 Episode"),
            }
        }

        findings = audit.missing_tv_series_seasons(items, aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing seasons: 2.")

    def test_falls_back_to_internal_gaps_for_a_series_not_on_tvdb(self) -> None:
        items = (
            _make_item(
                "Season 1 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Untracked Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Season 3 Episode",
                is_movie=False,
                is_episode=True,
                series_name="Untracked Series",
                season_number=3,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Season 1 Episode"),
            }
        }

        findings = audit.missing_tv_series_seasons(items, aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing seasons: 2.")


class MissingTvSeasonEpisodesTests(unittest.TestCase):
    def test_flags_only_internal_gaps_without_tvdb_data(self) -> None:
        items = (
            _make_item(
                "Episode 1",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Episode 3",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=3,
            ),
        )

        findings = audit.missing_tv_season_episodes(items)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing episodes: 2.")

    def test_no_finding_when_local_episodes_are_the_last_ones_and_no_tvdb_data(self) -> None:
        items = (
            _make_item(
                "Episode 1",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Episode 2",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=2,
            ),
        )

        findings = audit.missing_tv_season_episodes(items)

        self.assertEqual(findings, ())

    def test_flags_episodes_missing_after_the_last_local_episode_using_tvdb_data(self) -> None:
        items = (
            _make_item(
                "Episode 1",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Episode 2",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=2,
            ),
        )
        aired_positions = {
            "Example Series": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
                (1, 3): _make_tvdb_episode(season_number=1, episode_number=3, name="Episode 3"),
                (1, 4): _make_tvdb_episode(season_number=1, episode_number=4, name="Episode 4"),
            }
        }

        findings = audit.missing_tv_season_episodes(items, aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing episodes: 3-4.")

    def test_no_finding_when_local_episodes_match_tvdb_data_exactly(self) -> None:
        items = (
            _make_item(
                "Episode 1",
                is_movie=False,
                is_episode=True,
                series_name="Example Series",
                season_number=1,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Example Series": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
            }
        }

        findings = audit.missing_tv_season_episodes(items, aired_positions)

        self.assertEqual(findings, ())

    def test_falls_back_to_internal_gaps_for_a_series_not_on_tvdb(self) -> None:
        items = (
            _make_item(
                "Episode 1",
                is_movie=False,
                is_episode=True,
                series_name="Untracked Series",
                season_number=1,
                episode_number=1,
            ),
            _make_item(
                "Episode 3",
                is_movie=False,
                is_episode=True,
                series_name="Untracked Series",
                season_number=1,
                episode_number=3,
            ),
        )
        aired_positions = {
            "Example Series": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
            }
        }

        findings = audit.missing_tv_season_episodes(items, aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].message, "Missing episodes: 2.")


class MismatchedTvdbSeriesTests(unittest.TestCase):
    def _make_series_items(self, count: int, *, series_name: str = "Mismatched Show") -> tuple:
        return tuple(
            _make_item(
                f"Episode {number}",
                is_movie=False,
                is_episode=True,
                series_name=series_name,
                season_number=1,
                episode_number=number,
            )
            for number in range(1, count + 1)
        )

    def test_no_finding_when_local_episodes_mostly_match_tvdb(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 8)
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions)

        self.assertEqual(findings, ())

    def test_flags_series_whose_local_episodes_mostly_dont_match_tvdb(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(findings[0].check_name, "mismatched_tvdb_series")
        self.assertEqual(
            findings[0].message,
            "5 of 7 local episodes don't match any TheTVDB episode at their season/episode "
            "position - the matched TheTVDB series may be wrong.",
        )

    def test_no_finding_below_minimum_episode_threshold(self) -> None:
        items = self._make_series_items(4)
        aired_positions = {
            "Mismatched Show": {
                (1, 99): _make_tvdb_episode(season_number=1, episode_number=99, name="Unrelated"),
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_unmatched_ratio_is_below_threshold(self) -> None:
        items = self._make_series_items(10)
        aired_positions = {
            "Mismatched Show": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 7)
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_series_has_no_tvdb_data(self) -> None:
        items = self._make_series_items(7)

        findings = audit.mismatched_tvdb_series(items, aired_positions=None)

        self.assertEqual(findings, ())

    def test_ignores_season_zero_specials(self) -> None:
        items = self._make_series_items(7) + (
            _make_item(
                "Special",
                is_movie=False,
                is_episode=True,
                series_name="Mismatched Show",
                season_number=0,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Mismatched Show": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 8)
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions)

        self.assertEqual(findings, ())

    def test_does_not_log_season_zero_specials(self) -> None:
        items = self._make_series_items(7) + (
            _make_item(
                "Special",
                is_movie=False,
                is_episode=True,
                series_name="Mismatched Show",
                season_number=0,
                episode_number=1,
            ),
        )
        aired_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
            }
        }

        with self.assertLogs(audit.LOGGER, level="INFO") as log_context:
            audit.mismatched_tvdb_series(items, aired_positions)

        log_text = "\n".join(log_context.output)
        self.assertIn("checking 7 local episode(s)", log_text)
        self.assertNotIn("S00E01", log_text)

    def test_logs_per_episode_match_data_and_score(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
            }
        }
        dvd_positions = {
            "Mismatched Show": {
                (1, 3): _make_tvdb_episode(season_number=1, episode_number=3, name="Episode 3 DVD"),
            }
        }

        with self.assertLogs(audit.LOGGER, level="INFO") as log_context:
            findings = audit.mismatched_tvdb_series(items, aired_positions, dvd_positions)

        self.assertEqual(len(findings), 1)
        log_text = "\n".join(log_context.output)
        self.assertIn("S01E01 'Episode 1' -> matched (aired)", log_text)
        self.assertIn("S01E03 'Episode 3' -> matched (dvd)", log_text)
        self.assertIn("S01E04 'Episode 4' -> unmatched", log_text)
        self.assertIn("score 4/7 unmatched", log_text)
        self.assertIn("MISMATCH FLAGGED", log_text)

    def test_does_not_log_below_the_minimum_episode_threshold(self) -> None:
        items = self._make_series_items(4)
        aired_positions = {
            "Mismatched Show": {
                (1, 99): _make_tvdb_episode(season_number=1, episode_number=99, name="Unrelated"),
            }
        }

        with self.assertNoLogs(audit.LOGGER, level="INFO"):
            audit.mismatched_tvdb_series(items, aired_positions)

    def test_does_not_log_a_series_that_is_not_mismatched(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 8)
            }
        }

        with self.assertNoLogs(audit.LOGGER, level="INFO"):
            audit.mismatched_tvdb_series(items, aired_positions)

    def test_no_finding_when_local_episodes_mostly_match_dvd_order_only(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
            }
        }
        dvd_positions = {
            "Mismatched Show": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 8)
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_flags_series_unmatched_in_both_aired_and_dvd_order(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
            }
        }
        dvd_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
            }
        }

        findings = audit.mismatched_tvdb_series(items, aired_positions, dvd_positions)

        self.assertEqual(len(findings), 1)
        self.assertEqual(
            findings[0].message,
            "5 of 7 local episodes don't match any TheTVDB episode at their season/episode "
            "position - the matched TheTVDB series may be wrong.",
        )

    def test_audit_library_items_suppresses_tvdb_gap_checks_for_a_mismatched_series(self) -> None:
        items = self._make_series_items(7)
        aired_positions = {
            "Mismatched Show": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
                (1, 20): _make_tvdb_episode(season_number=1, episode_number=20, name="Episode 20"),
            }
        }

        findings = audit.audit_library_items(items, aired_positions)

        check_names = {finding.check_name for finding in findings}
        self.assertIn("mismatched_tvdb_series", check_names)
        self.assertNotIn("missing_episodes", check_names)


class BestMatchingTvdbSeriesTests(unittest.TestCase):
    def _make_series_items(self, count: int, *, series_name: str = "Mismatched Show") -> tuple:
        return tuple(
            _make_item(
                f"Episode {number}",
                is_movie=False,
                is_episode=True,
                series_name=series_name,
                season_number=1,
                episode_number=number,
            )
            for number in range(1, count + 1)
        )

    def test_returns_the_candidate_that_confidently_matches(self) -> None:
        items = self._make_series_items(7)
        candidates = {
            "wrong-id": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
            },
            "right-id": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 8)
            },
        }

        best_id = audit.best_matching_tvdb_series(items, "Mismatched Show", candidates)

        self.assertEqual(best_id, "right-id")

    def test_returns_none_when_no_candidate_is_a_confident_match(self) -> None:
        items = self._make_series_items(7)
        candidates = {
            "wrong-id": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
                (1, 2): _make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),
            },
        }

        best_id = audit.best_matching_tvdb_series(items, "Mismatched Show", candidates)

        self.assertIsNone(best_id)

    def test_returns_none_when_series_has_no_local_episodes(self) -> None:
        candidates = {
            "right-id": {
                (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),
            },
        }

        best_id = audit.best_matching_tvdb_series((), "Mismatched Show", candidates)

        self.assertIsNone(best_id)

    def test_prefers_the_candidate_with_the_fewest_unmatched_episodes(self) -> None:
        items = self._make_series_items(10)
        candidates = {
            "decent-id": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 10)
            },
            "perfect-id": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 11)
            },
        }

        best_id = audit.best_matching_tvdb_series(items, "Mismatched Show", candidates)

        self.assertEqual(best_id, "perfect-id")


class EpisodeOrderingTests(unittest.TestCase):
    def test_no_finding_when_local_title_matches_aired_order(self) -> None:
        item = _make_item(
            "Aired Title",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=3,
        )
        aired_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title"),),
            }
        }
        dvd_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="DVD Title"),),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_title_differs_from_aired_order_only_by_apostrophe(
        self,
    ) -> None:
        item = _make_item(
            "Lovers Walk",
            is_movie=False,
            is_episode=True,
            series_name="Buffy the Vampire Slayer",
            season_number=3,
            episode_number=8,
        )
        aired_positions = {
            "Buffy the Vampire Slayer": {
                (3, 8): (
                    _make_tvdb_episode(season_number=3, episode_number=8, name="Lover's Walk"),
                ),
            }
        }
        dvd_positions = {
            "Buffy the Vampire Slayer": {
                (3, 8): (
                    _make_tvdb_episode(season_number=3, episode_number=8, name="Lover's Walk"),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_title_differs_from_aired_order_only_by_accents(
        self,
    ) -> None:
        item = _make_item(
            "Deguello",
            is_movie=False,
            is_episode=True,
            series_name="Show",
            season_number=1,
            episode_number=1,
        )
        aired_positions = {
            "Show": {
                (1, 1): (_make_tvdb_episode(season_number=1, episode_number=1, name="Degüello"),),
            }
        }
        dvd_positions = {
            "Show": {
                (1, 1): (_make_tvdb_episode(season_number=1, episode_number=1, name="Degüello"),),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_title_differs_from_aired_order_only_by_dotted_abbreviation(
        self,
    ) -> None:
        item = _make_item(
            "Nothing Good Happens After 2 A.M.",
            is_movie=False,
            is_episode=True,
            series_name="Show",
            season_number=1,
            episode_number=18,
        )
        aired_positions = {
            "Show": {
                (1, 18): (
                    _make_tvdb_episode(
                        season_number=1,
                        episode_number=18,
                        name="Nothing Good Happens After 2 AM",
                    ),
                ),
            }
        }
        dvd_positions = {
            "Show": {
                (1, 18): (
                    _make_tvdb_episode(
                        season_number=1,
                        episode_number=18,
                        name="Nothing Good Happens After 2 AM",
                    ),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_title_differs_from_aired_order_only_by_hyphen(
        self,
    ) -> None:
        item = _make_item(
            "The Autumn of Break-Ups",
            is_movie=False,
            is_episode=True,
            series_name="Show",
            season_number=8,
            episode_number=5,
        )
        aired_positions = {
            "Show": {
                (8, 5): (
                    _make_tvdb_episode(
                        season_number=8, episode_number=5, name="The Autumn of Breakups"
                    ),
                ),
            }
        }
        dvd_positions = {
            "Show": {
                (8, 5): (
                    _make_tvdb_episode(
                        season_number=8, episode_number=5, name="The Autumn of Breakups"
                    ),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_title_differs_from_aired_order_only_by_word_spacing(
        self,
    ) -> None:
        item = _make_item(
            "Welcome to the Doll House",
            is_movie=False,
            is_episode=True,
            series_name="Show",
            season_number=6,
            episode_number=6,
        )
        aired_positions = {
            "Show": {
                (6, 6): (
                    _make_tvdb_episode(
                        season_number=6, episode_number=6, name="Welcome to the Dollhouse"
                    ),
                ),
            }
        }
        dvd_positions = {
            "Show": {
                (6, 6): (
                    _make_tvdb_episode(
                        season_number=6, episode_number=6, name="Welcome to the Dollhouse"
                    ),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_local_title_differs_from_aired_order_only_by_roman_part_number(
        self,
    ) -> None:
        item = _make_item(
            "The Savage Time: Part II",
            is_movie=False,
            is_episode=True,
            series_name="Show",
            season_number=1,
            episode_number=25,
        )
        aired_positions = {
            "Show": {
                (1, 25): (
                    _make_tvdb_episode(season_number=1, episode_number=25, name="The Savage Time (2)"),
                ),
            }
        }
        dvd_positions = {
            "Show": {
                (1, 25): (
                    _make_tvdb_episode(season_number=1, episode_number=25, name="The Savage Time (2)"),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_flags_when_local_title_matches_neither_ordering(self) -> None:
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=3,
        )
        aired_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title"),),
            }
        }
        dvd_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="DVD Title"),),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.check_name, "aired_dvd_order_mismatch")
        self.assertEqual(finding.category, AuditCategory.EPISODE_ORDER)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Aired Title", finding.message)
        self.assertIn("DVD Title", finding.message)
        self.assertIn("matches neither", finding.message)
        self.assertIs(finding.media_item, item)

    def test_no_finding_when_local_title_matches_dvd_order_instead(self) -> None:
        """A series correctly organized end-to-end in DVD order disagrees with
        aired order at every single episode - that's expected, not a
        discrepancy worth flagging, so a local title matching DVD order
        instead of aired order is not reported at all.
        """
        item = _make_item(
            "DVD Title",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=3,
        )
        aired_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title"),),
            }
        }
        dvd_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="DVD Title"),),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_dvd_order_unavailable_at_that_position(self) -> None:
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=3,
        )
        aired_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title"),),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, {})

        self.assertEqual(findings, ())

    def test_no_finding_when_position_missing_from_aired_order(self) -> None:
        item = _make_item(
            "Unmapped",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=99,
        )
        dvd_positions = {
            "Example Series": {
                (1, 99): (
                    _make_tvdb_episode(season_number=1, episode_number=99, name="Unmapped DVD"),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], {}, dvd_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_title_matches_any_candidate_sharing_a_position(self) -> None:
        """Regression test: several same-named TheTVDB series can each independently
        number their own "Season 1, Episode 1" (e.g. a decades-old show and a
        from-scratch modern revival sharing one name). Merging them must not
        let one candidate's episode silently overwrite another's at the same
        position - the local title should match if it agrees with any one of
        them.
        """
        item = _make_item(
            "Space Babies",
            is_movie=False,
            is_episode=True,
            series_name="Doctor Who",
            season_number=1,
            episode_number=1,
        )
        aired_positions = {
            "Doctor Who": {
                (1, 1): (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="An Unearthly Child"),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Space Babies"),
                ),
            }
        }
        dvd_positions = {
            "Doctor Who": {
                (1, 1): (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="An Unearthly Child"),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Space Babies"),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_lists_every_distinct_candidate_title_when_none_match(self) -> None:
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            series_name="Doctor Who",
            season_number=1,
            episode_number=1,
        )
        aired_positions = {
            "Doctor Who": {
                (1, 1): (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="An Unearthly Child"),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                ),
            }
        }
        dvd_positions = {
            "Doctor Who": {
                (1, 1): (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="An Unearthly Child"),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(len(findings), 1)
        message = findings[0].message
        self.assertIn('"An Unearthly Child"', message)
        self.assertIn('"Rose"', message)

    def test_no_finding_when_only_candidate_title_is_untranslated(self) -> None:
        """Regression test: TheTVDB silently falls back to a series' original-language
        name for an episode with no recorded English translation - there's no
        way to tell whether that untranslated name matches the local title or
        not, so it must not be treated as a mismatch.
        """
        item = _make_item(
            "Big Sword",
            is_movie=False,
            is_episode=True,
            series_name="Claymore",
            season_number=1,
            episode_number=1,
        )
        aired_positions = {
            "Claymore": {
                (1, 1): (
                    _make_tvdb_episode(
                        season_number=1, episode_number=1, name="大剣 -クレイモア-"
                    ),
                ),
            }
        }
        dvd_positions = {
            "Claymore": {
                (1, 1): (
                    _make_tvdb_episode(
                        season_number=1, episode_number=1, name="大剣 -クレイモア-"
                    ),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_flags_using_only_english_candidate_when_mixed_with_untranslated(self) -> None:
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            series_name="Claymore",
            season_number=1,
            episode_number=1,
        )
        aired_positions = {
            "Claymore": {
                (1, 1): (
                    _make_tvdb_episode(
                        season_number=1, episode_number=1, name="大剣 -クレイモア-"
                    ),
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Silver-Eyed Slayer"),
                ),
            }
        }
        dvd_positions = {
            "Claymore": {
                (1, 1): (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Silver-Eyed Slayer"),
                ),
            }
        }

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(len(findings), 1)
        message = findings[0].message
        self.assertIn('"Silver-Eyed Slayer"', message)
        self.assertNotIn("クレイモア", message)

    def test_no_finding_when_combined_title_matches_multi_episode_range(self) -> None:
        """A filename's SxxEyy-Ezz marker implies the file covers episodes yy
        through zz, so the metadata title is compared against all of their
        TheTVDB titles joined together, not just the first one.
        """
        item = _make_item(
            "Title Five / Title Six / Title Seven",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=5,
        )
        aired_positions = {
            "Show": {
                (1, 5): (_make_tvdb_episode(season_number=1, episode_number=5, name="Title Five"),),
                (1, 6): (_make_tvdb_episode(season_number=1, episode_number=6, name="Title Six"),),
                (1, 7): (
                    _make_tvdb_episode(season_number=1, episode_number=7, name="Title Seven"),
                ),
            }
        }
        dvd_positions = aired_positions

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_flags_combined_range_with_range_label_and_joined_titles(self) -> None:
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=5,
        )
        aired_positions = {
            "Show": {
                (1, 5): (_make_tvdb_episode(season_number=1, episode_number=5, name="Title Five"),),
                (1, 6): (_make_tvdb_episode(season_number=1, episode_number=6, name="Title Six"),),
                (1, 7): (
                    _make_tvdb_episode(season_number=1, episode_number=7, name="Title Seven"),
                ),
            }
        }
        dvd_positions = aired_positions

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(len(findings), 1)
        message = findings[0].message
        self.assertIn("S01E05-E07", message)
        self.assertIn('"Title Five / Title Six / Title Seven"', message)

    def test_no_finding_when_one_position_in_the_range_has_no_data(self) -> None:
        """Regression test: a partial range - some but not all of its episodes
        have TheTVDB data - can't confidently be compared at all, so it must
        not be treated as either a match or a mismatch.
        """
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=5,
        )
        aired_positions = {
            "Show": {
                (1, 5): (_make_tvdb_episode(season_number=1, episode_number=5, name="Title Five"),),
                (1, 7): (
                    _make_tvdb_episode(season_number=1, episode_number=7, name="Title Seven"),
                ),
            }
        }
        dvd_positions = aired_positions

        findings = audit.audit_episode_ordering([item], aired_positions, dvd_positions)

        self.assertEqual(findings, ())

    def test_ignores_movies_and_episodes_without_series_name_or_numbers(self) -> None:
        movie = _make_item("A Movie", is_movie=True, is_episode=False)
        episode_without_series = _make_item(
            "No Series",
            is_movie=False,
            is_episode=True,
            season_number=1,
            episode_number=1,
        )
        episode_without_numbers = _make_item(
            "No Numbers",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
        )

        findings = audit.audit_episode_ordering(
            [movie, episode_without_series, episode_without_numbers],
            {},
            {},
        )

        self.assertEqual(findings, ())


class ExpectedEpisodeTitleFromFilenameTests(unittest.TestCase):
    def test_returns_title_following_season_episode_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_strips_release_tags_and_dot_separators(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking.Bad.S01E01.Ozymandias.1080p.WEB-DL.x264-GROUP.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_does_not_truncate_title_ending_in_bare_tag_word(self) -> None:
        item = _make_item(
            title="Spider in the Web",
            is_movie=False,
            is_episode=True,
            path=Path("Babylon 5 - S02E06 - Spider in the Web.mp4"),
            season_number=2,
            episode_number=6,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Spider in the Web")

    def test_strips_bare_tag_word_followed_by_further_release_info(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show.S01E01.Pilot.WEB.x264-GROUP.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Pilot")

    def test_handles_multi_episode_range_with_dash(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E02-E03 - Ozymandias.mkv"),
            season_number=1,
            episode_number=2,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_handles_multi_episode_range_without_dash(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E02E03 - Ozymandias.mkv"),
            season_number=1,
            episode_number=2,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_preserves_trailing_parenthesized_copy_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias (1).mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias (1)")

    def test_preserves_leading_parenthesized_title_text(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Lost Girl - S01E12 - (Dis)Members Only.mkv"),
            season_number=1,
            episode_number=12,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "(Dis)Members Only")

    def test_does_not_truncate_bare_tag_word_followed_by_copy_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Lexx - S02E16 - The Web (1).mkv"),
            season_number=2,
            episode_number=16,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "The Web (1)")

    def test_does_not_truncate_bare_tag_word_buried_mid_title(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Curious George - S02E31-E32 - Curious George, Web Master + The Big Sleepy.mp4"),
            season_number=2,
            episode_number=31,
        )

        self.assertEqual(
            media.expected_episode_title_from_filename(item),
            "Curious George, Web Master + The Big Sleepy",
        )

    def test_strips_bare_release_group_name_after_source_and_codec_tags(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Highlander - S01E01 - The Gathering NTSC DVD x264 JCH.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "The Gathering")

    def test_does_not_strip_bare_trailing_word_after_only_one_tag(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Real Title x264 Weird.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(
            media.expected_episode_title_from_filename(item),
            "Real Title x264 Weird",
        )

    def test_strips_dot_split_audio_channel_tag_before_bare_group_name(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Reacher - S02E08 - Fly Boy 1080p REPACK BluRay DDP5.1.mkv"),
            season_number=2,
            episode_number=8,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Fly Boy")

    def test_strips_dot_split_channel_tag_sandwiched_before_a_hyphenated_group(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Reacher - S02E08 - Fly Boy 1080p REPACK BluRay DDP5.1.x264-NTb.mkv"),
            season_number=2,
            episode_number=8,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Fly Boy")

    def test_strips_single_tag_carrying_a_hyphenated_group_suffix(self) -> None:
        # Regression test for the "-GROUPNAME" suffix check silently
        # matching the wrong regex capture group after a later edit added
        # another parenthesized alternative earlier in the pattern.
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Real Title x264-NTb.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Real Title")

    def test_strips_parenthesized_tag_group_and_trailing_bare_tag(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Ted Lasso - S02E01 (1080p AV1) - SDR.mkv"),
            season_number=2,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))

    def test_returns_none_when_filename_omits_episode_title(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))

    def test_returns_none_when_filename_has_no_season_episode_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))

    def test_returns_none_for_non_episode_items(self) -> None:
        item = _make_item(
            title="Alien",
            path=Path("Alien - S01E01 - Something.mkv"),
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))


class ExpectedEpisodeNumbersFromFilenameTests(unittest.TestCase):
    def test_returns_single_number_for_ordinary_single_episode_file(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (1,))

    def test_returns_inclusive_range_for_dash_separated_marker(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5, 6, 7))

    def test_returns_inclusive_range_for_marker_without_dash(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05E07 - Combined.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5, 6, 7))

    def test_returns_full_range_when_episode_number_is_the_markers_last_number(self) -> None:
        """Regression test: a combined-episode file's Jellyfin item doesn't
        always carry the marker's *first* episode number - here the item's
        own IndexNumber is 38, but the filename's marker is "S01E37-E38".
        The range must still be found by searching for the marker
        generically and checking whether 38 is one of its numbers, not by
        assuming the item's own number is the marker's starting number.
        """
        item = _make_item(
            title="Top Cow + School of Otis",
            is_movie=False,
            is_episode=True,
            path=Path(
                "Back at the Barnyard - S01E37-E38 - Top Cow + School of Otis.mkv"
            ),
            season_number=1,
            episode_number=38,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (37, 38))

    def test_returns_full_range_when_episode_number_is_a_middle_number(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E06-E07 - Combined.mkv"),
            season_number=1,
            episode_number=6,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5, 6, 7))

    def test_returns_none_when_episode_number_is_not_part_of_any_marker(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            season_number=1,
            episode_number=99,
        )

        self.assertIsNone(media.expected_episode_numbers_from_filename(item))

    def test_does_not_treat_an_unrelated_decoy_number_as_part_of_the_range(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05 - Vol.02.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5,))

    def test_returns_none_when_filename_has_no_season_episode_marker(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_numbers_from_filename(item))

    def test_returns_none_for_non_episode_items(self) -> None:
        item = _make_item(
            title="Alien",
            path=Path("Alien - S01E01 - Something.mkv"),
        )

        self.assertIsNone(media.expected_episode_numbers_from_filename(item))


class GetDisplayEpisodeNumberTests(unittest.TestCase):
    def test_returns_range_for_multi_episode_filename(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.get_display_episode_number(item), "5-7")

    def test_returns_bare_number_for_ordinary_single_episode_file(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.get_display_episode_number(item), "1")

    def test_returns_bare_number_when_filename_has_no_recognizable_marker(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.get_display_episode_number(item), "1")

    def test_returns_empty_string_when_no_episode_number(self) -> None:
        item = _make_item(
            title="No Numbers",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
        )

        self.assertEqual(media.get_display_episode_number(item), "")


class CompareCsvFilesExcelGuardTests(unittest.TestCase):
    """reports/generator.py guards a hyphenated Episode range (e.g. "5-7")

    against Excel's automatic type detection misreading it as a date, by
    prefixing it with a leading apostrophe. compare_csv_files.py reads
    those same audit CSVs back in, so it must strip that guard off before
    using the value for anything, and re-apply it to whatever ends up in
    its own diff output.
    """

    def test_read_rows_strips_excel_guard_from_episode_column(self) -> None:
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "audit.csv"
            csv_path.write_text(
                "Library,Path,Episode\nTV Shows,/media/show.mkv,'5-7\n",
                encoding="utf-8",
            )

            header, rows = compare_csv_files.read_rows(csv_path)

        self.assertEqual(header, ("Library", "Path", "Episode"))
        self.assertEqual(rows, (("TV Shows", "/media/show.mkv", "5-7"),))

    def test_read_rows_leaves_plain_episode_numbers_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "audit.csv"
            csv_path.write_text(
                "Library,Path,Episode\nTV Shows,/media/show.mkv,5\n",
                encoding="utf-8",
            )

            _, rows = compare_csv_files.read_rows(csv_path)

        self.assertEqual(rows, (("TV Shows", "/media/show.mkv", "5"),))

    def test_combine_row_reapplies_excel_guard_for_matching_episode_range(self) -> None:
        header = ("Library", "Path", "Episode")
        row_a = ("TV Shows", "/media/a/show.mkv", "5-7")
        row_b = ("TV Shows", "/media/b/show.mkv", "5-7")

        combined = compare_csv_files.combine_row(header, "show.mkv", row_a, row_b)

        self.assertEqual(combined, ("TV Shows", "show.mkv", "'5-7"))

    def test_write_diff_csv_round_trips_episode_range_through_the_excel_guard(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            csv_a = temp_dir_path / "left_audit.csv"
            csv_b = temp_dir_path / "right_audit.csv"
            output_path = temp_dir_path / "diffs.csv"

            csv_a.write_text(
                "Library,Path,Episode,Missing Subtitles\n"
                "TV Shows,/left/show.mkv,'5-7,Yes\n",
                encoding="utf-8",
            )
            csv_b.write_text(
                "Library,Path,Episode,Missing Subtitles\n"
                "TV Shows,/right/show.mkv,'5-7,No\n",
                encoding="utf-8",
            )

            diff_count = compare_csv_files.write_diff_csv(csv_a, csv_b, output_path)
            with output_path.open("r", encoding="utf-8", newline="") as handle:
                # write_diff_csv's own output has a leading "left|right"
                # identifier line before the real header, unlike an audit
                # CSV - read it with a plain csv.reader rather than
                # read_rows(), which assumes the first line is the header.
                output_rows = [tuple(row) for row in csv.reader(handle)]

        self.assertEqual(diff_count, 1)
        header = output_rows[1]
        data_row = output_rows[2]
        episode_index = header.index("Episode")
        # The Episode value still carries its Excel guard in the raw
        # output file, same as a real audit CSV would.
        self.assertEqual(data_row[episode_index], "'5-7")


class ExpectedEpisodeTitleFromStreamTitlesTests(unittest.TestCase):
    def test_extracts_title_from_audio_track_carrying_original_release_name(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            season_number=1,
            episode_number=7,
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Firefly.S01E07.Jaynestown.1080p.BRRip.AC3.x264-LESS",
                ),
            ),
        )

        self.assertEqual(
            media.expected_episode_title_from_stream_titles(item),
            "Jaynestown",
        )

    def test_strips_space_separated_release_group_name_from_stream_title(self) -> None:
        item = _make_item(
            title="Wrong Title",
            is_movie=False,
            is_episode=True,
            path=Path("Highlander - S01E01 - The Gathering.mkv"),
            season_number=1,
            episode_number=1,
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Highlander.S01E01.The Gathering NTSC DVD x264 JCH",
                ),
            ),
        )

        self.assertEqual(
            media.expected_episode_title_from_stream_titles(item),
            "The Gathering",
        )

    def test_returns_none_for_a_technical_info_only_stream_title(self) -> None:
        # Some tools generate stream titles like "Show (Year) - SxxExx
        # (Resolution Codec) - Range" with no episode title text at all - the
        # marker still matches, but nothing real should follow it.
        item = _make_item(
            title="Goodbye Earl",
            is_movie=False,
            is_episode=True,
            path=Path("Ted Lasso - S02E01 - Goodbye Earl.mkv"),
            season_number=2,
            episode_number=1,
            video_track=VideoTrack(
                codec="av1",
                width=1920,
                height=960,
                bitrate=1663,
                hdr=False,
                video_range="SDR",
                title="Ted Lasso (2020) - S02E01 (1080p AV1) - SDR",
            ),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="opus",
                    channels=6,
                    title="Surround Sound 5.1 (Opus) - English - Default",
                ),
            ),
        )

        self.assertIsNone(media.expected_episode_title_from_stream_titles(item))

    def test_extracts_title_from_video_track_when_no_audio_track_matches(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            season_number=1,
            episode_number=7,
            video_track=VideoTrack(
                codec="h264",
                width=1920,
                height=1080,
                bitrate=3210,
                hdr=False,
                video_range="SDR",
                title="Firefly.S01E07.Jaynestown.1080p.BRRip.AC3.x264-LESS",
            ),
            audio_tracks=(
                AudioTrack(language="eng", codec="ac3", channels=6, title="English"),
            ),
        )

        self.assertEqual(
            media.expected_episode_title_from_stream_titles(item),
            "Jaynestown",
        )

    def test_checks_video_track_before_audio_tracks(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            season_number=1,
            episode_number=7,
            video_track=VideoTrack(
                codec="h264",
                width=1920,
                height=1080,
                bitrate=3210,
                hdr=False,
                video_range="SDR",
                title="Firefly.S01E07.From The Video Track",
            ),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Firefly.S01E07.From The Audio Track",
                ),
            ),
        )

        self.assertEqual(
            media.expected_episode_title_from_stream_titles(item),
            "From The Video Track",
        )

    def test_skips_earlier_audio_tracks_without_a_marker(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            season_number=1,
            episode_number=7,
            audio_tracks=(
                AudioTrack(language="eng", codec="ac3", channels=6, title="Commentary"),
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Firefly.S01E07.Jaynestown.1080p.BRRip.AC3.x264-LESS",
                ),
            ),
        )

        self.assertEqual(
            media.expected_episode_title_from_stream_titles(item),
            "Jaynestown",
        )

    def test_returns_none_when_no_track_has_a_matching_marker(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            season_number=1,
            episode_number=7,
            audio_tracks=(
                AudioTrack(language="eng", codec="ac3", channels=6, title="English"),
            ),
        )

        self.assertIsNone(media.expected_episode_title_from_stream_titles(item))

    def test_returns_none_when_no_tracks_have_titles(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            season_number=1,
            episode_number=7,
            audio_tracks=(
                AudioTrack(language="eng", codec="ac3", channels=6, title=None),
            ),
        )

        self.assertIsNone(media.expected_episode_title_from_stream_titles(item))

    def test_returns_none_for_non_episode_items(self) -> None:
        item = _make_item(
            title="Alien",
            path=Path("Alien.mkv"),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Alien.S01E01.Something.mkv",
                ),
            ),
        )

        self.assertIsNone(media.expected_episode_title_from_stream_titles(item))


class MismatchedEpisodeFilenameTitleTests(unittest.TestCase):
    def test_flags_episode_when_metadata_title_differs_from_filename(self) -> None:
        item = _make_item(
            title="Wrong Title",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        finding = audit.mismatched_episode_filename_title(item)

        self.assertIsNotNone(finding)
        self.assertEqual(finding.check_name, "mismatched_episode_filename_title")
        self.assertEqual(finding.category, AuditCategory.METADATA)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Ozymandias", finding.message)
        self.assertIn("Wrong Title", finding.message)

    def test_does_not_flag_matching_title_case_insensitively(self) -> None:
        item = _make_item(
            title="ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_matching_title_with_punctuation_differences(self) -> None:
        item = _make_item(
            title="Ozymandias!",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_abbreviated_title_with_embedded_periods(self) -> None:
        item = _make_item(
            title="S.W.A.T.",
            is_movie=False,
            is_episode=True,
            path=Path("Show.S01E01.S.W.A.T.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_curly_versus_straight_apostrophe(self) -> None:
        item = _make_item(
            title="Passion’s Harvest and a Sheldocracy",
            is_movie=False,
            is_episode=True,
            path=Path("Young Sheldon - S06E03 - Passion's Harvest and a Sheldocracy.mkv"),
            series_name="Young Sheldon",
            season_number=6,
            episode_number=3,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_apostrophe_dropped_entirely_versus_kept(self) -> None:
        item = _make_item(
            title="Lover's Walk",
            is_movie=False,
            is_episode=True,
            path=Path("Buffy the Vampire Slayer - S03E08 - Lovers Walk.mkv"),
            series_name="Buffy the Vampire Slayer",
            season_number=3,
            episode_number=8,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_accented_versus_unaccented_letters(self) -> None:
        item = _make_item(
            title="Degüello",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Deguello.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_dotted_abbreviation_versus_undotted(self) -> None:
        item = _make_item(
            title="Nothing Good Happens After 2 A.M.",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E18 - Nothing Good Happens After 2 AM.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=18,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_hyphenated_compound_versus_joined(self) -> None:
        item = _make_item(
            title="The Autumn of Break-Ups",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S08E05 - The Autumn of Breakups.mkv"),
            series_name="Show",
            season_number=8,
            episode_number=5,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_split_versus_joined_compound_word(self) -> None:
        item = _make_item(
            title="Welcome to the Doll House",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S06E06 - Welcome to the Dollhouse.mkv"),
            series_name="Show",
            season_number=6,
            episode_number=6,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_roman_numeral_versus_arabic_numeral_in_parens(self) -> None:
        item = _make_item(
            title="Poltergeist (I)",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Poltergeist (1).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_no_disambiguator(self) -> None:
        item = _make_item(
            title="Poltergeist",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Poltergeist (1).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_part_word(self) -> None:
        item = _make_item(
            title="Poltergeist, Part One",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Poltergeist (1).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_part_digit(self) -> None:
        item = _make_item(
            title="Poltergeist Part 2",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E02 - Poltergeist (2).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=2,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_part_roman_numeral(self) -> None:
        item = _make_item(
            title="The Savage Time: Part II",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E25 - The Savage Time (2).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=25,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_year_in_parens(self) -> None:
        item = _make_item(
            title="It (2016)",
            is_movie=True,
            is_episode=False,
            path=Path("It (2016).mkv"),
            year=2016,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_duplicated_multi_part_title_joined_by_slash(self) -> None:
        item = _make_item(
            title=(
                "The More You Moe, The Moe You Know (1) / "
                "The More You Moe, The Moe You Know (2)"
            ),
            is_movie=False,
            is_episode=True,
            path=Path(
                "Adventure Time - S07E14-E15 - The More You Moe, The Moe You Know.mkv"
            ),
            series_name="Adventure Time",
            season_number=7,
            episode_number=14,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_comma_versus_slash_joined_titles(self) -> None:
        item = _make_item(
            title="Title A / Title B",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Title A, Title B.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_still_flags_genuinely_different_titles(self) -> None:
        item = _make_item(
            title="Completely Different Title",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Title A, Title B.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNotNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_hyphenated_versus_space_separated_title(self) -> None:
        item = _make_item(
            title="Spider-Man",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Spider Man.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_exclamation_point_versus_no_punctuation(self) -> None:
        item = _make_item(
            title="Wait, What!",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Wait What.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_pt_abbreviation_versus_paren_number(self) -> None:
        item = _make_item(
            title="Poltergeist Pt. 2",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E02 - Poltergeist (2).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=2,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_leading_article_difference(self) -> None:
        item = _make_item(
            title="The Murdering Cowboy",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Murdering Cowboy.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_mid_title_article_difference(self) -> None:
        item = _make_item(
            title="A Trip to the Moon",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Trip to Moon.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_british_versus_american_spelling(self) -> None:
        item = _make_item(
            title="Encyclopaedia",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Encyclopedia.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_british_spelling_combined_with_leading_article(self) -> None:
        item = _make_item(
            title="The Colour of Money",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Color of Money.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_title_with_embedded_period_and_no_space(self) -> None:
        item = _make_item(
            title="Mr.Robot",
            is_movie=False,
            is_episode=True,
            path=Path("Show S01E01 Mr Robot.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_ampersand_versus_and(self) -> None:
        item = _make_item(
            title="Salt & Pepper",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Salt and Pepper.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_plus_versus_slash(self) -> None:
        item = _make_item(
            title="Trick / Treat",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Trick + Treat.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_ellipsis_versus_literal_periods(self) -> None:
        item = _make_item(
            title="Once Upon a Time…",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Once Upon a Time....mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_when_filename_omits_episode_title(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_movies(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Alien - S01E01 - Something.mkv"),
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))


class MismatchedEpisodeStreamTitleTests(unittest.TestCase):
    def test_flags_episode_when_metadata_title_differs_from_stream_title(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            series_name="Firefly",
            season_number=1,
            episode_number=7,
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Firefly.S01E07.Jaynestown.1080p.BRRip.AC3.x264-LESS",
                ),
            ),
        )

        finding = audit.mismatched_episode_stream_title(item)

        self.assertIsNotNone(finding)
        self.assertEqual(finding.check_name, "mismatched_episode_stream_title")
        self.assertEqual(finding.category, AuditCategory.METADATA)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Jaynestown", finding.message)
        self.assertIn("Safe", finding.message)

    def test_does_not_flag_matching_title_case_insensitively(self) -> None:
        item = _make_item(
            title="jaynestown",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Jaynestown.mkv"),
            series_name="Firefly",
            season_number=1,
            episode_number=7,
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=6,
                    title="Firefly.S01E07.Jaynestown.1080p.BRRip.AC3.x264-LESS",
                ),
            ),
        )

        self.assertIsNone(audit.mismatched_episode_stream_title(item))

    def test_does_not_flag_when_no_track_has_a_stream_title(self) -> None:
        item = _make_item(
            title="Safe",
            is_movie=False,
            is_episode=True,
            path=Path("Firefly - S01E07 - Safe.mkv"),
            series_name="Firefly",
            season_number=1,
            episode_number=7,
        )

        self.assertIsNone(audit.mismatched_episode_stream_title(item))


class ExpectedMovieTitleFromFilenameTests(unittest.TestCase):
    def test_returns_title_preceding_year_marker(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025).mp4"),
            year=2025,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "Installing The Dock")

    def test_ignores_edition_suffix_after_year(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025) - Timelapse.mp4"),
            year=2025,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "Installing The Dock")

    def test_strips_release_tags_and_dot_separators(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("The.Matrix.1999.1080p.WEB-DL.x264-GROUP.mkv"),
            year=1999,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "The Matrix")

    def test_prefers_parenthesized_year_when_title_contains_matching_number(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Fantasia 2000 (2000).mkv"),
            year=2000,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "Fantasia 2000")

    def test_preserves_leading_parenthesized_title_text(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("(500) Days of Summer (2009).mp4"),
            year=2009,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "(500) Days of Summer")

    def test_returns_none_when_filename_has_no_year_marker(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock.mp4"),
            year=2025,
        )

        self.assertIsNone(media.expected_movie_title_from_filename(item))

    def test_returns_none_when_year_is_missing(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025).mp4"),
            year=None,
        )

        self.assertIsNone(media.expected_movie_title_from_filename(item))

    def test_returns_none_for_non_movie_items(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show (2025).mkv"),
            year=2025,
        )

        self.assertIsNone(media.expected_movie_title_from_filename(item))


class MismatchedMovieFilenameTitleTests(unittest.TestCase):
    def test_flags_movie_when_metadata_title_differs_from_filename(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025) - Timelapse.mp4"),
            year=2025,
        )

        finding = audit.mismatched_movie_filename_title(item)

        self.assertIsNotNone(finding)
        self.assertEqual(finding.check_name, "mismatched_movie_filename_title")
        self.assertEqual(finding.category, AuditCategory.METADATA)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Installing The Dock", finding.message)
        self.assertIn("Wrong Title", finding.message)

    def test_does_not_flag_matching_title_case_insensitively(self) -> None:
        item = _make_item(
            title="installing the dock",
            path=Path("Installing The Dock (2025).mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_ampersand_versus_and(self) -> None:
        item = _make_item(
            title="Salt & Pepper",
            path=Path("Salt and Pepper (2025).mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_plus_versus_slash(self) -> None:
        item = _make_item(
            title="Trick / Treat",
            path=Path("Trick + Treat (2025).mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_when_filename_omits_year(self) -> None:
        item = _make_item(
            title="Installing The Dock",
            path=Path("Installing The Dock.mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_episodes(self) -> None:
        item = _make_item(
            title="Wrong Title",
            is_movie=False,
            is_episode=True,
            path=Path("Show (2025).mkv"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))


class JellyfinArtworkHelperTests(unittest.TestCase):
    def test_jellyfin_artwork_helpers_detect_non_empty_tags(self) -> None:
        item = _make_item(
            image_tags={
                "Primary": "primary-tag",
                "Backdrop": "backdrop-tag",
                "Thumb": "thumb-tag",
            }
        )

        self.assertTrue(media.has_jellyfin_primary_image(item))
        self.assertTrue(media.has_jellyfin_backdrop(item))
        self.assertTrue(media.has_jellyfin_thumb(item))

    def test_jellyfin_artwork_helpers_reject_missing_or_blank_tags(self) -> None:
        item = _make_item(
            image_tags={
                "Primary": "   ",
                "Backdrop": "",
            }
        )

        self.assertFalse(media.has_jellyfin_primary_image(item))
        self.assertFalse(media.has_jellyfin_backdrop(item))
        self.assertFalse(media.has_jellyfin_thumb(item))

    def test_jellyfin_image_types_returns_sorted_known_tags(self) -> None:
        item = _make_item(
            image_tags={
                "Primary": "primary-tag",
                "Backdrop": "backdrop-tag",
                "Thumb": "   ",
                "Banner": "banner-tag",
            }
        )

        self.assertEqual(
            media.jellyfin_image_types(item),
            ("Backdrop", "Primary"),
        )

class ReportGenerationTests(unittest.TestCase):
    def test_csv_rows_reflect_per_item_check_flags(self) -> None:
        movie_item = _make_item(
            title="Movie One",
            library="Movies",
            path=Path("Movie One (2024)/Movie One (2024).mkv"),
        )
        episode_item = _make_item(
            title="Episode Two",
            item_id="episode-two",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Show Name",
            season_number=1,
            episode_number=2,
            path=Path("Show Name/Season 01/Show Name S01E02.mkv"),
        )
        findings = (
            AuditFinding(
                category=AuditCategory.SUBTITLES,
                severity=AuditSeverity.WARNING,
                check_name="missing_english_subtitles",
                message="No configured English subtitles were found.",
                media_item=movie_item,
            ),
            AuditFinding(
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="mismatched_episode_filename_title",
                message="Filename suggests a different episode title.",
                media_item=episode_item,
            ),
            AuditFinding(
                category=AuditCategory.AUDIO,
                severity=AuditSeverity.WARNING,
                check_name="unknown_audio_codec",
                message="No primary audio codec was found.",
                media_item=episode_item,
            ),
        )
        library_result_movies = LibraryAuditResult(
            library=_make_library(library_id="movies", name="Movies", collection_type="movies"),
            media_items_processed=1,
            audited_items=(movie_item,),
            items_with_english_subtitles=0,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=findings[:1],
        )
        library_result_tv = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=1,
            audited_items=(episode_item,),
            items_with_english_subtitles=1,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=findings[1:],
        )
        result = AuditServerResult(
            libraries_audited=2,
            media_items_processed=2,
            library_results=(library_result_movies, library_result_tv),
            findings=findings,
        )

        rows = report_generator._csv_rows(result)

        self.assertEqual(
            report_generator.CSV_HEADER,
            (
                "Library",
                "Path",
                "Series",
                "Title",
                "Season",
                "Episode",
                "Missing Subtitles",
                "Missing Primary",
                "Mismatched Filename Title",
                "Mismatched Stream Title",
                "Unknown Audio Codec",
                "Unknown Video Codec",
                "Mismatched TheTVDB Series",
                "Aired/DVD Order Mismatch",
                "Missing Episode Number",
                "Missing Seasons",
                "Missing Episodes",
            ),
        )
        self.assertEqual(
            rows,
            (
                (
                    "Movies",
                    str(Path("Movie One (2024)/Movie One (2024).mkv")),
                    "",
                    "Movie One",
                    "",
                    "",
                    "Yes",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                ),
                (
                    "TV Shows",
                    str(Path("Show Name/Season 01/Show Name S01E02.mkv")),
                    "Show Name",
                    "Episode Two",
                    "1",
                    "2",
                    "No",
                    "No",
                    "Yes",
                    "No",
                    "Yes",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                    "No",
                ),
            ),
        )

    def test_csv_rows_flag_mismatched_tvdb_series_and_aired_dvd_order_mismatch(self) -> None:
        episode_item = _make_item(
            title="Episode Two",
            item_id="episode-two",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Show Name",
            season_number=1,
            episode_number=2,
            path=Path("Show Name/Season 01/Show Name S01E02.mkv"),
        )
        findings = (
            AuditFinding(
                category=AuditCategory.METADATA,
                severity=AuditSeverity.WARNING,
                check_name="mismatched_tvdb_series",
                message="Most local episodes don't match any TheTVDB episode.",
                media_item=episode_item,
            ),
            AuditFinding(
                category=AuditCategory.EPISODE_ORDER,
                severity=AuditSeverity.WARNING,
                check_name="aired_dvd_order_mismatch",
                message="Matches neither TheTVDB's aired-order nor DVD-order title.",
                media_item=episode_item,
            ),
        )
        library_result = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=1,
            audited_items=(episode_item,),
            items_with_english_subtitles=1,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=findings,
        )
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(library_result,),
            findings=findings,
        )

        rows = report_generator._csv_rows(result)

        self.assertEqual(len(rows), 1)
        header = report_generator.CSV_HEADER
        self.assertEqual(rows[0][header.index("Mismatched TheTVDB Series")], "Yes")
        self.assertEqual(rows[0][header.index("Aired/DVD Order Mismatch")], "Yes")

    def test_csv_row_shows_episode_range_for_combined_episode_file(self) -> None:
        """A range value like "5-7" is exactly the shape Excel's automatic
        type detection likes to reinterpret as a date (e.g. "5-6" commonly
        becomes "Jun-05") when the CSV is opened by double-clicking it - a
        leading apostrophe guards against that, the standard "force this
        cell to text" signal Excel's plain-text CSV import understands.
        """
        combined_item = _make_item(
            title="Combined",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Show Name",
            season_number=1,
            episode_number=5,
            path=Path("Show Name/Season 01/Show Name S01E05-E07.mkv"),
        )
        library_result = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=1,
            audited_items=(combined_item,),
            items_with_english_subtitles=1,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(),
        )
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(library_result,),
            findings=(),
        )

        rows = report_generator._csv_rows(result)

        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0][5], "'5-7")

    def test_csv_row_does_not_guard_a_plain_single_episode_number(self) -> None:
        item = _make_item(
            title="Ozymandias",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
            path=Path("Breaking Bad/Season 01/Breaking Bad S01E01.mkv"),
        )
        library_result = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=1,
            audited_items=(item,),
            items_with_english_subtitles=1,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(),
        )
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(library_result,),
            findings=(),
        )

        rows = report_generator._csv_rows(result)

        self.assertEqual(rows[0][5], "1")

    def test_write_html_report_creates_simplified_site_tree(self) -> None:
        movie_item = _make_item(title="Alien", library="Movies")
        actionable_finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Alien",
            message="Primary image missing",
            check_name="missing_primary_image",
            media_item=movie_item,
        )
        non_actionable_finding = _make_finding(
            category=AuditCategory.VIDEO,
            severity=AuditSeverity.INFO,
            title="Alien",
            message="HDR",
            check_name="hdr_video",
            media_item=movie_item,
        )
        library_result = LibraryAuditResult(
            library=_make_library(
                library_id="movies",
                name="Movies",
                collection_type="movies",
                locations=(Path("D:\\Media\\Movies"),),
            ),
            media_items_processed=1,
            audited_items=(movie_item,),
            items_with_english_subtitles=0,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(actionable_finding, non_actionable_finding),
        )
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(library_result,),
            findings=(actionable_finding, non_actionable_finding),
            server_key="server1",
            server_name="My Jellyfin Server",
        )

        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "audit_report.html"
            previous_value = os.environ.get("AUDIT_HTML_FILENAME")
            os.environ["AUDIT_HTML_FILENAME"] = str(output_path)
            clear_config_cache()
            try:
                reports.write_csv_report(result)
                index_path = reports.write_html_report(result)
            finally:
                if previous_value is None:
                    os.environ.pop("AUDIT_HTML_FILENAME", None)
                else:
                    os.environ["AUDIT_HTML_FILENAME"] = previous_value
                clear_config_cache()

            root_dir = Path(temp_dir) / "audit_report"
            server_dir = root_dir / "server1"
            self.assertEqual(index_path, server_dir / "index.html")
            self.assertTrue((root_dir / "css" / "style.css").exists())
            self.assertTrue((root_dir / "js" / "report.js").exists())
            self.assertTrue((server_dir / "My_Jellyfin_Server_audit.csv").exists())
            self.assertTrue((server_dir / "libraries" / "movies.html").exists())
            self.assertTrue((server_dir / "checks" / "missing_primary_image.html").exists())
            self.assertFalse((server_dir / "categories").exists())
            self.assertFalse((server_dir / "media").exists())
            self.assertFalse((server_dir / "checks" / "hdr_video.html").exists())

            root_index_html = (root_dir / "index.html").read_text(encoding="utf-8")
            index_html = index_path.read_text(encoding="utf-8")
            library_html = (server_dir / "libraries" / "movies.html").read_text(
                encoding="utf-8"
            )
            check_html = (server_dir / "checks" / "missing_primary_image.html").read_text(
                encoding="utf-8"
            )
            report_js = (root_dir / "js" / "report.js").read_text(encoding="utf-8")
            audited_at_text = datetime.fromtimestamp(
                index_path.stat().st_mtime
            ).astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")

        self.assertIn('href="server1/index.html"', root_index_html)
        self.assertIn(f"My Jellyfin Server ({audited_at_text})", root_index_html)
        self.assertIn('id="theme-toggle"', root_index_html)
        self.assertIn("jellyfin-library-auditor-theme", root_index_html)
        self.assertIn("Jellyfin Library Auditor (My Jellyfin Server)", index_html)
        self.assertIn('<span class="nav-server-name">My Jellyfin Server</span>', index_html)
        self.assertIn('<span class="nav-server-name">My Jellyfin Server</span>', library_html)
        self.assertIn('<span class="nav-server-name">My Jellyfin Server</span>', check_html)
        self.assertIn("Actionable Findings", index_html)
        self.assertIn('href="My_Jellyfin_Server_audit.csv"', index_html)
        self.assertIn("Download CSV", index_html)
        self.assertIn('id="theme-toggle"', index_html)
        self.assertNotIn('id="report-search"', index_html)
        self.assertNotIn("Expand All", index_html)
        self.assertIn("Audit Checks", index_html)
        self.assertIn('../css/style.css', index_html)
        self.assertIn("table.querySelector('[data-search-row]')", report_js)
        self.assertIn('id="report-search"', library_html)
        self.assertIn('id="theme-toggle"', library_html)
        self.assertIn('../../css/style.css', library_html)
        self.assertIn("Title", library_html)
        self.assertIn("Primary Image", library_html)
        self.assertNotIn("Backdrop", library_html)
        self.assertIn("Findings", library_html)
        self.assertIn("✗ missing", library_html)
        self.assertIn("Alien", library_html)
        self.assertIn("Primary Image", check_html)
        self.assertNotIn("HDR", check_html)
        self.assertNotIn("Backdrop", root_index_html)
        self.assertNotIn("Backdrop", index_html)
        self.assertNotIn("Backdrop", check_html)

    def test_write_html_report_preserves_existing_servers_and_rebuilds_root_index(self) -> None:
        first_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
        )
        second_result = AuditServerResult(
            libraries_audited=2,
            media_items_processed=5,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
        )

        with TemporaryDirectory() as temp_dir:
            output_path = Path(temp_dir) / "audit_report.html"
            previous_value = os.environ.get("AUDIT_HTML_FILENAME")
            os.environ["AUDIT_HTML_FILENAME"] = str(output_path)
            clear_config_cache()
            try:
                first_index_path = reports.write_html_report(first_result)
                second_index_path = reports.write_html_report(second_result)
            finally:
                if previous_value is None:
                    os.environ.pop("AUDIT_HTML_FILENAME", None)
                else:
                    os.environ["AUDIT_HTML_FILENAME"] = previous_value
                clear_config_cache()

            root_dir = Path(temp_dir) / "audit_report"
            root_index_html = (root_dir / "index.html").read_text(encoding="utf-8")
            first_server_exists = (root_dir / "server1" / "index.html").exists()
            second_server_exists = (root_dir / "server2" / "index.html").exists()

        self.assertEqual(first_index_path, root_dir / "server1" / "index.html")
        self.assertEqual(second_index_path, root_dir / "server2" / "index.html")
        self.assertTrue(first_server_exists)
        self.assertTrue(second_server_exists)
        self.assertIn('href="server1/index.html"', root_index_html)
        self.assertIn('href="server2/index.html"', root_index_html)
        self.assertIn("Server One", root_index_html)
        self.assertIn("Server Two", root_index_html)

    def test_library_page_groups_series_and_seasons(self) -> None:
        movie_finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Movie Alpha",
            message="Poster missing",
            check_name="missing_poster",
            media_item=_make_item(
                title="Movie Alpha",
                library="TV Shows",
                is_movie=True,
                is_episode=False,
            ),
        )
        episode_finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            message="Subtitle missing",
            check_name="missing_english_subtitles",
            media_item=_make_item(
                title="Pilot",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=1,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=2,
                library_results=(),
                findings=(movie_finding, episode_finding),
                server_name="My Jellyfin Server",
            ),
            (movie_finding, episode_finding),
        )
        html = report_library.render_library_page(
            "TV Shows",
            (movie_finding, episode_finding),
            site_links=site_links,
        )

        self.assertIn("Breaking Bad", html)
        self.assertIn("Poster", html)
        self.assertIn("English Subtitles", html)
        self.assertIn("Movie Alpha", html)
        self.assertNotIn("<details", html)

    def test_library_page_status_columns_include_sort_values(self) -> None:
        finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Alien",
            message="Poster missing",
            check_name="missing_poster",
            media_item=_make_item(title="Alien", library="Movies"),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_library.render_library_page(
            "Movies",
            (finding,),
            site_links=site_links,
        )

        self.assertIn('data-sort-value="0"><span class="status-label status-missing">', html)

    def test_library_page_heading_shows_row_count(self) -> None:
        first_finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Alien",
            message="Poster missing",
            check_name="missing_poster",
            media_item=_make_item(title="Alien", library="Movies"),
        )
        second_finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Predator",
            message="Poster missing",
            check_name="missing_poster",
            media_item=_make_item(title="Predator", library="Movies"),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=2,
                library_results=(),
                findings=(first_finding, second_finding),
                server_name="My Jellyfin Server",
            ),
            (first_finding, second_finding),
        )

        html = report_library.render_library_page(
            "Movies",
            (first_finding, second_finding),
            site_links=site_links,
        )

        self.assertIn(
            '<h2>Movies <span class="table-row-count" data-row-count>(2)</span></h2>',
            html,
        )

    def test_check_page_heading_shows_row_count(self) -> None:
        first_finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            message="Subtitle missing",
            check_name="missing_english_subtitles",
            media_item=_make_item(title="Pilot", library="TV Shows"),
        )
        second_finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Ozymandias",
            message="Subtitle missing",
            check_name="missing_english_subtitles",
            media_item=_make_item(title="Ozymandias", library="TV Shows"),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=2,
                library_results=(),
                findings=(first_finding, second_finding),
                server_name="My Jellyfin Server",
            ),
            (first_finding, second_finding),
        )

        html = report_generator.render_check_page(
            "missing_english_subtitles",
            (first_finding, second_finding),
            site_links=site_links,
        )

        self.assertIn(
            '<h2>Missing English Subtitles <span class="table-row-count" data-row-count>(2)</span></h2>',
            html,
        )

    def test_check_page_includes_numeric_sort_values_for_season_and_episode(self) -> None:
        finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            message="Subtitle missing",
            check_name="missing_english_subtitles",
            media_item=_make_item(
                title="Pilot",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                series_name="Breaking Bad",
                season_name="Season 12",
                season_number=12,
                episode_number=3,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_generator.render_check_page(
            "missing_english_subtitles",
            (finding,),
            site_links=site_links,
        )

        self.assertIn('data-sort-value="12">Season 12</td>', html)
        self.assertIn('data-sort-value="3">3</td>', html)

    def test_check_page_shows_episode_range_for_combined_episode_file(self) -> None:
        """A file spanning a multi-episode range (SxxEyy-Ezz) must show the
        full range in its Episode column, not just the first episode
        number - otherwise the table silently understates which episodes
        the file actually covers. The sort value stays the plain starting
        number, so the row still sorts correctly among single-episode rows.
        """
        finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Combined",
            message="Subtitle missing",
            check_name="missing_english_subtitles",
            media_item=_make_item(
                title="Combined",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                path=Path("Show - S01E05-E07 - Combined.mkv"),
                series_name="Show",
                season_name="Season 1",
                season_number=1,
                episode_number=5,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_generator.render_check_page(
            "missing_english_subtitles",
            (finding,),
            site_links=site_links,
        )

        self.assertIn('data-sort-value="5">5-7</td>', html)

    def test_check_page_includes_finding_details_for_missing_episode_gaps(self) -> None:
        finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            message="Missing episodes: 2, 4-5.",
            check_name="missing_episodes",
            media_item=_make_item(
                title="Pilot",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=1,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_generator.render_check_page(
            "missing_episodes",
            (finding,),
            site_links=site_links,
        )

        self.assertIn(">Details</button></th>", html)
        self.assertIn("Missing episodes: 2, 4-5.", html)
        self.assertIn(">Season</button></th>", html)
        self.assertNotIn(">Episode</button></th>", html)
        self.assertNotIn('data-sort-value="1">1</td>', html)

    def test_check_page_hides_season_and_episode_for_missing_seasons(self) -> None:
        finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            message="Missing seasons: 2.",
            check_name="missing_seasons",
            media_item=_make_item(
                title="Pilot",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=1,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_generator.render_check_page(
            "missing_seasons",
            (finding,),
            site_links=site_links,
        )

        self.assertIn(">Details</button></th>", html)
        self.assertNotIn(">Season</button></th>", html)
        self.assertNotIn(">Episode</button></th>", html)
        self.assertIn("Missing seasons: 2.", html)

    def test_check_page_shows_suggested_title_column_for_mismatched_filename_title(
        self,
    ) -> None:
        finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Wrong Title",
            message='Filename suggests episode title "Ozymandias" but metadata title is "Wrong Title".',
            check_name="mismatched_episode_filename_title",
            media_item=_make_item(
                title="Wrong Title",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=1,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_generator.render_check_page(
            "mismatched_episode_filename_title",
            (finding,),
            site_links=site_links,
        )

        self.assertIn(">Library</button></th>", html)
        self.assertIn(">Series</button></th>", html)
        self.assertIn(">Season</button></th>", html)
        self.assertIn(">Episode</button></th>", html)
        self.assertIn(">Title</button></th>", html)
        self.assertIn(">Suggested Title (Filename)</button></th>", html)
        self.assertNotIn(">Details</button></th>", html)
        self.assertIn(">Wrong Title<", html)
        self.assertIn(">Ozymandias<", html)
        self.assertNotIn("Filename suggests episode title", html)

    def test_check_page_shows_suggested_title_column_for_mismatched_stream_title(
        self,
    ) -> None:
        finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Safe",
            message='An embedded stream title suggests episode title "Jaynestown" but metadata title is "Safe".',
            check_name="mismatched_episode_stream_title",
            media_item=_make_item(
                title="Safe",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                path=Path("Firefly - S01E07 - Safe.mkv"),
                series_name="Firefly",
                season_name="Season 1",
                season_number=1,
                episode_number=7,
                audio_tracks=(
                    AudioTrack(
                        language="eng",
                        codec="ac3",
                        channels=6,
                        title="Firefly.S01E07.Jaynestown.1080p.BRRip.AC3.x264-LESS",
                    ),
                ),
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=1,
                media_items_processed=1,
                library_results=(),
                findings=(finding,),
                server_name="My Jellyfin Server",
            ),
            (finding,),
        )

        html = report_generator.render_check_page(
            "mismatched_episode_stream_title",
            (finding,),
            site_links=site_links,
        )

        self.assertIn(">Library</button></th>", html)
        self.assertIn(">Series</button></th>", html)
        self.assertIn(">Season</button></th>", html)
        self.assertIn(">Episode</button></th>", html)
        self.assertIn(">Title</button></th>", html)
        self.assertIn(">Suggested Title (Stream)</button></th>", html)
        self.assertNotIn(">Details</button></th>", html)
        self.assertIn(">Safe<", html)
        self.assertIn(">Jaynestown<", html)
        self.assertNotIn("An embedded stream title suggests", html)

    def test_check_page_orders_multi_library_rows_by_library_then_media(self) -> None:
        movie_finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Alien",
            check_name="missing_poster",
            media_item=_make_item(
                title="Alien",
                library="Movies",
                is_movie=True,
                is_episode=False,
            ),
        )
        later_episode_finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Ozymandias",
            check_name="missing_english_subtitles",
            media_item=_make_item(
                title="Ozymandias",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                series_name="Breaking Bad",
                season_name="Season 5",
                season_number=5,
                episode_number=14,
            ),
        )
        earlier_episode_finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_english_subtitles",
            media_item=_make_item(
                title="Pilot",
                library="TV Shows",
                is_movie=False,
                is_episode=True,
                series_name="Breaking Bad",
                season_name="Season 1",
                season_number=1,
                episode_number=1,
            ),
        )
        site_links = report_generator._site_links(
            AuditServerResult(
                libraries_audited=2,
                media_items_processed=3,
                library_results=(),
                findings=(later_episode_finding, earlier_episode_finding, movie_finding),
                server_name="My Jellyfin Server",
            ),
            (later_episode_finding, earlier_episode_finding, movie_finding),
        )

        rows = report_checks._check_rows(
            (later_episode_finding, earlier_episode_finding, movie_finding),
            site_links=site_links,
        )

        self.assertIn(">Movies<", rows[0])
        self.assertIn(">Alien<", rows[0])
        self.assertIn(">TV Shows<", rows[1])
        self.assertIn(">Pilot<", rows[1])
        self.assertIn(">TV Shows<", rows[2])
        self.assertIn(">Ozymandias<", rows[2])
        self.assertIn(
            'data-sort-value="tv shows|breaking bad|0:00000001|0:00000001|pilot"',
            rows[1],
        )

    def test_write_comparison_reports_creates_expected_pages(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            image_tags={"Primary": "left-primary"},
            video_track=VideoTrack(
                codec="h264",
                width=1920,
                height=1080,
                bitrate=None,
                hdr=False,
                video_range=None,
            ),
        )
        left_missing_seasons_item = _make_item(
            title="Pilot",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Example Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
        )
        left_missing_seasons_finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_seasons",
            message="Missing seasons: 2.",
            media_item=left_missing_seasons_item,
        )
        right_item = _make_item(
            title="Alien",
            library="Movies",
            image_tags={},
            subtitle_tracks=(
                SubtitleTrack(
                    language="en",
                    codec="srt",
                    is_external=True,
                    is_default=False,
                    is_forced=False,
                ),
            ),
            video_track=VideoTrack(
                codec="hevc",
                width=1920,
                height=1080,
                bitrate=None,
                hdr=False,
                video_range=None,
            ),
        )
        right_missing_episodes_item = _make_item(
            title="Pilot",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Example Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
        )
        right_missing_episodes_finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_episodes",
            message="Missing episodes: 2.",
            media_item=right_missing_episodes_item,
        )
        left_finding = _make_finding(
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Alien",
            check_name="missing_poster",
            media_item=left_item,
        )
        right_finding = _make_finding(
            category=AuditCategory.SUBTITLES,
            severity=AuditSeverity.WARNING,
            title="Alien",
            check_name="missing_english_subtitles",
            media_item=right_item,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                        locations=(Path("D:\\Media\\Movies"),),
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(left_finding,),
                ),
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows-left",
                        name="TV Shows",
                        collection_type="tv",
                        locations=(Path("D:\\Media\\TV Shows"),),
                    ),
                    media_items_processed=1,
                    audited_items=(left_missing_seasons_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(left_missing_seasons_finding,),
                ),
            ),
            findings=(left_finding, left_missing_seasons_finding),
            server_name="Left Server",
            server_key="left",
            server_url="http://left:8096",
            server_settings=(
                _make_comparison_setting("UI Culture", "en-US"),
                _make_comparison_setting("Enable Folder View", "Yes"),
                _make_comparison_setting("Remote Client Bitrate Limit", "0"),
                _make_comparison_setting("Playback Hardware Acceleration", "qsv"),
                _make_comparison_setting("Playback H264 CRF", "23"),
            ),
            library_settings=(
                LibraryComparisonSettings(
                    library_name="Movies",
                    settings=(
                        _make_comparison_setting("Collection Type", "movies"),
                        _make_comparison_setting("Locations", "D:\\Media\\Movies"),
                        _make_comparison_setting("Realtime Monitor", "Yes"),
                        _make_comparison_setting(
                            "Preferred Metadata Language",
                            "en",
                        ),
                        _make_comparison_setting(
                            "Movie Metadata Fetchers",
                            "TheMovieDb, Imdb",
                        ),
                    ),
                ),
            ),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies-right",
                        name="Movies",
                        collection_type="boxsets",
                        locations=(Path("E:\\Media\\Movies"),),
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(right_finding,),
                ),
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows-right",
                        name="TV Shows",
                        collection_type="tv",
                        locations=(Path("E:\\Media\\TV Shows"),),
                    ),
                    media_items_processed=1,
                    audited_items=(right_missing_episodes_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(right_missing_episodes_finding,),
                ),
            ),
            findings=(right_finding, right_missing_episodes_finding),
            server_name="Right Server",
            server_key="right",
            server_url="http://right:8096",
            server_settings=(
                _make_comparison_setting("UI Culture", "fr-FR"),
                _make_comparison_setting("Enable Folder View", "No"),
                _make_comparison_setting("Remote Client Bitrate Limit", "8000000"),
                _make_comparison_setting("Playback Hardware Acceleration", "none"),
                _make_comparison_setting("Playback H264 CRF", "20"),
            ),
            library_settings=(
                LibraryComparisonSettings(
                    library_name="Movies",
                    settings=(
                        _make_comparison_setting("Collection Type", "boxsets"),
                        _make_comparison_setting("Locations", "E:\\Media\\Movies"),
                        _make_comparison_setting("Realtime Monitor", "No"),
                        _make_comparison_setting(
                            "Preferred Metadata Language",
                            "fr",
                        ),
                        _make_comparison_setting(
                            "Movie Metadata Fetchers",
                            "TheMovieDb",
                        ),
                    ),
                ),
            ),
        )

        with TemporaryDirectory() as temp_dir:
            index_path = write_comparison_reports(
                left_result,
                right_result,
                Path(temp_dir) / "audit_results" / "comparison_results",
            )
            root_dir = Path(temp_dir) / "audit_results"
            comparison_dir = root_dir / "comparison_results"

            self.assertEqual(index_path, comparison_dir / "index.html")
            self.assertTrue((root_dir / "css" / "style.css").exists())
            self.assertTrue((root_dir / "js" / "report.js").exists())
            self.assertTrue((comparison_dir / "libraries.html").exists())
            self.assertTrue((comparison_dir / "artwork.html").exists())
            self.assertTrue((comparison_dir / "subtitles.html").exists())
            self.assertTrue((comparison_dir / "configuration.html").exists())

            libraries_html = (comparison_dir / "libraries.html").read_text(
                encoding="utf-8"
            )
            artwork_html = (comparison_dir / "artwork.html").read_text(
                encoding="utf-8"
            )
            subtitles_html = (comparison_dir / "subtitles.html").read_text(
                encoding="utf-8"
            )
            configuration_html = (comparison_dir / "configuration.html").read_text(
                encoding="utf-8"
            )

        self.assertIn("Library Comparison", libraries_html)
        self.assertIn("Libraries By Server", libraries_html)
        self.assertIn("Left Server", libraries_html)
        self.assertIn("Right Server", libraries_html)
        self.assertIn("Missing Seasons", libraries_html)
        self.assertIn("Missing Episodes", libraries_html)
        self.assertIn("Mismatched TheTVDB Series", libraries_html)
        self.assertIn("Missing seasons: 2.", libraries_html)
        self.assertIn("Missing episodes: 2.", libraries_html)
        self.assertIn("Example Show", libraries_html)
        self.assertNotIn("Libraries Missing From Left", libraries_html)
        self.assertNotIn("Libraries Missing From Right", libraries_html)
        self.assertIn('class="table-shell table-scroll-shell"', libraries_html)
        self.assertIn(
            '<h2>Missing Seasons <span class="table-row-count" data-row-count>(1)</span></h2>',
            libraries_html,
        )
        self.assertIn(
            '<h2>Missing Episodes <span class="table-row-count" data-row-count>(1)</span></h2>',
            libraries_html,
        )
        self.assertIn("Artwork Comparison", artwork_html)
        self.assertIn("Alien", artwork_html)
        self.assertIn("../css/style.css", artwork_html)
        self.assertIn('id="theme-toggle"', artwork_html)
        self.assertIn("jellyfin-library-auditor-theme", artwork_html)
        self.assertNotIn("Hide same", artwork_html)
        self.assertNotIn("Backdrop", artwork_html)
        self.assertIn('class="data-table comparison-table"', libraries_html)
        self.assertEqual(libraries_html.count("Hide same"), 4)
        self.assertNotIn('class="comparison-diff-row"', artwork_html)
        self.assertIn('class="comparison-diff">Yes</td>', artwork_html)
        self.assertIn('class="comparison-diff">No</td>', artwork_html)
        self.assertIn('data-hide-same="false"', libraries_html)
        self.assertIn('class="comparison-diff">Yes</td>', subtitles_html)
        self.assertIn('class="comparison-diff">No</td>', subtitles_html)
        self.assertIn("Server Settings", configuration_html)
        self.assertIn("Library Settings", configuration_html)
        self.assertIn('class="table-shell table-scroll-shell"', configuration_html)
        self.assertIn('data-hide-same="false"', configuration_html)
        self.assertIn("toggleSameRows(this)", configuration_html)
        self.assertIn("Configured Server Key", configuration_html)
        self.assertIn("UI Culture", configuration_html)
        self.assertIn("Enable Folder View", configuration_html)
        self.assertIn("Remote Client Bitrate Limit", configuration_html)
        self.assertIn("Playback Hardware Acceleration", configuration_html)
        self.assertIn("Playback H264 CRF", configuration_html)
        self.assertIn("Server URL", configuration_html)
        self.assertIn("http://left:8096", configuration_html)
        self.assertIn("http://right:8096", configuration_html)
        self.assertIn("Collection Type", configuration_html)
        self.assertIn("Locations", configuration_html)
        self.assertIn("Realtime Monitor", configuration_html)
        self.assertIn("Preferred Metadata Language", configuration_html)
        self.assertIn("Movie Metadata Fetchers", configuration_html)
        self.assertIn("D:\\Media\\Movies", configuration_html)
        self.assertIn("E:\\Media\\Movies", configuration_html)
        self.assertIn('class="comparison-diff">movies</td>', configuration_html)
        self.assertIn('class="comparison-diff">boxsets</td>', configuration_html)
        self.assertIn('class="comparison-diff">qsv</td>', configuration_html)
        self.assertIn('class="comparison-diff">none</td>', configuration_html)
        self.assertIn('class="comparison-diff">en-US</td>', configuration_html)
        self.assertIn('class="comparison-diff">fr-FR</td>', configuration_html)
        self.assertIn('class="comparison-diff">Yes</td>', configuration_html)
        self.assertIn('class="comparison-diff">No</td>', configuration_html)
        self.assertNotIn("Metadata Differences", configuration_html)
        self.assertIn("Mismatched Metadata", libraries_html)
        self.assertIn('<th colspan="2">Video Codec</th>', libraries_html)
        self.assertIn('class="comparison-diff">h264</td>', libraries_html)
        self.assertIn('class="comparison-diff">hevc</td>', libraries_html)

    def test_comparison_matches_same_title_when_year_differs(self) -> None:
        left_item = _make_item(
            title="Dune",
            library="Movies",
            year=2021,
        )
        right_item = _make_item(
            title="Dune",
            library="Movies",
            year=2024,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Left Server",
            server_key="left",
            server_url="http://left:8096",
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Right Server",
            server_key="right",
            server_url="http://right:8096",
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())
        self.assertEqual(
            comparison["mismatched_metadata"],
            (
                (
                    "Movies",
                    "Dune",
                    "dune",
                    "dune",
                    "left",
                    "right",
                    "Dune",
                    "Dune",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "2021",
                    "2024",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "dune.mkv",
                ),
            ),
        )

    def test_comparison_flags_title_differences_for_same_filename(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            year=1979,
        )
        right_item = _make_item(
            title="Alien: Special Edition",
            library="Movies",
            path=Path("Alien.mkv"),
            year=1979,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Left Server",
            server_key="left",
            server_url="http://left:8096",
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Right Server",
            server_key="right",
            server_url="http://right:8096",
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(
            comparison["mismatched_metadata"],
            (
                (
                    "Movies",
                    "Alien",
                    "alien",
                    "alien:-special-edition",
                    "left",
                    "right",
                    "Alien",
                    "Alien: Special Edition",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "1979",
                    "1979",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "alien.mkv",
                ),
            ),
        )

        configuration_html = comparison_generator._configuration_page(
            left_result,
            right_result,
            comparison,
        )
        libraries_html = comparison_generator._libraries_page(
            left_result,
            right_result,
            comparison,
        )

        self.assertNotIn("Metadata Differences", configuration_html)
        self.assertNotIn("Alien: Special Edition", configuration_html)
        self.assertIn("Mismatched Metadata", libraries_html)
        self.assertIn('<th colspan="2">Title</th>', libraries_html)
        self.assertIn('<th colspan="2">Year</th>', libraries_html)
        self.assertIn('class="comparison-diff">Alien</td>', libraries_html)
        self.assertIn('class="comparison-diff">Alien: Special Edition</td>', libraries_html)
        self.assertNotIn('class="comparison-diff">1979</td>', libraries_html)

    def test_comparison_includes_missing_seasons_and_episodes(self) -> None:
        left_gap_item = _make_item(
            title="Episode 1",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Gap Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
        )
        right_gap_item = _make_item(
            title="Episode 1",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Gap Show",
            season_name="Season 3",
            season_number=3,
            episode_number=1,
        )
        left_missing_episodes = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Episode 1",
            check_name="missing_episodes",
            message="Missing episodes: 2, 4-5.",
            media_item=left_gap_item,
        )
        right_missing_seasons = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Episode 1",
            check_name="missing_seasons",
            message="Missing seasons: 2.",
            media_item=right_gap_item,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows-left",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(left_gap_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(left_missing_episodes,),
                ),
            ),
            findings=(left_missing_episodes,),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows-right",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(right_gap_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(right_missing_seasons,),
                ),
            ),
            findings=(right_missing_seasons,),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["left_missing_seasons"], ())
        self.assertEqual(
            comparison["left_missing_episodes"],
            (("TV Shows", left_missing_episodes),),
        )
        self.assertEqual(
            comparison["right_missing_seasons"],
            (("TV Shows", right_missing_seasons),),
        )
        self.assertEqual(comparison["right_missing_episodes"], ())

    def test_paired_missing_gap_tables_align_both_servers(self) -> None:
        left_item = _make_item(
            title="Pilot",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Gap Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
        )
        right_item = _make_item(
            title="Pilot",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Gap Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
        )
        same_seasons_left = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_seasons",
            message="Missing seasons: 2.",
            media_item=left_item,
        )
        same_seasons_right = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_seasons",
            message="Missing seasons: 2.",
            media_item=right_item,
        )
        different_episodes_left = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_episodes",
            message="Missing episodes: 2.",
            media_item=left_item,
        )
        different_episodes_right = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Pilot",
            check_name="missing_episodes",
            message="Missing episodes: 3.",
            media_item=right_item,
        )

        season_rows = comparison_generator._paired_missing_seasons_rows(
            (("TV Shows", same_seasons_left),),
            (("TV Shows", same_seasons_right),),
        )
        episode_rows = comparison_generator._paired_missing_episodes_rows(
            (("TV Shows", different_episodes_left),),
            (("TV Shows", different_episodes_right),),
        )

        self.assertEqual(len(season_rows), 1)
        self.assertIn(">TV Shows</td>", season_rows[0])
        self.assertIn(">Gap Show</td>", season_rows[0])
        self.assertNotIn("data-diff-row", season_rows[0])
        self.assertIn(">Missing seasons: 2.</td>", season_rows[0])
        self.assertEqual(len(episode_rows), 1)
        self.assertIn(">1</td>", episode_rows[0])
        self.assertIn("data-diff-row", episode_rows[0])
        self.assertIn(">Missing episodes: 2.</td>", episode_rows[0])
        self.assertIn(">Missing episodes: 3.</td>", episode_rows[0])

    def test_library_list_rows_align_matching_library_names(self) -> None:
        left_result = AuditServerResult(
            libraries_audited=3,
            media_items_processed=0,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="anime",
                        name="Anime",
                        collection_type="tv",
                    ),
                    media_items_processed=0,
                    audited_items=(),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=0,
                    audited_items=(),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=0,
                    audited_items=(),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=3,
            media_items_processed=0,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=0,
                    audited_items=(),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=0,
                    audited_items=(),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
                LibraryAuditResult(
                    library=_make_library(
                        library_id="docs",
                        name="Documentaries",
                        collection_type="movies",
                    ),
                    media_items_processed=0,
                    audited_items=(),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        rows = comparison_generator._library_list_rows(left_result, right_result)

        self.assertEqual(
            rows,
            (
                '<tr data-diff-row><td>Anime</td><td></td></tr>',
                '<tr data-diff-row><td></td><td>Documentaries</td></tr>',
                '<tr><td>Movies</td><td>Movies</td></tr>',
                '<tr><td>TV Shows</td><td>TV Shows</td></tr>',
            ),
        )

    def test_comparison_normalizes_movie_titles_for_matching(self) -> None:
        left_item = _make_item(
            title="Pokémon the Movie 2000",
            library="Movies",
        )
        right_item = _make_item(
            title="Pokémon the Movie 2000 (1999)",
            library="Movies",
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())

    def test_comparison_normalizes_episode_titles_for_matching(self) -> None:
        left_item = _make_item(
            title="Mrs. McGinty's Dead",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Agatha Christie's Poirot",
            season_name="Season 11",
            season_number=11,
            episode_number=1,
        )
        right_item = _make_item(
            title="Mrs McGinty's Dead",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Agatha Christie's Poirot",
            season_name="Season 11",
            season_number=11,
            episode_number=1,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())

    def test_comparison_pairs_episodes_when_season_name_matches(self) -> None:
        left_item = _make_item(
            title="Three Act Tragedy",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Agatha Christie's Poirot",
            season_name="Season 12",
            season_number=None,
            episode_number=1,
        )
        right_item = _make_item(
            title="Three Act Tragedy",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Agatha Christie's Poirot",
            season_name="Season 12",
            season_number=12,
            episode_number=1,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())

    def test_comparison_reports_missing_media_on_correct_side(self) -> None:
        left_item = _make_item(
            title="Left Only Episode",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Example Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
        )
        right_item = _make_item(
            title="Right Only Episode",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Example Show",
            season_name="Season 1",
            season_number=1,
            episode_number=2,
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(
            tuple(item.title for _, item in comparison["missing_left_media"]),
            ("Right Only Episode",),
        )
        self.assertEqual(
            tuple(item.title for _, item in comparison["missing_right_media"]),
            ("Left Only Episode",),
        )

    def test_comparison_matches_items_by_shared_base_filename(self) -> None:
        left_item = _make_item(
            title="The Great Escape",
            library="Movies",
            path=Path("D:\\Media\\Movies\\The.Great.Escape.1963.1080p.mkv"),
        )
        right_item = _make_item(
            title="Great Escape, The (1963)",
            library="Movies",
            path=Path("E:\\Media\\Movies\\The.Great.Escape.1963.1080p.mkv"),
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())

    def test_comparison_falls_back_to_metadata_when_filenames_differ(self) -> None:
        left_item = _make_item(
            title="Twins",
            library="Movies",
            path=Path("D:\\Media\\Movies\\Twins (1988) - Copy 1.mkv"),
        )
        right_item = _make_item(
            title="Twins",
            library="Movies",
            path=Path("E:\\Media\\Movies\\Twins (1988) - Copy 2.mkv"),
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies",
                        name="Movies",
                        collection_type="movies",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())

    def test_comparison_flags_mismatched_metadata_for_filename_matched_items(self) -> None:
        left_item = _make_item(
            title="Episode Nine",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Left Show",
            season_name="Season 1",
            season_number=1,
            episode_number=9,
            path=Path("/mnt/left/media/TV Shows/Show/Season 01/Show.S01E09.mkv"),
        )
        right_item = _make_item(
            title="The Ninth",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Right Show",
            season_name="Season 2",
            season_number=2,
            episode_number=10,
            path=Path("/mnt/right/media/TV Shows/Show/Season 01/Show.S01E09.mkv"),
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Left Server",
            server_key="left",
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Right Server",
            server_key="right",
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(
            comparison["mismatched_metadata"],
            (
                (
                    "TV Shows",
                    "Show.S01E09",
                    "episode-nine",
                    "the-ninth",
                    "left",
                    "right",
                    "Left Show",
                    "Right Show",
                    "1",
                    "2",
                    "9",
                    "10",
                    "Episode Nine",
                    "The Ninth",
                    "2024",
                    "2024",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "",
                    "tv shows/show/season 01/show.s01e09.mkv",
                ),
            ),
        )
        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())

        libraries_html = comparison_generator._libraries_page(left_result, right_result, comparison)

        self.assertIn(
            '<h2>Mismatched Metadata <span class="table-row-count" data-row-count>(1)</span></h2>',
            libraries_html,
        )
        self.assertIn('<th colspan="2">Title</th>', libraries_html)
        self.assertIn('<th colspan="2">Season Number</th>', libraries_html)
        self.assertIn('<th colspan="2">Episode Number</th>', libraries_html)
        self.assertIn('<th colspan="3">Episode Name</th>', libraries_html)
        self.assertIn('<th colspan="2">Year</th>', libraries_html)
        self.assertIn('<th colspan="2">Resolution</th>', libraries_html)
        self.assertIn('<th colspan="2">Video Codec</th>', libraries_html)
        self.assertIn('<th colspan="2">Audio Codec</th>', libraries_html)
        self.assertIn(">Left Server<", libraries_html)
        self.assertIn(">Right Server<", libraries_html)
        self.assertIn(
            'data-sort-value="tv shows/show/season 01/show.s01e09.mkv">Show.S01E09</td>',
            libraries_html,
        )
        self.assertIn('class="comparison-diff">Left Show</td>', libraries_html)
        self.assertIn('class="comparison-diff">Right Show</td>', libraries_html)
        self.assertIn('class="comparison-diff">Episode Nine</td>', libraries_html)
        self.assertIn('class="comparison-diff">The Ninth</td>', libraries_html)
        self.assertIn(
            'data-command="python transfer_metadata.py --from-server &quot;left&quot; '
            '--from-item &quot;episode-nine&quot; --to-server &quot;right&quot; '
            '--to-item &quot;the-ninth&quot;"',
            libraries_html,
        )
        self.assertIn('onclick="copyTransferCommand(this)">&#8594;</button>', libraries_html)

    def test_comparison_omits_matching_metadata_from_mismatch_report(self) -> None:
        left_item = _make_item(
            title="Same Episode",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Same Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
            path=Path("Show.S01E01.mkv"),
        )
        right_item = _make_item(
            title="Same Episode",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Same Show",
            season_name="Season 1",
            season_number=1,
            episode_number=1,
            path=Path("Show.S01E01.mkv"),
        )
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(left_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["mismatched_metadata"], ())

    def test_media_missing_row_displays_numeric_season_values(self) -> None:
        item = _make_item(
            title="Story Samurai",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Abbott Elementary",
            season_name="Season 2",
            season_number=2,
            episode_number=3,
        )

        row_html = comparison_generator._media_missing_row("TV Shows", item)

        self.assertIn('data-sort-value="2">2</td>', row_html)
        self.assertIn('data-sort-value="3">3</td>', row_html)
        self.assertNotIn(">Season 2<", row_html)

    def test_media_missing_row_shows_episode_range_for_combined_episode_file(self) -> None:
        item = _make_item(
            title="Combined",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            series_name="Show",
            season_name="Season 1",
            season_number=1,
            episode_number=5,
        )

        row_html = comparison_generator._media_missing_row("TV Shows", item)

        self.assertIn('data-sort-value="5">5-7</td>', row_html)

    def test_comparison_pairs_duplicate_versions_by_codec_signature(self) -> None:
        left_hd = _make_item(
            title="Once More, with Feeling",
            item_id="left-hd",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Buffy the Vampire Slayer",
            season_name="Season 6",
            season_number=6,
            episode_number=7,
            video_track=VideoTrack(
                codec="h264",
                width=1280,
                height=720,
                bitrate=None,
                hdr=False,
                video_range=None,
            ),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="eac3",
                    channels=6,
                    title=None,
                ),
            ),
        )
        left_sd = _make_item(
            title="Once More, with Feeling",
            item_id="left-sd",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Buffy the Vampire Slayer",
            season_name="Season 6",
            season_number=6,
            episode_number=7,
            video_track=VideoTrack(
                codec="h264",
                width=720,
                height=480,
                bitrate=None,
                hdr=False,
                video_range=None,
            ),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=2,
                    title=None,
                ),
            ),
        )
        right_sd = _make_item(
            title="Once More, with Feeling",
            item_id="right-sd",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Buffy the Vampire Slayer",
            season_name="Season 6",
            season_number=6,
            episode_number=7,
            video_track=VideoTrack(
                codec="h264",
                width=720,
                height=480,
                bitrate=None,
                hdr=False,
                video_range=None,
            ),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="ac3",
                    channels=2,
                    title=None,
                ),
            ),
        )
        right_hd = _make_item(
            title="Once More, with Feeling",
            item_id="right-hd",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Buffy the Vampire Slayer",
            season_name="Season 6",
            season_number=6,
            episode_number=7,
            video_track=VideoTrack(
                codec="h264",
                width=1280,
                height=720,
                bitrate=None,
                hdr=False,
                video_range=None,
            ),
            audio_tracks=(
                AudioTrack(
                    language="eng",
                    codec="eac3",
                    channels=6,
                    title=None,
                ),
            ),
        )

        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=2,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=2,
                    audited_items=(left_hd, left_sd),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=2,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="shows",
                        name="TV Shows",
                        collection_type="tv",
                    ),
                    media_items_processed=2,
                    audited_items=(right_sd, right_hd),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
        )

        comparison = comparison_generator._build_comparison(left_result, right_result)

        self.assertEqual(comparison["missing_left_media"], ())
        self.assertEqual(comparison["missing_right_media"], ())
        self.assertEqual(comparison["mismatched_metadata"], ())

    def test_comparison_generator_uses_safe_default_output_directory(self) -> None:
        self.assertEqual(
            DEFAULT_COMPARISON_OUTPUT_DIR,
            Path("audit_results") / "comparison_results",
        )


class CompareCommandTests(unittest.TestCase):
    def test_main_with_all_audits_every_configured_server(self) -> None:
        first_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )
        second_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )
        third_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server3",
            server_name="Server Three",
            server_url="http://server3:8096",
        )

        with patch(
            "auditor.audit_server",
            side_effect=[first_result, second_result, third_result],
        ) as mock_audit:
            with patch("auditor.write_csv_report") as mock_csv:
                with patch("auditor.write_html_report") as mock_html:
                    with patch("auditor.get_config") as mock_get_config:
                        with patch("auditor.reset_audit_results_root") as mock_reset:
                            with patch("auditor.write_audit_results_index") as mock_index:
                                mock_get_config.return_value = config.AppConfig(
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
                                    processing=ProcessingConfig(
                                        enable_movies=True,
                                        enable_tv=True,
                                    ),
                                    servers=ServerCollection(
                                        default_server="server3",
                                        servers={
                                            "server1": ServerConfig(
                                                key="server1",
                                                name="Server One",
                                                url="http://server1:8096",
                                                api_key="token1",
                                            ),
                                            "server2": ServerConfig(
                                                key="server2",
                                                name="Server Two",
                                                url="http://server2:8096",
                                                api_key="token2",
                                            ),
                                            "server3": ServerConfig(
                                                key="server3",
                                                name="Server Three",
                                                url="http://server3:8096",
                                                api_key="token3",
                                            ),
                                        },
                                    ),
                                    tvdb=config.TvdbConfig(api_key=None),
                                )
                                exit_code = auditor.main(["--all"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_audit.call_args_list,
            [
                call("server1", (), include_configuration_snapshot=False, tvdb_client=None, check_episode_order=False),
                call("server2", (), include_configuration_snapshot=False, tvdb_client=None, check_episode_order=False),
                call("server3", (), include_configuration_snapshot=False, tvdb_client=None, check_episode_order=False),
            ],
        )
        self.assertEqual(
            mock_csv.call_args_list,
            [((first_result,),), ((second_result,),), ((third_result,),)],
        )
        self.assertEqual(
            mock_html.call_args_list,
            [((first_result,),), ((second_result,),), ((third_result,),)],
        )
        mock_reset.assert_called_once()
        mock_index.assert_called_once()

    def test_main_with_bare_compare_uses_first_two_configured_servers(self) -> None:
        base_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )
        compare_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )

        with patch("auditor.audit_server", side_effect=[base_result, compare_result]) as mock_audit:
            with patch("auditor.write_csv_report"):
                with patch("auditor.write_html_report"):
                    with patch("auditor.write_comparison_reports"):
                        with patch("auditor.get_config") as mock_get_config:
                            with patch("auditor.reset_audit_results_root"):
                                with patch("auditor.write_audit_results_index"):
                                    mock_get_config.return_value = config.AppConfig(
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
                                        processing=ProcessingConfig(
                                            enable_movies=True,
                                            enable_tv=True,
                                        ),
                                        servers=ServerCollection(
                                            default_server="server3",
                                            servers={
                                                "server1": ServerConfig(
                                                    key="server1",
                                                    name="Server One",
                                                    url="http://server1:8096",
                                                    api_key="token1",
                                                ),
                                                "server2": ServerConfig(
                                                    key="server2",
                                                    name="Server Two",
                                                    url="http://server2:8096",
                                                    api_key="token2",
                                                ),
                                                "server3": ServerConfig(
                                                    key="server3",
                                                    name="Server Three",
                                                    url="http://server3:8096",
                                                    api_key="token3",
                                                ),
                                            },
                                        ),
                                        tvdb=config.TvdbConfig(api_key=None),
                                    )
                                    exit_code = auditor.main(["--compare"])

        self.assertEqual(exit_code, 0)
        self.assertEqual(
            mock_audit.call_args_list,
            [
                call("server1", (), include_configuration_snapshot=True, tvdb_client=None, check_episode_order=False),
                call("server2", (), include_configuration_snapshot=True, tvdb_client=None, check_episode_order=False),
            ],
        )

    def test_main_with_compare_writes_comparison_reports(self) -> None:
        base_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )
        compare_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )

        with patch("auditor.audit_server", side_effect=[base_result, compare_result]) as mock_audit:
            with patch("auditor.write_csv_report") as mock_csv:
                with patch("auditor.write_html_report") as mock_html:
                    with patch("auditor.write_comparison_reports") as mock_compare:
                        with patch("auditor.get_config") as mock_get_config:
                            with patch("auditor.reset_audit_results_root") as mock_reset:
                                with patch("auditor.write_audit_results_index") as mock_index:
                                    mock_get_config.return_value = config.AppConfig(
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
                                        processing=ProcessingConfig(
                                            enable_movies=True,
                                            enable_tv=True,
                                        ),
                                        servers=ServerCollection(
                                            default_server="server1",
                                            servers={
                                                "server1": ServerConfig(
                                                    key="server1",
                                                    name="Server One",
                                                    url="http://server1:8096",
                                                    api_key="token",
                                                ),
                                            },
                                        ),
                                        tvdb=config.TvdbConfig(api_key=None),
                                    )
                                    exit_code = auditor.main(
                                        ["--server", "server1", "--compare", "server2"]
                                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_audit.call_count, 2)
        self.assertEqual(mock_csv.call_args_list, [((base_result,),), ((compare_result,),)])
        self.assertEqual(mock_html.call_args_list, [((base_result,),), ((compare_result,),)])
        mock_compare.assert_called_once_with(
            base_result,
            compare_result,
            transfer_results=None,
            image_transfer_results=None,
            subtitle_transfer_results=None,
            left_csv_path=mock_csv.return_value,
            right_csv_path=mock_csv.return_value,
        )
        mock_reset.assert_called_once()
        mock_index.assert_called_once()

    def test_main_with_verify_reaudits_compare_server_after_transfer(self) -> None:
        base_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )
        compare_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )
        verified_compare_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )

        transfer_results = (
            MetadataTransferResult(
                library="TV Shows",
                display_name="Example",
                status="transferred",
                changed_fields=("Name",),
            ),
        )

        with patch(
            "auditor.audit_server",
            side_effect=[base_result, compare_result, verified_compare_result],
        ) as mock_audit:
            with patch("auditor.write_csv_report") as mock_csv:
                with patch("auditor.write_html_report"):
                    with patch("auditor.write_comparison_reports") as mock_compare:
                        with patch("auditor.get_config") as mock_get_config:
                            with patch("auditor.reset_audit_results_root"):
                                with patch("auditor.write_audit_results_index"):
                                    with patch(
                                        "auditor._run_bulk_metadata_transfer",
                                        return_value=(0, transfer_results),
                                    ):
                                        mock_get_config.return_value = config.AppConfig(
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
                                            processing=ProcessingConfig(
                                                enable_movies=True,
                                                enable_tv=True,
                                            ),
                                            servers=ServerCollection(
                                                default_server="server1",
                                                servers={
                                                    "server1": ServerConfig(
                                                        key="server1",
                                                        name="Server One",
                                                        url="http://server1:8096",
                                                        api_key="token",
                                                    ),
                                                },
                                            ),
                                            tvdb=config.TvdbConfig(api_key=None),
                                        )
                                        exit_code = auditor.main(
                                            [
                                                "--server", "server1",
                                                "--compare", "server2",
                                                "--transfer-metadata",
                                                "--yes",
                                                "--verify",
                                            ]
                                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_audit.call_count, 3)
        self.assertEqual(
            mock_audit.call_args_list[2],
            call("server2", (), include_configuration_snapshot=True),
        )
        mock_compare.assert_called_once_with(
            base_result,
            verified_compare_result,
            transfer_results=transfer_results,
            image_transfer_results=None,
            subtitle_transfer_results=None,
            left_csv_path=mock_csv.return_value,
            right_csv_path=mock_csv.return_value,
        )

    def test_main_with_verify_skips_reaudit_when_nothing_actually_transferred(self) -> None:
        base_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )
        compare_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )
        transfer_results = (
            MetadataTransferResult(library="TV Shows", display_name="Example", status="unchanged"),
        )

        with patch(
            "auditor.audit_server", side_effect=[base_result, compare_result]
        ) as mock_audit:
            with patch("auditor.write_csv_report") as mock_csv:
                with patch("auditor.write_html_report"):
                    with patch("auditor.write_comparison_reports") as mock_compare:
                        with patch("auditor.get_config") as mock_get_config:
                            with patch("auditor.reset_audit_results_root"):
                                with patch("auditor.write_audit_results_index"):
                                    with patch(
                                        "auditor._run_bulk_metadata_transfer",
                                        return_value=(0, transfer_results),
                                    ):
                                        mock_get_config.return_value = config.AppConfig(
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
                                            processing=ProcessingConfig(
                                                enable_movies=True,
                                                enable_tv=True,
                                            ),
                                            servers=ServerCollection(
                                                default_server="server1",
                                                servers={
                                                    "server1": ServerConfig(
                                                        key="server1",
                                                        name="Server One",
                                                        url="http://server1:8096",
                                                        api_key="token",
                                                    ),
                                                },
                                            ),
                                            tvdb=config.TvdbConfig(api_key=None),
                                        )
                                        exit_code = auditor.main(
                                            [
                                                "--server", "server1",
                                                "--compare", "server2",
                                                "--transfer-metadata",
                                                "--yes",
                                                "--verify",
                                            ]
                                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_audit.call_count, 2)
        mock_compare.assert_called_once_with(
            base_result,
            compare_result,
            transfer_results=transfer_results,
            image_transfer_results=None,
            subtitle_transfer_results=None,
            left_csv_path=mock_csv.return_value,
            right_csv_path=mock_csv.return_value,
        )

    def test_main_with_verify_and_dry_run_skips_reaudit(self) -> None:
        base_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )
        compare_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server2",
            server_name="Server Two",
            server_url="http://server2:8096",
        )

        with patch(
            "auditor.audit_server", side_effect=[base_result, compare_result]
        ) as mock_audit:
            with patch("auditor.write_csv_report") as mock_csv:
                with patch("auditor.write_html_report"):
                    with patch("auditor.write_comparison_reports") as mock_compare:
                        with patch("auditor.get_config") as mock_get_config:
                            with patch("auditor.reset_audit_results_root"):
                                with patch("auditor.write_audit_results_index"):
                                    mock_get_config.return_value = config.AppConfig(
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
                                        processing=ProcessingConfig(
                                            enable_movies=True,
                                            enable_tv=True,
                                        ),
                                        servers=ServerCollection(
                                            default_server="server1",
                                            servers={
                                                "server1": ServerConfig(
                                                    key="server1",
                                                    name="Server One",
                                                    url="http://server1:8096",
                                                    api_key="token",
                                                ),
                                            },
                                        ),
                                        tvdb=config.TvdbConfig(api_key=None),
                                    )
                                    exit_code = auditor.main(
                                        [
                                            "--server", "server1",
                                            "--compare", "server2",
                                            "--transfer-metadata",
                                            "--dry-run",
                                            "--verify",
                                        ]
                                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(mock_audit.call_count, 2)
        mock_compare.assert_called_once_with(
            base_result,
            compare_result,
            transfer_results=(),
            image_transfer_results=None,
            subtitle_transfer_results=None,
            left_csv_path=mock_csv.return_value,
            right_csv_path=mock_csv.return_value,
        )

    def test_all_cannot_be_combined_with_compare(self) -> None:
        exit_code = auditor.main(["--all", "--compare", "server2"])

        self.assertEqual(exit_code, 2)

    def test_all_cannot_be_combined_with_server(self) -> None:
        exit_code = auditor.main(["--all", "--server", "server1"])

        self.assertEqual(exit_code, 2)

    def test_compare_requires_different_server(self) -> None:
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="server1",
            server_name="Server One",
            server_url="http://server1:8096",
        )

        with patch("auditor.audit_server", side_effect=[result, result]):
            exit_code = auditor.main(["--server", "server1", "--compare", "server1"])

        self.assertEqual(exit_code, 2)


class JellyfinClientMetadataTests(unittest.TestCase):
    def _make_client(self) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(server)

    def test_get_item_resolves_admin_user_and_requests_user_scoped_item(self) -> None:
        client = self._make_client()
        users_response = MagicMock()
        users_response.content = b"[]"
        users_response.json.return_value = [
            {"Id": "regular-user", "Policy": {"IsAdministrator": False}},
            {"Id": "admin-user", "Policy": {"IsAdministrator": True}},
        ]
        item_response = MagicMock()
        item_response.content = b'{"Id": "abc"}'
        item_response.json.return_value = {"Id": "abc", "Name": "Title"}
        with patch.object(
            client._session, "request", side_effect=[users_response, item_response]
        ) as mock_request:
            result = client.get_item("abc")

        self.assertEqual(mock_request.call_count, 2)
        mock_request.assert_any_call(
            method="GET",
            url="http://s:8096/Users",
            params=None,
            json=None,
            timeout=client._timeout,
        )
        mock_request.assert_any_call(
            method="GET",
            url="http://s:8096/Users/admin-user/Items/abc",
            params={"Fields": jellyfin.ITEM_DETAIL_FIELDS},
            json=None,
            timeout=client._timeout,
        )
        self.assertEqual(result, {"Id": "abc", "Name": "Title"})

    def test_get_item_caches_resolved_user_across_calls(self) -> None:
        client = self._make_client()
        users_response = MagicMock()
        users_response.content = b"[]"
        users_response.json.return_value = [{"Id": "only-user", "Policy": {}}]
        item_response = MagicMock()
        item_response.content = b"{}"
        item_response.json.return_value = {"Id": "abc"}
        with patch.object(
            client._session,
            "request",
            side_effect=[users_response, item_response, item_response],
        ) as mock_request:
            client.get_item("abc")
            client.get_item("def")

        self.assertEqual(mock_request.call_count, 3)

    def test_update_item_posts_full_item_document(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        with patch.object(client._session, "request", return_value=response) as mock_request:
            client.update_item("abc", {"Id": "abc", "Name": "New Title"})

        mock_request.assert_called_once_with(
            method="POST",
            url="http://s:8096/Items/abc",
            params=None,
            json={"Id": "abc", "Name": "New Title"},
            timeout=client._timeout,
        )

    def test_update_item_tolerates_empty_response_body(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        response.json.side_effect = ValueError("no body")
        with patch.object(client._session, "request", return_value=response):
            client.update_item("abc", {"Id": "abc"})

    def test_update_item_surfaces_error_response_body(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 500
        response.content = b'{"error": "NullReferenceException in UpdateItemHelper"}'
        response.text = '{"error": "NullReferenceException in UpdateItemHelper"}'
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(jellyfin.JellyfinRequestError) as context:
                client.update_item("abc", {"Id": "abc"})

        self.assertIn("status 500", str(context.exception))
        self.assertIn("NullReferenceException", str(context.exception))

    def test_get_item_image_returns_bytes_and_content_type(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 200
        response.content = b"\xff\xd8\xff\xe0fakejpeg"
        response.headers = {"Content-Type": "image/jpeg; charset=binary"}
        with patch.object(client._session, "request", return_value=response) as mock_request:
            result = client.get_item_image("abc", "Primary")

        mock_request.assert_called_once_with(
            method="GET",
            url="http://s:8096/Items/abc/Images/Primary",
            headers={"Accept": "*/*"},
            timeout=client._timeout,
        )
        self.assertEqual(result, (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg"))

    def test_get_item_image_returns_none_on_404(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 404
        with patch.object(client._session, "request", return_value=response):
            result = client.get_item_image("abc", "Backdrop")

        self.assertIsNone(result)

    def test_get_item_image_surfaces_other_errors(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 500
        response.content = b"boom"
        response.text = "boom"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client.get_item_image("abc", "Primary")

    def test_upload_item_image_posts_base64_body_with_content_type(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        with patch.object(client._session, "request", return_value=response) as mock_request:
            client.upload_item_image("abc", "Primary", b"rawbytes", "image/png")

        mock_request.assert_called_once_with(
            method="POST",
            url="http://s:8096/Items/abc/Images/Primary",
            data=base64.b64encode(b"rawbytes"),
            headers={"Content-Type": "image/png"},
            timeout=client._timeout,
        )

    def test_upload_item_image_surfaces_error(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 500
        response.content = b"boom"
        response.text = "boom"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client.upload_item_image("abc", "Primary", b"rawbytes", "image/png")


class JellyfinClientRetryTests(unittest.TestCase):
    def _make_client(self, *, max_retries: int = 3) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(
            server, max_retries=max_retries, retry_backoff_seconds=0.0
        )

    def test_recovers_after_a_read_timeout(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b'{"Items": []}'
        response.json.return_value = {"Items": []}
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.exceptions.ReadTimeout("timed out"), response],
        ) as mock_request, patch.object(jellyfin.time, "sleep") as mock_sleep:
            result = client._request(jellyfin.ITEMS_ENDPOINT)

        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once()
        self.assertEqual(result, {"Items": []})

    def test_recovers_after_a_connection_error(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.exceptions.ConnectionError("refused"), response],
        ) as mock_request, patch.object(jellyfin.time, "sleep"):
            client.update_item("abc", {"Id": "abc"})

        self.assertEqual(mock_request.call_count, 2)

    def test_raises_after_exhausting_all_retry_attempts(self) -> None:
        client = self._make_client(max_retries=2)
        with patch.object(
            client._session,
            "request",
            side_effect=requests.exceptions.ReadTimeout("timed out"),
        ) as mock_request, patch.object(jellyfin.time, "sleep") as mock_sleep:
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client._request(jellyfin.ITEMS_ENDPOINT)

        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once()

    def test_does_not_retry_on_non_transient_errors(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 404
        response.content = b"not found"
        response.text = "not found"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(
            client._session, "request", return_value=response
        ) as mock_request, patch.object(jellyfin.time, "sleep") as mock_sleep:
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client._request(jellyfin.ITEMS_ENDPOINT)

        mock_request.assert_called_once()
        mock_sleep.assert_not_called()


class TvdbClientTests(unittest.TestCase):
    def _make_client(self) -> tvdb.TvdbClient:
        return tvdb.TvdbClient("test-api-key")

    @staticmethod
    def _make_response(payload: object) -> MagicMock:
        response = MagicMock()
        response.content = b"{}"
        response.json.return_value = payload
        return response

    def test_get_series_episodes_logs_in_then_fetches_requested_ordering(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        episodes_response = self._make_response(
            {
                "data": {
                    "episodes": [
                        {
                            "id": 1,
                            "seasonNumber": 1,
                            "number": 1,
                            "name": "Pilot",
                            "overview": "The first episode.",
                            "runtime": 22,
                        },
                    ]
                }
            }
        )
        empty_page_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, episodes_response, empty_page_response],
        ) as mock_request:
            episodes = client.get_series_episodes("123", "dvd")

        self.assertEqual(mock_request.call_count, 3)
        mock_request.assert_any_call(
            method="POST",
            url="https://api4.thetvdb.com/v4/login",
            params=None,
            json={"apikey": "test-api-key"},
            timeout=client._timeout,
        )
        mock_request.assert_any_call(
            method="GET",
            url="https://api4.thetvdb.com/v4/series/123/episodes/dvd",
            params={"page": 0, "lang": "eng"},
            json=None,
            timeout=client._timeout,
        )
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].name, "Pilot")
        self.assertEqual(episodes[0].season_number, 1)
        self.assertEqual(episodes[0].episode_number, 1)
        self.assertEqual(episodes[0].overview, "The first episode.")
        self.assertEqual(episodes[0].runtime_minutes, 22)

    def test_get_series_episodes_reuses_cached_token_across_calls(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        empty_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, empty_response, empty_response],
        ) as mock_request:
            client.get_series_episodes("1", "official")
            client.get_series_episodes("2", "official")

        self.assertEqual(mock_request.call_count, 3)

    def test_get_series_episodes_paginates_until_an_empty_page(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})

        def _page(episode_id: int) -> MagicMock:
            return self._make_response(
                {
                    "data": {
                        "episodes": [
                            {
                                "id": episode_id,
                                "seasonNumber": 1,
                                "number": episode_id,
                                "name": f"Episode {episode_id}",
                            }
                        ]
                    }
                }
            )

        empty_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, _page(1), _page(2), empty_response],
        ):
            episodes = client.get_series_episodes("1", "official")

        self.assertEqual([episode.id for episode in episodes], [1, 2])

    def test_skips_episode_entries_missing_season_or_episode_number(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        page_response = self._make_response(
            {
                "data": {
                    "episodes": [
                        {"id": 1, "seasonNumber": None, "number": 1, "name": "Missing Season"},
                        {"id": 2, "seasonNumber": 1, "number": 2, "name": "Fine"},
                    ]
                }
            }
        )
        empty_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, page_response, empty_response],
        ):
            episodes = client.get_series_episodes("1", "official")

        self.assertEqual([episode.id for episode in episodes], [2])

    def test_wraps_http_errors_as_tvdb_request_error(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 401
        response.content = b"unauthorized"
        response.text = "unauthorized"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)

        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(tvdb.TvdbRequestError):
                client.get_series_episodes("1", "official")

    def test_rejects_blank_api_key(self) -> None:
        with self.assertRaises(tvdb.TvdbConfigurationError):
            tvdb.TvdbClient("   ")

    def test_get_series_episodes_returns_cached_result_without_any_http_call(self) -> None:
        cached_episode = _make_tvdb_episode(season_number=1, episode_number=1, name="Cached")
        cache = MagicMock()
        cache.get.return_value = (cached_episode,)
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request") as mock_request:
            episodes = client.get_series_episodes("123", "official")

        mock_request.assert_not_called()
        self.assertEqual(episodes, (cached_episode,))
        cache.get.assert_called_once_with("123", "official")

    def test_get_series_episodes_populates_cache_on_a_miss(self) -> None:
        cache = MagicMock()
        cache.get.return_value = None
        client = tvdb.TvdbClient("test-api-key", cache=cache)
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        episodes_response = self._make_response(
            {"data": {"episodes": [{"id": 1, "seasonNumber": 1, "number": 1, "name": "Pilot"}]}}
        )
        empty_page_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, episodes_response, empty_page_response],
        ):
            episodes = client.get_series_episodes("123", "official")

        cache.set.assert_called_once_with("123", "official", episodes)

    def test_get_series_episodes_records_series_name_when_given(self) -> None:
        cache = MagicMock()
        cache.get.return_value = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Cached"),
        )
        cache.get_series_name.return_value = None
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request") as mock_request:
            client.get_series_episodes("123", "official", series_name="Ghosts")

        mock_request.assert_not_called()
        cache.set_series_name.assert_called_once_with("123", "Ghosts")

    def test_get_series_episodes_does_not_rewrite_an_unchanged_series_name(self) -> None:
        cache = MagicMock()
        cache.get.return_value = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Cached"),
        )
        cache.get_series_name.return_value = "Ghosts"
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        client.get_series_episodes("123", "official", series_name="Ghosts")

        cache.set_series_name.assert_not_called()

    def test_get_series_episodes_does_not_record_a_series_name_when_not_given(self) -> None:
        cache = MagicMock()
        cache.get.return_value = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Cached"),
        )
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        client.get_series_episodes("123", "official")

        cache.set_series_name.assert_not_called()

    def test_search_series_logs_in_then_returns_results(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        search_response = self._make_response(
            {
                "data": [
                    {
                        "tvdb_id": "400267",
                        "name": "Ghosts",
                        "year": "2021",
                        "overview": "A US sitcom.",
                    },
                    {
                        "tvdb_id": "361701",
                        "name": "Ghosts",
                        "year": "2019",
                        "overview": "A UK sitcom.",
                    },
                ]
            }
        )

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, search_response],
        ) as mock_request:
            results = client.search_series("Ghosts")

        mock_request.assert_any_call(
            method="GET",
            url="https://api4.thetvdb.com/v4/search",
            params={"query": "Ghosts", "type": "series"},
            json=None,
            timeout=client._timeout,
        )
        self.assertEqual([result.id for result in results], ["400267", "361701"])
        self.assertEqual(results[0].name, "Ghosts")
        self.assertEqual(results[0].year, "2021")
        self.assertEqual(results[0].overview, "A US sitcom.")

    def test_search_series_falls_back_to_id_field_when_tvdb_id_missing(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        search_response = self._make_response(
            {"data": [{"id": "series-400267", "name": "Ghosts"}]}
        )

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, search_response],
        ):
            results = client.search_series("Ghosts")

        self.assertEqual(results[0].id, "400267")

    def test_search_series_returns_cached_result_without_any_http_call(self) -> None:
        cached_result = _make_tvdb_search_result(series_id="400267", name="Ghosts")
        cache = MagicMock()
        cache.get_search.return_value = (cached_result,)
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request") as mock_request:
            results = client.search_series("Ghosts")

        mock_request.assert_not_called()
        self.assertEqual(results, (cached_result,))
        cache.get_search.assert_called_once_with("Ghosts")

    def test_search_series_populates_cache_on_a_miss(self) -> None:
        cache = MagicMock()
        cache.get_search.return_value = None
        client = tvdb.TvdbClient("test-api-key", cache=cache)
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        search_response = self._make_response(
            {"data": [{"tvdb_id": "400267", "name": "Ghosts"}]}
        )

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, search_response],
        ):
            results = client.search_series("Ghosts")

        cache.set_search.assert_called_once_with("Ghosts", results)


class TvdbEpisodeCacheTests(unittest.TestCase):
    def test_returns_none_for_an_empty_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")

            self.assertIsNone(cache.get("123", "official"))

    def test_returns_a_value_stored_within_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(
                Path(temp_dir) / "cache.json",
                ttl=timedelta(days=7),
            )
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("123", "official", episodes)

            self.assertEqual(cache.get("123", "official"), episodes)

    def test_returns_none_once_the_entry_is_older_than_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)
            cache.set("123", "official", episodes)

            stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            document["series"]["123"]["official"]["fetched_at"] = stale_timestamp
            cache_path.write_text(json.dumps(document), encoding="utf-8")

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))

            self.assertIsNone(reloaded_cache.get("123", "official"))

    def test_force_refresh_always_misses_on_read_but_still_persists_writes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path, force_refresh=True)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("123", "official", episodes)

            self.assertIsNone(cache.get("123", "official"))
            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path, force_refresh=False)
            self.assertEqual(reloaded_cache.get("123", "official"), episodes)

    def test_persists_across_separate_cache_instances(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            episodes = (
                _make_tvdb_episode(
                    season_number=1,
                    episode_number=1,
                    name="Pilot",
                    overview="An overview.",
                    runtime_minutes=42,
                ),
            )
            tvdb.TvdbEpisodeCache(cache_path).set("123", "dvd", episodes)

            second_cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertEqual(second_cache.get("123", "dvd"), episodes)

    def test_tolerates_a_missing_cache_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "does-not-exist.json")

            self.assertIsNone(cache.get("123", "official"))

    def test_tolerates_a_corrupt_cache_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache_path.write_text("not valid json", encoding="utf-8")

            cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertIsNone(cache.get("123", "official"))

    def test_distinguishes_between_season_types_for_the_same_series(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")
            official_episodes = (
                _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),
            )
            dvd_episodes = (
                _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),
            )

            cache.set("123", "official", official_episodes)
            cache.set("123", "dvd", dvd_episodes)

            self.assertEqual(cache.get("123", "official"), official_episodes)
            self.assertEqual(cache.get("123", "dvd"), dvd_episodes)

    def test_search_returns_none_for_an_empty_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")

            self.assertIsNone(cache.get_search("Ghosts"))

    def test_search_returns_a_value_stored_within_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(
                Path(temp_dir) / "cache.json",
                ttl=timedelta(days=7),
            )
            results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)

            cache.set_search("Ghosts", results)

            self.assertEqual(cache.get_search("Ghosts"), results)

    def test_search_key_is_case_and_whitespace_insensitive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")
            results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)

            cache.set_search("  Ghosts ", results)

            self.assertEqual(cache.get_search("ghosts"), results)

    def test_search_returns_none_once_the_entry_is_older_than_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))
            results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)
            cache.set_search("Ghosts", results)

            stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            document["searches"]["ghosts"]["fetched_at"] = stale_timestamp
            cache_path.write_text(json.dumps(document), encoding="utf-8")

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))

            self.assertIsNone(reloaded_cache.get_search("Ghosts"))

    def test_search_persists_across_separate_cache_instances(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            results = (
                _make_tvdb_search_result(
                    series_id="400267",
                    name="Ghosts",
                    year="2021",
                    overview="A US sitcom.",
                ),
            )
            tvdb.TvdbEpisodeCache(cache_path).set_search("Ghosts", results)

            second_cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertEqual(second_cache.get_search("Ghosts"), results)

    def test_search_cache_does_not_disturb_episode_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)
            search_results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)

            cache.set("400267", "official", episodes)
            cache.set_search("Ghosts", search_results)

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path)
            self.assertEqual(reloaded_cache.get("400267", "official"), episodes)
            self.assertEqual(reloaded_cache.get_search("Ghosts"), search_results)

    def test_returns_none_for_an_unknown_series_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")

            self.assertIsNone(cache.get_series_name("400267"))

    def test_series_name_persists_across_separate_cache_instances(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            tvdb.TvdbEpisodeCache(cache_path).set_series_name("400267", "Ghosts")

            second_cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertEqual(second_cache.get_series_name("400267"), "Ghosts")

    def test_series_name_survives_alongside_episode_and_search_entries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("400267", "official", episodes)
            cache.set_series_name("400267", "Ghosts")

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path)
            self.assertEqual(reloaded_cache.get("400267", "official"), episodes)
            self.assertEqual(reloaded_cache.get_series_name("400267"), "Ghosts")

    def test_series_name_appears_in_the_raw_cache_document(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("400267", "official", episodes)
            cache.set_series_name("400267", "Ghosts")

            document = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(document["series"]["400267"]["name"], "Ghosts")
            self.assertIn("official", document["series"]["400267"])


class TransferMetadataMergeTests(unittest.TestCase):
    def test_overwrites_only_transferable_fields(self) -> None:
        source_dto = {
            "Id": "source-id",
            "Name": "Correct Title",
            "Overview": "Correct overview",
            "Genres": ["Drama"],
            "ImageTags": {"Primary": "sourcetag"},
        }
        destination_dto = {
            "Id": "dest-id",
            "ServerId": "dest-server",
            "Path": "/media/dest/file.mkv",
            "Name": "Wrong Title",
            "Overview": "Wrong overview",
            "Genres": [],
            "ImageTags": {"Primary": "desttag"},
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["Name"], "Correct Title")
        self.assertEqual(merged["Overview"], "Correct overview")
        self.assertEqual(merged["Genres"], ["Drama"])
        self.assertEqual(merged["Id"], "dest-id")
        self.assertEqual(merged["ServerId"], "dest-server")
        self.assertEqual(merged["Path"], "/media/dest/file.mkv")
        self.assertEqual(merged["ImageTags"], {"Primary": "desttag"})

    def test_omits_non_editable_fields_even_when_present_on_destination(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title"}
        destination_dto = {
            "Id": "dest-id",
            "Name": "Wrong Title",
            "Trickplay": {"abc": {"1": {"whatever": True}}},
            "MediaSources": [{"Id": "media-source"}],
            "UserData": {"Played": True},
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertNotIn("Trickplay", merged)
        self.assertNotIn("MediaSources", merged)
        self.assertNotIn("UserData", merged)

    def test_preserves_identity_and_hierarchy_fields_not_in_transferable_set(self) -> None:
        """Regression test: fields like IndexNumber must never be dropped from the
        outgoing payload, since Jellyfin's update endpoint clears any field the
        request omits rather than leaving it untouched."""
        source_dto = {"Id": "source-id", "Name": "Correct Title"}
        destination_dto = {
            "Id": "dest-id",
            "Name": "Wrong Title",
            "Type": "Episode",
            "SeriesId": "series-id",
            "SeasonId": "season-id",
            "IndexNumber": 6,
            "ParentIndexNumber": 1,
            "Path": "/media/dest/Show/Season 01/Show.S01E06.mkv",
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["Type"], "Episode")
        self.assertEqual(merged["SeriesId"], "series-id")
        self.assertEqual(merged["SeasonId"], "season-id")
        self.assertEqual(merged["IndexNumber"], 6)
        self.assertEqual(merged["ParentIndexNumber"], 1)
        self.assertEqual(merged["Path"], "/media/dest/Show/Season 01/Show.S01E06.mkv")

    def test_explicit_null_on_source_does_not_clobber_destination_value(self) -> None:
        """Regression test: Jellyfin returns explicit nulls for unset fields (e.g. an
        episode with no standalone ProductionYear), and that null must not overwrite
        a real value the destination already has."""
        source_dto = {"Id": "source-id", "Name": "Correct Title", "ProductionYear": None}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title", "ProductionYear": 2019}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["ProductionYear"], 2019)

    def test_transfers_episode_and_season_number_when_source_has_a_value(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title", "IndexNumber": 9, "ParentIndexNumber": 1}
        destination_dto = {
            "Id": "dest-id",
            "Name": "Wrong Title",
            "IndexNumber": 10,
            "ParentIndexNumber": 2,
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["IndexNumber"], 9)
        self.assertEqual(merged["ParentIndexNumber"], 1)

    def test_null_episode_number_on_source_does_not_clobber_destination_value(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title", "IndexNumber": None}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title", "IndexNumber": 10}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["IndexNumber"], 10)

    def test_leaves_destination_value_when_source_field_missing(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Only Name"}
        destination_dto = {"Id": "dest-id", "Name": "Old Name", "Overview": "Kept overview"}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["Name"], "Only Name")
        self.assertEqual(merged["Overview"], "Kept overview")


class SkippedNullSourceFieldsTests(unittest.TestCase):
    def test_reports_fields_with_no_source_value(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title", "IndexNumber": None}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "IndexNumber": 10}

        skipped = transfer_metadata._skipped_null_source_fields(source_dto, destination_dto)

        self.assertEqual(skipped, ("IndexNumber",))

    def test_omits_fields_where_destination_also_has_no_value(self) -> None:
        source_dto = {"Id": "src", "IndexNumber": None}
        destination_dto = {"Id": "dst", "IndexNumber": None}

        skipped = transfer_metadata._skipped_null_source_fields(source_dto, destination_dto)

        self.assertEqual(skipped, ())

    def test_omits_fields_the_source_actually_has_a_value_for(self) -> None:
        source_dto = {"Id": "src", "IndexNumber": 9}
        destination_dto = {"Id": "dst", "IndexNumber": 10}

        skipped = transfer_metadata._skipped_null_source_fields(source_dto, destination_dto)

        self.assertEqual(skipped, ())


class MissingImageTransferTargetsTests(unittest.TestCase):
    def _make_single_item_result(
        self, *, item, server_name: str, server_key: str
    ) -> AuditServerResult:
        return AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies", name="Movies", collection_type="movies"
                    ),
                    media_items_processed=1,
                    audited_items=(item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name=server_name,
            server_key=server_key,
            server_url=f"http://{server_key}:8096",
        )

    def test_returns_one_target_per_artwork_difference(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={"Primary": "left-tag"},
        )
        right_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={},
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.missing_image_transfer_targets(left_result, right_result)

        self.assertEqual(
            targets,
            (
                ImageTransferTarget(
                    library="Movies",
                    display_name=left_item.display_name,
                    left_title=left_item.title,
                    left_server_key="left",
                    left_item_id=left_item.id,
                    right_server_key="right",
                    right_item_id=right_item.id,
                ),
            ),
        )

    def test_returns_nothing_when_only_destination_has_the_image(self) -> None:
        left_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), image_tags={}
        )
        right_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={"Primary": "right-tag"},
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.missing_image_transfer_targets(left_result, right_result)

        self.assertEqual(targets, ())

    def test_returns_nothing_when_artwork_matches(self) -> None:
        left_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), image_tags={}
        )
        right_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), image_tags={}
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.missing_image_transfer_targets(left_result, right_result)

        self.assertEqual(targets, ())

    def test_returns_nothing_when_a_server_key_is_missing(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={"Primary": "left-tag"},
        )
        right_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), image_tags={}
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies", name="Movies", collection_type="movies"
                    ),
                    media_items_processed=1,
                    audited_items=(right_item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name="Right Server",
            server_key=None,
        )

        targets = comparison_generator.missing_image_transfer_targets(left_result, right_result)

        self.assertEqual(targets, ())


class MismatchedMetadataTransferTargetsTests(unittest.TestCase):
    def _make_tv_result(
        self, *, items, server_name: str, server_key: str
    ) -> AuditServerResult:
        return AuditServerResult(
            libraries_audited=1,
            media_items_processed=len(items),
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="tv", name="TV Shows", collection_type="tv"
                    ),
                    media_items_processed=len(items),
                    audited_items=tuple(items),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name=server_name,
            server_key=server_key,
            server_url=f"http://{server_key}:8096",
        )

    def test_episode_target_carries_series_name_and_season_number(self) -> None:
        left_item = _make_item(
            title="Correct Title",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Show Name",
            season_number=2,
            episode_number=3,
            path=Path("Show Name/Season 02/Show Name S02E03.mkv"),
        )
        right_item = _make_item(
            title="Wrong Title",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Show Name",
            season_number=2,
            episode_number=3,
            path=Path("Show Name/Season 02/Show Name S02E03.mkv"),
        )
        left_result = self._make_tv_result(
            items=(left_item,), server_name="Left Server", server_key="left"
        )
        right_result = self._make_tv_result(
            items=(right_item,), server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.mismatched_metadata_transfer_targets(
            left_result, right_result
        )

        self.assertEqual(len(targets), 1)
        self.assertEqual(targets[0].series_name, "Show Name")
        self.assertEqual(targets[0].season_number, 2)

    def test_movie_target_has_no_series_name_or_season_number(self) -> None:
        left_item = _make_item(title="Correct Title", library="Movies", path=Path("Movie.mkv"))
        right_item = _make_item(title="Wrong Title", library="Movies", path=Path("Movie.mkv"))
        left_result = self._make_tv_result(
            items=(left_item,), server_name="Left Server", server_key="left"
        )
        right_result = self._make_tv_result(
            items=(right_item,), server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.mismatched_metadata_transfer_targets(
            left_result, right_result
        )

        self.assertEqual(len(targets), 1)
        self.assertIsNone(targets[0].series_name)
        self.assertIsNone(targets[0].season_number)


class MissingSubtitleTransferTargetsTests(unittest.TestCase):
    def _make_single_item_result(
        self, *, item, server_name: str, server_key: str
    ) -> AuditServerResult:
        return AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies", name="Movies", collection_type="movies"
                    ),
                    media_items_processed=1,
                    audited_items=(item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name=server_name,
            server_key=server_key,
            server_url=f"http://{server_key}:8096",
        )

    def _english_subtitle_track(self) -> SubtitleTrack:
        return SubtitleTrack(
            language="eng",
            codec="srt",
            is_external=True,
            is_default=False,
            is_forced=False,
        )

    def test_returns_one_target_when_only_source_has_english_subtitle(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            subtitle_tracks=(self._english_subtitle_track(),),
        )
        right_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), subtitle_tracks=()
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.missing_subtitle_transfer_targets(left_result, right_result)

        self.assertEqual(
            targets,
            (
                SubtitleTransferTarget(
                    library="Movies",
                    display_name=left_item.display_name,
                    left_server_key="left",
                    left_item_id=left_item.id,
                    right_server_key="right",
                    right_item_id=right_item.id,
                ),
            ),
        )

    def test_returns_nothing_when_only_destination_has_english_subtitle(self) -> None:
        left_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), subtitle_tracks=()
        )
        right_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            subtitle_tracks=(self._english_subtitle_track(),),
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.missing_subtitle_transfer_targets(left_result, right_result)

        self.assertEqual(targets, ())

    def test_returns_nothing_when_subtitles_match(self) -> None:
        left_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), subtitle_tracks=()
        )
        right_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), subtitle_tracks=()
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        targets = comparison_generator.missing_subtitle_transfer_targets(left_result, right_result)

        self.assertEqual(targets, ())


class ComparisonSummaryCountsTests(unittest.TestCase):
    def _make_single_item_result(
        self, *, item, server_name: str, server_key: str
    ) -> AuditServerResult:
        return AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(
                LibraryAuditResult(
                    library=_make_library(
                        library_id="movies", name="Movies", collection_type="movies"
                    ),
                    media_items_processed=1,
                    audited_items=(item,),
                    items_with_english_subtitles=0,
                    items_with_local_nfo=0,
                    items_with_local_backdrop=0,
                    findings=(),
                ),
            ),
            findings=(),
            server_name=server_name,
            server_key=server_key,
            server_url=f"http://{server_key}:8096",
        )

    def test_counts_reflect_remaining_artwork_difference(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={"Primary": "left-tag"},
        )
        right_item = _make_item(
            title="Alien", library="Movies", path=Path("Alien.mkv"), image_tags={}
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        counts = comparison_generator.comparison_summary_counts(left_result, right_result)

        self.assertEqual(counts["artwork_differences"], 1)
        self.assertEqual(counts["mismatched_metadata"], 0)
        self.assertEqual(counts["missing_media"], 0)

    def test_counts_are_zero_when_servers_match(self) -> None:
        left_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={"Primary": "same-tag"},
        )
        right_item = _make_item(
            title="Alien",
            library="Movies",
            path=Path("Alien.mkv"),
            image_tags={"Primary": "same-tag"},
        )
        left_result = self._make_single_item_result(
            item=left_item, server_name="Left Server", server_key="left"
        )
        right_result = self._make_single_item_result(
            item=right_item, server_name="Right Server", server_key="right"
        )

        counts = comparison_generator.comparison_summary_counts(left_result, right_result)

        self.assertEqual(
            counts,
            {
                "missing_libraries": 0,
                "missing_media": 0,
                "missing_seasons": 0,
                "missing_episodes": 0,
                "mismatched_metadata": 0,
                "artwork_differences": 0,
                "subtitle_differences": 0,
            },
        )


class TransferImagesPlanTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self, images: dict[tuple[str, str], tuple[bytes, str]]):
            self._images = images
            self.uploaded: list[tuple[str, str, bytes, str]] = []

        def get_item_image(self, item_id: str, image_type: str):
            return self._images.get((item_id, image_type))

        def upload_item_image(self, item_id: str, image_type: str, image_bytes: bytes, content_type: str) -> None:
            self.uploaded.append((item_id, image_type, image_bytes, content_type))

    def test_plan_reads_source_image(self) -> None:
        from_client = self._FakeClient({("src", "Primary"): (b"bytes", "image/jpeg")})
        to_client = self._FakeClient({})

        plan = transfer_images.plan_image_transfer(from_client, to_client, "src", "dst", "Primary")

        self.assertTrue(plan.has_image)
        self.assertEqual(plan.image_bytes, b"bytes")
        self.assertEqual(plan.content_type, "image/jpeg")

    def test_plan_has_no_image_when_source_lacks_one(self) -> None:
        from_client = self._FakeClient({})
        to_client = self._FakeClient({})

        plan = transfer_images.plan_image_transfer(from_client, to_client, "src", "dst", "Backdrop")

        self.assertFalse(plan.has_image)
        self.assertIsNone(plan.image_bytes)
        self.assertIsNone(plan.content_type)

    def test_apply_uploads_planned_image_to_destination(self) -> None:
        from_client = self._FakeClient({("src", "Primary"): (b"bytes", "image/jpeg")})
        to_client = self._FakeClient({})
        plan = transfer_images.plan_image_transfer(from_client, to_client, "src", "dst", "Primary")

        transfer_images.apply_image_transfer(to_client, plan)

        self.assertEqual(to_client.uploaded, [("dst", "Primary", b"bytes", "image/jpeg")])


class TransferImageCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "transfer_images.IMAGE_TRANSFER_LOG_FILE",
            Path(temp_dir.name) / "image_transfer.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
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
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                    "right": ServerConfig(
                        key="right", name="Right", url="http://right:8096", api_key="right-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key=None),
        )

    def _make_fake_client(self, source_dto, destination_dtos_by_call, images, upload_calls):
        """``destination_dtos_by_call`` lets get_item("dst") return a different
        dto before vs. after the upload, so tests can assert the post-upload
        ImageTags re-check actually reflects the new value."""
        calls = {"get_destination": 0}

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_item(self, item_id):
                if self.server.key == "left":
                    return source_dto
                index = min(calls["get_destination"], len(destination_dtos_by_call) - 1)
                calls["get_destination"] += 1
                return destination_dtos_by_call[index]

            def get_item_image(self, item_id, image_type):
                return images.get((item_id, image_type))

            def upload_item_image(self, item_id, image_type, image_bytes, content_type):
                upload_calls.append((self.server.key, item_id, image_type, image_bytes, content_type))

        return FakeClient

    def test_transfers_image_after_confirmation_and_reports_new_tag(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_before = {"Id": "dst", "Name": "Correct Title", "ImageTags": {}}
        destination_after = {"Id": "dst", "Name": "Correct Title", "ImageTags": {"Primary": "new-tag"}}
        images = {("src-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            source_dto, [destination_before, destination_after], images, upload_calls
        )

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = transfer_images.transfer_image(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        image_type="Primary",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [("right", "dst-id", "Primary", b"bytes", "image/jpeg")])

    def test_reports_when_source_has_no_image(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "ImageTags": {}}
        upload_calls: list = []
        fake_client = self._make_fake_client(source_dto, [destination_dto], {}, upload_calls)

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                exit_code = transfer_images.transfer_image(
                    from_server_key="left",
                    from_item_id="src-id",
                    to_server_key="right",
                    to_item_id="dst-id",
                    image_type="Backdrop",
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [])

    def test_aborts_when_confirmation_declined(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "ImageTags": {}}
        images = {("src-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(source_dto, [destination_dto], images, upload_calls)

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="n"):
                    exit_code = transfer_images.transfer_image(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        image_type="Primary",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 1)
        self.assertEqual(upload_calls, [])

    def test_writes_transfer_details_to_log_file(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "ImageTags": {"Primary": "new-tag"}}
        images = {("src-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(source_dto, [destination_dto], images, upload_calls)

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    transfer_images.transfer_image(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        image_type="Primary",
                        assume_yes=False,
                    )

        log_text = transfer_images.IMAGE_TRANSFER_LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("Upload complete", log_text)
        self.assertIn("new-tag", log_text)


class TransferMetadataCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "transfer_metadata.METADATA_TRANSFER_LOG_FILE",
            Path(temp_dir.name) / "metadata_transfer.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
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
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                    "right": ServerConfig(
                        key="right", name="Right", url="http://right:8096", api_key="right-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key=None),
        )

    def _make_fake_client(self, source_dto, destination_dto, update_calls):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_item(self, item_id):
                return source_dto if self.server.key == "left" else destination_dto

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))

        return FakeClient

    def test_transfers_metadata_after_confirmation(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "dst-id")
        self.assertEqual(updated_dto["Name"], "Correct Title")
        self.assertEqual(updated_dto["Id"], "dst")

    def test_writes_transfer_details_to_log_file(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        log_contents = transfer_metadata.METADATA_TRANSFER_LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("Transfer metadata: Left -> Right", log_contents)
        self.assertIn("Name: 'Wrong Title' -> 'Correct Title'", log_contents)
        self.assertIn("Metadata transfer complete.", log_contents)

    def test_appends_across_multiple_runs_instead_of_truncating(self) -> None:
        source_dto = {"Id": "src", "Name": "Same Title"}
        destination_dto = {"Id": "dst", "Name": "Same Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                transfer_metadata.transfer_metadata(
                    from_server_key="left", from_item_id="src-id",
                    to_server_key="right", to_item_id="dst-id", assume_yes=True,
                )
                transfer_metadata.transfer_metadata(
                    from_server_key="left", from_item_id="src-id",
                    to_server_key="right", to_item_id="dst-id", assume_yes=True,
                )

        log_contents = transfer_metadata.METADATA_TRANSFER_LOG_FILE.read_text(encoding="utf-8")
        self.assertEqual(log_contents.count("Transfer metadata: Left -> Right"), 2)

    def test_aborts_when_user_declines_confirmation(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="n"):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])

    def test_assume_yes_skips_confirmation_prompt(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called when assume_yes=True")

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", side_effect=_unexpected_input):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)

    def test_no_prompt_when_nothing_to_transfer(self) -> None:
        source_dto = {"Id": "src", "Name": "Same Title"}
        destination_dto = {"Id": "dst", "Name": "Same Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called when there is nothing to transfer")

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", side_effect=_unexpected_input):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_prints_note_when_source_has_no_value_for_a_field(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title", "IndexNumber": None}
        destination_dto = {
            "Id": "dst",
            "Name": "Wrong Title",
            "Path": "/media/dst/file.mkv",
            "IndexNumber": 10,
        }
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        captured_stdout = io.StringIO()
        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    with contextlib.redirect_stdout(captured_stdout):
                        exit_code = transfer_metadata.transfer_metadata(
                            from_server_key="left",
                            from_item_id="src-id",
                            to_server_key="right",
                            to_item_id="dst-id",
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        output = captured_stdout.getvalue()
        self.assertIn("no value for these fields", output)
        self.assertIn("IndexNumber", output)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1]["IndexNumber"], 10)

    def test_refuses_to_update_when_destination_path_missing(self) -> None:
        """Regression test: Jellyfin cleared Path on an update whose payload omitted
        it, turning a real episode into a pathless "virtual" item that Jellyfin's
        library scanner then deleted. The transfer must refuse rather than repeat
        that."""
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                exit_code = transfer_metadata.transfer_metadata(
                    from_server_key="left",
                    from_item_id="src-id",
                    to_server_key="right",
                    to_item_id="dst-id",
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])

    def test_unknown_server_key_returns_usage_error(self) -> None:
        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            exit_code = transfer_metadata.transfer_metadata(
                from_server_key="missing",
                from_item_id="x",
                to_server_key="right",
                to_item_id="y",
                assume_yes=True,
            )

        self.assertEqual(exit_code, 2)


class TransferCellRenderingTests(unittest.TestCase):
    def test_omits_button_when_server_key_missing(self) -> None:
        html = comparison_generator._transfer_cell("", "left-item", "right", "right-item")

        self.assertEqual(html, '<td class="transfer-cell"></td>')

    def test_builds_copy_command_with_quoted_arguments(self) -> None:
        html = comparison_generator._transfer_cell("left", "abc123", "right", "def456")

        self.assertIn(
            'data-command="python transfer_metadata.py --from-server &quot;left&quot; '
            '--from-item &quot;abc123&quot; --to-server &quot;right&quot; '
            '--to-item &quot;def456&quot;"',
            html,
        )
        self.assertIn('onclick="copyTransferCommand(this)">&#8594;</button>', html)


class SubtitleTransferCellRenderingTests(unittest.TestCase):
    def test_omits_button_when_server_key_missing(self) -> None:
        html = comparison_generator._subtitle_transfer_cell(
            "", "left-item", True, "right", "right-item", False
        )

        self.assertEqual(html, '<td class="transfer-cell"></td>')

    def test_omits_button_when_subtitle_presence_matches(self) -> None:
        html = comparison_generator._subtitle_transfer_cell(
            "left", "left-item", True, "right", "right-item", True
        )

        self.assertEqual(html, '<td class="transfer-cell"></td>')

    def test_points_right_when_left_has_subtitles(self) -> None:
        html = comparison_generator._subtitle_transfer_cell(
            "left", "abc123", True, "right", "def456", False
        )

        self.assertIn(
            'data-command="python transfer_subtitles.py --from-server &quot;left&quot; '
            '--from-item &quot;abc123&quot; --to-server &quot;right&quot; '
            '--to-item &quot;def456&quot;"',
            html,
        )
        self.assertIn('onclick="copyTransferCommand(this)">&#8594;</button>', html)

    def test_points_left_when_right_has_subtitles(self) -> None:
        html = comparison_generator._subtitle_transfer_cell(
            "left", "abc123", False, "right", "def456", True
        )

        self.assertIn(
            'data-command="python transfer_subtitles.py --from-server &quot;right&quot; '
            '--from-item &quot;def456&quot; --to-server &quot;left&quot; '
            '--to-item &quot;abc123&quot;"',
            html,
        )
        self.assertIn('onclick="copyTransferCommand(this)">&#8592;</button>', html)


class TransferResultsSectionRenderingTests(unittest.TestCase):
    def test_row_shows_status_and_changed_fields(self) -> None:
        result = MetadataTransferResult(
            library="TV Shows",
            display_name="Show.S01E01",
            status="transferred",
            changed_fields=("Name", "Overview"),
        )

        row_html = comparison_generator._transfer_result_row(result)

        self.assertIn("<td>TV Shows</td>", row_html)
        self.assertIn("<td>Show.S01E01</td>", row_html)
        self.assertIn("status-present", row_html)
        self.assertIn("transferred", row_html)
        self.assertIn("<td>Name, Overview</td>", row_html)

    def test_row_shows_rejection_detail(self) -> None:
        result = MetadataTransferResult(
            library="TV Shows",
            display_name="Show.S01E02",
            status="rejected",
            detail="the destination item is missing required field(s) Path",
        )

        row_html = comparison_generator._transfer_result_row(result)

        self.assertIn("status-missing", row_html)
        self.assertIn("rejected", row_html)
        self.assertIn("missing required field(s) Path", row_html)

    def test_section_renders_one_row_per_result(self) -> None:
        results = (
            MetadataTransferResult(library="TV Shows", display_name="A", status="transferred"),
            MetadataTransferResult(library="TV Shows", display_name="B", status="unchanged"),
        )

        section_html = comparison_generator._transfer_results_section(results)

        self.assertIn("Transfer Results", section_html)
        self.assertIn(">A<", section_html)
        self.assertIn(">B<", section_html)

    def test_libraries_page_omits_section_when_transfer_results_is_none(self) -> None:
        left_result, right_result = _make_empty_comparison_results()
        comparison = comparison_generator._build_comparison(left_result, right_result)

        html = comparison_generator._libraries_page(left_result, right_result, comparison)

        self.assertNotIn("Transfer Results", html)

    def test_libraries_page_includes_section_when_transfer_results_given(self) -> None:
        left_result, right_result = _make_empty_comparison_results()
        comparison = comparison_generator._build_comparison(left_result, right_result)
        transfer_results = (
            MetadataTransferResult(library="TV Shows", display_name="A", status="transferred"),
        )

        html = comparison_generator._libraries_page(
            left_result, right_result, comparison, transfer_results=transfer_results
        )

        self.assertIn("Transfer Results", html)
        self.assertIn(">A<", html)

    def test_libraries_page_includes_empty_section_for_zero_results(self) -> None:
        """--transfer-metadata with nothing to transfer should still show the table
        (empty), distinguishing "ran with nothing to do" from "didn't run"."""
        left_result, right_result = _make_empty_comparison_results()
        comparison = comparison_generator._build_comparison(left_result, right_result)

        html = comparison_generator._libraries_page(
            left_result, right_result, comparison, transfer_results=()
        )

        self.assertIn("Transfer Results", html)


class ImageTransferResultsSectionRenderingTests(unittest.TestCase):
    def test_row_shows_status_and_image_type(self) -> None:
        result = ImageTransferResult(
            library="Movies",
            display_name="Alien",
            image_type="Primary",
            status="transferred",
        )

        row_html = comparison_generator._image_transfer_result_row(result)

        self.assertIn("<td>Movies</td>", row_html)
        self.assertIn("<td>Alien</td>", row_html)
        self.assertIn("<td>Primary</td>", row_html)
        self.assertIn("status-present", row_html)
        self.assertIn("transferred", row_html)

    def test_row_shows_unavailable_status(self) -> None:
        result = ImageTransferResult(
            library="Movies", display_name="Alien", image_type="Backdrop", status="unavailable"
        )

        row_html = comparison_generator._image_transfer_result_row(result)

        self.assertIn("no source image", row_html)

    def test_section_renders_one_row_per_result(self) -> None:
        results = (
            ImageTransferResult(library="Movies", display_name="A", image_type="Primary", status="transferred"),
            ImageTransferResult(library="Movies", display_name="B", image_type="Backdrop", status="unavailable"),
        )

        section_html = comparison_generator._image_transfer_results_section(results)

        self.assertIn("Image Transfer Results", section_html)
        self.assertIn(">A<", section_html)
        self.assertIn(">B<", section_html)

    def test_artwork_page_omits_section_when_image_transfer_results_is_none(self) -> None:
        left_result, right_result = _make_empty_comparison_results()
        comparison = comparison_generator._build_comparison(left_result, right_result)

        html = comparison_generator._artwork_page(left_result, right_result, comparison)

        self.assertNotIn("Image Transfer Results", html)

    def test_artwork_page_includes_section_when_image_transfer_results_given(self) -> None:
        left_result, right_result = _make_empty_comparison_results()
        comparison = comparison_generator._build_comparison(left_result, right_result)
        image_transfer_results = (
            ImageTransferResult(library="Movies", display_name="A", image_type="Primary", status="transferred"),
        )

        html = comparison_generator._artwork_page(
            left_result, right_result, comparison, image_transfer_results=image_transfer_results
        )

        self.assertIn("Image Transfer Results", html)
        self.assertIn(">A<", html)


class MetadataTransferFileLoggingTests(unittest.TestCase):
    def _isolated_logger(self, logger: logging.Logger) -> list[logging.Handler]:
        """Snapshot a logger's handlers and restore them after the test."""
        handlers_before = list(logger.handlers)

        def _remove_added_handlers() -> None:
            for handler in list(logger.handlers):
                if handler not in handlers_before:
                    logger.removeHandler(handler)
                    handler.close()

        self.addCleanup(_remove_added_handlers)
        return handlers_before

    def test_metadata_transfer_logging_attaches_to_its_own_logger_only(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "metadata_transfer.log"
        log_patch = patch("transfer_metadata.METADATA_TRANSFER_LOG_FILE", log_path)
        log_patch.start()
        self.addCleanup(log_patch.stop)

        handlers_before = self._isolated_logger(transfer_metadata.LOGGER)

        auditor._enable_metadata_transfer_file_logging()

        added_handlers = [h for h in transfer_metadata.LOGGER.handlers if h not in handlers_before]
        self.assertEqual(len(added_handlers), 1)
        self.assertIsInstance(added_handlers[0], logging.FileHandler)
        self.assertEqual(Path(added_handlers[0].baseFilename), log_path.resolve())

        transfer_metadata.LOGGER.info("metadata transfer message")
        auditor.LOGGER.info("general audit message")

        log_contents = log_path.read_text(encoding="utf-8")
        self.assertIn("metadata transfer message", log_contents)
        self.assertNotIn("general audit message", log_contents)

    def test_image_transfer_logging_attaches_to_its_own_logger_only(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "image_transfer.log"
        log_patch = patch("transfer_images.IMAGE_TRANSFER_LOG_FILE", log_path)
        log_patch.start()
        self.addCleanup(log_patch.stop)

        handlers_before = self._isolated_logger(transfer_images.LOGGER)

        auditor._enable_image_transfer_file_logging()

        added_handlers = [h for h in transfer_images.LOGGER.handlers if h not in handlers_before]
        self.assertEqual(len(added_handlers), 1)
        self.assertEqual(Path(added_handlers[0].baseFilename), log_path.resolve())

        transfer_images.LOGGER.info("image transfer message")
        transfer_metadata.LOGGER.info("metadata transfer message")

        log_contents = log_path.read_text(encoding="utf-8")
        self.assertIn("image transfer message", log_contents)
        self.assertNotIn("metadata transfer message", log_contents)

    def test_subtitle_transfer_logging_attaches_to_its_own_logger_only(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "subtitle_transfer.log"
        log_patch = patch("transfer_subtitles.SUBTITLE_TRANSFER_LOG_FILE", log_path)
        log_patch.start()
        self.addCleanup(log_patch.stop)

        handlers_before = self._isolated_logger(transfer_subtitles.LOGGER)

        auditor._enable_subtitle_transfer_file_logging()

        added_handlers = [h for h in transfer_subtitles.LOGGER.handlers if h not in handlers_before]
        self.assertEqual(len(added_handlers), 1)
        self.assertEqual(Path(added_handlers[0].baseFilename), log_path.resolve())

        transfer_subtitles.LOGGER.info("subtitle transfer message")
        transfer_images.LOGGER.info("image transfer message")

        log_contents = log_path.read_text(encoding="utf-8")
        self.assertIn("subtitle transfer message", log_contents)
        self.assertNotIn("image transfer message", log_contents)

    def test_general_file_logging_excludes_transfer_type_messages(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_path = Path(temp_dir.name) / "audit.log"
        log_patch = patch("auditor.AUDIT_LOG_FILE", log_path)
        log_patch.start()
        self.addCleanup(log_patch.stop)

        handlers_before = self._isolated_logger(auditor.LOGGER)

        auditor._enable_general_file_logging()

        added_handlers = [h for h in auditor.LOGGER.handlers if h not in handlers_before]
        self.assertEqual(len(added_handlers), 1)
        self.assertEqual(Path(added_handlers[0].baseFilename), log_path.resolve())

        auditor.LOGGER.info("general audit message")
        transfer_metadata.LOGGER.info("metadata transfer message")
        transfer_images.LOGGER.info("image transfer message")
        transfer_subtitles.LOGGER.info("subtitle transfer message")

        log_contents = log_path.read_text(encoding="utf-8")
        self.assertIn("general audit message", log_contents)
        self.assertNotIn("metadata transfer message", log_contents)
        self.assertNotIn("image transfer message", log_contents)
        self.assertNotIn("subtitle transfer message", log_contents)


class BulkMetadataTransferTests(unittest.TestCase):
    def _make_config(self) -> config.AppConfig:
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
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                    "right": ServerConfig(
                        key="right", name="Right", url="http://right:8096", api_key="right-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key=None),
        )

    def _make_results(self) -> tuple[AuditServerResult, AuditServerResult]:
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_name="Left Server",
            server_key="left",
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_name="Right Server",
            server_key="right",
        )
        return left_result, right_result

    def _make_fake_client(self, dtos_by_server_and_item, update_calls):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def close(self):
                pass

            def get_item(self, item_id):
                return dtos_by_server_and_item[(self.server.key, item_id)]

            def update_item(self, item_id, item_dto):
                update_calls.append((self.server.key, item_id, item_dto))

        return FakeClient

    def test_returns_zero_when_no_mismatched_metadata(self) -> None:
        left_result, right_result = self._make_results()

        with patch("auditor.mismatched_metadata_transfer_targets", return_value=()):
            exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                left_result, right_result, dry_run=False, assume_yes=True
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(transfer_results, ())

    def test_transfers_after_batch_confirmation(self) -> None:
        left_result, right_result = self._make_results()
        target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S01E01",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        dtos = {
            ("left", "left-id"): {"Id": "left-id", "Name": "Correct Title", "Path": "/media/left/file.mkv"},
            ("right", "right-id"): {"Id": "right-id", "Name": "Wrong Title", "Path": "/media/right/file.mkv"},
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        with patch("auditor.mismatched_metadata_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        server_key, item_id, item_dto = update_calls[0]
        self.assertEqual(server_key, "right")
        self.assertEqual(item_id, "right-id")
        self.assertEqual(item_dto["Name"], "Correct Title")
        self.assertEqual(len(transfer_results), 1)
        self.assertEqual(transfer_results[0].status, "transferred")
        self.assertEqual(transfer_results[0].library, "TV Shows")
        self.assertEqual(transfer_results[0].display_name, "Show.S01E01")
        self.assertEqual(transfer_results[0].changed_fields, ("Name",))

    def test_aborts_batch_when_confirmation_declined(self) -> None:
        left_result, right_result = self._make_results()
        target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S01E01",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        update_calls: list = []
        fake_client = self._make_fake_client({}, update_calls)

        with patch("auditor.mismatched_metadata_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="n"):
                        exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])
        self.assertEqual(transfer_results, ())

    def test_dry_run_does_not_write_and_does_not_prompt(self) -> None:
        left_result, right_result = self._make_results()
        target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S01E01",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        dtos = {
            ("left", "left-id"): {"Id": "left-id", "Name": "Correct Title", "Path": "/media/left/file.mkv"},
            ("right", "right-id"): {"Id": "right-id", "Name": "Wrong Title", "Path": "/media/right/file.mkv"},
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called during --dry-run")

        with patch("auditor.mismatched_metadata_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", side_effect=_unexpected_input):
                        exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                            left_result, right_result, dry_run=True, assume_yes=False
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])
        self.assertEqual(len(transfer_results), 1)
        self.assertEqual(transfer_results[0].status, "would_transfer")
        self.assertEqual(transfer_results[0].changed_fields, ("Name",))

    def test_continues_past_rejected_item_and_reports_failure(self) -> None:
        left_result, right_result = self._make_results()
        rejected_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Rejected Episode",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
        )
        ok_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Fine Episode",
            left_server_key="left",
            left_item_id="left-2",
            right_server_key="right",
            right_item_id="right-2",
        )
        dtos = {
            ("left", "left-1"): {"Id": "left-1", "Name": "Correct Title"},
            ("right", "right-1"): {"Id": "right-1", "Name": "Wrong Title"},
            ("left", "left-2"): {"Id": "left-2", "Name": "Correct Title 2", "Path": "/media/left/file2.mkv"},
            ("right", "right-2"): {
                "Id": "right-2",
                "Name": "Wrong Title 2",
                "Path": "/media/right/file2.mkv",
            },
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        with patch(
            "auditor.mismatched_metadata_transfer_targets",
            return_value=(rejected_target, ok_target),
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                        left_result, right_result, dry_run=False, assume_yes=True
                    )

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1], "right-2")
        self.assertEqual(len(transfer_results), 2)
        rejected_result, transferred_result = transfer_results
        self.assertEqual(rejected_result.display_name, "Rejected Episode")
        self.assertEqual(rejected_result.status, "rejected")
        self.assertIn("Path", rejected_result.detail)
        self.assertEqual(transferred_result.display_name, "Fine Episode")
        self.assertEqual(transferred_result.status, "transferred")

    def test_limit_truncates_targets_before_attempting_any(self) -> None:
        left_result, right_result = self._make_results()
        first_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Episode One",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
        )
        second_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Episode Two",
            left_server_key="left",
            left_item_id="left-2",
            right_server_key="right",
            right_item_id="right-2",
        )
        dtos = {
            ("left", "left-1"): {"Id": "left-1", "Name": "Correct Title 1"},
            ("right", "right-1"): {"Id": "right-1", "Name": "Wrong Title 1", "Path": "/media/right/file1.mkv"},
            ("left", "left-2"): {"Id": "left-2", "Name": "Correct Title 2"},
            ("right", "right-2"): {"Id": "right-2", "Name": "Wrong Title 2", "Path": "/media/right/file2.mkv"},
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        with patch(
            "auditor.mismatched_metadata_transfer_targets",
            return_value=(first_target, second_target),
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                        left_result, right_result, dry_run=False, assume_yes=True, limit=1
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1], "right-1")
        self.assertEqual(len(transfer_results), 1)
        self.assertEqual(transfer_results[0].display_name, "Episode One")

    def test_series_name_filter_excludes_other_series_and_movies(self) -> None:
        left_result, right_result = self._make_results()
        matching_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S01E01",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
            series_name="Show Name",
            season_number=1,
        )
        other_series_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Other.S01E01",
            left_server_key="left",
            left_item_id="left-2",
            right_server_key="right",
            right_item_id="right-2",
            series_name="Other Show",
            season_number=1,
        )
        movie_target = MetadataTransferTarget(
            library="Movies",
            display_name="Movie",
            left_server_key="left",
            left_item_id="left-3",
            right_server_key="right",
            right_item_id="right-3",
            series_name=None,
            season_number=None,
        )
        dtos = {
            ("left", "left-1"): {"Id": "left-1", "Name": "Correct Title"},
            ("right", "right-1"): {"Id": "right-1", "Name": "Wrong Title", "Path": "/media/right/1.mkv"},
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        with patch(
            "auditor.mismatched_metadata_transfer_targets",
            return_value=(matching_target, other_series_target, movie_target),
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                        left_result,
                        right_result,
                        dry_run=False,
                        assume_yes=True,
                        # Deliberately mixed case, since the match is case-insensitive.
                        series_name="show name",
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1], "right-1")
        self.assertEqual(len(transfer_results), 1)

    def test_season_number_filter_further_limits_within_matched_series(self) -> None:
        left_result, right_result = self._make_results()
        season_one_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S01E01",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
            series_name="Show Name",
            season_number=1,
        )
        season_two_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S02E01",
            left_server_key="left",
            left_item_id="left-2",
            right_server_key="right",
            right_item_id="right-2",
            series_name="Show Name",
            season_number=2,
        )
        dtos = {
            ("left", "left-2"): {"Id": "left-2", "Name": "Correct Title 2"},
            ("right", "right-2"): {"Id": "right-2", "Name": "Wrong Title 2", "Path": "/media/right/2.mkv"},
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        with patch(
            "auditor.mismatched_metadata_transfer_targets",
            return_value=(season_one_target, season_two_target),
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                        left_result,
                        right_result,
                        dry_run=False,
                        assume_yes=True,
                        series_name="Show Name",
                        season_number=2,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1], "right-2")
        self.assertEqual(len(transfer_results), 1)

    def test_no_matches_for_series_name_filter_reports_zero_without_error(self) -> None:
        left_result, right_result = self._make_results()
        target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S01E01",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
            series_name="Show Name",
            season_number=1,
        )

        with patch(
            "auditor.mismatched_metadata_transfer_targets", return_value=(target,)
        ):
            exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                left_result,
                right_result,
                dry_run=False,
                assume_yes=True,
                series_name="No Such Show",
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(transfer_results, ())


class BulkImageTransferTests(unittest.TestCase):
    def _make_config(self) -> config.AppConfig:
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
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                    "right": ServerConfig(
                        key="right", name="Right", url="http://right:8096", api_key="right-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key=None),
        )

    def _make_results(self) -> tuple[AuditServerResult, AuditServerResult]:
        left_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_name="Left Server",
            server_key="left",
        )
        right_result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_name="Right Server",
            server_key="right",
        )
        return left_result, right_result

    def _make_fake_client(
        self,
        images_by_server_and_item,
        upload_calls,
        *,
        fail_uploads_for=frozenset(),
        destination_names=None,
        destination_image_tags=None,
    ):
        names = destination_names or {}
        image_tags_by_item = destination_image_tags or {}

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def close(self):
                pass

            def get_item(self, item_id):
                return {
                    "Name": names.get((self.server.key, item_id), item_id),
                    "ImageTags": image_tags_by_item.get((self.server.key, item_id), {}),
                }

            def get_item_image(self, item_id, image_type):
                return images_by_server_and_item.get((self.server.key, item_id, image_type))

            def upload_item_image(self, item_id, image_type, image_bytes, content_type):
                if (self.server.key, item_id, image_type) in fail_uploads_for:
                    raise jellyfin.JellyfinError("upload failed")
                upload_calls.append((self.server.key, item_id, image_type, image_bytes, content_type))

        return FakeClient

    def test_returns_zero_when_no_artwork_differences(self) -> None:
        left_result, right_result = self._make_results()

        with patch("auditor.missing_image_transfer_targets", return_value=()):
            exit_code, results = auditor._run_bulk_image_transfer(
                left_result, right_result, dry_run=False, assume_yes=True
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(results, ())

    def test_transfers_available_images_after_batch_confirmation(self) -> None:
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        images = {("left", "left-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images, upload_calls, destination_names={("right", "right-id"): "Alien"}
        )

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [("right", "right-id", "Primary", b"bytes", "image/jpeg")])
        self.assertEqual(len(results), len(auditor.BULK_IMAGE_TYPES))
        by_type = {result.image_type: result for result in results}
        self.assertEqual(by_type["Primary"].status, "transferred")

    def test_only_attempts_bulk_image_types(self) -> None:
        """Regression test: the bulk run only attempts BULK_IMAGE_TYPES
        (Primary), not the full transfer_images.IMAGE_TYPES set - Backdrop
        and Thumb were pure wasted work since these libraries never had
        source images of those types in practice."""
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        images = {
            ("left", "left-id", "Primary"): (b"bytes", "image/jpeg"),
            ("left", "left-id", "Backdrop"): (b"backdrop-bytes", "image/jpeg"),
            ("left", "left-id", "Thumb"): (b"thumb-bytes", "image/jpeg"),
        }
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images, upload_calls, destination_names={("right", "right-id"): "Alien"}
        )

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual({result.image_type for result in results}, {"Primary"})
        self.assertEqual(upload_calls, [("right", "right-id", "Primary", b"bytes", "image/jpeg")])

    def test_skips_image_type_the_destination_already_has(self) -> None:
        """Regression test: even when a pair is a transfer candidate,
        transferring an image type the destination already has anyway would
        silently overwrite a perfectly fine existing image it never needed
        replaced."""
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="TV Shows",
            display_name="Arcane - Season 1 - S01E01 - Welcome to the Playground",
            left_title="Welcome to the Playground",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        images = {("left", "left-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images,
            upload_calls,
            destination_names={("right", "right-id"): "Welcome to the Playground"},
            destination_image_tags={("right", "right-id"): {"Primary": "existing-tag"}},
        )

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [])
        by_type = {result.image_type: result for result in results}
        self.assertEqual(by_type["Primary"].status, "already_present")

    def test_limit_truncates_targets_before_attempting_any(self) -> None:
        left_result, right_result = self._make_results()
        first_target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
        )
        second_target = ImageTransferTarget(
            library="Movies",
            display_name="Predator",
            left_title="Predator",
            left_server_key="left",
            left_item_id="left-2",
            right_server_key="right",
            right_item_id="right-2",
        )
        images = {
            ("left", "left-1", "Primary"): (b"bytes1", "image/jpeg"),
            ("left", "left-2", "Primary"): (b"bytes2", "image/jpeg"),
        }
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images,
            upload_calls,
            destination_names={("right", "right-1"): "Alien", ("right", "right-2"): "Predator"},
        )

        with patch(
            "auditor.missing_image_transfer_targets", return_value=(first_target, second_target)
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False, limit=1
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [("right", "right-1", "Primary", b"bytes1", "image/jpeg")])
        self.assertTrue(all(result.display_name == "Alien" for result in results))

    def test_aborts_batch_when_confirmation_declined(self) -> None:
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        upload_calls: list = []
        fake_client = self._make_fake_client({}, upload_calls)

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="n"):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(upload_calls, [])
        self.assertEqual(results, ())

    def test_dry_run_does_not_write_and_does_not_prompt(self) -> None:
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        images = {("left", "left-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images, upload_calls, destination_names={("right", "right-id"): "Alien"}
        )

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called during --dry-run")

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", side_effect=_unexpected_input):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=True, assume_yes=False
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [])
        by_type = {result.image_type: result for result in results}
        self.assertEqual(by_type["Primary"].status, "would_transfer")

    def test_continues_past_upload_failure_and_reports_failure(self) -> None:
        left_result, right_result = self._make_results()
        failing_target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-1",
            right_server_key="right",
            right_item_id="right-1",
        )
        ok_target = ImageTransferTarget(
            library="Movies",
            display_name="Predator",
            left_title="Predator",
            left_server_key="left",
            left_item_id="left-2",
            right_server_key="right",
            right_item_id="right-2",
        )
        images = {
            ("left", "left-1", "Primary"): (b"bytes1", "image/jpeg"),
            ("left", "left-2", "Primary"): (b"bytes2", "image/jpeg"),
        }
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images,
            upload_calls,
            fail_uploads_for={("right", "right-1", "Primary")},
            destination_names={("right", "right-1"): "Alien", ("right", "right-2"): "Predator"},
        )

        with patch(
            "auditor.missing_image_transfer_targets", return_value=(failing_target, ok_target)
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code, results = auditor._run_bulk_image_transfer(
                            left_result, right_result, dry_run=False, assume_yes=False
                        )

        self.assertEqual(exit_code, 1)
        by_name = {result.display_name: result for result in results}
        self.assertEqual(by_name["Alien"].status, "failed")
        self.assertIn("upload failed", by_name["Alien"].detail)
        self.assertEqual(by_name["Predator"].status, "transferred")
        self.assertEqual(upload_calls, [("right", "right-2", "Primary", b"bytes2", "image/jpeg")])

    def test_warns_but_still_transfers_when_destination_name_does_not_match(self) -> None:
        """Regression test: a bulk run previously had no way to catch a
        target list pointing at the wrong destination item, since it never
        fetched the destination item's own identity before writing."""
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="Movies",
            display_name="Alien",
            left_title="Alien",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        images = {("left", "left-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images, upload_calls, destination_names={("right", "right-id"): "Some Other Movie"}
        )

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        with self.assertLogs("transfer_images", level="WARNING") as logs:
                            exit_code, results = auditor._run_bulk_image_transfer(
                                left_result, right_result, dry_run=False, assume_yes=False
                            )

        self.assertEqual(exit_code, 0)
        self.assertTrue(any("Some Other Movie" in message for message in logs.output))
        by_type = {result.image_type: result for result in results}
        self.assertEqual(by_type["Primary"].status, "transferred")

    def test_does_not_warn_for_a_correctly_paired_episode(self) -> None:
        """Regression test: display_name for an episode is a composed label
        ("Series - Season - S04E13 - Title"), but Jellyfin's own item Name is
        just the bare title - comparing against display_name instead of
        left_title flagged every correctly-paired episode as a false-positive
        mismatch."""
        left_result, right_result = self._make_results()
        target = ImageTransferTarget(
            library="TV Shows",
            display_name="Babylon 5 - Season 4 - S04E13 - Rumors, Bargains and Lies",
            left_title="Rumors, Bargains and Lies",
            left_server_key="left",
            left_item_id="left-id",
            right_server_key="right",
            right_item_id="right-id",
        )
        images = {("left", "left-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            images,
            upload_calls,
            destination_names={("right", "right-id"): "Rumors, Bargains and Lies"},
        )

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    with patch("builtins.input", return_value="y"):
                        with self.assertLogs("transfer_images", level="INFO") as logs:
                            exit_code, results = auditor._run_bulk_image_transfer(
                                left_result, right_result, dry_run=False, assume_yes=False
                            )

        self.assertEqual(exit_code, 0)
        self.assertFalse(any(record.levelname == "WARNING" for record in logs.records))
        by_type = {result.image_type: result for result in results}
        self.assertEqual(by_type["Primary"].status, "transferred")


def _library_response(*, tv1_id: str = "tv1", tv2_id: str = "tv2", movies_id: str = "mov1") -> MagicMock:
    response = MagicMock()
    response.content = b'{"Items": []}'
    response.json.return_value = {
        "Items": [
            {"Id": tv1_id, "Name": "TV Shows", "CollectionType": "tvshows"},
            {"Id": tv2_id, "Name": "Anime", "CollectionType": "tvshows"},
            {"Id": movies_id, "Name": "Movies", "CollectionType": "movies"},
        ]
    }
    return response


def _items_response(items: list[dict]) -> MagicMock:
    response = MagicMock()
    response.content = b'{"Items": []}'
    response.json.return_value = {"Items": items, "TotalRecordCount": len(items)}
    return response


class JellyfinClientSeriesLookupTests(unittest.TestCase):
    def _make_client(self) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(server)

    def test_find_series_matches_case_insensitively(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response(
                    [{"Id": "s1", "Name": "Breaking Bad", "ProviderIds": {"Tvdb": "81189"}}]
                ),
                _items_response([{"Id": "s2", "Name": "Cowboy Bebop", "ProviderIds": {}}]),
            ],
        ):
            matches = client.find_series("breaking bad")

        self.assertEqual(
            matches,
            (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),),
        )

    def test_find_series_returns_every_ambiguous_match(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "s1", "Name": "The Office", "ProviderIds": {}}]),
                _items_response([{"Id": "s2", "Name": "The Office", "ProviderIds": {}}]),
            ],
        ):
            matches = client.find_series("The Office")

        self.assertEqual(len(matches), 2)
        self.assertEqual(
            {match.library_name for match in matches}, {"TV Shows", "Anime"}
        )

    def test_get_series_tvdb_ids_groups_multiple_ids_under_one_name(self) -> None:
        """Regression test: two Series items can share a display name (e.g.
        TheTVDB splitting a show into a new entry for a later era) - both
        their TheTVDB ids must be kept, not just whichever is seen last."""
        client = self._make_client()
        raw_series = [
            {"Id": "s1", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "78804"}},
            {"Id": "s2", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "449991"}},
            {"Id": "s3", "Name": "Firefly", "ProviderIds": {"Tvdb": "78874"}},
        ]
        with patch.object(client._session, "request", return_value=_items_response(raw_series)):
            series_tvdb_ids = client.get_series_tvdb_ids("tv1")

        self.assertEqual(
            series_tvdb_ids,
            {"Doctor Who": ("78804", "449991"), "Firefly": ("78874",)},
        )

    def test_get_series_tvdb_ids_deduplicates_a_repeated_id_for_one_name(self) -> None:
        client = self._make_client()
        raw_series = [
            {"Id": "s1", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "78804"}},
            {"Id": "s2", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "78804"}},
        ]
        with patch.object(client._session, "request", return_value=_items_response(raw_series)):
            series_tvdb_ids = client.get_series_tvdb_ids("tv1")

        self.assertEqual(series_tvdb_ids, {"Doctor Who": ("78804",)})

    def test_get_series_tvdb_ids_omits_series_without_a_tvdb_id(self) -> None:
        client = self._make_client()
        raw_series = [{"Id": "s1", "Name": "Cowboy Bebop", "ProviderIds": {}}]
        with patch.object(client._session, "request", return_value=_items_response(raw_series)):
            series_tvdb_ids = client.get_series_tvdb_ids("tv1")

        self.assertEqual(series_tvdb_ids, {})

    def test_find_series_with_library_name_only_queries_that_library(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "s2", "Name": "Cowboy Bebop", "ProviderIds": {}}]),
            ],
        ) as mock_request:
            matches = client.find_series("Cowboy Bebop", library_name="anime")

        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(
            matches,
            (jellyfin.SeriesMatch(library_name="Anime", series_id="s2", tvdb_id=None),),
        )

    def test_get_series_season_episodes_filters_and_sorts(self) -> None:
        client = self._make_client()
        raw_episodes = [
            {"Id": "e3", "Name": "Third", "ParentIndexNumber": 1, "IndexNumber": 3},
            {"Id": "e1", "Name": "First", "ParentIndexNumber": 1, "IndexNumber": 1},
            {"Id": "e-other-season", "Name": "Other Season", "ParentIndexNumber": 2, "IndexNumber": 1},
            {"Id": "e-no-number", "Name": "No Number", "ParentIndexNumber": 1, "IndexNumber": None},
        ]
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            episodes = client.get_series_season_episodes("series-id", 1)

        self.assertEqual(
            episodes,
            (
                jellyfin.EpisodeSummary(id="e1", name="First", episode_number=1),
                jellyfin.EpisodeSummary(id="e3", name="Third", episode_number=3),
            ),
        )

    def test_get_series_season_episodes_falls_back_to_season_name(self) -> None:
        """Regression test: Jellyfin sometimes leaves ParentIndexNumber unset
        on an episode it couldn't confidently number, even though the
        containing Season item still has a plain "Season N" name - without
        falling back to that name, such an episode silently never matches
        any season lookup."""
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e1",
                "Name": "First",
                "ParentIndexNumber": None,
                "SeasonName": "Season 7",
                "IndexNumber": 1,
            },
            {
                "Id": "e-other-season",
                "Name": "Other Season",
                "ParentIndexNumber": None,
                "SeasonName": "Season 8",
                "IndexNumber": 1,
            },
        ]
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            episodes = client.get_series_season_episodes("series-id", 7)

        self.assertEqual(
            episodes,
            (jellyfin.EpisodeSummary(id="e1", name="First", episode_number=1),),
        )

    def test_get_series_season_episodes_all_keeps_unnumbered_sorted_last_by_path(self) -> None:
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e3",
                "Name": "Third",
                "ParentIndexNumber": 1,
                "IndexNumber": 3,
                "Path": "/media/show/S01E03.mkv",
            },
            {
                "Id": "e-other-season",
                "Name": "Other Season",
                "ParentIndexNumber": 2,
                "IndexNumber": 1,
                "Path": "/media/show/S02E01.mkv",
            },
            {
                "Id": "e-b",
                "Name": "No Number B",
                "ParentIndexNumber": 1,
                "IndexNumber": None,
                "Path": "/media/show/b-unnumbered.mkv",
            },
            {
                "Id": "e-a",
                "Name": "No Number A",
                "ParentIndexNumber": 1,
                "IndexNumber": None,
                "Path": "/media/show/a-unnumbered.mkv",
            },
        ]
        index_numbers_by_id = {item["Id"]: item["IndexNumber"] for item in raw_episodes}
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            with patch.object(
                client,
                "_lookup_index_number",
                side_effect=lambda item_id: index_numbers_by_id[item_id],
            ):
                episodes = client.get_series_season_episodes_all("series-id", 1)

        self.assertEqual(
            episodes,
            (
                jellyfin.SeasonEpisodeSummary(
                    id="e3", name="Third", path=Path("/media/show/S01E03.mkv"), episode_number=3
                ),
                jellyfin.SeasonEpisodeSummary(
                    id="e-a",
                    name="No Number A",
                    path=Path("/media/show/a-unnumbered.mkv"),
                    episode_number=None,
                ),
                jellyfin.SeasonEpisodeSummary(
                    id="e-b",
                    name="No Number B",
                    path=Path("/media/show/b-unnumbered.mkv"),
                    episode_number=None,
                ),
            ),
        )

    def test_get_series_season_episodes_all_falls_back_to_season_name(self) -> None:
        """Regression test: this is exactly the case
        apply_episode_numbers.py needs - an unnumbered episode whose season
        is only known by its "Season N" name must still be found under that
        season, not silently dropped."""
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e-unnumbered",
                "Name": "Bad Company",
                "ParentIndexNumber": None,
                "SeasonName": "Season 7",
                "IndexNumber": None,
                "Path": "/media/show/Bad Company.mkv",
            },
            {
                "Id": "e-other-season",
                "Name": "Other Season",
                "ParentIndexNumber": None,
                "SeasonName": "Season 8",
                "IndexNumber": None,
                "Path": "/media/show/other.mkv",
            },
        ]
        index_numbers_by_id = {item["Id"]: item["IndexNumber"] for item in raw_episodes}
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            with patch.object(
                client,
                "_lookup_index_number",
                side_effect=lambda item_id: index_numbers_by_id[item_id],
            ):
                episodes = client.get_series_season_episodes_all("series-id", 7)

        self.assertEqual(
            episodes,
            (
                jellyfin.SeasonEpisodeSummary(
                    id="e-unnumbered",
                    name="Bad Company",
                    path=Path("/media/show/Bad Company.mkv"),
                    episode_number=None,
                ),
            ),
        )

    def test_get_series_season_episodes_all_uses_fresh_per_item_index_number(self) -> None:
        """Regression test: the /Items listing can lag behind a field edit
        made outside a library scan (a direct API PATCH, or a manual edit in
        the Jellyfin UI) in either direction - it can still show a number
        that was since cleared, or omit one that was just set. The per-item
        lookup result must win over whatever the listing says."""
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e-cleared",
                "Name": "Cleared",
                "ParentIndexNumber": 1,
                "IndexNumber": 5,
                "Path": "/media/show/S01E05.mkv",
            },
            {
                "Id": "e-set",
                "Name": "Set",
                "ParentIndexNumber": 1,
                "IndexNumber": None,
                "Path": "/media/show/S01E06.mkv",
            },
        ]
        fresh_index_numbers_by_id = {"e-cleared": None, "e-set": 6}
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            with patch.object(
                client,
                "_lookup_index_number",
                side_effect=lambda item_id: fresh_index_numbers_by_id[item_id],
            ):
                episodes = client.get_series_season_episodes_all("series-id", 1)

        self.assertEqual(
            episodes,
            (
                jellyfin.SeasonEpisodeSummary(
                    id="e-set", name="Set", path=Path("/media/show/S01E06.mkv"), episode_number=6
                ),
                jellyfin.SeasonEpisodeSummary(
                    id="e-cleared",
                    name="Cleared",
                    path=Path("/media/show/S01E05.mkv"),
                    episode_number=None,
                ),
            ),
        )


class ApplyDvdMetadataPlanTests(unittest.TestCase):
    def _make_client(self, item_by_id: dict) -> object:
        class FakeClient:
            def get_item(self, item_id: str) -> dict:
                return item_by_id[item_id]

        return FakeClient()

    def test_plan_dvd_apply_computes_name_overview_and_original_title_backup(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "Overview": "Aired overview.",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)
        dvd_positions = {
            (1, 1): _make_tvdb_episode(
                season_number=1, episode_number=1, name="DVD Title", overview="DVD overview."
            )
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertFalse(plan.no_target_match)
        self.assertIsNone(plan.rejected_reason)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(
            plan.changes,
            (
                ("Name", "Aired Title", "DVD Title"),
                ("Overview", "Aired overview.", "DVD overview."),
                ("OriginalTitle", None, "Aired Title"),
            ),
        )
        self.assertEqual(plan.merged_dto["Name"], "DVD Title")
        self.assertEqual(plan.merged_dto["Overview"], "DVD overview.")
        self.assertEqual(plan.merged_dto["OriginalTitle"], "Aired Title")
        self.assertEqual(plan.merged_dto["Id"], "ep1")
        self.assertEqual(plan.merged_dto["Path"], "/media/show/S01E01.mkv")

    def test_plan_dvd_apply_locks_changed_fields(self) -> None:
        """Regression test: without locking the fields it edits, a library with
        TheTVDB's internet metadata provider enabled treats Name/Overview as
        provider-owned and silently reverts the edit on its next refresh, even
        though the API write itself succeeds. Mirrors what Jellyfin's own
        "Edit Metadata" dialog does when a field is changed by hand."""
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "LockedFields": ["Genres"],
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)
        dvd_positions = {(1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title")}

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertEqual(plan.merged_dto["LockedFields"], ["Genres", "Name"])

    def test_plan_dvd_apply_never_locks_original_title(self) -> None:
        """Regression test: Jellyfin deserializes LockedFields into its own
        MetadataField enum, which has no OriginalTitle member. Sending it
        there fails the *entire* update with a 400 - "The JSON value could
        not be converted to MediaBrowser.Model.Entities.MetadataField" -
        even though OriginalTitle itself is a perfectly normal field to
        write outside of LockedFields."""
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)
        dvd_positions = {(1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title")}

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertEqual(plan.merged_dto["OriginalTitle"], "Aired Title")
        self.assertNotIn("OriginalTitle", plan.merged_dto["LockedFields"])

    def test_plan_dvd_apply_no_change_does_not_touch_locked_fields(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Same Title",
                    "LockedFields": ["Genres"],
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Same Title", episode_number=1)
        dvd_positions = {(1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Same Title")}

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertEqual(plan.merged_dto["LockedFields"], ["Genres"])

    def test_plan_dvd_apply_never_touches_episode_or_season_number(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "IndexNumber": 1,
                    "ParentIndexNumber": 1,
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)
        dvd_positions = {(1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title")}

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertNotIn("IndexNumber", [field for field, _, _ in plan.changes])
        self.assertNotIn("ParentIndexNumber", [field for field, _, _ in plan.changes])
        self.assertEqual(plan.merged_dto["IndexNumber"], 1)
        self.assertEqual(plan.merged_dto["ParentIndexNumber"], 1)

    def test_plan_dvd_apply_no_change_when_already_matches(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Same Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Same Title", episode_number=1)
        dvd_positions = {(1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Same Title")}

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertFalse(plan.has_changes)
        self.assertFalse(plan.is_actionable)

    def test_plan_dvd_apply_no_dvd_match_skips_item_fetch(self) -> None:
        def _unexpected_get_item(item_id: str) -> dict:
            raise AssertionError("get_item should not be called when there is no DVD match")

        client = MagicMock()
        client.get_item.side_effect = _unexpected_get_item
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=5)

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, {}, restore_aired=False
        )

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)

    def test_plan_dvd_apply_rejects_when_path_missing(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Name": "Aired Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)
        dvd_positions = {(1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title")}

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertIsNotNone(plan.rejected_reason)
        self.assertFalse(plan.is_actionable)

    def test_plan_aired_restore_prefers_original_title_over_tvdb_lookup(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "DVD Title",
                    "OriginalTitle": "Backed Up Aired Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(
                season_number=1,
                episode_number=1,
                name="Fresh TVDB Aired Title",
                overview="Fresh aired overview.",
            )
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, aired_positions, restore_aired=True
        )

        self.assertFalse(plan.no_target_match)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.merged_dto["Name"], "Backed Up Aired Title")
        # Overview has no backup field, so it still comes from TheTVDB.
        self.assertEqual(plan.merged_dto["Overview"], "Fresh aired overview.")

    def test_plan_aired_restore_falls_back_to_tvdb_when_no_original_title(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "DVD Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="TVDB Aired Title")
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, aired_positions, restore_aired=True
        )

        self.assertEqual(plan.merged_dto["Name"], "TVDB Aired Title")

    def test_plan_aired_restore_no_match_when_no_backup_and_no_tvdb_data(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "DVD Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1)

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, {}, restore_aired=True
        )

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)

    def test_plan_aired_restore_uses_original_title_even_without_tvdb_match(self) -> None:
        """OriginalTitle alone is enough to restore Name, even if TheTVDB has
        nothing at this position - the backup doesn't depend on TheTVDB."""
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "DVD Title",
                    "OriginalTitle": "Backed Up Aired Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1)

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, {}, restore_aired=True
        )

        self.assertFalse(plan.no_target_match)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.merged_dto["Name"], "Backed Up Aired Title")


class ApplyDvdMetadataCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "apply_dvd_metadata.DVD_METADATA_LOG_FILE",
            Path(temp_dir.name) / "dvd_metadata_apply.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
        return config.AppConfig(
            reporting=config.ReportingConfig(
                media_path_prefix="",
                csv_output=config.CsvOutputConfig(
                    movies=Path("movies_report.csv"), tv=Path("tv_report.csv")
                ),
                output=config.ReportOutputConfig(
                    audit_csv=Path("audit_report.csv"), audit_html=Path("audit_results")
                ),
                english_language_codes=("en", "eng", ""),
            ),
            processing=ProcessingConfig(enable_movies=True, enable_tv=True),
            servers=ServerCollection(
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                    "right": ServerConfig(
                        key="right", name="Right", url="http://right:8096", api_key="right-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key="tvdb-secret"),
        )

    def _make_fake_client(
        self,
        *,
        series_matches: tuple,
        episodes: tuple,
        items_by_id: dict,
        update_calls: list,
    ):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None):
                return series_matches

            def get_series_season_episodes(self, series_id, season_number):
                return episodes

            def get_series_episode_positions(self, series_id):
                # Empty by default: resolve_series_tvdb_id() short-circuits
                # to the Jellyfin-assigned id without needing a TheTVDB
                # search at all, so most tests don't need to care about
                # series-id resolution.
                return frozenset()

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        return FakeClient

    def _make_fake_tvdb_client(self, episodes: tuple, *, expected_season_type: str = "dvd"):
        class FakeTvdbClient:
            def __init__(self, api_key, **kwargs):
                self.api_key = api_key

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_series_episodes(self, series_id, season_type, *, series_name=None):
                assert season_type == expected_season_type
                return episodes

            def search_series(self, name):
                return ()

        return FakeTvdbClient

    def test_updates_episodes_after_confirmation(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}
        }
        dvd_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(dvd_episodes)

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "ep1")
        self.assertEqual(updated_dto["Name"], "DVD Title")

    def test_uses_better_matching_tvdb_series_over_jellyfins_assigned_id(self) -> None:
        """Regression test: Jellyfin's own assigned TheTVDB id can itself be
        wrong when TheTVDB has more than one series entry sharing a name -
        the update must be sourced from whichever id's positions actually
        cover the local series across every season, not blindly from
        whatever Jellyfin happened to assign.
        """
        series_matches = (
            jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="78804"),
        )
        episodes = (
            jellyfin.EpisodeSummary(id="ep1", name="Wrong Show's Title", episode_number=1),
        )
        items_by_id = {
            "ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Wrong Show's Title"}
        }
        update_calls: list = []

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None):
                return series_matches

            def get_series_season_episodes(self, series_id, season_number):
                return episodes

            def get_series_episode_positions(self, series_id):
                # Local library spans two seasons; only the "449991"
                # candidate below covers both.
                return frozenset({(1, 1), (2, 1)})

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        class FakeTvdbClient:
            def __init__(self, api_key, **kwargs):
                self.api_key = api_key

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def search_series(self, name):
                return (_make_tvdb_search_result(series_id="449991", name="Doctor Who"),)

            def get_series_episodes(self, series_id, season_type, *, series_name=None):
                if season_type == "official":
                    if series_id == "78804":
                        return (
                            _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                        )
                    return (
                        _make_tvdb_episode(season_number=1, episode_number=1, name="Space Babies"),
                        _make_tvdb_episode(season_number=2, episode_number=1, name="Episode 2"),
                    )
                # DVD-order fetch, for the actual update - only reached for
                # whichever id resolution picked.
                assert series_id == "449991"
                return (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),
                )

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", FakeClient):
                with patch("apply_dvd_metadata.TvdbClient", FakeTvdbClient):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Doctor Who",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1]["Name"], "DVD Title")

    def test_detects_write_that_did_not_take_effect(self) -> None:
        """Regression test: update_item() returning success does not guarantee
        Jellyfin actually kept the new value - a locked/provider-owned field can
        silently revert on its own refresh. The re-read after applying must
        catch this instead of reporting "updated" for a no-op write."""
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}
        }
        dvd_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)
        update_calls: list = []

        class RevertingFakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None):
                return series_matches

            def get_series_season_episodes(self, series_id, season_number):
                return episodes

            def get_series_episode_positions(self, series_id):
                return frozenset()

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                # Simulate Jellyfin accepting the write but a metadata
                # provider immediately reverting it - items_by_id is left
                # unchanged, so the post-write re-read still sees the old
                # value.

        fake_tvdb_client = self._make_fake_tvdb_client(dvd_episodes)

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", RevertingFakeClient):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(len(update_calls), 1)

    def test_defaults_to_configured_default_server(self) -> None:
        """Regression test: this must use servers.toml's default_server, not
        just the first server listed in the file - those can differ."""
        base_config = self._make_config()
        config_with_non_first_default = config.AppConfig(
            reporting=base_config.reporting,
            processing=base_config.processing,
            servers=ServerCollection(
                default_server="right",
                servers=base_config.servers.servers,
            ),
            tvdb=base_config.tvdb,
        )
        series_matches = ()
        fake_client = self._make_fake_client(
            series_matches=series_matches, episodes=(), items_by_id={}, update_calls=[]
        )
        fake_tvdb_client = self._make_fake_tvdb_client(())
        seen_servers: list = []

        class TrackingFakeClient(fake_client):
            def __init__(self, server, **kwargs):
                seen_servers.append(server.key)
                super().__init__(server, **kwargs)

        with patch(
            "apply_dvd_metadata.get_config", return_value=config_with_non_first_default
        ):
            with patch("apply_dvd_metadata.JellyfinClient", TrackingFakeClient):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    apply_dvd_metadata.run_apply_dvd_metadata(
                        series_name="Breaking Bad",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(seen_servers, ["right"])

    def test_skips_prompt_with_yes(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}
        }
        dvd_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(dvd_episodes)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called with --yes")

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", side_effect=_unexpected_input):
                        exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)

    def test_aborts_when_user_declines(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}
        }
        dvd_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(dvd_episodes)

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="n"):
                        exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])

    def test_no_series_found_is_an_error(self) -> None:
        fake_client = self._make_fake_client(
            series_matches=(), episodes=(), items_by_id={}, update_calls=[]
        )
        fake_tvdb_client = self._make_fake_tvdb_client(())

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                        series_name="Nonexistent Show",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 1)

    def test_ambiguous_series_is_an_error(self) -> None:
        series_matches = (
            jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="1"),
            jellyfin.SeriesMatch(library_name="Anime", series_id="s2", tvdb_id="2"),
        )
        fake_client = self._make_fake_client(
            series_matches=series_matches, episodes=(), items_by_id={}, update_calls=[]
        )
        fake_tvdb_client = self._make_fake_tvdb_client(())

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                        series_name="The Office",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 1)

    def test_series_without_tvdb_id_is_an_error(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id=None),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Some Title", episode_number=1),)
        fake_client = self._make_fake_client(
            series_matches=series_matches, episodes=episodes, items_by_id={}, update_calls=[]
        )
        fake_tvdb_client = self._make_fake_tvdb_client(())

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                        series_name="Breaking Bad",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 1)

    def test_missing_tvdb_api_key_is_a_usage_error(self) -> None:
        app_config = self._make_config()
        app_config_no_tvdb = config.AppConfig(
            reporting=app_config.reporting,
            processing=app_config.processing,
            servers=app_config.servers,
            tvdb=config.TvdbConfig(api_key=None),
        )

        with patch("apply_dvd_metadata.get_config", return_value=app_config_no_tvdb):
            exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                series_name="Breaking Bad",
                season_number=1,
                server_key=None,
                library_name=None,
                assume_yes=True,
            )

        self.assertEqual(exit_code, 2)

    def test_no_episodes_in_season_is_not_an_error(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        fake_client = self._make_fake_client(
            series_matches=series_matches, episodes=(), items_by_id={}, update_calls=[]
        )
        fake_tvdb_client = self._make_fake_tvdb_client(())

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                        series_name="Breaking Bad",
                        season_number=99,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)

    def test_writes_apply_details_to_log_file(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}
        }
        dvd_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=[],
        )
        fake_tvdb_client = self._make_fake_tvdb_client(dvd_episodes)

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        log_contents = apply_dvd_metadata.DVD_METADATA_LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("Aired Title", log_contents)
        self.assertIn("DVD Title", log_contents)
        self.assertIn("S01E01: updated.", log_contents)

    def test_aired_undoes_a_previous_dvd_apply_using_original_title(self) -> None:
        """End-to-end regression test for the --aired undo path: an episode
        previously switched to DVD order (Name backed up into OriginalTitle)
        must be restored using that backup, not a fresh TheTVDB aired-order
        lookup, and OriginalTitle itself must query TheTVDB's "official"
        ordering, not "dvd"."""
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1),)
        items_by_id = {
            "ep1": {
                "Id": "ep1",
                "Path": "/media/show/S01E01.mkv",
                "Name": "DVD Title",
                "OriginalTitle": "Backed Up Aired Title",
            }
        }
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Fresh TVDB Aired Title"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(
            aired_episodes, expected_season_type="official"
        )

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", fake_client):
                with patch("apply_dvd_metadata.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_dvd_metadata.run_apply_dvd_metadata(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                            restore_aired=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "ep1")
        self.assertEqual(updated_dto["Name"], "Backed Up Aired Title")


class ApplyEpisodeTitlesPlanTests(unittest.TestCase):
    def _make_client(self, item_by_id: dict) -> object:
        class FakeClient:
            def get_item(self, item_id: str) -> dict:
                return item_by_id[item_id]

        return FakeClient()

    def test_plan_computes_name_and_original_title_backup(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Wrong Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title")
        }

        plan = apply_episode_titles.plan_episode_title_update(client, episode, 1, aired_positions)

        self.assertFalse(plan.no_target_match)
        self.assertFalse(plan.already_matches)
        self.assertIsNone(plan.rejected_reason)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(
            plan.changes,
            (
                ("Name", "Wrong Title", "Aired Title"),
                ("OriginalTitle", None, "Wrong Title"),
            ),
        )
        self.assertEqual(plan.merged_dto["Name"], "Aired Title")
        self.assertEqual(plan.merged_dto["OriginalTitle"], "Wrong Title")
        self.assertEqual(plan.merged_dto["Id"], "ep1")
        self.assertEqual(plan.merged_dto["Path"], "/media/show/S01E01.mkv")

    def test_plan_skips_title_already_matching_under_lenient_comparison(self) -> None:
        """Regression test: an episode whose title already reads the same as
        TheTVDB's under audit.titles_match()'s lenient rules (here, just a
        US/UK spelling difference) must not be rewritten to TheTVDB's exact
        spelling - that would be needless churn for a title the audit check
        itself wouldn't flag as a mismatch.
        """
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "The Colour of Money"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="The Colour of Money", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="The Color of Money")
        }

        plan = apply_episode_titles.plan_episode_title_update(client, episode, 1, aired_positions)

        self.assertTrue(plan.already_matches)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)
        self.assertEqual(plan.changes, ())

    def test_plan_locks_changed_name_field(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Wrong Title",
                    "LockedFields": ["Genres"],
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title")
        }

        plan = apply_episode_titles.plan_episode_title_update(client, episode, 1, aired_positions)

        self.assertEqual(plan.merged_dto["LockedFields"], ["Genres", "Name"])

    def test_plan_never_locks_original_title(self) -> None:
        """Regression test: Jellyfin deserializes LockedFields into its own
        MetadataField enum, which has no OriginalTitle member. Sending it
        there fails the *entire* update with a 400 - "The JSON value could
        not be converted to MediaBrowser.Model.Entities.MetadataField" -
        even though OriginalTitle itself is a perfectly normal field to
        write outside of LockedFields."""
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Wrong Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title")
        }

        plan = apply_episode_titles.plan_episode_title_update(client, episode, 1, aired_positions)

        self.assertEqual(plan.merged_dto["OriginalTitle"], "Wrong Title")
        self.assertNotIn("OriginalTitle", plan.merged_dto["LockedFields"])

    def test_plan_never_touches_episode_or_season_number(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Wrong Title",
                    "IndexNumber": 1,
                    "ParentIndexNumber": 1,
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title")
        }

        plan = apply_episode_titles.plan_episode_title_update(client, episode, 1, aired_positions)

        self.assertEqual(plan.merged_dto["IndexNumber"], 1)
        self.assertEqual(plan.merged_dto["ParentIndexNumber"], 1)

    def test_plan_reports_no_target_match_without_fetching_the_item(self) -> None:
        class ExplodingClient:
            def get_item(self, item_id: str) -> dict:
                raise AssertionError("should not fetch an item with no TheTVDB match")

        episode = jellyfin.EpisodeSummary(id="ep1", name="Some Title", episode_number=99)

        plan = apply_episode_titles.plan_episode_title_update(ExplodingClient(), episode, 1, {})

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)

    def test_plan_skips_already_matching_title_without_fetching_the_item(self) -> None:
        """The episode summary's own Name is enough to rule out most
        already-correct episodes, so no Jellyfin item fetch is needed for
        them at all.
        """

        class ExplodingClient:
            def get_item(self, item_id: str) -> dict:
                raise AssertionError("should not fetch an item that already matches")

        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title")
        }

        plan = apply_episode_titles.plan_episode_title_update(
            ExplodingClient(), episode, 1, aired_positions
        )

        self.assertTrue(plan.already_matches)

    def test_plan_rejects_when_required_fields_missing(self) -> None:
        client = self._make_client({"ep1": {"Id": "ep1", "Name": "Wrong Title"}})
        episode = jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title")
        }

        plan = apply_episode_titles.plan_episode_title_update(client, episode, 1, aired_positions)

        self.assertIsNotNone(plan.rejected_reason)
        self.assertIn("Path", plan.rejected_reason)
        self.assertFalse(plan.is_actionable)


class ApplyEpisodeTitlesRestorePlanTests(unittest.TestCase):
    def _make_client(self, item_by_id: dict) -> object:
        class FakeClient:
            def get_item(self, item_id: str) -> dict:
                return item_by_id[item_id]

        return FakeClient()

    def test_plan_restores_name_from_original_title_backup(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "OriginalTitle": "Original Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertFalse(plan.no_target_match)
        self.assertFalse(plan.already_matches)
        self.assertIsNone(plan.rejected_reason)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.changes, (("Name", "Aired Title", "Original Title"),))
        self.assertEqual(plan.merged_dto["Name"], "Original Title")
        # OriginalTitle itself is left untouched - nothing further to
        # preserve once Name is already restored.
        self.assertEqual(plan.merged_dto["OriginalTitle"], "Original Title")

    def test_plan_reports_no_backup_when_original_title_is_missing(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)

    def test_plan_reports_no_backup_when_original_title_is_blank(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "OriginalTitle": "",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)

    def test_plan_already_matches_when_name_equals_original_title(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Original Title",
                    "OriginalTitle": "Original Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Original Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertTrue(plan.already_matches)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)
        self.assertEqual(plan.changes, ())

    def test_plan_locks_changed_name_field(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "OriginalTitle": "Original Title",
                    "LockedFields": ["Genres"],
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertEqual(plan.merged_dto["LockedFields"], ["Genres", "Name"])

    def test_plan_never_touches_episode_or_season_number(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E01.mkv",
                    "Name": "Aired Title",
                    "OriginalTitle": "Original Title",
                    "IndexNumber": 1,
                    "ParentIndexNumber": 1,
                }
            }
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertEqual(plan.merged_dto["IndexNumber"], 1)
        self.assertEqual(plan.merged_dto["ParentIndexNumber"], 1)

    def test_plan_rejects_when_required_fields_missing(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Name": "Aired Title", "OriginalTitle": "Original Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1)

        plan = apply_episode_titles.plan_episode_title_restore(client, episode, 1)

        self.assertIsNotNone(plan.rejected_reason)
        self.assertIn("Path", plan.rejected_reason)
        self.assertFalse(plan.is_actionable)


class ResolveSeriesTvdbIdTests(unittest.TestCase):
    def _make_client(self, local_positions: frozenset) -> object:
        class FakeClient:
            def get_series_episode_positions(self, series_id: str) -> frozenset:
                return local_positions

        return FakeClient()

    def _make_tvdb_client(
        self,
        *,
        search_results: tuple = (),
        episodes_by_id: dict | None = None,
    ) -> object:
        class FakeTvdbClient:
            def search_series(self, name: str) -> tuple:
                return search_results

            def get_series_episodes(self, series_id: str, season_type: str, *, series_name=None):
                assert season_type == "official"
                return (episodes_by_id or {}).get(series_id, ())

        return FakeTvdbClient()

    def test_returns_assigned_id_unchanged_when_no_local_episodes(self) -> None:
        client = self._make_client(frozenset())
        tvdb_client = self._make_tvdb_client()

        result = apply_episode_titles.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804"
        )

        self.assertEqual(result, "78804")

    def test_returns_assigned_id_unchanged_when_no_other_candidates_found(self) -> None:
        client = self._make_client(frozenset({(1, 1)}))
        tvdb_client = self._make_tvdb_client(search_results=())

        result = apply_episode_titles.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804"
        )

        self.assertEqual(result, "78804")

    def test_prefers_search_candidate_that_better_explains_local_episodes(self) -> None:
        """Regression test: Jellyfin's own assigned TheTVDB id can itself be
        wrong when TheTVDB has more than one series entry sharing a name
        (e.g. a decades-old show and a from-scratch modern revival, each
        numbering their own "Season 1" independently) - the id whose
        positions actually cover the local library, across every season,
        must win even when it isn't the one Jellyfin picked.
        """
        # Local episodes span two seasons - the assigned id ("78804",
        # standing in for the 2005 reboot) only explains season 1, while
        # the better candidate ("449991", standing in for the 2023 relaunch)
        # explains both.
        local_positions = frozenset({(1, 1), (1, 2), (2, 1), (2, 2)})
        client = self._make_client(local_positions)
        tvdb_client = self._make_tvdb_client(
            search_results=(
                _make_tvdb_search_result(series_id="449991", name="Doctor Who"),
            ),
            episodes_by_id={
                "78804": (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                    _make_tvdb_episode(season_number=1, episode_number=2, name="The End of the World"),
                ),
                "449991": (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Space Babies"),
                    _make_tvdb_episode(season_number=1, episode_number=2, name="The Devil's Chord"),
                    _make_tvdb_episode(season_number=2, episode_number=1, name="Episode 1"),
                    _make_tvdb_episode(season_number=2, episode_number=2, name="Episode 2"),
                ),
            },
        )

        result = apply_episode_titles.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804"
        )

        self.assertEqual(result, "449991")

    def test_keeps_assigned_id_when_it_already_best_explains_local_episodes(self) -> None:
        local_positions = frozenset({(1, 1), (1, 2)})
        client = self._make_client(local_positions)
        tvdb_client = self._make_tvdb_client(
            search_results=(_make_tvdb_search_result(series_id="other-id", name="Doctor Who"),),
            episodes_by_id={
                "78804": (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                    _make_tvdb_episode(season_number=1, episode_number=2, name="The End of the World"),
                ),
                "other-id": (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Something Else"),
                ),
            },
        )

        result = apply_episode_titles.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804"
        )

        self.assertEqual(result, "78804")

    def test_returns_search_candidate_when_no_id_was_assigned_at_all(self) -> None:
        local_positions = frozenset({(1, 1)})
        client = self._make_client(local_positions)
        tvdb_client = self._make_tvdb_client(
            search_results=(_make_tvdb_search_result(series_id="found-id", name="Some Show"),),
            episodes_by_id={
                "found-id": (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),
                ),
            },
        )

        result = apply_episode_titles.resolve_series_tvdb_id(
            client, tvdb_client, "Some Show", "series-id", None
        )

        self.assertEqual(result, "found-id")

    def test_returns_none_when_no_id_assigned_and_no_candidate_found(self) -> None:
        local_positions = frozenset({(1, 1)})
        client = self._make_client(local_positions)
        tvdb_client = self._make_tvdb_client(search_results=())

        result = apply_episode_titles.resolve_series_tvdb_id(
            client, tvdb_client, "Some Show", "series-id", None
        )

        self.assertIsNone(result)


class ApplyEpisodeTitlesCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "apply_episode_titles.EPISODE_TITLES_LOG_FILE",
            Path(temp_dir.name) / "episode_titles_apply.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
        return config.AppConfig(
            reporting=config.ReportingConfig(
                media_path_prefix="",
                csv_output=config.CsvOutputConfig(
                    movies=Path("movies_report.csv"), tv=Path("tv_report.csv")
                ),
                output=config.ReportOutputConfig(
                    audit_csv=Path("audit_report.csv"), audit_html=Path("audit_results")
                ),
                english_language_codes=("en", "eng", ""),
            ),
            processing=ProcessingConfig(enable_movies=True, enable_tv=True),
            servers=ServerCollection(
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key="tvdb-secret"),
        )

    def _make_fake_client(
        self,
        *,
        series_matches: tuple,
        episodes: tuple,
        items_by_id: dict,
        update_calls: list,
    ):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None):
                return series_matches

            def get_series_season_episodes(self, series_id, season_number):
                return episodes

            def get_series_episode_positions(self, series_id):
                # Empty by default: resolve_series_tvdb_id() short-circuits
                # to the Jellyfin-assigned id without needing a TheTVDB
                # search at all, so most tests don't need to care about
                # series-id resolution.
                return frozenset()

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        return FakeClient

    def _make_fake_tvdb_client(self, episodes: tuple, *, expected_season_type: str = "official"):
        class FakeTvdbClient:
            def __init__(self, api_key, **kwargs):
                self.api_key = api_key

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_series_episodes(self, series_id, season_type, *, series_name=None):
                assert season_type == expected_season_type
                return episodes

            def search_series(self, name):
                return ()

        return FakeTvdbClient

    def test_renames_episodes_after_confirmation_using_aired_order_by_default(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1),)
        items_by_id = {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Wrong Title"}}
        aired_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes, expected_season_type="official")

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                with patch("apply_episode_titles.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_titles.run_apply_episode_titles(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "ep1")
        self.assertEqual(updated_dto["Name"], "Aired Title")
        self.assertEqual(updated_dto["OriginalTitle"], "Wrong Title")

    def test_dvd_order_flag_fetches_dvd_episodes_instead(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1),)
        items_by_id = {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Wrong Title"}}
        dvd_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(dvd_episodes, expected_season_type="dvd")

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                with patch("apply_episode_titles.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_titles.run_apply_episode_titles(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                            use_dvd_order=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1]["Name"], "DVD Title")

    def test_nothing_to_do_when_all_titles_already_match(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}}
        aired_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                with patch("apply_episode_titles.TvdbClient", fake_tvdb_client):
                    exit_code = apply_episode_titles.run_apply_episode_titles(
                        series_name="Breaking Bad",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_declining_confirmation_aborts_without_writing(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Wrong Title", episode_number=1),)
        items_by_id = {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Wrong Title"}}
        aired_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                with patch("apply_episode_titles.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="n"):
                        exit_code = apply_episode_titles.run_apply_episode_titles(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])

    def test_missing_tvdb_api_key_is_a_usage_error(self) -> None:
        config_without_tvdb = self._make_config()
        config_without_tvdb = config.AppConfig(
            reporting=config_without_tvdb.reporting,
            processing=config_without_tvdb.processing,
            servers=config_without_tvdb.servers,
            tvdb=config.TvdbConfig(api_key=""),
        )

        with patch("apply_episode_titles.get_config", return_value=config_without_tvdb):
            exit_code = apply_episode_titles.run_apply_episode_titles(
                series_name="Breaking Bad",
                season_number=1,
                server_key=None,
                library_name=None,
                assume_yes=True,
            )

        self.assertEqual(exit_code, 2)

    def test_ambiguous_series_is_an_error(self) -> None:
        series_matches = (
            jellyfin.SeriesMatch(library_name="Anime", series_id="s1", tvdb_id="1"),
            jellyfin.SeriesMatch(library_name="TV Shows", series_id="s2", tvdb_id="2"),
        )
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=(),
            items_by_id={},
            update_calls=[],
        )

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                exit_code = apply_episode_titles.run_apply_episode_titles(
                    series_name="The Office",
                    season_number=1,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 1)

    def test_restore_sets_name_from_original_title_backup(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {
                "Id": "ep1",
                "Path": "/media/show/S01E01.mkv",
                "Name": "Aired Title",
                "OriginalTitle": "Original Title",
            }
        }
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )

        class ExplodingTvdbClient:
            def __init__(self, *args, **kwargs):
                raise AssertionError("--restore must never construct a TheTVDB client")

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                with patch("apply_episode_titles.TvdbClient", ExplodingTvdbClient):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_titles.run_apply_episode_titles(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                            restore=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "ep1")
        self.assertEqual(updated_dto["Name"], "Original Title")
        self.assertEqual(updated_dto["OriginalTitle"], "Original Title")

    def test_restore_works_without_a_configured_tvdb_api_key(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {
            "ep1": {
                "Id": "ep1",
                "Path": "/media/show/S01E01.mkv",
                "Name": "Aired Title",
                "OriginalTitle": "Original Title",
            }
        }
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        config_without_tvdb = self._make_config()
        config_without_tvdb = config.AppConfig(
            reporting=config_without_tvdb.reporting,
            processing=config_without_tvdb.processing,
            servers=config_without_tvdb.servers,
            tvdb=config.TvdbConfig(api_key=""),
        )

        with patch("apply_episode_titles.get_config", return_value=config_without_tvdb):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = apply_episode_titles.run_apply_episode_titles(
                        series_name="Breaking Bad",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=False,
                        restore=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)

    def test_restore_reports_nothing_to_do_without_a_backup(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1),)
        items_by_id = {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Aired Title"}}
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )

        with patch("apply_episode_titles.get_config", return_value=self._make_config()):
            with patch("apply_episode_titles.JellyfinClient", fake_client):
                exit_code = apply_episode_titles.run_apply_episode_titles(
                    series_name="Breaking Bad",
                    season_number=1,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                    restore=True,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_main_rejects_restore_combined_with_dvd_order(self) -> None:
        exit_code = apply_episode_titles.main(
            [
                "--series-name",
                "Breaking Bad",
                "--season-number",
                "1",
                "--restore",
                "--dvd-order",
            ]
        )

        self.assertEqual(exit_code, 2)


class ApplyEpisodeNumbersPlanTests(unittest.TestCase):
    def _make_client(self, item_by_id: dict) -> object:
        class FakeClient:
            def get_item(self, item_id: str) -> dict:
                return item_by_id[item_id]

        return FakeClient()

    def test_plan_computes_index_number_change(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/unnumbered.mkv", "Name": "A Title"}}
        )
        episode = jellyfin.SeasonEpisodeSummary(
            id="ep1", name="A Title", path=Path("/media/show/unnumbered.mkv"), episode_number=None
        )
        aired_episode = _make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title")

        plan = apply_episode_numbers.plan_episode_number_update(client, episode, aired_episode)

        self.assertFalse(plan.no_target_match)
        self.assertIsNone(plan.rejected_reason)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.assigned_number, 3)
        self.assertEqual(plan.changes, (("IndexNumber", None, 3),))
        self.assertEqual(plan.merged_dto["IndexNumber"], 3)
        self.assertEqual(plan.merged_dto["Id"], "ep1")
        self.assertEqual(plan.merged_dto["Path"], "/media/show/unnumbered.mkv")

    def test_plan_leaves_other_fields_untouched(self) -> None:
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/unnumbered.mkv",
                    "Name": "A Title",
                    "Overview": "Some overview.",
                }
            }
        )
        episode = jellyfin.SeasonEpisodeSummary(
            id="ep1", name="A Title", path=Path("/media/show/unnumbered.mkv"), episode_number=None
        )
        aired_episode = _make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title")

        plan = apply_episode_numbers.plan_episode_number_update(client, episode, aired_episode)

        self.assertEqual(plan.merged_dto["Name"], "A Title")
        self.assertEqual(plan.merged_dto["Overview"], "Some overview.")

    def test_plan_no_target_match_skips_item_fetch(self) -> None:
        def _unexpected_get_item(item_id: str) -> dict:
            raise AssertionError("get_item should not be called when there is no aired-order match")

        client = MagicMock()
        client.get_item.side_effect = _unexpected_get_item
        episode = jellyfin.SeasonEpisodeSummary(
            id="ep1", name="A Title", path=Path("/media/show/unnumbered.mkv"), episode_number=None
        )

        plan = apply_episode_numbers.plan_episode_number_update(client, episode, None)

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)

    def test_plan_rejects_when_path_missing(self) -> None:
        client = self._make_client({"ep1": {"Id": "ep1", "Name": "A Title"}})
        episode = jellyfin.SeasonEpisodeSummary(
            id="ep1", name="A Title", path=None, episode_number=None
        )
        aired_episode = _make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title")

        plan = apply_episode_numbers.plan_episode_number_update(client, episode, aired_episode)

        self.assertIsNotNone(plan.rejected_reason)
        self.assertFalse(plan.is_actionable)


class PopTitleMatchTests(unittest.TestCase):
    def test_prefers_exact_title_over_article_insensitive_match(self) -> None:
        """An exact title match must win even when a different candidate
        would also match once a leading article is ignored, so a genuine
        same-titled-except-for-an-article coincidence never steals a
        better, exact match."""
        exact_match = _make_tvdb_episode(season_number=1, episode_number=1, name="Cowboy")
        article_only_match = _make_tvdb_episode(season_number=1, episode_number=2, name="The Cowboy")
        remaining = [article_only_match, exact_match]

        matched = apply_episode_numbers._pop_title_match(remaining, "Cowboy")

        self.assertIs(matched, exact_match)
        self.assertEqual(remaining, [article_only_match])

    def test_falls_back_to_article_insensitive_match(self) -> None:
        candidate = _make_tvdb_episode(season_number=1, episode_number=10, name="Murdering Cowboy")
        remaining = [candidate]

        matched = apply_episode_numbers._pop_title_match(remaining, "The Murdering Cowboy")

        self.assertIs(matched, candidate)
        self.assertEqual(remaining, [])

    def test_returns_none_and_leaves_list_unchanged_when_nothing_matches(self) -> None:
        candidate = _make_tvdb_episode(season_number=1, episode_number=1, name="Unrelated Title")
        remaining = [candidate]

        matched = apply_episode_numbers._pop_title_match(remaining, "Something Else Entirely")

        self.assertIsNone(matched)
        self.assertEqual(remaining, [candidate])


class FuzzyTitleMatchTests(unittest.TestCase):
    def test_finds_best_scoring_candidate_above_threshold(self) -> None:
        close = _make_tvdb_episode(season_number=1, episode_number=12, name="The Great Philadelphia Mob War")
        unrelated = _make_tvdb_episode(season_number=1, episode_number=13, name="Some Totally Unrelated Episode")
        remaining = [unrelated, close]

        result = apply_episode_numbers._best_fuzzy_match(remaining, "The Great Philly Mob War")

        self.assertIsNotNone(result)
        candidate, ratio = result
        self.assertIs(candidate, close)
        self.assertGreaterEqual(ratio, apply_episode_numbers._FUZZY_MATCH_MIN_RATIO)
        # Not mutated - unlike _pop_title_match, the caller decides whether
        # to consume the candidate only after the user confirms it.
        self.assertEqual(remaining, [unrelated, close])

    def test_returns_none_when_nothing_scores_high_enough(self) -> None:
        candidate = _make_tvdb_episode(season_number=1, episode_number=1, name="Unrelated Title")

        result = apply_episode_numbers._best_fuzzy_match([candidate], "Something Else Entirely")

        self.assertIsNone(result)


class ApplyEpisodeNumbersCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "apply_episode_numbers.EPISODE_NUMBER_APPLY_LOG_FILE",
            Path(temp_dir.name) / "episode_numbers_apply.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
        return config.AppConfig(
            reporting=config.ReportingConfig(
                media_path_prefix="",
                csv_output=config.CsvOutputConfig(
                    movies=Path("movies_report.csv"), tv=Path("tv_report.csv")
                ),
                output=config.ReportOutputConfig(
                    audit_csv=Path("audit_report.csv"), audit_html=Path("audit_results")
                ),
                english_language_codes=("en", "eng", ""),
            ),
            processing=ProcessingConfig(enable_movies=True, enable_tv=True),
            servers=ServerCollection(
                default_server="left",
                servers={
                    "left": ServerConfig(
                        key="left", name="Left", url="http://left:8096", api_key="left-token"
                    ),
                },
            ),
            tvdb=config.TvdbConfig(api_key="tvdb-secret"),
        )

    def _make_fake_client(
        self,
        *,
        series_matches: tuple,
        episodes: tuple,
        items_by_id: dict,
        update_calls: list,
    ):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None):
                return series_matches

            def get_series_season_episodes_all(self, series_id, season_number):
                return episodes

            def get_series_episode_positions(self, series_id):
                # Empty by default: resolve_series_tvdb_id() short-circuits
                # to the Jellyfin-assigned id without needing a TheTVDB
                # search at all, so most tests don't need to care about
                # series-id resolution.
                return frozenset()

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        return FakeClient

    def _make_fake_tvdb_client(self, episodes: tuple):
        class FakeTvdbClient:
            def __init__(self, api_key, **kwargs):
                self.api_key = api_key

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_series_episodes(self, series_id, season_type, *, series_name=None):
                assert season_type == "official"
                return episodes

            def search_series(self, name):
                return ()

        return FakeTvdbClient

    def test_matches_episodes_by_title_regardless_of_filename_order(self) -> None:
        """Regression test: episodes must be matched to TheTVDB by title, not
        by on-disk filename order - a series can be filed with one
        descriptively-titled file per episode whose alphabetical order has
        nothing to do with aired order. Here the alphabetically-first file
        ("a-unnumbered.mkv") is actually the aired-order *third* episode by
        title, and the alphabetically-second file is the aired-order
        *second* episode - a purely positional/sequential assignment would
        get both of these backwards."""
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep1", name="First", path=Path("/media/show/S01E01.mkv"), episode_number=1
            ),
            jellyfin.SeasonEpisodeSummary(
                id="ep-a", name="Third", path=Path("/media/show/a-unnumbered.mkv"), episode_number=None
            ),
            jellyfin.SeasonEpisodeSummary(
                id="ep-b", name="Second", path=Path("/media/show/b-unnumbered.mkv"), episode_number=None
            ),
        )
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/a-unnumbered.mkv", "Name": "Third"},
            "ep-b": {"Id": "ep-b", "Path": "/media/show/b-unnumbered.mkv", "Name": "Second"},
        }
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="First"),
            _make_tvdb_episode(season_number=1, episode_number=2, name="Second"),
            _make_tvdb_episode(season_number=1, episode_number=3, name="Third"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        update_calls_by_id = dict(update_calls)
        self.assertEqual(len(update_calls), 2)
        self.assertEqual(update_calls_by_id["ep-a"]["IndexNumber"], 3)
        self.assertEqual(update_calls_by_id["ep-b"]["IndexNumber"], 2)

    def test_no_unnumbered_episodes_is_a_no_op(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep1", name="First", path=Path("/media/show/S01E01.mkv"), episode_number=1
            ),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id={},
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(())

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    exit_code = apply_episode_numbers.run_apply_episode_numbers(
                        series_name="Breaking Bad",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_episode_with_no_matching_title_is_skipped(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a", name="First", path=Path("/media/show/a-unnumbered.mkv"), episode_number=None
            ),
            jellyfin.SeasonEpisodeSummary(
                id="ep-b", name="Some Other Title", path=Path("/media/show/b-unnumbered.mkv"), episode_number=None
            ),
        )
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/a-unnumbered.mkv", "Name": "First"},
        }
        aired_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="First"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    exit_code = apply_episode_numbers.run_apply_episode_numbers(
                        series_name="Breaking Bad",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0], "ep-a")
        self.assertEqual(update_calls[0][1]["IndexNumber"], 1)

    def test_matches_title_ignoring_a_leading_article_difference(self) -> None:
        """Regression test: a real-world file was titled "The Murdering
        Cowboy" while TheTVDB's own title for that episode was "Murdering
        Cowboy" (no article) - an exact title comparison alone would leave
        a plainly-correct match unassigned."""
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a",
                name="The Murdering Cowboy",
                path=Path("/media/show/a-unnumbered.mkv"),
                episode_number=None,
            ),
        )
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/a-unnumbered.mkv", "Name": "The Murdering Cowboy"},
        }
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=10, name="Murdering Cowboy"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    exit_code = apply_episode_numbers.run_apply_episode_numbers(
                        series_name="The FBI Files",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0], "ep-a")
        self.assertEqual(update_calls[0][1]["IndexNumber"], 10)

    def test_skips_prompt_with_yes(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a", name="First", path=Path("/media/show/a-unnumbered.mkv"), episode_number=None
            ),
        )
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/a-unnumbered.mkv", "Name": "First"},
        }
        aired_episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="First"),)
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called with --yes")

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", side_effect=_unexpected_input):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="Breaking Bad",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)

    def test_uses_better_matching_tvdb_series_over_jellyfins_assigned_id(self) -> None:
        """Regression test: Jellyfin's own assigned TheTVDB id can itself be
        wrong when TheTVDB has more than one series entry sharing a name -
        numbering must be sourced from whichever id's positions actually
        cover the local series across every season, not blindly from
        whatever Jellyfin happened to assign.
        """
        series_matches = (
            jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="78804"),
        )
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a",
                name="Space Babies",
                path=Path("/media/show/a-unnumbered.mkv"),
                episode_number=None,
            ),
        )
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/a-unnumbered.mkv", "Name": "Space Babies"},
        }
        update_calls: list = []

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None):
                return series_matches

            def get_series_season_episodes_all(self, series_id, season_number):
                return episodes

            def get_series_episode_positions(self, series_id):
                # Local library spans two seasons; only the "449991"
                # candidate below covers both.
                return frozenset({(2, 1)})

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        class FakeTvdbClient:
            def __init__(self, api_key, **kwargs):
                self.api_key = api_key

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def search_series(self, name):
                return (_make_tvdb_search_result(series_id="449991", name="Doctor Who"),)

            def get_series_episodes(self, series_id, season_type, *, series_name=None):
                assert season_type == "official"
                if series_id == "78804":
                    return (
                        _make_tvdb_episode(season_number=1, episode_number=1, name="Rose"),
                    )
                return (
                    _make_tvdb_episode(season_number=1, episode_number=1, name="Space Babies"),
                    _make_tvdb_episode(season_number=2, episode_number=1, name="Episode 2"),
                )

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", FakeClient):
                with patch("apply_episode_numbers.TvdbClient", FakeTvdbClient):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="Doctor Who",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "ep-a")
        self.assertEqual(updated_dto["IndexNumber"], 1)

    def test_applies_fuzzy_match_when_user_confirms(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a",
                name="The Great Philly Mob War",
                path=Path("/media/show/a-unnumbered.mkv"),
                episode_number=None,
            ),
        )
        items_by_id = {
            "ep-a": {
                "Id": "ep-a",
                "Path": "/media/show/a-unnumbered.mkv",
                "Name": "The Great Philly Mob War",
            },
        }
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=12, name="The Great Philadelphia Mob War"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="The FBI Files",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][0], "ep-a")
        self.assertEqual(update_calls[0][1]["IndexNumber"], 12)
        self.assertEqual(update_calls[0][1]["Name"], "The Great Philadelphia Mob War")
        self.assertIn("Name", update_calls[0][1]["LockedFields"])

    def test_exact_match_never_overwrites_name(self) -> None:
        """Regression test: only a user-confirmed fuzzy match should touch
        Name - an exact or article-insensitive match already means the
        titles are equivalent, so overwriting Name would just reformat it
        to TheTVDB's punctuation/casing for no reason."""
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a",
                name="The Murdering Cowboy",
                path=Path("/media/show/a-unnumbered.mkv"),
                episode_number=None,
            ),
        )
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/a-unnumbered.mkv", "Name": "The Murdering Cowboy"},
        }
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=10, name="Murdering Cowboy"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id=items_by_id,
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    exit_code = apply_episode_numbers.run_apply_episode_numbers(
                        series_name="The FBI Files",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1]["Name"], "The Murdering Cowboy")
        self.assertNotIn("LockedFields", update_calls[0][1])

    def test_declined_fuzzy_match_is_skipped(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a",
                name="The Great Philly Mob War",
                path=Path("/media/show/a-unnumbered.mkv"),
                episode_number=None,
            ),
        )
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=12, name="The Great Philadelphia Mob War"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id={},
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="n"):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="The FBI Files",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_yes_flag_never_attempts_a_fuzzy_match(self) -> None:
        """Regression test: fuzzy matching needs a human's judgment call, so
        --yes (meant for unattended runs) must leave a title with no exact
        match alone rather than silently accepting a fuzzy guess."""
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep-a",
                name="The Great Philly Mob War",
                path=Path("/media/show/a-unnumbered.mkv"),
                episode_number=None,
            ),
        )
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=12, name="The Great Philadelphia Mob War"),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches,
            episodes=episodes,
            items_by_id={},
            update_calls=update_calls,
        )
        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called with --yes")

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", fake_client):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", side_effect=_unexpected_input):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="The FBI Files",
                            season_number=1,
                            server_key=None,
                            library_name=None,
                            assume_yes=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])


if __name__ == "__main__":
    unittest.main()
