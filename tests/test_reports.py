"""Tests for the reports/ package (CSV/HTML report generation)."""

from __future__ import annotations

import os
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

from audit_types import AuditCategory
from audit_types import AuditFinding
from audit_types import AuditSeverity
from comparison import write_comparison_reports
from config import clear_config_cache
from models import SubtitleTrack
from models import VideoTrack
import reports
from reports import checks as report_checks
from reports import generator as report_generator
from reports import library as report_library
from results import AuditServerResult
from results import LibraryComparisonSettings
from results import LibraryAuditResult

from tests.helpers import _make_comparison_setting
from tests.helpers import _make_finding
from tests.helpers import _make_item
from tests.helpers import _make_library


class ReportGenerationTests(unittest.TestCase):
    def test_csv_rows_reflect_per_item_check_flags(self) -> None:
        movie_item = _make_item(
            title="Movie One",
            library="Movies",
            path=Path("Movies/Movie One (2024)/Movie One (2024).mkv"),
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
            path=Path("TV Shows/Show Name/Season 01/Show Name S01E02.mkv"),
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
            tvdb_available_series=frozenset({"Show Name"}),
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
                "Base Directory",
                "Base Filename",
                "Series",
                "Title",
                "Season",
                "Episode",
                "Missing Subtitles",
                "Missing Primary",
                "Mismatched Filename Title",
                "Mismatched TheTVDB Title",
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
                    "Movie One (2024)",
                    "Movie One (2024).mkv",
                    "",
                    "Movie One",
                    "",
                    "",
                    "Yes",
                    "No",
                    "No",
                    "N/A",
                    "No",
                    "No",
                    "N/A",
                    "No",
                    "No",
                    "No",
                    "No",
                ),
                (
                    "TV Shows",
                    "Show Name",
                    "Show Name S01E02.mkv",
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
            tvdb_available_series=frozenset({"Show Name"}),
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
        # Once a series is flagged mismatched_tvdb_series, its TheTVDB data
        # is considered untrustworthy - audit.audit_library_items() never
        # actually compares titles against it (see trustworthy_aired_positions),
        # so this reads "N/A" rather than a "No" that would misleadingly
        # claim the title comparison was made and came back clean.
        self.assertEqual(rows[0][header.index("Mismatched TheTVDB Title")], "N/A")

    def test_csv_rows_show_na_for_tvdb_columns_when_tvdb_has_no_data(self) -> None:
        movie_item = _make_item(
            title="Movie One",
            library="Movies",
            path=Path("Movies/Movie One (2024)/Movie One (2024).mkv"),
        )
        no_tvdb_episode = _make_item(
            title="Episode One",
            item_id="episode-one",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Unlisted Show",
            season_number=1,
            episode_number=1,
            path=Path("TV Shows/Unlisted Show/Season 01/Unlisted Show S01E01.mkv"),
        )
        matched_episode = _make_item(
            title="Episode Two",
            item_id="episode-two",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Listed Show",
            season_number=1,
            episode_number=2,
            path=Path("TV Shows/Listed Show/Season 01/Listed Show S01E02.mkv"),
        )
        library_result_movies = LibraryAuditResult(
            library=_make_library(library_id="movies", name="Movies", collection_type="movies"),
            media_items_processed=1,
            audited_items=(movie_item,),
            items_with_english_subtitles=0,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(),
        )
        library_result_tv = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=2,
            audited_items=(no_tvdb_episode, matched_episode),
            items_with_english_subtitles=0,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(),
            # Only "Listed Show" has TheTVDB data this run - "Unlisted Show"
            # is absent entirely (no TheTVDB match found for it at all).
            tvdb_available_series=frozenset({"Listed Show"}),
        )
        result = AuditServerResult(
            libraries_audited=2,
            media_items_processed=3,
            library_results=(library_result_movies, library_result_tv),
            findings=(),
        )

        rows = report_generator._csv_rows(result)

        header = report_generator.CSV_HEADER
        rows_by_title = {row[header.index("Title")]: row for row in rows}

        # A movie never has TheTVDB data to compare against at all.
        self.assertEqual(rows_by_title["Movie One"][header.index("Mismatched TheTVDB Series")], "N/A")
        self.assertEqual(rows_by_title["Movie One"][header.index("Mismatched TheTVDB Title")], "N/A")

        # A series TheTVDB has no data for at all - not flagged as a
        # mismatch (nothing to compare against), just genuinely absent.
        self.assertEqual(
            rows_by_title["Episode One"][header.index("Mismatched TheTVDB Series")], "N/A"
        )
        self.assertEqual(
            rows_by_title["Episode One"][header.index("Mismatched TheTVDB Title")], "N/A"
        )

        # A series TheTVDB does have data for reads a real Yes/No, not N/A.
        self.assertEqual(
            rows_by_title["Episode Two"][header.index("Mismatched TheTVDB Series")], "No"
        )
        self.assertEqual(
            rows_by_title["Episode Two"][header.index("Mismatched TheTVDB Title")], "No"
        )

    def test_csv_rows_show_noe_for_tvdb_title_when_no_english_translation(self) -> None:
        """Regression test: an episode whose TheTVDB position has data but
        none of it in English must read "NoE" for Mismatched TheTVDB Title -
        distinct from "No" (a real comparison was made and passed) and from
        "N/A" (there's no TheTVDB data to compare against at all).
        """
        untranslated_episode = _make_item(
            title="Big Sword",
            item_id="episode-untranslated",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            series_name="Claymore",
            season_number=1,
            episode_number=1,
            path=Path("TV Shows/Claymore/Season 01/Claymore S01E01.mkv"),
        )
        library_result = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=1,
            audited_items=(untranslated_episode,),
            items_with_english_subtitles=0,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(
                _make_finding(
                    category=AuditCategory.METADATA,
                    severity=AuditSeverity.INFO,
                    title="TheTVDB title not in English",
                    check_name="tvdb_title_not_english",
                    media_item=untranslated_episode,
                ),
            ),
            tvdb_available_series=frozenset({"Claymore"}),
        )
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(library_result,),
            findings=library_result.findings,
        )

        rows = report_generator._csv_rows(result)

        header = report_generator.CSV_HEADER
        self.assertEqual(rows[0][header.index("Mismatched TheTVDB Title")], "NoE")
        # Unaffected column - not a "no English title" concern.
        self.assertEqual(rows[0][header.index("Mismatched TheTVDB Series")], "No")

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
        episode_index = report_generator.CSV_HEADER.index("Episode")
        self.assertEqual(rows[0][episode_index], "'5-7")

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

        episode_index = report_generator.CSV_HEADER.index("Episode")
        self.assertEqual(rows[0][episode_index], "1")

    def test_tvdb_title_not_english_is_excluded_from_actionable_findings(self) -> None:
        """"NoE" is a limitation, not a problem to fix - it must not inflate
        the HTML dashboard's actionable-findings counts/check pages, same as
        missing_backdrop already isn't.
        """
        self.assertIn("tvdb_title_not_english", report_generator.NON_ACTIONABLE_CHECKS)

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
            category=AuditCategory.ARTWORK,
            severity=AuditSeverity.INFO,
            title="Alien",
            message="Backdrop missing",
            check_name="missing_backdrop",
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

        # Patched to a fixed value rather than read back from the written
        # file's mtime: comparing an mtime-derived timestamp against the
        # separately-captured datetime.now() the generator actually used is
        # flaky whenever the two calls straddle a wall-clock second
        # boundary, since both are truncated to one-second precision.
        fixed_generated_at_text = "2024-01-01 12:00:00 UTC"
        with patch("reports.generator._generated_at_text", return_value=fixed_generated_at_text):
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
                self.assertFalse((server_dir / "checks" / "missing_backdrop.html").exists())

                root_index_html = (root_dir / "index.html").read_text(encoding="utf-8")
                index_html = index_path.read_text(encoding="utf-8")
                library_html = (server_dir / "libraries" / "movies.html").read_text(
                    encoding="utf-8"
                )
                check_html = (server_dir / "checks" / "missing_primary_image.html").read_text(
                    encoding="utf-8"
                )
                report_js = (root_dir / "js" / "report.js").read_text(encoding="utf-8")

        self.assertIn('href="server1/index.html"', root_index_html)
        self.assertIn(f"My Jellyfin Server ({fixed_generated_at_text})", root_index_html)
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

    def test_library_page_renders_movie_and_episode_findings_in_one_flat_table(self) -> None:
        """render_library_page() renders every finding as one row in a
        single flat table, regardless of whether it's a movie or an
        episode - there's no grouping/nesting by series or season (no
        <details> or similar), so an episode's series name has to appear
        directly in its own row instead.
        """
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

    def test_check_page_shows_details_column_for_mismatched_tvdb_title(self) -> None:
        """Unlike mismatched_episode_filename_title (whose suggested title is
        cheaply recomputable from the item's own path alone),
        mismatched_tvdb_title's suggested title depends on TheTVDB position
        data the HTML-rendering layer never sees - so, like
        mismatched_tvdb_series, it uses the generic Details column (the
        finding's own message) rather than a dedicated suggested-title one.
        """
        finding = _make_finding(
            category=AuditCategory.METADATA,
            severity=AuditSeverity.WARNING,
            title="Safe",
            message='S01E07 is titled "Safe", but TheTVDB\'s cached aired-order title at that position is "Jaynestown".',
            check_name="mismatched_tvdb_title",
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
            "mismatched_tvdb_title",
            (finding,),
            site_links=site_links,
        )

        self.assertIn(">Library</button></th>", html)
        self.assertIn(">Series</button></th>", html)
        self.assertIn(">Season</button></th>", html)
        self.assertIn(">Episode</button></th>", html)
        self.assertIn(">Title</button></th>", html)
        self.assertIn(">Details</button></th>", html)
        self.assertIn(">Safe<", html)
        self.assertIn("TheTVDB&#x27;s cached aired-order title", html)

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
