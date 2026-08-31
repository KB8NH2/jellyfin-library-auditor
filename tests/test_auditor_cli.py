"""Tests for auditor.py's CLI parsing, orchestration, and bulk transfer runners."""

from __future__ import annotations

import logging
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import call
from unittest.mock import patch

import auditor
from audit_types import AuditCategory
from audit_types import AuditSeverity
from comparison import ImageTransferTarget
from comparison import MetadataTransferResult
from comparison import MetadataTransferTarget
import config
from config import ProcessingConfig
from config import ServerCollection
from config import ServerConfig
import jellyfin
from models import MediaItem
from models import MediaLibrary
from models import SubtitleTrack
from results import AuditServerResult
from results import ComparisonSetting
from results import LibraryComparisonSettings
import transfer_images
import transfer_metadata
import transfer_subtitles
import tvdb

from tests.helpers import _make_app_config
from tests.helpers import _make_comparison_setting
from tests.helpers import _make_finding
from tests.helpers import _make_item
from tests.helpers import _make_left_right_app_config
from tests.helpers import _make_library
from tests.helpers import _make_tvdb_episode
from tests.helpers import _make_tvdb_search_result


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

    def test_audit_server_accumulates_jellyfin_request_count(self) -> None:
        """Regression test: the summary's per-server "Jellyfin API calls"
        count must reflect the client this call actually used, keyed by
        server key, and must add to (not overwrite) whatever a caller
        already accumulated there - e.g. from an earlier audit_server() call
        against the same server in the same run.
        """

        class FakeClient:
            def __init__(self, *args, **kwargs) -> None:
                del args
                del kwargs

            def __enter__(self) -> "FakeClient":
                return self

            def __exit__(self, *args) -> None:
                del args

            def ping(self) -> bool:
                return True

            def get_server_name(self) -> str:
                return "Primary Server"

            def get_libraries(self) -> list[MediaLibrary]:
                return []

            @property
            def request_count(self) -> int:
                return 7

        fake_config = _make_app_config()

        with patch("auditor.get_config", return_value=fake_config):
            with patch("auditor.JellyfinClient", FakeClient):
                client_request_counts = {"primary": 3}
                auditor.audit_server("primary", client_request_counts=client_request_counts)

        self.assertEqual(client_request_counts, {"primary": 10})


class MainInvocationLoggingTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch("auditor.AUDIT_LOG_FILE", Path(temp_dir.name) / "audit.log")
        log_patch.start()
        self.addCleanup(log_patch.stop)

        handlers_before = list(auditor.LOGGER.handlers)

        def _remove_added_handlers() -> None:
            for handler in list(auditor.LOGGER.handlers):
                if handler not in handlers_before:
                    auditor.LOGGER.removeHandler(handler)
                    handler.close()

        self.addCleanup(_remove_added_handlers)

    def test_main_logs_the_invoking_command_line(self) -> None:
        with patch("auditor.get_config", return_value=_make_app_config()):
            exit_code = auditor.main(["--server", "does-not-matter"])

        log_contents = auditor.AUDIT_LOG_FILE.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 1)
        self.assertIn("Command: auditor --server does-not-matter", log_contents)

    def test_main_summary_includes_jellyfin_and_tvdb_api_call_counts(self) -> None:
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(),
            findings=(),
            server_key="primary",
            server_name="Primary",
            server_url="http://primary:8096",
        )

        class FakeTvdbClient:
            def __init__(self, *args, **kwargs) -> None:
                pass

            def __enter__(self) -> "FakeTvdbClient":
                return self

            def __exit__(self, *args) -> None:
                del args

            @property
            def request_count(self) -> int:
                return 12

        with patch("auditor.audit_server", return_value=result):
            with patch("auditor.write_csv_report"):
                with patch("auditor.write_html_report"):
                    with patch("auditor.reset_audit_results_root"):
                        with patch("auditor.write_audit_results_index"):
                            with patch(
                                "auditor.get_config",
                                return_value=_make_app_config(tvdb_api_key="tvdb-secret"),
                            ):
                                with patch("auditor.TvdbClient", FakeTvdbClient):
                                    exit_code = auditor.main(["--server", "primary"])

        log_contents = auditor.AUDIT_LOG_FILE.read_text(encoding="utf-8")
        self.assertEqual(exit_code, 0)
        self.assertIn("TheTVDB API calls: 12", log_contents)
        # audit_server() is mocked, so the fake never mutates
        # client_request_counts itself - the summary line still reports
        # whatever total that dict holds (0 here), proving the value comes
        # from client_request_counts rather than being hardcoded.
        self.assertIn("Jellyfin API calls: 0", log_contents)


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
                call(
                    "server1", (), include_configuration_snapshot=False, tvdb_client=None,
                    check_episode_order=False, client_request_counts={},
                ),
                call(
                    "server2", (), include_configuration_snapshot=False, tvdb_client=None,
                    check_episode_order=False, client_request_counts={},
                ),
                call(
                    "server3", (), include_configuration_snapshot=False, tvdb_client=None,
                    check_episode_order=False, client_request_counts={},
                ),
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
                call(
                    "server1", (), include_configuration_snapshot=True, tvdb_client=None,
                    check_episode_order=False, client_request_counts={},
                ),
                call(
                    "server2", (), include_configuration_snapshot=True, tvdb_client=None,
                    check_episode_order=False, client_request_counts={},
                ),
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
            call("server2", (), include_configuration_snapshot=True, client_request_counts={}),
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
        return _make_left_right_app_config()

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

    def _make_fake_client(self, dtos_by_server_and_item, update_calls, *, request_count=0):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def close(self):
                pass

            def get_item(self, item_id):
                return dtos_by_server_and_item[(self.server.key, item_id)]

            def update_item(self, item_id, item_dto):
                update_calls.append((self.server.key, item_id, item_dto))

            @property
            def request_count(self):
                return request_count

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

    def test_accumulates_client_request_counts_per_server(self) -> None:
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
        fake_client = self._make_fake_client(dtos, update_calls, request_count=5)

        with patch("auditor.mismatched_metadata_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    client_request_counts = {"left": 2}
                    auditor._run_bulk_metadata_transfer(
                        left_result,
                        right_result,
                        dry_run=False,
                        assume_yes=True,
                        client_request_counts=client_request_counts,
                    )

        self.assertEqual(client_request_counts, {"left": 7, "right": 5})

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

    def test_season_number_zero_selects_specials_not_every_season(self) -> None:
        """Regression test: season_number=0 (Specials) is a legitimate,
        falsy season number. The filter must check ``is not None``, not
        truthiness - a `if season_number:` regression would silently treat
        0 as "no filter" and sweep every season back in.
        """
        left_result, right_result = self._make_results()
        specials_target = MetadataTransferTarget(
            library="TV Shows",
            display_name="Show.S00E01",
            left_server_key="left",
            left_item_id="left-0",
            right_server_key="right",
            right_item_id="right-0",
            series_name="Show Name",
            season_number=0,
        )
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
        dtos = {
            ("left", "left-0"): {"Id": "left-0", "Name": "Correct Special"},
            ("right", "right-0"): {"Id": "right-0", "Name": "Wrong Special", "Path": "/media/right/0.mkv"},
        }
        update_calls: list = []
        fake_client = self._make_fake_client(dtos, update_calls)

        with patch(
            "auditor.mismatched_metadata_transfer_targets",
            return_value=(specials_target, season_one_target),
        ):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    exit_code, transfer_results = auditor._run_bulk_metadata_transfer(
                        left_result,
                        right_result,
                        dry_run=False,
                        assume_yes=True,
                        series_name="Show Name",
                        season_number=0,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1], "right-0")
        self.assertEqual(len(transfer_results), 1)

    def test_season_number_without_series_name_is_rejected_at_the_function_level(self) -> None:
        """Regression test: parse_args() already rejects --season-number
        without --series-name at the CLI layer, but _run_bulk_metadata_transfer
        itself had no equivalent guard - any other caller passing
        season_number alone would silently filter every series' matching
        season instead of raising.
        """
        left_result, right_result = self._make_results()

        with self.assertRaises(ValueError):
            auditor._run_bulk_metadata_transfer(
                left_result,
                right_result,
                dry_run=False,
                assume_yes=True,
                season_number=1,
            )

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
        return _make_left_right_app_config()

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
        request_count=0,
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

            @property
            def request_count(self):
                return request_count

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

    def test_accumulates_client_request_counts_per_server(self) -> None:
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
            images,
            upload_calls,
            destination_names={("right", "right-id"): "Alien"},
            request_count=4,
        )

        with patch("auditor.missing_image_transfer_targets", return_value=(target,)):
            with patch("auditor.get_config", return_value=self._make_config()):
                with patch("auditor.JellyfinClient", fake_client):
                    client_request_counts = {"right": 1}
                    auditor._run_bulk_image_transfer(
                        left_result,
                        right_result,
                        dry_run=False,
                        assume_yes=True,
                        client_request_counts=client_request_counts,
                    )

        self.assertEqual(client_request_counts, {"left": 4, "right": 5})

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
