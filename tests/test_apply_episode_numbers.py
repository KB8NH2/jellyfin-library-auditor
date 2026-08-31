"""Tests for apply_episode_numbers.py."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

import apply_episode_numbers
import config
import jellyfin

from tests.helpers import _make_left_right_app_config
from tests.helpers import _make_tvdb_episode
from tests.helpers import _make_tvdb_search_result


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
        return _make_left_right_app_config(tvdb_api_key="tvdb-secret", include_right_server=False)

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

            def find_series(self, series_name, *, library_name=None, path_filter=None):
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

    def test_omitted_season_number_updates_every_season(self) -> None:
        """Regression test: --season-number is optional - without it, every
        season the series has is fetched and updated in one batch, and each
        season's unnumbered episodes are matched only against that same
        season's own TheTVDB aired-order episodes.
        """
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),)
        episodes_by_season = {
            1: (
                jellyfin.SeasonEpisodeSummary(
                    id="ep-a", name="First", path=Path("/media/show/S01/a.mkv"), episode_number=None
                ),
            ),
            2: (
                jellyfin.SeasonEpisodeSummary(
                    id="ep-b", name="First", path=Path("/media/show/S02/a.mkv"), episode_number=None
                ),
            ),
        }
        items_by_id = {
            "ep-a": {"Id": "ep-a", "Path": "/media/show/S01/a.mkv", "Name": "First"},
            "ep-b": {"Id": "ep-b", "Path": "/media/show/S02/a.mkv", "Name": "First"},
        }
        aired_episodes = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="First"),
            _make_tvdb_episode(season_number=2, episode_number=1, name="First"),
        )
        update_calls: list = []

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None, path_filter=None):
                return series_matches

            def get_series_season_numbers(self, series_id):
                return tuple(sorted(episodes_by_season))

            def get_series_season_episodes_all(self, series_id, season_number):
                return episodes_by_season[season_number]

            def get_series_episode_positions(self, series_id):
                return frozenset()

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        fake_tvdb_client = self._make_fake_tvdb_client(aired_episodes)

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", FakeClient):
                with patch("apply_episode_numbers.TvdbClient", fake_tvdb_client):
                    with patch("builtins.input", return_value="y"):
                        exit_code = apply_episode_numbers.run_apply_episode_numbers(
                            series_name="Breaking Bad",
                            season_number=None,
                            server_key=None,
                            library_name=None,
                            assume_yes=True,
                        )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 2)
        updated_ids = {item_id for item_id, _ in update_calls}
        self.assertEqual(updated_ids, {"ep-a", "ep-b"})
        self.assertEqual(items_by_id["ep-a"]["IndexNumber"], 1)
        self.assertEqual(items_by_id["ep-b"]["IndexNumber"], 1)

    def test_season_number_defaults_to_none_when_omitted(self) -> None:
        parser = apply_episode_numbers._build_argument_parser()

        args = parser.parse_args(["--series-name", "Breaking Bad"])

        self.assertIsNone(args.season_number)

    def test_main_rejects_negative_season_number(self) -> None:
        exit_code = apply_episode_numbers.main(
            ["--series-name", "Breaking Bad", "--season-number", "-1"]
        )

        self.assertEqual(exit_code, 2)

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

            def find_series(self, series_name, *, library_name=None, path_filter=None):
                return series_matches

            def get_series_season_episodes_all(self, series_id, season_number):
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

    def test_path_filter_is_passed_through_to_find_series(self) -> None:
        find_series_calls: list = []

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_series(self, series_name, *, library_name=None, path_filter=None):
                find_series_calls.append((library_name, path_filter))
                return ()

        with patch("apply_episode_numbers.get_config", return_value=self._make_config()):
            with patch("apply_episode_numbers.JellyfinClient", FakeClient):
                apply_episode_numbers.run_apply_episode_numbers(
                    series_name="The FBI Files",
                    season_number=1,
                    server_key=None,
                    library_name="TV Shows",
                    path_filter="us version",
                    assume_yes=True,
                )

        self.assertEqual(find_series_calls, [("TV Shows", "us version")])


if __name__ == "__main__":
    unittest.main()
