"""Shared TheTVDB series-id resolution for the apply_*.py tools.

apply_tvdb_metadata.py and apply_episode_numbers.py both look up a series by
name in Jellyfin, then act using whatever TheTVDB id
Jellyfin has assigned to it - but that assigned id can itself be wrong,
since TheTVDB sometimes has more than one series entry sharing the exact
same name (e.g. a decades-old show and a from-scratch modern revival, each
independently numbering their own "Season 1"). Jellyfin's automatic
matching has no way to know which one actually explains a given local
library, and a wrong match here wouldn't just go uncorrected - it would
actively corrupt data by acting on some *other* show's episode list
(overwriting episode metadata, renaming titles to the wrong show's, or
assigning episode numbers that belong to a different show entirely).
"""

from __future__ import annotations

import logging
from collections.abc import Mapping

from jellyfin import JellyfinClient
from tvdb import TvdbClient
from tvdb import TvdbEpisode
from tvdb import TvdbError


# Bounds worst-case TheTVDB calls for a generically-named series without
# likely missing the real match - TheTVDB ranks search results
# most-relevant first.
MAX_TVDB_SEARCH_CANDIDATES = 5


def unmatched_position_count(
    local_positions: frozenset[tuple[int, int]],
    candidate_positions: Mapping[tuple[int, int], TvdbEpisode],
) -> int:
    """Return how many local positions a candidate's episode list doesn't cover."""
    return sum(1 for position in local_positions if position not in candidate_positions)


def resolve_series_tvdb_id(
    client: JellyfinClient,
    tvdb_client: TvdbClient,
    series_name: str,
    series_id: str,
    assigned_tvdb_id: str | None,
    *,
    logger: logging.Logger,
) -> str | None:
    """Return the TheTVDB series id that best explains this series' local episodes.

    This searches TheTVDB by name for up to ``MAX_TVDB_SEARCH_CANDIDATES``
    same-named candidates, adds the assigned id itself if it isn't already
    among them, fetches each candidate's aired-order episode list, and picks
    whichever one's positions best overlap this series' full local
    (season, episode) set - across every season, not just the one the
    caller is currently acting on, since a wrong id can still coincidentally
    explain a single season while failing everywhere else. Aired order is
    used for this comparison regardless of which ordering the caller
    ultimately wants data from, since it's the ordering most likely to be
    fully populated for the genuinely correct series.

    Args:
        client: Client for the server the series lives on.
        tvdb_client: TheTVDB client to search and fetch candidate episode
            lists with.
        series_name: Series display name, used for the TheTVDB search.
        series_id: Jellyfin Series item identifier, to read local episode
            positions from.
        assigned_tvdb_id: The TheTVDB id Jellyfin currently has assigned to
            this series, if any - always considered as a candidate even
            when TheTVDB's search doesn't itself surface it.
        logger: Logger to report a skipped TheTVDB lookup to, so the
            warning is attributed to the calling tool (e.g.
            "apply_tvdb_metadata") rather than this shared module.

    Returns:
        The best-fitting TheTVDB id, or ``assigned_tvdb_id`` unchanged when
        there's nothing to compare against (no local episodes at all, or
        the search fails) or no other candidate beats it. ``None`` only
        when there's no assigned id and no candidate was found at all.
    """
    local_positions = client.get_series_episode_positions(series_id)
    if not local_positions:
        return assigned_tvdb_id

    candidate_ids: list[str] = [assigned_tvdb_id] if assigned_tvdb_id is not None else []

    try:
        search_results = tvdb_client.search_series(series_name)
    except TvdbError as error:
        logger.warning("Skipping TheTVDB series search for %r: %s", series_name, error)
        search_results = ()

    considered = 0
    for result in search_results:
        if result.id in candidate_ids:
            continue
        if considered >= MAX_TVDB_SEARCH_CANDIDATES:
            break
        considered += 1
        candidate_ids.append(result.id)

    if not candidate_ids:
        return None
    if len(candidate_ids) == 1:
        return candidate_ids[0]

    best_id = candidate_ids[0]
    best_unmatched = None
    for candidate_id in candidate_ids:
        try:
            episodes = tvdb_client.get_series_episodes(
                candidate_id, "official", series_name=series_name
            )
        except TvdbError as error:
            logger.warning(
                "Skipping TheTVDB candidate %s for %r: %s", candidate_id, series_name, error
            )
            continue
        candidate_positions = {
            (episode.season_number, episode.episode_number): episode for episode in episodes
        }
        unmatched = unmatched_position_count(local_positions, candidate_positions)
        if best_unmatched is None or unmatched < best_unmatched:
            best_unmatched = unmatched
            best_id = candidate_id

    return best_id


__all__ = [
    "MAX_TVDB_SEARCH_CANDIDATES",
    "resolve_series_tvdb_id",
    "unmatched_position_count",
]
