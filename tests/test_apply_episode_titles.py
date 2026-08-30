"""Tests for apply_episode_titles.py."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import apply_episode_titles
import config
import jellyfin

from tests.helpers import _make_left_right_app_config
from tests.helpers import _make_tvdb_episode
from tests.helpers import _make_tvdb_search_result


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
