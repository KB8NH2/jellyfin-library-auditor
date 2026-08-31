"""Tests for apply_titles_from_filename.py."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import apply_titles_from_filename
import jellyfin

from tests.helpers import _make_left_right_app_config


class PlanUpdateTests(unittest.TestCase):
    def _make_client(self, item_by_id: dict) -> object:
        class FakeClient:
            def get_item(self, item_id: str) -> dict:
                return item_by_id[item_id]

        return FakeClient()

    def test_plan_computes_name_and_original_title_backup(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01 Episode Title.mkv", "Name": "Wrong Title"}}
        )

        plan = apply_titles_from_filename.plan_filename_title_update(
            client, "ep1", "S01E01", "Wrong Title", "Episode Title"
        )

        self.assertFalse(plan.no_target_match)
        self.assertFalse(plan.already_matches)
        self.assertIsNone(plan.rejected_reason)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(
            plan.changes,
            (
                ("Name", "Wrong Title", "Episode Title"),
                ("OriginalTitle", None, "Wrong Title"),
            ),
        )
        self.assertEqual(plan.merged_dto["Name"], "Episode Title")
        self.assertEqual(plan.merged_dto["OriginalTitle"], "Wrong Title")

    def test_plan_skips_title_already_matching_under_lenient_comparison(self) -> None:
        client = self._make_client(
            {"movie1": {"Id": "movie1", "Path": "/media/movies/Movie.mkv", "Name": "The Colour of Money"}}
        )

        plan = apply_titles_from_filename.plan_filename_title_update(
            client, "movie1", "Movie", "The Colour of Money", "The Color of Money"
        )

        self.assertTrue(plan.already_matches)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)

    def test_plan_reports_no_target_match_without_fetching_the_item(self) -> None:
        class ExplodingClient:
            def get_item(self, item_id: str) -> dict:
                raise AssertionError("should not fetch an item with no filename-implied title")

        plan = apply_titles_from_filename.plan_filename_title_update(
            ExplodingClient(), "ep1", "S01E99", "Some Title", None
        )

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)

    def test_plan_skips_already_matching_title_without_fetching_the_item(self) -> None:
        class ExplodingClient:
            def get_item(self, item_id: str) -> dict:
                raise AssertionError("should not fetch an item that already matches")

        plan = apply_titles_from_filename.plan_filename_title_update(
            ExplodingClient(), "ep1", "S01E01", "Episode Title", "Episode Title"
        )

        self.assertTrue(plan.already_matches)

    def test_plan_rejects_when_required_fields_missing(self) -> None:
        client = self._make_client({"ep1": {"Id": "ep1", "Name": "Wrong Title"}})

        plan = apply_titles_from_filename.plan_filename_title_update(
            client, "ep1", "S01E01", "Wrong Title", "Episode Title"
        )

        self.assertIsNotNone(plan.rejected_reason)
        self.assertFalse(plan.is_actionable)

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

        plan = apply_titles_from_filename.plan_filename_title_update(
            client, "ep1", "S01E01", "Wrong Title", "Episode Title"
        )

        self.assertEqual(plan.merged_dto["LockedFields"], ["Genres", "Name"])


class PlanRestoreTests(unittest.TestCase):
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
                    "Name": "Episode Title",
                    "OriginalTitle": "Original Title",
                }
            }
        )

        plan = apply_titles_from_filename.plan_filename_title_restore(
            client, "ep1", "S01E01", "Episode Title"
        )

        self.assertFalse(plan.no_target_match)
        self.assertFalse(plan.already_matches)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.changes, (("Name", "Episode Title", "Original Title"),))
        self.assertEqual(plan.merged_dto["Name"], "Original Title")
        self.assertEqual(plan.merged_dto["OriginalTitle"], "Original Title")

    def test_plan_reports_no_backup_when_original_title_is_missing(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "Episode Title"}}
        )

        plan = apply_titles_from_filename.plan_filename_title_restore(
            client, "ep1", "S01E01", "Episode Title"
        )

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

        plan = apply_titles_from_filename.plan_filename_title_restore(
            client, "ep1", "S01E01", "Original Title"
        )

        self.assertTrue(plan.already_matches)
        self.assertFalse(plan.is_actionable)
        self.assertIsNone(plan.merged_dto)


class ExpectedTitleHelperTests(unittest.TestCase):
    def test_expected_title_for_episode_reads_the_sxxexx_marker(self) -> None:
        title = apply_titles_from_filename._expected_title_for_episode(
            Path("/media/show/Season 01/Show S01E02 Episode Title.mkv"), 1, 2
        )

        self.assertEqual(title, "Episode Title")

    def test_expected_title_for_episode_returns_none_without_a_marker(self) -> None:
        title = apply_titles_from_filename._expected_title_for_episode(
            Path("/media/show/Season 01/Show.mkv"), 1, 2
        )

        self.assertIsNone(title)

    def test_expected_title_for_movie_reads_the_year_marker(self) -> None:
        title = apply_titles_from_filename._expected_title_for_movie(
            Path("/media/movies/Movie Name (2001).mkv"), 2001
        )

        self.assertEqual(title, "Movie Name")

    def test_expected_title_for_movie_returns_none_without_a_year_match(self) -> None:
        title = apply_titles_from_filename._expected_title_for_movie(
            Path("/media/movies/Movie Name.mkv"), 2001
        )

        self.assertIsNone(title)


class RunApplyTitlesFromFilenameCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "apply_titles_from_filename.TITLES_FROM_FILENAME_LOG_FILE",
            Path(temp_dir.name) / "titles_from_filename_apply.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self):
        return _make_left_right_app_config(include_right_server=False)

    def _make_fake_client(
        self,
        *,
        series_matches: tuple = (),
        movie_matches: tuple = (),
        episodes: tuple = (),
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

            def find_movie(self, movie_name, *, library_name=None, path_filter=None):
                return movie_matches

            def get_series_season_episodes_all(self, series_id, season_number):
                return episodes

            def get_item(self, item_id):
                return items_by_id[item_id]

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))
                items_by_id[item_id] = item_dto

        return FakeClient

    def test_renames_movie_after_confirmation(self) -> None:
        movie_matches = (
            jellyfin.MovieMatch(
                library_name="Movies",
                movie_id="movie1",
                name="Wrong Title",
                path=Path("/media/movies/Movie Name (2001).mkv"),
                year=2001,
            ),
        )
        items_by_id = {
            "movie1": {"Id": "movie1", "Path": "/media/movies/Movie Name (2001).mkv", "Name": "Wrong Title"}
        }
        update_calls: list = []
        fake_client = self._make_fake_client(
            movie_matches=movie_matches, items_by_id=items_by_id, update_calls=update_calls
        )

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                        movie_name="Wrong Title",
                        series_name=None,
                        season_number=None,
                        server_key=None,
                        library_name=None,
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "movie1")
        self.assertEqual(updated_dto["Name"], "Movie Name")
        self.assertEqual(updated_dto["OriginalTitle"], "Wrong Title")

    def test_renames_every_episode_in_the_season(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id=None),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(
                id="ep1",
                name="Wrong Title",
                path=Path("/media/show/Season 01/Show S01E01 Episode Title.mkv"),
                episode_number=1,
            ),
        )
        items_by_id = {
            "ep1": {
                "Id": "ep1",
                "Path": "/media/show/Season 01/Show S01E01 Episode Title.mkv",
                "Name": "Wrong Title",
            }
        }
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches, episodes=episodes, items_by_id=items_by_id, update_calls=update_calls
        )

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                        movie_name=None,
                        series_name="Show",
                        season_number=1,
                        server_key=None,
                        library_name=None,
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1]["Name"], "Episode Title")

    def test_episode_missing_a_path_is_skipped_as_no_target_match(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id=None),)
        episodes = (
            jellyfin.SeasonEpisodeSummary(id="ep1", name="Some Title", path=None, episode_number=1),
        )
        update_calls: list = []
        fake_client = self._make_fake_client(
            series_matches=series_matches, episodes=episodes, items_by_id={}, update_calls=update_calls
        )

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name=None,
                    series_name="Show",
                    season_number=1,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_ambiguous_movie_is_an_error(self) -> None:
        movie_matches = (
            jellyfin.MovieMatch(library_name="Movies", movie_id="m1", name="Dup", path=None, year=None),
            jellyfin.MovieMatch(library_name="Kids Movies", movie_id="m2", name="Dup", path=None, year=None),
        )
        fake_client = self._make_fake_client(movie_matches=movie_matches, items_by_id={}, update_calls=[])

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name="Dup",
                    series_name=None,
                    season_number=None,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 1)

    def test_movie_not_found_is_an_error(self) -> None:
        fake_client = self._make_fake_client(movie_matches=(), items_by_id={}, update_calls=[])

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name="Missing",
                    series_name=None,
                    season_number=None,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 1)

    def test_restore_sets_name_from_original_title_backup(self) -> None:
        movie_matches = (
            jellyfin.MovieMatch(
                library_name="Movies",
                movie_id="movie1",
                name="Movie Name",
                path=Path("/media/movies/Movie Name (2001).mkv"),
                year=2001,
            ),
        )
        items_by_id = {
            "movie1": {
                "Id": "movie1",
                "Path": "/media/movies/Movie Name (2001).mkv",
                "Name": "Movie Name",
                "OriginalTitle": "Original Title",
            }
        }
        update_calls: list = []
        fake_client = self._make_fake_client(
            movie_matches=movie_matches, items_by_id=items_by_id, update_calls=update_calls
        )

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                        movie_name="Movie Name",
                        series_name=None,
                        season_number=None,
                        server_key=None,
                        library_name=None,
                        assume_yes=False,
                        restore=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "movie1")
        self.assertEqual(updated_dto["Name"], "Original Title")

    def test_restore_reports_nothing_to_do_without_a_backup(self) -> None:
        movie_matches = (
            jellyfin.MovieMatch(
                library_name="Movies",
                movie_id="movie1",
                name="Movie Name",
                path=Path("/media/movies/Movie Name (2001).mkv"),
                year=2001,
            ),
        )
        items_by_id = {
            "movie1": {"Id": "movie1", "Path": "/media/movies/Movie Name (2001).mkv", "Name": "Movie Name"}
        }
        update_calls: list = []
        fake_client = self._make_fake_client(
            movie_matches=movie_matches, items_by_id=items_by_id, update_calls=update_calls
        )

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", fake_client):
                exit_code = apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name="Movie Name",
                    series_name=None,
                    season_number=None,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                    restore=True,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_raises_when_neither_movie_nor_series_given(self) -> None:
        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with self.assertRaises(ValueError):
                apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name=None,
                    series_name=None,
                    season_number=None,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

    def test_raises_when_both_movie_and_series_given(self) -> None:
        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with self.assertRaises(ValueError):
                apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name="Movie Name",
                    series_name="Show",
                    season_number=1,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

    def test_raises_when_season_number_given_without_series_name(self) -> None:
        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with self.assertRaises(ValueError):
                apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name=None,
                    series_name=None,
                    season_number=1,
                    server_key=None,
                    library_name=None,
                    assume_yes=True,
                )

    def test_path_filter_is_passed_through_to_find_movie(self) -> None:
        find_movie_calls: list = []

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def find_movie(self, movie_name, *, library_name=None, path_filter=None):
                find_movie_calls.append((library_name, path_filter))
                return ()

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", FakeClient):
                apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name="Dup",
                    series_name=None,
                    season_number=None,
                    server_key=None,
                    library_name="Movies",
                    path_filter="horror",
                    assume_yes=True,
                )

        self.assertEqual(find_movie_calls, [("Movies", "horror")])

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

        with patch("apply_titles_from_filename.get_config", return_value=self._make_config()):
            with patch("apply_titles_from_filename.JellyfinClient", FakeClient):
                apply_titles_from_filename.run_apply_titles_from_filename(
                    movie_name=None,
                    series_name="Show",
                    season_number=1,
                    server_key=None,
                    library_name="TV Shows",
                    path_filter="us version",
                    assume_yes=True,
                )

        self.assertEqual(find_series_calls, [("TV Shows", "us version")])


class MainArgumentValidationTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "apply_titles_from_filename.TITLES_FROM_FILENAME_LOG_FILE",
            Path(temp_dir.name) / "titles_from_filename_apply.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def test_rejects_movie_combined_with_series_name(self) -> None:
        exit_code = apply_titles_from_filename.main(
            ["--movie", "Movie Name", "--series-name", "Show", "--season-number", "1"]
        )

        self.assertEqual(exit_code, 2)

    def test_rejects_neither_movie_nor_series_name(self) -> None:
        exit_code = apply_titles_from_filename.main([])

        self.assertEqual(exit_code, 2)

    def test_rejects_series_name_without_season_number(self) -> None:
        exit_code = apply_titles_from_filename.main(["--series-name", "Show"])

        self.assertEqual(exit_code, 2)

    def test_rejects_season_number_without_series_name(self) -> None:
        exit_code = apply_titles_from_filename.main(["--movie", "X", "--season-number", "1"])

        self.assertEqual(exit_code, 2)

    def test_rejects_negative_season_number(self) -> None:
        exit_code = apply_titles_from_filename.main(
            ["--series-name", "Show", "--season-number", "-1"]
        )

        self.assertEqual(exit_code, 2)

    def test_path_argument_is_forwarded_to_run_apply_titles_from_filename(self) -> None:
        captured_kwargs: dict = {}

        def fake_run(**kwargs):
            captured_kwargs.update(kwargs)
            return 0

        with patch("apply_titles_from_filename.run_apply_titles_from_filename", fake_run):
            exit_code = apply_titles_from_filename.main(
                ["--movie", "Movie Name", "--path", "horror"]
            )

        self.assertEqual(exit_code, 0)
        self.assertEqual(captured_kwargs["path_filter"], "horror")


if __name__ == "__main__":
    unittest.main()
