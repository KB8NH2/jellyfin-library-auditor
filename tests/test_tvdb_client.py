"""Tests for tvdb.py's TvdbClient and TvdbEpisodeCache."""

from __future__ import annotations

from datetime import datetime
from datetime import timedelta
from datetime import timezone
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

import requests
import tvdb

from tests.helpers import _make_tvdb_episode
from tests.helpers import _make_tvdb_search_result


class TvdbClientTests(unittest.TestCase):
    def _make_client(self) -> tvdb.TvdbClient:
        return tvdb.TvdbClient("test-api-key")

    @staticmethod
    def _make_response(payload: object) -> MagicMock:
        response = MagicMock()
        response.content = b"{}"
        response.json.return_value = payload
        return response

    def test_get_series_episodes_logs_in_then_fetches_requested_ordering(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        episodes_response = self._make_response(
            {
                "data": {
                    "episodes": [
                        {
                            "id": 1,
                            "seasonNumber": 1,
                            "number": 1,
                            "name": "Pilot",
                            "overview": "The first episode.",
                            "runtime": 22,
                        },
                    ]
                }
            }
        )
        empty_page_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, episodes_response, empty_page_response],
        ) as mock_request:
            episodes = client.get_series_episodes("123", "dvd")

        self.assertEqual(mock_request.call_count, 3)
        mock_request.assert_any_call(
            method="POST",
            url="https://api4.thetvdb.com/v4/login",
            params=None,
            json={"apikey": "test-api-key"},
            timeout=client._timeout,
        )
        mock_request.assert_any_call(
            method="GET",
            url="https://api4.thetvdb.com/v4/series/123/episodes/dvd",
            params={"page": 0, "lang": "eng"},
            json=None,
            timeout=client._timeout,
        )
        self.assertEqual(len(episodes), 1)
        self.assertEqual(episodes[0].name, "Pilot")
        self.assertEqual(episodes[0].season_number, 1)
        self.assertEqual(episodes[0].episode_number, 1)
        self.assertEqual(episodes[0].overview, "The first episode.")
        self.assertEqual(episodes[0].runtime_minutes, 22)

    def test_get_series_episodes_reuses_cached_token_across_calls(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        empty_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, empty_response, empty_response],
        ) as mock_request:
            client.get_series_episodes("1", "official")
            client.get_series_episodes("2", "official")

        self.assertEqual(mock_request.call_count, 3)

    def test_get_series_episodes_paginates_until_an_empty_page(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})

        def _page(episode_id: int) -> MagicMock:
            return self._make_response(
                {
                    "data": {
                        "episodes": [
                            {
                                "id": episode_id,
                                "seasonNumber": 1,
                                "number": episode_id,
                                "name": f"Episode {episode_id}",
                            }
                        ]
                    }
                }
            )

        empty_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, _page(1), _page(2), empty_response],
        ):
            episodes = client.get_series_episodes("1", "official")

        self.assertEqual([episode.id for episode in episodes], [1, 2])

    def test_skips_episode_entries_missing_season_or_episode_number(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        page_response = self._make_response(
            {
                "data": {
                    "episodes": [
                        {"id": 1, "seasonNumber": None, "number": 1, "name": "Missing Season"},
                        {"id": 2, "seasonNumber": 1, "number": 2, "name": "Fine"},
                    ]
                }
            }
        )
        empty_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, page_response, empty_response],
        ):
            episodes = client.get_series_episodes("1", "official")

        self.assertEqual([episode.id for episode in episodes], [2])

    def test_wraps_http_errors_as_tvdb_request_error(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 401
        response.content = b"unauthorized"
        response.text = "unauthorized"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)

        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(tvdb.TvdbRequestError):
                client.get_series_episodes("1", "official")

    def test_rejects_blank_api_key(self) -> None:
        with self.assertRaises(tvdb.TvdbConfigurationError):
            tvdb.TvdbClient("   ")

    def test_get_series_episodes_returns_cached_result_without_any_http_call(self) -> None:
        cached_episode = _make_tvdb_episode(season_number=1, episode_number=1, name="Cached")
        cache = MagicMock()
        cache.get.return_value = (cached_episode,)
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request") as mock_request:
            episodes = client.get_series_episodes("123", "official")

        mock_request.assert_not_called()
        self.assertEqual(episodes, (cached_episode,))
        cache.get.assert_called_once_with("123", "official")

    def test_get_series_episodes_populates_cache_on_a_miss(self) -> None:
        cache = MagicMock()
        cache.get.return_value = None
        client = tvdb.TvdbClient("test-api-key", cache=cache)
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        episodes_response = self._make_response(
            {"data": {"episodes": [{"id": 1, "seasonNumber": 1, "number": 1, "name": "Pilot"}]}}
        )
        empty_page_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, episodes_response, empty_page_response],
        ):
            episodes = client.get_series_episodes("123", "official")

        cache.set.assert_called_once_with("123", "official", episodes)

    def test_get_series_episodes_records_series_name_when_given(self) -> None:
        cache = MagicMock()
        cache.get.return_value = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Cached"),
        )
        cache.get_series_name.return_value = None
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request") as mock_request:
            client.get_series_episodes("123", "official", series_name="Ghosts")

        mock_request.assert_not_called()
        cache.set_series_name.assert_called_once_with("123", "Ghosts")

    def test_get_series_episodes_does_not_rewrite_an_unchanged_series_name(self) -> None:
        cache = MagicMock()
        cache.get.return_value = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Cached"),
        )
        cache.get_series_name.return_value = "Ghosts"
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        client.get_series_episodes("123", "official", series_name="Ghosts")

        cache.set_series_name.assert_not_called()

    def test_get_series_episodes_does_not_record_a_series_name_when_not_given(self) -> None:
        cache = MagicMock()
        cache.get.return_value = (
            _make_tvdb_episode(season_number=1, episode_number=1, name="Cached"),
        )
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        client.get_series_episodes("123", "official")

        cache.set_series_name.assert_not_called()

    def test_search_series_logs_in_then_returns_results(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        search_response = self._make_response(
            {
                "data": [
                    {
                        "tvdb_id": "400267",
                        "name": "Ghosts",
                        "year": "2021",
                        "overview": "A US sitcom.",
                    },
                    {
                        "tvdb_id": "361701",
                        "name": "Ghosts",
                        "year": "2019",
                        "overview": "A UK sitcom.",
                    },
                ]
            }
        )

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, search_response],
        ) as mock_request:
            results = client.search_series("Ghosts")

        mock_request.assert_any_call(
            method="GET",
            url="https://api4.thetvdb.com/v4/search",
            params={"query": "Ghosts", "type": "series"},
            json=None,
            timeout=client._timeout,
        )
        self.assertEqual([result.id for result in results], ["400267", "361701"])
        self.assertEqual(results[0].name, "Ghosts")
        self.assertEqual(results[0].year, "2021")
        self.assertEqual(results[0].overview, "A US sitcom.")

    def test_search_series_falls_back_to_id_field_when_tvdb_id_missing(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        search_response = self._make_response(
            {"data": [{"id": "series-400267", "name": "Ghosts"}]}
        )

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, search_response],
        ):
            results = client.search_series("Ghosts")

        self.assertEqual(results[0].id, "400267")

    def test_search_series_returns_cached_result_without_any_http_call(self) -> None:
        cached_result = _make_tvdb_search_result(series_id="400267", name="Ghosts")
        cache = MagicMock()
        cache.get_search.return_value = (cached_result,)
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request") as mock_request:
            results = client.search_series("Ghosts")

        mock_request.assert_not_called()
        self.assertEqual(results, (cached_result,))
        cache.get_search.assert_called_once_with("Ghosts")

    def test_search_series_populates_cache_on_a_miss(self) -> None:
        cache = MagicMock()
        cache.get_search.return_value = None
        client = tvdb.TvdbClient("test-api-key", cache=cache)
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        search_response = self._make_response(
            {"data": [{"tvdb_id": "400267", "name": "Ghosts"}]}
        )

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, search_response],
        ):
            results = client.search_series("Ghosts")

        cache.set_search.assert_called_once_with("Ghosts", results)

    def test_request_count_starts_at_zero(self) -> None:
        client = self._make_client()

        self.assertEqual(client.request_count, 0)

    def test_request_count_counts_login_and_each_page(self) -> None:
        client = self._make_client()
        login_response = self._make_response({"data": {"token": "jwt-token"}})
        episodes_response = self._make_response(
            {"data": {"episodes": [{"id": 1, "seasonNumber": 1, "number": 1, "name": "Pilot"}]}}
        )
        empty_page_response = self._make_response({"data": {"episodes": []}})

        with patch.object(
            client._session,
            "request",
            side_effect=[login_response, episodes_response, empty_page_response],
        ):
            client.get_series_episodes("123", "dvd")

        self.assertEqual(client.request_count, 3)

    def test_request_count_is_not_incremented_by_a_cache_hit(self) -> None:
        cached_result = _make_tvdb_search_result(series_id="400267", name="Ghosts")
        cache = MagicMock()
        cache.get_search.return_value = (cached_result,)
        client = tvdb.TvdbClient("test-api-key", cache=cache)

        with patch.object(client._session, "request"):
            client.search_series("Ghosts")

        self.assertEqual(client.request_count, 0)

    def test_request_count_includes_image_downloads(self) -> None:
        client = self._make_client()
        image_response = MagicMock()
        image_response.content = b"image-bytes"
        image_response.headers = {"Content-Type": "image/jpeg"}
        with patch.object(client._session, "request", return_value=image_response):
            client.download_image("https://artworks.thetvdb.com/banners/episode.jpg")

        self.assertEqual(client.request_count, 1)


