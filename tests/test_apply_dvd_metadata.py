"""Tests for apply_dvd_metadata.py."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

import apply_dvd_metadata
import config
from config import ServerCollection
import jellyfin

from tests.helpers import _make_left_right_app_config
from tests.helpers import _make_tvdb_episode
from tests.helpers import _make_tvdb_search_result


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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None)
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

    def test_plan_dvd_apply_combines_name_and_overview_for_a_multi_episode_range(self) -> None:
        """Regression test: a file spanning a multi-episode range (e.g.
        S01E17-E18) must combine both positions' TheTVDB title/overview -
        Jellyfin's own episode_number for such an item is just the range's
        first episode, so looking up only that one position silently
        dropped the second episode's title/overview from the update.
        """
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E17-E18.mkv",
                    "Name": "Wrong Title",
                    "Overview": "Wrong overview.",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(
            id="ep1", name="Wrong Title", episode_number=17, path=Path("/media/show/S01E17-E18.mkv")
        )
        dvd_positions = {
            (1, 17): _make_tvdb_episode(
                season_number=1, episode_number=17, name="Title A", overview="Overview A."
            ),
            (1, 18): _make_tvdb_episode(
                season_number=1, episode_number=18, name="Title B", overview="Overview B."
            ),
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertFalse(plan.no_target_match)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.merged_dto["Name"], "Title A / Title B")
        self.assertEqual(plan.merged_dto["Overview"], "Overview A.\n\nOverview B.")
        self.assertEqual(plan.merged_dto["OriginalTitle"], "Wrong Title")

    def test_plan_dvd_apply_leaves_name_unchanged_but_still_updates_overview_when_untranslated(
        self,
    ) -> None:
        """Regression test: TheTVDB silently falls back to a series'
        original-language name for an episode with no recorded English
        translation - Name must not be rewritten to that foreign-script
        text, but Overview (unaffected by this protection) can still update
        independently, since it's a genuinely separate field.
        """
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None)
        dvd_positions = {
            (1, 1): _make_tvdb_episode(
                season_number=1,
                episode_number=1,
                name="大剣 -クレイモア-",
                overview="DVD overview.",
            )
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False
        )

        self.assertTrue(plan.no_english_title)
        self.assertTrue(plan.is_actionable)
        self.assertEqual(plan.merged_dto["Name"], "Aired Title")
        self.assertNotIn("OriginalTitle", plan.merged_dto)
        self.assertEqual(plan.merged_dto["Overview"], "DVD overview.")
        self.assertEqual([field for field, _, _ in plan.changes], ["Overview"])

    def test_plan_aired_restore_leaves_name_unchanged_when_fallback_is_untranslated(self) -> None:
        """The TheTVDB-fallback path (no OriginalTitle backup) must also
        refuse an untranslated title, same as the DVD-apply direction.
        """
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "DVD Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="大剣 -クレイモア-")
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, aired_positions, restore_aired=True
        )

        self.assertTrue(plan.no_english_title)
        self.assertEqual(plan.merged_dto["Name"], "DVD Title")

    def test_plan_aired_restore_ignores_tvdb_language_when_backup_exists(self) -> None:
        """An OriginalTitle backup restores Name without ever consulting
        TheTVDB, so its language must not affect no_english_title - the
        restore succeeds normally even if TheTVDB's own data happens to be
        untranslated at this position.
        """
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="大剣 -クレイモア-")
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, aired_positions, restore_aired=True
        )

        self.assertFalse(plan.no_english_title)
        self.assertEqual(plan.merged_dto["Name"], "Backed Up Aired Title")

    def test_plan_dvd_apply_no_match_when_only_one_position_of_a_range_has_data(self) -> None:
        """A partial update built from only some of a multi-episode range's
        positions would be guessing - every position the filename implies
        must have TheTVDB data before an update is planned at all.
        """
        episode = jellyfin.EpisodeSummary(
            id="ep1", name="Wrong Title", episode_number=17, path=Path("/media/show/S01E17-E18.mkv")
        )
        dvd_positions = {
            (1, 17): _make_tvdb_episode(season_number=1, episode_number=17, name="Title A"),
        }

        plan = apply_dvd_metadata.plan_episode_update(
            self._make_client({}), episode, 1, dvd_positions, restore_aired=False
        )

        self.assertTrue(plan.no_target_match)
        self.assertFalse(plan.is_actionable)

    def test_plan_dvd_apply_uses_first_position_image_for_a_multi_episode_range(self) -> None:
        """A multi-episode item's Primary image slot is singular - there's
        no way to combine two images into one, so the range's first
        position (the one Jellyfin's own episode_number reflects) is used.
        """
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E17-E18.mkv",
                    "Name": "Title A / Title B",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(
            id="ep1",
            name="Title A / Title B",
            episode_number=17,
            path=Path("/media/show/S01E17-E18.mkv"),
        )
        dvd_positions = {
            (1, 17): apply_dvd_metadata.TvdbEpisode(
                id=17,
                season_number=1,
                episode_number=17,
                name="Title A",
                overview=None,
                runtime_minutes=None,
                image_url="https://example.com/17.jpg",
            ),
            (1, 18): apply_dvd_metadata.TvdbEpisode(
                id=18,
                season_number=1,
                episode_number=18,
                name="Title B",
                overview=None,
                runtime_minutes=None,
                image_url="https://example.com/18.jpg",
            ),
        }
        tvdb_client = MagicMock()
        tvdb_client.download_image.return_value = (b"image-bytes", "image/jpeg")

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, dvd_positions, restore_aired=False, images=True, tvdb_client=tvdb_client
        )

        tvdb_client.download_image.assert_called_once_with("https://example.com/17.jpg")
        self.assertEqual(plan.image_bytes, b"image-bytes")

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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Same Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Same Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=5, path=None)

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
        episode = jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None)
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
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None)
        aired_positions = {
            (1, 1): _make_tvdb_episode(season_number=1, episode_number=1, name="TVDB Aired Title")
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, aired_positions, restore_aired=True
        )

        self.assertEqual(plan.merged_dto["Name"], "TVDB Aired Title")

    def test_plan_aired_restore_combines_tvdb_titles_for_a_multi_episode_range(self) -> None:
        """The TheTVDB-fallback path (no OriginalTitle backup) must combine
        every position a multi-episode range covers, same as the DVD-apply
        direction - only the OriginalTitle-backup path is already a single,
        pre-combined string needing no recombining.
        """
        client = self._make_client(
            {
                "ep1": {
                    "Id": "ep1",
                    "Path": "/media/show/S01E17-E18.mkv",
                    "Name": "DVD Title",
                }
            }
        )
        episode = jellyfin.EpisodeSummary(
            id="ep1", name="DVD Title", episode_number=17, path=Path("/media/show/S01E17-E18.mkv")
        )
        aired_positions = {
            (1, 17): _make_tvdb_episode(season_number=1, episode_number=17, name="Title A"),
            (1, 18): _make_tvdb_episode(season_number=1, episode_number=18, name="Title B"),
        }

        plan = apply_dvd_metadata.plan_episode_update(
            client, episode, 1, aired_positions, restore_aired=True
        )

        self.assertEqual(plan.merged_dto["Name"], "Title A / Title B")

    def test_plan_aired_restore_no_match_when_no_backup_and_no_tvdb_data(self) -> None:
        client = self._make_client(
            {"ep1": {"Id": "ep1", "Path": "/media/show/S01E01.mkv", "Name": "DVD Title"}}
        )
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None)

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
        episode = jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None)

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
        return _make_left_right_app_config(tvdb_api_key="tvdb-secret")

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
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None),)
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
            jellyfin.EpisodeSummary(id="ep1", name="Wrong Show's Title", episode_number=1, path=None),
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

            def find_series(self, series_name, *, library_name=None, path_filter=None):
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
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None),)
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

            def find_series(self, series_name, *, library_name=None, path_filter=None):
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
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None),)
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
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None),)
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

        with patch("apply_dvd_metadata.get_config", return_value=self._make_config()):
            with patch("apply_dvd_metadata.JellyfinClient", FakeClient):
                apply_dvd_metadata.run_apply_dvd_metadata(
                    series_name="The Office",
                    season_number=1,
                    server_key=None,
                    library_name="TV Shows",
                    path_filter="us version",
                    assume_yes=True,
                )

        self.assertEqual(find_series_calls, [("TV Shows", "us version")])

    def test_series_without_tvdb_id_is_an_error(self) -> None:
        series_matches = (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id=None),)
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Some Title", episode_number=1, path=None),)
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
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="Aired Title", episode_number=1, path=None),)
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
        episodes = (jellyfin.EpisodeSummary(id="ep1", name="DVD Title", episode_number=1, path=None),)
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
