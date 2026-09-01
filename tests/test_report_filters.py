"""Tests for report_filters.py."""

from __future__ import annotations

import unittest

from report_filters import filter_report_output
from results import AuditServerResult
from results import LibraryAuditResult
from tests.helpers import _make_item
from tests.helpers import _make_library


class FilterReportOutputTests(unittest.TestCase):
    def test_preserves_tvdb_available_series_on_the_filtered_library_result(self) -> None:
        # Regression test: filter_report_output() rebuilds each
        # LibraryAuditResult, and originally dropped tvdb_available_series
        # in the process (it defaulted back to frozenset()), which made
        # every TheTVDB-dependent CSV/HTML/XLSX column read "N/A" even for
        # series TheTVDB actually had data for - since every real report
        # run goes through this function (see reports/generator.py).
        item = _make_item(
            title="Episode One",
            is_movie=False,
            is_episode=True,
            library="TV Shows",
            series_name="Listed Show",
            season_number=1,
            episode_number=1,
        )
        library_result = LibraryAuditResult(
            library=_make_library(library_id="tv", name="TV Shows", collection_type="tv"),
            media_items_processed=1,
            audited_items=(item,),
            items_with_english_subtitles=0,
            items_with_local_nfo=0,
            items_with_local_backdrop=0,
            findings=(),
            tvdb_available_series=frozenset({"Listed Show"}),
        )
        result = AuditServerResult(
            libraries_audited=1,
            media_items_processed=1,
            library_results=(library_result,),
            findings=(),
        )

        filtered = filter_report_output(result)

        self.assertEqual(
            filtered.library_results[0].tvdb_available_series, frozenset({"Listed Show"})
        )
        self.assertEqual(filtered.tvdb_available_series, frozenset({"Listed Show"}))


if __name__ == "__main__":
    unittest.main()
