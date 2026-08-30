"""Tests for the comparison/ package (cross-server comparison report generation)."""

from __future__ import annotations

from pathlib import Path
import unittest

from audit_types import AuditCategory
from audit_types import AuditSeverity
from comparison import ImageTransferResult
from comparison import ImageTransferTarget
from comparison import MetadataTransferResult
from comparison import SubtitleTransferTarget
from comparison import generator as comparison_generator
from comparison.generator import DEFAULT_COMPARISON_OUTPUT_DIR
from models import AudioTrack
from models import SubtitleTrack
from models import VideoTrack
from results import AuditServerResult
from results import LibraryAuditResult

from tests.helpers import _make_empty_comparison_results
from tests.helpers import _make_finding
from tests.helpers import _make_item
from tests.helpers import _make_library
from tests.helpers import _make_single_library_result


class ComparisonSummaryCountsTests(unittest.TestCase):
    def _make_single_item_result(
        self, *, item, server_name: str, server_key: str
    ) -> AuditServerResult:
        return _make_single_library_result(
            (item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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


class MissingImageTransferTargetsTests(unittest.TestCase):
    def _make_single_item_result(
        self, *, item, server_name: str, server_key: str
    ) -> AuditServerResult:
        return _make_single_library_result(
            (item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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
        return _make_single_library_result(
            (item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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


class BuildComparisonTests(unittest.TestCase):
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
            server_name="Left Server",
            server_key="left",
            server_url="http://left:8096",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
            server_name="Left Server",
            server_key="left",
            server_url="http://left:8096",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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
        left_result = _make_single_library_result(
            (left_gap_item,),
            library_id="shows-left",
            library_name="TV Shows",
            collection_type="tv",
            findings=(left_missing_episodes,),
        )
        right_result = _make_single_library_result(
            (right_gap_item,),
            library_id="shows-right",
            library_name="TV Shows",
            collection_type="tv",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="movies",
            library_name="Movies",
            collection_type="movies",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
            server_name="Left Server",
            server_key="left",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
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
        left_result = _make_single_library_result(
            (left_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
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

        left_result = _make_single_library_result(
            (left_hd, left_sd),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
        )
        right_result = _make_single_library_result(
            (right_sd, right_hd),
            library_id="shows",
            library_name="TV Shows",
            collection_type="tv",
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