class TvdbEpisodeCacheTests(unittest.TestCase):
    def test_returns_none_for_an_empty_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")

            self.assertIsNone(cache.get("123", "official"))

    def test_returns_a_value_stored_within_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(
                Path(temp_dir) / "cache.json",
                ttl=timedelta(days=7),
            )
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("123", "official", episodes)

            self.assertEqual(cache.get("123", "official"), episodes)

    def test_returns_none_once_the_entry_is_older_than_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)
            cache.set("123", "official", episodes)

            stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            document["series"]["123"]["official"]["fetched_at"] = stale_timestamp
            cache_path.write_text(json.dumps(document), encoding="utf-8")

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))

            self.assertIsNone(reloaded_cache.get("123", "official"))

    def test_force_refresh_always_misses_on_read_but_still_persists_writes(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path, force_refresh=True)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("123", "official", episodes)

            self.assertIsNone(cache.get("123", "official"))
            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path, force_refresh=False)
            self.assertEqual(reloaded_cache.get("123", "official"), episodes)

    def test_persists_across_separate_cache_instances(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            episodes = (
                _make_tvdb_episode(
                    season_number=1,
                    episode_number=1,
                    name="Pilot",
                    overview="An overview.",
                    runtime_minutes=42,
                ),
            )
            tvdb.TvdbEpisodeCache(cache_path).set("123", "dvd", episodes)

            second_cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertEqual(second_cache.get("123", "dvd"), episodes)

    def test_tolerates_a_missing_cache_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "does-not-exist.json")

            self.assertIsNone(cache.get("123", "official"))

    def test_tolerates_a_corrupt_cache_file(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache_path.write_text("not valid json", encoding="utf-8")

            cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertIsNone(cache.get("123", "official"))

    def test_distinguishes_between_season_types_for_the_same_series(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")
            official_episodes = (
                _make_tvdb_episode(season_number=1, episode_number=1, name="Aired Title"),
            )
            dvd_episodes = (
                _make_tvdb_episode(season_number=1, episode_number=1, name="DVD Title"),
            )

            cache.set("123", "official", official_episodes)
            cache.set("123", "dvd", dvd_episodes)

            self.assertEqual(cache.get("123", "official"), official_episodes)
            self.assertEqual(cache.get("123", "dvd"), dvd_episodes)

    def test_search_returns_none_for_an_empty_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")

            self.assertIsNone(cache.get_search("Ghosts"))

    def test_search_returns_a_value_stored_within_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(
                Path(temp_dir) / "cache.json",
                ttl=timedelta(days=7),
            )
            results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)

            cache.set_search("Ghosts", results)

            self.assertEqual(cache.get_search("Ghosts"), results)

    def test_search_key_is_case_and_whitespace_insensitive(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")
            results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)

            cache.set_search("  Ghosts ", results)

            self.assertEqual(cache.get_search("ghosts"), results)

    def test_search_returns_none_once_the_entry_is_older_than_the_ttl(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))
            results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)
            cache.set_search("Ghosts", results)

            stale_timestamp = (datetime.now(timezone.utc) - timedelta(days=8)).isoformat()
            document = json.loads(cache_path.read_text(encoding="utf-8"))
            document["searches"]["ghosts"]["fetched_at"] = stale_timestamp
            cache_path.write_text(json.dumps(document), encoding="utf-8")

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path, ttl=timedelta(days=7))

            self.assertIsNone(reloaded_cache.get_search("Ghosts"))

    def test_search_persists_across_separate_cache_instances(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            results = (
                _make_tvdb_search_result(
                    series_id="400267",
                    name="Ghosts",
                    year="2021",
                    overview="A US sitcom.",
                ),
            )
            tvdb.TvdbEpisodeCache(cache_path).set_search("Ghosts", results)

            second_cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertEqual(second_cache.get_search("Ghosts"), results)

    def test_search_cache_does_not_disturb_episode_cache(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)
            search_results = (_make_tvdb_search_result(series_id="400267", name="Ghosts"),)

            cache.set("400267", "official", episodes)
            cache.set_search("Ghosts", search_results)

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path)
            self.assertEqual(reloaded_cache.get("400267", "official"), episodes)
            self.assertEqual(reloaded_cache.get_search("Ghosts"), search_results)

    def test_returns_none_for_an_unknown_series_name(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache = tvdb.TvdbEpisodeCache(Path(temp_dir) / "cache.json")

            self.assertIsNone(cache.get_series_name("400267"))

    def test_series_name_persists_across_separate_cache_instances(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            tvdb.TvdbEpisodeCache(cache_path).set_series_name("400267", "Ghosts")

            second_cache = tvdb.TvdbEpisodeCache(cache_path)

            self.assertEqual(second_cache.get_series_name("400267"), "Ghosts")

    def test_series_name_survives_alongside_episode_and_search_entries(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("400267", "official", episodes)
            cache.set_series_name("400267", "Ghosts")

            reloaded_cache = tvdb.TvdbEpisodeCache(cache_path)
            self.assertEqual(reloaded_cache.get("400267", "official"), episodes)
            self.assertEqual(reloaded_cache.get_series_name("400267"), "Ghosts")

    def test_series_name_appears_in_the_raw_cache_document(self) -> None:
        with TemporaryDirectory() as temp_dir:
            cache_path = Path(temp_dir) / "cache.json"
            cache = tvdb.TvdbEpisodeCache(cache_path)
            episodes = (_make_tvdb_episode(season_number=1, episode_number=1, name="Pilot"),)

            cache.set("400267", "official", episodes)
            cache.set_series_name("400267", "Ghosts")

            document = json.loads(cache_path.read_text(encoding="utf-8"))
            self.assertEqual(document["series"]["400267"]["name"], "Ghosts")
            self.assertIn("official", document["series"]["400267"])
