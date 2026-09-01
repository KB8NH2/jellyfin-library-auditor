"""Tests for tvdb_series_resolution.py.

Shared by apply_tvdb_metadata.py and apply_episode_numbers.py - covering it
here covers both call sites.
"""

from __future__ import annotations

import logging
import unittest

import tvdb
import tvdb_series_resolution

from tests.helpers import _make_tvdb_episode
from tests.helpers import _make_tvdb_search_result


_LOGGER = logging.getLogger("test_tvdb_series_resolution")


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

        result = tvdb_series_resolution.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804", logger=_LOGGER
        )

        self.assertEqual(result, "78804")

    def test_returns_assigned_id_unchanged_when_no_other_candidates_found(self) -> None:
        client = self._make_client(frozenset({(1, 1)}))
        tvdb_client = self._make_tvdb_client(search_results=())

        result = tvdb_series_resolution.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804", logger=_LOGGER
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

        result = tvdb_series_resolution.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804", logger=_LOGGER
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

        result = tvdb_series_resolution.resolve_series_tvdb_id(
            client, tvdb_client, "Doctor Who", "series-id", "78804", logger=_LOGGER
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

        result = tvdb_series_resolution.resolve_series_tvdb_id(
            client, tvdb_client, "Some Show", "series-id", None, logger=_LOGGER
        )

        self.assertEqual(result, "found-id")

    def test_returns_none_when_no_id_assigned_and_no_candidate_found(self) -> None:
        local_positions = frozenset({(1, 1)})
        client = self._make_client(local_positions)
        tvdb_client = self._make_tvdb_client(search_results=())

        result = tvdb_series_resolution.resolve_series_tvdb_id(
            client, tvdb_client, "Some Show", "series-id", None, logger=_LOGGER
        )

        self.assertIsNone(result)

    def test_logs_a_warning_through_the_caller_supplied_logger_on_search_failure(self) -> None:
        """Regression test: a skipped-lookup warning must be attributed to
        whichever tool called this (apply_tvdb_metadata, apply_episode_numbers),
        not to tvdb_series_resolution itself - the
        logger is a required parameter specifically so each call site's
        warning shows up in that tool's own log output.
        """
        client = self._make_client(frozenset({(1, 1)}))

        class ExplodingTvdbClient:
            def search_series(self, name: str) -> tuple:
                raise tvdb.TvdbError("boom")

        with self.assertLogs("test_tvdb_series_resolution", level="WARNING") as log_context:
            result = tvdb_series_resolution.resolve_series_tvdb_id(
                client, ExplodingTvdbClient(), "Some Show", "series-id", "78804", logger=_LOGGER
            )

        self.assertEqual(result, "78804")
        self.assertIn("Skipping TheTVDB series search", log_context.output[0])
