"""Tests for jellyfin.py's JellyfinClient."""

from __future__ import annotations

import base64
from pathlib import Path
import unittest
from unittest.mock import MagicMock
from unittest.mock import patch

from config import ServerConfig
import jellyfin
from jellyfin import JellyfinClient
import requests


class JellyfinClientMetadataTests(unittest.TestCase):
    def _make_client(self) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(server)

    def test_get_item_resolves_admin_user_and_requests_user_scoped_item(self) -> None:
        client = self._make_client()
        users_response = MagicMock()
        users_response.content = b"[]"
        users_response.json.return_value = [
            {"Id": "regular-user", "Policy": {"IsAdministrator": False}},
            {"Id": "admin-user", "Policy": {"IsAdministrator": True}},
        ]
        item_response = MagicMock()
        item_response.content = b'{"Id": "abc"}'
        item_response.json.return_value = {"Id": "abc", "Name": "Title"}
        with patch.object(
            client._session, "request", side_effect=[users_response, item_response]
        ) as mock_request:
            result = client.get_item("abc")

        self.assertEqual(mock_request.call_count, 2)
        mock_request.assert_any_call(
            method="GET",
            url="http://s:8096/Users",
            params=None,
            json=None,
            timeout=client._timeout,
        )
        mock_request.assert_any_call(
            method="GET",
            url="http://s:8096/Users/admin-user/Items/abc",
            params={"Fields": jellyfin.ITEM_DETAIL_FIELDS},
            json=None,
            timeout=client._timeout,
        )
        self.assertEqual(result, {"Id": "abc", "Name": "Title"})

    def test_get_item_caches_resolved_user_across_calls(self) -> None:
        client = self._make_client()
        users_response = MagicMock()
        users_response.content = b"[]"
        users_response.json.return_value = [{"Id": "only-user", "Policy": {}}]
        item_response = MagicMock()
        item_response.content = b"{}"
        item_response.json.return_value = {"Id": "abc"}
        with patch.object(
            client._session,
            "request",
            side_effect=[users_response, item_response, item_response],
        ) as mock_request:
            client.get_item("abc")
            client.get_item("def")

        self.assertEqual(mock_request.call_count, 3)

    def test_update_item_posts_full_item_document(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        with patch.object(client._session, "request", return_value=response) as mock_request:
            client.update_item("abc", {"Id": "abc", "Name": "New Title"})

        mock_request.assert_called_once_with(
            method="POST",
            url="http://s:8096/Items/abc",
            params=None,
            json={"Id": "abc", "Name": "New Title"},
            timeout=client._timeout,
        )

    def test_update_item_tolerates_empty_response_body(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        response.json.side_effect = ValueError("no body")
        with patch.object(client._session, "request", return_value=response):
            client.update_item("abc", {"Id": "abc"})

    def test_update_item_surfaces_error_response_body(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 500
        response.content = b'{"error": "NullReferenceException in UpdateItemHelper"}'
        response.text = '{"error": "NullReferenceException in UpdateItemHelper"}'
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(jellyfin.JellyfinRequestError) as context:
                client.update_item("abc", {"Id": "abc"})

        self.assertIn("status 500", str(context.exception))
        self.assertIn("NullReferenceException", str(context.exception))

    def test_get_item_image_returns_bytes_and_content_type(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 200
        response.content = b"\xff\xd8\xff\xe0fakejpeg"
        response.headers = {"Content-Type": "image/jpeg; charset=binary"}
        with patch.object(client._session, "request", return_value=response) as mock_request:
            result = client.get_item_image("abc", "Primary")

        mock_request.assert_called_once_with(
            method="GET",
            url="http://s:8096/Items/abc/Images/Primary",
            headers={"Accept": "*/*"},
            timeout=client._timeout,
        )
        self.assertEqual(result, (b"\xff\xd8\xff\xe0fakejpeg", "image/jpeg"))

    def test_get_item_image_returns_none_on_404(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 404
        with patch.object(client._session, "request", return_value=response):
            result = client.get_item_image("abc", "Backdrop")

        self.assertIsNone(result)

    def test_get_item_image_surfaces_other_errors(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 500
        response.content = b"boom"
        response.text = "boom"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client.get_item_image("abc", "Primary")

    def test_upload_item_image_posts_base64_body_with_content_type(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        with patch.object(client._session, "request", return_value=response) as mock_request:
            client.upload_item_image("abc", "Primary", b"rawbytes", "image/png")

        mock_request.assert_called_once_with(
            method="POST",
            url="http://s:8096/Items/abc/Images/Primary",
            data=base64.b64encode(b"rawbytes"),
            headers={"Content-Type": "image/png"},
            timeout=client._timeout,
        )

    def test_upload_item_image_surfaces_error(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 500
        response.content = b"boom"
        response.text = "boom"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(client._session, "request", return_value=response):
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client.upload_item_image("abc", "Primary", b"rawbytes", "image/png")


class StaleTitleResolutionTests(unittest.TestCase):
    def _make_client(self) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(server)

    @staticmethod
    def _library_listing_response(item: dict) -> MagicMock:
        response = MagicMock()
        response.content = b'{"Items": []}'
        response.json.return_value = {"Items": [item], "TotalRecordCount": 1}
        return response

    @staticmethod
    def _users_response() -> MagicMock:
        response = MagicMock()
        response.content = b"[]"
        response.json.return_value = [{"Id": "admin-user", "Policy": {"IsAdministrator": True}}]
        return response

    @staticmethod
    def _item_detail_response(name: str) -> MagicMock:
        response = MagicMock()
        response.content = b"{}"
        response.json.return_value = {"Id": "item1", "Name": name}
        return response

    def test_locked_name_item_is_refreshed_from_a_per_item_lookup(self) -> None:
        """Regression test: the /Items listing can still show an item's
        pre-rename Name for a while after apply_titles_from_filename.py (or
        apply_episode_titles.py/apply_dvd_metadata.py) locks and changes it,
        even though Jellyfin's own UI already shows the new title. A locked
        Name is exactly the signal that the extra per-item request is
        worth it - unlocked items are never re-checked.
        """
        client = self._make_client()
        library = jellyfin.MediaLibrary(id="lib1", name="Movies", collection_type="movies", locations=())
        raw_item = {
            "Id": "item1",
            "Type": "Movie",
            "Name": "Stale Title",
            "Path": "/media/movies/Movie Name (2001).mkv",
            "LockedFields": ["Name"],
        }

        with patch.object(
            client._session,
            "request",
            side_effect=[
                self._library_listing_response(raw_item),
                self._users_response(),
                self._item_detail_response("Movie Name"),
            ],
        ) as mock_request:
            items = client._get_library_items_for_library(library)

        self.assertEqual(mock_request.call_count, 3)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].title, "Movie Name")

    def test_unlocked_name_item_is_not_refreshed(self) -> None:
        client = self._make_client()
        library = jellyfin.MediaLibrary(id="lib1", name="Movies", collection_type="movies", locations=())
        raw_item = {
            "Id": "item1",
            "Type": "Movie",
            "Name": "Movie Name",
            "Path": "/media/movies/Movie Name (2001).mkv",
        }

        with patch.object(
            client._session,
            "request",
            side_effect=[self._library_listing_response(raw_item)],
        ) as mock_request:
            items = client._get_library_items_for_library(library)

        mock_request.assert_called_once()
        self.assertEqual(items[0].title, "Movie Name")

    def test_locked_name_item_already_matching_is_not_rewritten(self) -> None:
        """A locked item still costs one extra per-item request (there's no
        way to know it's already fresh without checking), but must not be
        reported as changed when the per-item lookup agrees with the
        listing."""
        client = self._make_client()
        library = jellyfin.MediaLibrary(id="lib1", name="Movies", collection_type="movies", locations=())
        raw_item = {
            "Id": "item1",
            "Type": "Movie",
            "Name": "Movie Name",
            "Path": "/media/movies/Movie Name (2001).mkv",
            "LockedFields": ["Name"],
        }

        with patch.object(
            client._session,
            "request",
            side_effect=[
                self._library_listing_response(raw_item),
                self._users_response(),
                self._item_detail_response("Movie Name"),
            ],
        ):
            items = client._get_library_items_for_library(library)

        self.assertEqual(items[0].title, "Movie Name")


class JellyfinClientRetryTests(unittest.TestCase):
    def _make_client(self, *, max_retries: int = 3) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(
            server, max_retries=max_retries, retry_backoff_seconds=0.0
        )

    def test_recovers_after_a_read_timeout(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b'{"Items": []}'
        response.json.return_value = {"Items": []}
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.exceptions.ReadTimeout("timed out"), response],
        ) as mock_request, patch.object(jellyfin.time, "sleep") as mock_sleep:
            result = client._request(jellyfin.ITEMS_ENDPOINT)

        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once()
        self.assertEqual(result, {"Items": []})

    def test_recovers_after_a_connection_error(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b""
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.exceptions.ConnectionError("refused"), response],
        ) as mock_request, patch.object(jellyfin.time, "sleep"):
            client.update_item("abc", {"Id": "abc"})

        self.assertEqual(mock_request.call_count, 2)

    def test_raises_after_exhausting_all_retry_attempts(self) -> None:
        client = self._make_client(max_retries=2)
        with patch.object(
            client._session,
            "request",
            side_effect=requests.exceptions.ReadTimeout("timed out"),
        ) as mock_request, patch.object(jellyfin.time, "sleep") as mock_sleep:
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client._request(jellyfin.ITEMS_ENDPOINT)

        self.assertEqual(mock_request.call_count, 2)
        mock_sleep.assert_called_once()

    def test_does_not_retry_on_non_transient_errors(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.status_code = 404
        response.content = b"not found"
        response.text = "not found"
        response.raise_for_status.side_effect = requests.HTTPError(response=response)
        with patch.object(
            client._session, "request", return_value=response
        ) as mock_request, patch.object(jellyfin.time, "sleep") as mock_sleep:
            with self.assertRaises(jellyfin.JellyfinRequestError):
                client._request(jellyfin.ITEMS_ENDPOINT)

        mock_request.assert_called_once()
        mock_sleep.assert_not_called()

    def test_request_count_starts_at_zero(self) -> None:
        client = self._make_client()

        self.assertEqual(client.request_count, 0)

    def test_request_count_increments_once_per_successful_request(self) -> None:
        client = self._make_client()
        response = MagicMock()
        response.content = b'{"Items": []}'
        response.json.return_value = {"Items": []}
        with patch.object(client._session, "request", return_value=response):
            client._request(jellyfin.ITEMS_ENDPOINT)
            client._request(jellyfin.ITEMS_ENDPOINT)

        self.assertEqual(client.request_count, 2)

    def test_request_count_includes_a_retried_attempt(self) -> None:
        """Regression test: a retried attempt is a real network round trip
        too, not just the logical operation it was part of - it must be
        counted the same as one that succeeded on the first try."""
        client = self._make_client()
        response = MagicMock()
        response.content = b'{"Items": []}'
        response.json.return_value = {"Items": []}
        with patch.object(
            client._session,
            "request",
            side_effect=[requests.exceptions.ReadTimeout("timed out"), response],
        ), patch.object(jellyfin.time, "sleep"):
            client._request(jellyfin.ITEMS_ENDPOINT)

        self.assertEqual(client.request_count, 2)


def _library_response(*, tv1_id: str = "tv1", tv2_id: str = "tv2", movies_id: str = "mov1") -> MagicMock:
    response = MagicMock()
    response.content = b'{"Items": []}'
    response.json.return_value = {
        "Items": [
            {"Id": tv1_id, "Name": "TV Shows", "CollectionType": "tvshows"},
            {"Id": tv2_id, "Name": "Anime", "CollectionType": "tvshows"},
            {"Id": movies_id, "Name": "Movies", "CollectionType": "movies"},
        ]
    }
    return response


def _items_response(items: list[dict]) -> MagicMock:
    response = MagicMock()
    response.content = b'{"Items": []}'
    response.json.return_value = {"Items": items, "TotalRecordCount": len(items)}
    return response


class JellyfinClientSeriesLookupTests(unittest.TestCase):
    def _make_client(self) -> JellyfinClient:
        server = ServerConfig(key="s", name="S", url="http://s:8096", api_key="token")
        return JellyfinClient(server)

    def test_find_series_matches_case_insensitively(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response(
                    [{"Id": "s1", "Name": "Breaking Bad", "ProviderIds": {"Tvdb": "81189"}}]
                ),
                _items_response([{"Id": "s2", "Name": "Cowboy Bebop", "ProviderIds": {}}]),
            ],
        ):
            matches = client.find_series("breaking bad")

        self.assertEqual(
            matches,
            (jellyfin.SeriesMatch(library_name="TV Shows", series_id="s1", tvdb_id="81189"),),
        )

    def test_find_series_returns_every_ambiguous_match(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "s1", "Name": "The Office", "ProviderIds": {}}]),
                _items_response([{"Id": "s2", "Name": "The Office", "ProviderIds": {}}]),
            ],
        ):
            matches = client.find_series("The Office")

        self.assertEqual(len(matches), 2)
        self.assertEqual(
            {match.library_name for match in matches}, {"TV Shows", "Anime"}
        )

    def test_get_series_tvdb_ids_groups_multiple_ids_under_one_name(self) -> None:
        """Regression test: two Series items can share a display name (e.g.
        TheTVDB splitting a show into a new entry for a later era) - both
        their TheTVDB ids must be kept, not just whichever is seen last."""
        client = self._make_client()
        raw_series = [
            {"Id": "s1", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "78804"}},
            {"Id": "s2", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "449991"}},
            {"Id": "s3", "Name": "Firefly", "ProviderIds": {"Tvdb": "78874"}},
        ]
        with patch.object(client._session, "request", return_value=_items_response(raw_series)):
            series_tvdb_ids = client.get_series_tvdb_ids("tv1")

        self.assertEqual(
            series_tvdb_ids,
            {"Doctor Who": ("78804", "449991"), "Firefly": ("78874",)},
        )

    def test_get_series_tvdb_ids_deduplicates_a_repeated_id_for_one_name(self) -> None:
        client = self._make_client()
        raw_series = [
            {"Id": "s1", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "78804"}},
            {"Id": "s2", "Name": "Doctor Who", "ProviderIds": {"Tvdb": "78804"}},
        ]
        with patch.object(client._session, "request", return_value=_items_response(raw_series)):
            series_tvdb_ids = client.get_series_tvdb_ids("tv1")

        self.assertEqual(series_tvdb_ids, {"Doctor Who": ("78804",)})

    def test_get_series_tvdb_ids_omits_series_without_a_tvdb_id(self) -> None:
        client = self._make_client()
        raw_series = [{"Id": "s1", "Name": "Cowboy Bebop", "ProviderIds": {}}]
        with patch.object(client._session, "request", return_value=_items_response(raw_series)):
            series_tvdb_ids = client.get_series_tvdb_ids("tv1")

        self.assertEqual(series_tvdb_ids, {})

    def test_find_series_with_library_name_only_queries_that_library(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "s2", "Name": "Cowboy Bebop", "ProviderIds": {}}]),
            ],
        ) as mock_request:
            matches = client.find_series("Cowboy Bebop", library_name="anime")

        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(
            matches,
            (jellyfin.SeriesMatch(library_name="Anime", series_id="s2", tvdb_id=None),),
        )

    def test_find_series_with_path_filter_only_keeps_matching_paths(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response(
                    [
                        {
                            "Id": "s1",
                            "Name": "The Office",
                            "ProviderIds": {},
                            "Path": "/media/TV Shows/The Office (US)",
                        }
                    ]
                ),
                _items_response(
                    [
                        {
                            "Id": "s2",
                            "Name": "The Office",
                            "ProviderIds": {},
                            "Path": "/media/Anime/The Office (UK)",
                        }
                    ]
                ),
            ],
        ):
            matches = client.find_series("The Office", path_filter="(uk)")

        self.assertEqual(
            matches,
            (
                jellyfin.SeriesMatch(
                    library_name="Anime",
                    series_id="s2",
                    tvdb_id=None,
                    path=Path("/media/Anime/The Office (UK)"),
                ),
            ),
        )

    def test_find_series_with_path_filter_excludes_a_series_with_no_path(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "s1", "Name": "The Office", "ProviderIds": {}}]),
                _items_response([]),
            ],
        ):
            matches = client.find_series("The Office", path_filter="us")

        self.assertEqual(matches, ())

    def test_find_movie_matches_case_insensitively_and_returns_path_and_year(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response(
                    [
                        {
                            "Id": "m1",
                            "Name": "Movie Name",
                            "Path": "/media/movies/Movie Name (2001).mkv",
                            "ProductionYear": 2001,
                        }
                    ]
                ),
            ],
        ):
            matches = client.find_movie("movie name")

        self.assertEqual(
            matches,
            (
                jellyfin.MovieMatch(
                    library_name="Movies",
                    movie_id="m1",
                    name="Movie Name",
                    path=Path("/media/movies/Movie Name (2001).mkv"),
                    year=2001,
                ),
            ),
        )

    def test_find_movie_only_queries_movie_libraries(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[_library_response(), _items_response([])],
        ) as mock_request:
            matches = client.find_movie("Nonexistent")

        # One request for libraries, one for the single movie library - the
        # two TV libraries must never be queried for a movie lookup.
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(matches, ())

    def test_find_movie_with_library_name_only_queries_that_library(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "m1", "Name": "Movie Name"}]),
            ],
        ) as mock_request:
            matches = client.find_movie("Movie Name", library_name="movies")

        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(
            matches,
            (
                jellyfin.MovieMatch(
                    library_name="Movies", movie_id="m1", name="Movie Name", path=None, year=None
                ),
            ),
        )

    def test_find_movie_with_path_filter_only_keeps_matching_paths(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response(
                    [
                        {
                            "Id": "m1",
                            "Name": "Dup",
                            "Path": "/media/Movies/Kids/Dup (2001).mkv",
                            "ProductionYear": 2001,
                        },
                        {
                            "Id": "m2",
                            "Name": "Dup",
                            "Path": "/media/Movies/Horror/Dup (1988).mkv",
                            "ProductionYear": 1988,
                        },
                    ]
                ),
            ],
        ):
            matches = client.find_movie("Dup", path_filter="horror")

        self.assertEqual(
            matches,
            (
                jellyfin.MovieMatch(
                    library_name="Movies",
                    movie_id="m2",
                    name="Dup",
                    path=Path("/media/Movies/Horror/Dup (1988).mkv"),
                    year=1988,
                ),
            ),
        )

    def test_find_movie_with_path_filter_excludes_a_movie_with_no_path(self) -> None:
        client = self._make_client()
        with patch.object(
            client._session,
            "request",
            side_effect=[
                _library_response(),
                _items_response([{"Id": "m1", "Name": "Dup", "ProductionYear": 2001}]),
            ],
        ):
            matches = client.find_movie("Dup", path_filter="anything")

        self.assertEqual(matches, ())

    def test_get_series_season_episodes_filters_and_sorts(self) -> None:
        client = self._make_client()
        raw_episodes = [
            {"Id": "e3", "Name": "Third", "ParentIndexNumber": 1, "IndexNumber": 3},
            {"Id": "e1", "Name": "First", "ParentIndexNumber": 1, "IndexNumber": 1},
            {"Id": "e-other-season", "Name": "Other Season", "ParentIndexNumber": 2, "IndexNumber": 1},
            {"Id": "e-no-number", "Name": "No Number", "ParentIndexNumber": 1, "IndexNumber": None},
        ]
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            episodes = client.get_series_season_episodes("series-id", 1)

        self.assertEqual(
            episodes,
            (
                jellyfin.EpisodeSummary(id="e1", name="First", episode_number=1, path=None),
                jellyfin.EpisodeSummary(id="e3", name="Third", episode_number=3, path=None),
            ),
        )

    def test_get_series_season_episodes_falls_back_to_season_name(self) -> None:
        """Regression test: Jellyfin sometimes leaves ParentIndexNumber unset
        on an episode it couldn't confidently number, even though the
        containing Season item still has a plain "Season N" name - without
        falling back to that name, such an episode silently never matches
        any season lookup."""
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e1",
                "Name": "First",
                "ParentIndexNumber": None,
                "SeasonName": "Season 7",
                "IndexNumber": 1,
            },
            {
                "Id": "e-other-season",
                "Name": "Other Season",
                "ParentIndexNumber": None,
                "SeasonName": "Season 8",
                "IndexNumber": 1,
            },
        ]
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            episodes = client.get_series_season_episodes("series-id", 7)

        self.assertEqual(
            episodes,
            (jellyfin.EpisodeSummary(id="e1", name="First", episode_number=1, path=None),),
        )

    def test_get_series_season_episodes_all_keeps_unnumbered_sorted_last_by_path(self) -> None:
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e3",
                "Name": "Third",
                "ParentIndexNumber": 1,
                "IndexNumber": 3,
                "Path": "/media/show/S01E03.mkv",
            },
            {
                "Id": "e-other-season",
                "Name": "Other Season",
                "ParentIndexNumber": 2,
                "IndexNumber": 1,
                "Path": "/media/show/S02E01.mkv",
            },
            {
                "Id": "e-b",
                "Name": "No Number B",
                "ParentIndexNumber": 1,
                "IndexNumber": None,
                "Path": "/media/show/b-unnumbered.mkv",
            },
            {
                "Id": "e-a",
                "Name": "No Number A",
                "ParentIndexNumber": 1,
                "IndexNumber": None,
                "Path": "/media/show/a-unnumbered.mkv",
            },
        ]
        index_numbers_by_id = {item["Id"]: item["IndexNumber"] for item in raw_episodes}
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            with patch.object(
                client,
                "_lookup_index_number",
                side_effect=lambda item_id: index_numbers_by_id[item_id],
            ):
                episodes = client.get_series_season_episodes_all("series-id", 1)

        self.assertEqual(
            episodes,
            (
                jellyfin.SeasonEpisodeSummary(
                    id="e3", name="Third", path=Path("/media/show/S01E03.mkv"), episode_number=3
                ),
                jellyfin.SeasonEpisodeSummary(
                    id="e-a",
                    name="No Number A",
                    path=Path("/media/show/a-unnumbered.mkv"),
                    episode_number=None,
                ),
                jellyfin.SeasonEpisodeSummary(
                    id="e-b",
                    name="No Number B",
                    path=Path("/media/show/b-unnumbered.mkv"),
                    episode_number=None,
                ),
            ),
        )

    def test_get_series_season_episodes_all_falls_back_to_season_name(self) -> None:
        """Regression test: this is exactly the case
        apply_episode_numbers.py needs - an unnumbered episode whose season
        is only known by its "Season N" name must still be found under that
        season, not silently dropped."""
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e-unnumbered",
                "Name": "Bad Company",
                "ParentIndexNumber": None,
                "SeasonName": "Season 7",
                "IndexNumber": None,
                "Path": "/media/show/Bad Company.mkv",
            },
            {
                "Id": "e-other-season",
                "Name": "Other Season",
                "ParentIndexNumber": None,
                "SeasonName": "Season 8",
                "IndexNumber": None,
                "Path": "/media/show/other.mkv",
            },
        ]
        index_numbers_by_id = {item["Id"]: item["IndexNumber"] for item in raw_episodes}
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            with patch.object(
                client,
                "_lookup_index_number",
                side_effect=lambda item_id: index_numbers_by_id[item_id],
            ):
                episodes = client.get_series_season_episodes_all("series-id", 7)

        self.assertEqual(
            episodes,
            (
                jellyfin.SeasonEpisodeSummary(
                    id="e-unnumbered",
                    name="Bad Company",
                    path=Path("/media/show/Bad Company.mkv"),
                    episode_number=None,
                ),
            ),
        )

    def test_get_series_season_episodes_all_uses_fresh_per_item_index_number(self) -> None:
        """Regression test: the /Items listing can lag behind a field edit
        made outside a library scan (a direct API PATCH, or a manual edit in
        the Jellyfin UI) in either direction - it can still show a number
        that was since cleared, or omit one that was just set. The per-item
        lookup result must win over whatever the listing says."""
        client = self._make_client()
        raw_episodes = [
            {
                "Id": "e-cleared",
                "Name": "Cleared",
                "ParentIndexNumber": 1,
                "IndexNumber": 5,
                "Path": "/media/show/S01E05.mkv",
            },
            {
                "Id": "e-set",
                "Name": "Set",
                "ParentIndexNumber": 1,
                "IndexNumber": None,
                "Path": "/media/show/S01E06.mkv",
            },
        ]
        fresh_index_numbers_by_id = {"e-cleared": None, "e-set": 6}
        with patch.object(
            client._session, "request", return_value=_items_response(raw_episodes)
        ):
            with patch.object(
                client,
                "_lookup_index_number",
                side_effect=lambda item_id: fresh_index_numbers_by_id[item_id],
            ):
                episodes = client.get_series_season_episodes_all("series-id", 1)

        self.assertEqual(
            episodes,
            (
                jellyfin.SeasonEpisodeSummary(
                    id="e-set", name="Set", path=Path("/media/show/S01E06.mkv"), episode_number=6
                ),
                jellyfin.SeasonEpisodeSummary(
                    id="e-cleared",
                    name="Cleared",
                    path=Path("/media/show/S01E05.mkv"),
                    episode_number=None,
                ),
            ),
        )
