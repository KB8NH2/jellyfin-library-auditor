"""Tests for audit.py's per-item and per-library audit rules."""

from __future__ import annotations

from pathlib import Path
import unittest

import audit
from audit_types import AuditCategory
from audit_types import AuditSeverity
import media

from tests.helpers import _make_item
from tests.helpers import _make_tvdb_episode


class AuditFindingsTests(unittest.TestCase):
    def test_audit_media_item_flags_every_failing_check(self) -> None:
        """audit_media_item() applies every per-item check with no
        actionability filtering of its own - "missing_backdrop" fires here
        even though it's excluded from the HTML report later (see
        reports.generator.NON_ACTIONABLE_CHECKS, exercised in
        test_reports.py's test_write_html_report_creates_simplified_site_tree).
        """
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
        """Season 0 must be excluded from the unmatched-ratio calculation
        entirely, not just tolerated as one more (unmatched) episode -
        otherwise enough specials, which mismatched_tvdb_series never looks
        up TheTVDB data for, would themselves drag a correctly-matched
        series over the mismatch threshold. Five real episodes (all
        matched) plus five specials (necessarily unmatched, since
        aired_positions has no season-0 entries) would score 5/10 = 0.5 -
        at the mismatch threshold - if specials were counted; excluded, it
        scores 0/5 and reports no finding.
        """
        items = self._make_series_items(5) + tuple(
            _make_item(
                f"Special {number}",
                is_movie=False,
                is_episode=True,
                series_name="Mismatched Show",
                season_number=0,
                episode_number=number,
            )
            for number in range(1, 6)
        )
        aired_positions = {
            "Mismatched Show": {
                (1, number): _make_tvdb_episode(season_number=1, episode_number=number, name=f"Episode {number}")
                for number in range(1, 6)
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


class MismatchedTvdbTitleTests(unittest.TestCase):
    """Unlike audit_episode_ordering/aired_dvd_order_mismatch (which only
    flags a local title matching neither aired nor DVD order), this only
    ever compares against aired order - the ordering tvdb_cache.json and
    TheTVDB's own default reflect - and runs unconditionally whenever a
    TheTVDB api_key is configured, not just with --check-episode-order.
    """

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

        findings = audit.mismatched_tvdb_title([item], aired_positions)

        self.assertEqual(findings, ())

    def test_flags_when_local_title_differs_from_aired_order(self) -> None:
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

        findings = audit.mismatched_tvdb_title([item], aired_positions)

        self.assertEqual(len(findings), 1)
        finding = findings[0]
        self.assertEqual(finding.check_name, "mismatched_tvdb_title")
        self.assertEqual(finding.category, AuditCategory.METADATA)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Aired Title", finding.message)
        self.assertIn("Something Else Entirely", finding.message)
        self.assertIs(finding.media_item, item)

    def test_no_finding_when_no_aired_positions_given(self) -> None:
        item = _make_item(
            "Something Else Entirely",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=3,
        )

        self.assertEqual(audit.mismatched_tvdb_title([item], None), ())
        self.assertEqual(audit.mismatched_tvdb_title([item], {}), ())

    def test_no_finding_when_position_missing_from_aired_order(self) -> None:
        item = _make_item(
            "Unmapped",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
            season_number=1,
            episode_number=99,
        )
        aired_positions = {
            "Example Series": {
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="Aired Title"),),
            }
        }

        findings = audit.mismatched_tvdb_title([item], aired_positions)

        self.assertEqual(findings, ())

    def test_no_finding_for_non_episode_items(self) -> None:
        item = _make_item("Movie", is_movie=True, is_episode=False)
        aired_positions = {"Example Series": {(1, 1): (_make_tvdb_episode(name="X"),)}}

        self.assertEqual(audit.mismatched_tvdb_title([item], aired_positions), ())

    def test_no_finding_when_title_matches_any_candidate_sharing_a_position(self) -> None:
        """Regression test: several same-named TheTVDB series can each
        independently number their own "Season 1, Episode 1" - the local
        title should match if it agrees with any one of them.
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

        findings = audit.mismatched_tvdb_title([item], aired_positions)

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

        findings = audit.mismatched_tvdb_title([item], aired_positions)

        self.assertEqual(len(findings), 1)
        message = findings[0].message
        self.assertIn('"An Unearthly Child"', message)
        self.assertIn('"Rose"', message)

    def test_no_finding_when_only_candidate_title_is_untranslated(self) -> None:
        """Regression test: TheTVDB silently falls back to a series'
        original-language name for an episode with no recorded English
        translation - there's no way to tell whether that untranslated name
        matches the local title or not, so it must not be treated as a
        mismatch.
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
                    _make_tvdb_episode(season_number=1, episode_number=1, name="大剣 -クレイモア-"),
                ),
            }
        }

        findings = audit.mismatched_tvdb_title([item], aired_positions)

        self.assertEqual(findings, ())

    def test_no_finding_when_combined_title_matches_multi_episode_range(self) -> None:
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
                (1, 7): (_make_tvdb_episode(season_number=1, episode_number=7, name="Title Seven"),),
            }
        }

        findings = audit.mismatched_tvdb_title([item], aired_positions)

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
                (1, 7): (_make_tvdb_episode(season_number=1, episode_number=7, name="Title Seven"),),
            }
        }

        findings = audit.mismatched_tvdb_title([item], aired_positions)

        self.assertEqual(len(findings), 1)
        self.assertIn("S01E05-E07", findings[0].message)
        self.assertIn("Title Five / Title Six / Title Seven", findings[0].message)

    def test_audit_library_items_suppresses_title_check_for_a_mismatched_series(self) -> None:
        """Mirrors mismatched_tvdb_series' own suppression test: a series
        already flagged as matched to the wrong TheTVDB entry shouldn't also
        get a spurious title-mismatch finding sourced from that same wrong
        match.
        """
        items = tuple(
            _make_item(
                f"Episode {number}",
                is_movie=False,
                is_episode=True,
                series_name="Mismatched Show",
                season_number=1,
                episode_number=number,
            )
            for number in range(1, 8)
        )
        aired_positions = {
            "Mismatched Show": {
                (1, 1): (_make_tvdb_episode(season_number=1, episode_number=1, name="Episode 1"),),
                (1, 2): (_make_tvdb_episode(season_number=1, episode_number=2, name="Episode 2"),),
                (1, 3): (_make_tvdb_episode(season_number=1, episode_number=3, name="Something Else"),),
            }
        }

        findings = audit.audit_library_items(items, aired_positions)

        check_names = {finding.check_name for finding in findings}
        self.assertIn("mismatched_tvdb_series", check_names)
        self.assertNotIn("mismatched_tvdb_title", check_names)


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
