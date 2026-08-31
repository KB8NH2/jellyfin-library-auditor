"""Tests for media.py's filename/title parsing and audit.py's title-mismatch checks built on it."""

from __future__ import annotations

from pathlib import Path
import unittest
from unittest.mock import patch

import audit
from audit_types import AuditCategory
from audit_types import AuditSeverity
import config
import media

from tests.helpers import _make_app_config
from tests.helpers import _make_item


class ExpectedEpisodeTitleFromFilenameTests(unittest.TestCase):
    def test_returns_title_following_season_episode_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_strips_release_tags_and_dot_separators(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking.Bad.S01E01.Ozymandias.1080p.WEB-DL.x264-GROUP.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_does_not_truncate_title_ending_in_bare_tag_word(self) -> None:
        item = _make_item(
            title="Spider in the Web",
            is_movie=False,
            is_episode=True,
            path=Path("Babylon 5 - S02E06 - Spider in the Web.mp4"),
            season_number=2,
            episode_number=6,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Spider in the Web")

    def test_strips_bare_tag_word_followed_by_further_release_info(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show.S01E01.Pilot.WEB.x264-GROUP.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Pilot")

    def test_handles_multi_episode_range_with_dash(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E02-E03 - Ozymandias.mkv"),
            season_number=1,
            episode_number=2,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_handles_multi_episode_range_without_dash(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E02E03 - Ozymandias.mkv"),
            season_number=1,
            episode_number=2,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias")

    def test_preserves_trailing_parenthesized_copy_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias (1).mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Ozymandias (1)")

    def test_preserves_leading_parenthesized_title_text(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Lost Girl - S01E12 - (Dis)Members Only.mkv"),
            season_number=1,
            episode_number=12,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "(Dis)Members Only")

    def test_does_not_truncate_bare_tag_word_followed_by_copy_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Lexx - S02E16 - The Web (1).mkv"),
            season_number=2,
            episode_number=16,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "The Web (1)")

    def test_does_not_truncate_bare_tag_word_buried_mid_title(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Curious George - S02E31-E32 - Curious George, Web Master + The Big Sleepy.mp4"),
            season_number=2,
            episode_number=31,
        )

        self.assertEqual(
            media.expected_episode_title_from_filename(item),
            "Curious George, Web Master + The Big Sleepy",
        )

    def test_strips_bare_release_group_name_after_source_and_codec_tags(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Highlander - S01E01 - The Gathering NTSC DVD x264 JCH.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "The Gathering")

    def test_does_not_strip_bare_trailing_word_after_only_one_tag(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Real Title x264 Weird.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(
            media.expected_episode_title_from_filename(item),
            "Real Title x264 Weird",
        )

    def test_strips_dot_split_audio_channel_tag_before_bare_group_name(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Reacher - S02E08 - Fly Boy 1080p REPACK BluRay DDP5.1.mkv"),
            season_number=2,
            episode_number=8,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Fly Boy")

    def test_strips_dot_split_channel_tag_sandwiched_before_a_hyphenated_group(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Reacher - S02E08 - Fly Boy 1080p REPACK BluRay DDP5.1.x264-NTb.mkv"),
            season_number=2,
            episode_number=8,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Fly Boy")

    def test_strips_single_tag_carrying_a_hyphenated_group_suffix(self) -> None:
        # Regression test for the "-GROUPNAME" suffix check silently
        # matching the wrong regex capture group after a later edit added
        # another parenthesized alternative earlier in the pattern.
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Real Title x264-NTb.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_title_from_filename(item), "Real Title")

    def test_strips_parenthesized_tag_group_and_trailing_bare_tag(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Ted Lasso - S02E01 (1080p AV1) - SDR.mkv"),
            season_number=2,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))

    def test_returns_none_when_filename_omits_episode_title(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))

    def test_returns_none_when_filename_has_no_season_episode_marker(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))

    def test_returns_none_for_non_episode_items(self) -> None:
        item = _make_item(
            title="Alien",
            path=Path("Alien - S01E01 - Something.mkv"),
        )

        self.assertIsNone(media.expected_episode_title_from_filename(item))


class ExpectedEpisodeNumbersFromFilenameTests(unittest.TestCase):
    def test_returns_single_number_for_ordinary_single_episode_file(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (1,))

    def test_returns_inclusive_range_for_dash_separated_marker(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5, 6, 7))

    def test_returns_inclusive_range_for_marker_without_dash(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05E07 - Combined.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5, 6, 7))

    def test_returns_full_range_when_episode_number_is_the_markers_last_number(self) -> None:
        """Regression test: a combined-episode file's Jellyfin item doesn't
        always carry the marker's *first* episode number - here the item's
        own IndexNumber is 38, but the filename's marker is "S01E37-E38".
        The range must still be found by searching for the marker
        generically and checking whether 38 is one of its numbers, not by
        assuming the item's own number is the marker's starting number.
        """
        item = _make_item(
            title="Top Cow + School of Otis",
            is_movie=False,
            is_episode=True,
            path=Path(
                "Back at the Barnyard - S01E37-E38 - Top Cow + School of Otis.mkv"
            ),
            season_number=1,
            episode_number=38,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (37, 38))

    def test_returns_full_range_when_episode_number_is_a_middle_number(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E06-E07 - Combined.mkv"),
            season_number=1,
            episode_number=6,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5, 6, 7))

    def test_returns_none_when_episode_number_is_not_part_of_any_marker(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            season_number=1,
            episode_number=99,
        )

        self.assertIsNone(media.expected_episode_numbers_from_filename(item))

    def test_does_not_treat_an_unrelated_decoy_number_as_part_of_the_range(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05 - Vol.02.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.expected_episode_numbers_from_filename(item), (5,))

    def test_returns_none_when_filename_has_no_season_episode_marker(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(media.expected_episode_numbers_from_filename(item))

    def test_returns_none_for_non_episode_items(self) -> None:
        item = _make_item(
            title="Alien",
            path=Path("Alien - S01E01 - Something.mkv"),
        )

        self.assertIsNone(media.expected_episode_numbers_from_filename(item))


class GetDisplayEpisodeNumberTests(unittest.TestCase):
    def test_returns_range_for_multi_episode_filename(self) -> None:
        item = _make_item(
            title="Combined",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E05-E07 - Combined.mkv"),
            season_number=1,
            episode_number=5,
        )

        self.assertEqual(media.get_display_episode_number(item), "5-7")

    def test_returns_bare_number_for_ordinary_single_episode_file(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.get_display_episode_number(item), "1")

    def test_returns_bare_number_when_filename_has_no_recognizable_marker(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - Ozymandias.mkv"),
            season_number=1,
            episode_number=1,
        )

        self.assertEqual(media.get_display_episode_number(item), "1")

    def test_returns_empty_string_when_no_episode_number(self) -> None:
        item = _make_item(
            title="No Numbers",
            is_movie=False,
            is_episode=True,
            series_name="Example Series",
        )

        self.assertEqual(media.get_display_episode_number(item), "")


class MismatchedEpisodeFilenameTitleTests(unittest.TestCase):
    def test_flags_episode_when_metadata_title_differs_from_filename(self) -> None:
        item = _make_item(
            title="Wrong Title",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        finding = audit.mismatched_episode_filename_title(item)

        self.assertIsNotNone(finding)
        self.assertEqual(finding.check_name, "mismatched_episode_filename_title")
        self.assertEqual(finding.category, AuditCategory.METADATA)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Ozymandias", finding.message)
        self.assertIn("Wrong Title", finding.message)

    def test_does_not_flag_matching_title_case_insensitively(self) -> None:
        item = _make_item(
            title="ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_matching_title_with_punctuation_differences(self) -> None:
        item = _make_item(
            title="Ozymandias!",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01 - Ozymandias.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_abbreviated_title_with_embedded_periods(self) -> None:
        item = _make_item(
            title="S.W.A.T.",
            is_movie=False,
            is_episode=True,
            path=Path("Show.S01E01.S.W.A.T.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_curly_versus_straight_apostrophe(self) -> None:
        item = _make_item(
            title="Passion’s Harvest and a Sheldocracy",
            is_movie=False,
            is_episode=True,
            path=Path("Young Sheldon - S06E03 - Passion's Harvest and a Sheldocracy.mkv"),
            series_name="Young Sheldon",
            season_number=6,
            episode_number=3,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_apostrophe_dropped_entirely_versus_kept(self) -> None:
        item = _make_item(
            title="Lover's Walk",
            is_movie=False,
            is_episode=True,
            path=Path("Buffy the Vampire Slayer - S03E08 - Lovers Walk.mkv"),
            series_name="Buffy the Vampire Slayer",
            season_number=3,
            episode_number=8,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_accented_versus_unaccented_letters(self) -> None:
        item = _make_item(
            title="Degüello",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Deguello.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_dotted_abbreviation_versus_undotted(self) -> None:
        item = _make_item(
            title="Nothing Good Happens After 2 A.M.",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E18 - Nothing Good Happens After 2 AM.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=18,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_hyphenated_compound_versus_joined(self) -> None:
        item = _make_item(
            title="The Autumn of Break-Ups",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S08E05 - The Autumn of Breakups.mkv"),
            series_name="Show",
            season_number=8,
            episode_number=5,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_split_versus_joined_compound_word(self) -> None:
        item = _make_item(
            title="Welcome to the Doll House",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S06E06 - Welcome to the Dollhouse.mkv"),
            series_name="Show",
            season_number=6,
            episode_number=6,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_roman_numeral_versus_arabic_numeral_in_parens(self) -> None:
        item = _make_item(
            title="Poltergeist (I)",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Poltergeist (1).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_no_disambiguator(self) -> None:
        item = _make_item(
            title="Poltergeist",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Poltergeist (1).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_part_word(self) -> None:
        item = _make_item(
            title="Poltergeist, Part One",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Poltergeist (1).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_part_digit(self) -> None:
        item = _make_item(
            title="Poltergeist Part 2",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E02 - Poltergeist (2).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=2,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_paren_number_versus_part_roman_numeral(self) -> None:
        item = _make_item(
            title="The Savage Time: Part II",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E25 - The Savage Time (2).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=25,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_duplicated_multi_part_title_joined_by_slash(self) -> None:
        item = _make_item(
            title=(
                "The More You Moe, The Moe You Know (1) / "
                "The More You Moe, The Moe You Know (2)"
            ),
            is_movie=False,
            is_episode=True,
            path=Path(
                "Adventure Time - S07E14-E15 - The More You Moe, The Moe You Know.mkv"
            ),
            series_name="Adventure Time",
            season_number=7,
            episode_number=14,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_comma_versus_slash_joined_titles(self) -> None:
        item = _make_item(
            title="Title A / Title B",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Title A, Title B.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_still_flags_genuinely_different_titles(self) -> None:
        item = _make_item(
            title="Completely Different Title",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Title A, Title B.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNotNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_hyphenated_versus_space_separated_title(self) -> None:
        item = _make_item(
            title="Spider-Man",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Spider Man.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_exclamation_point_versus_no_punctuation(self) -> None:
        item = _make_item(
            title="Wait, What!",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Wait What.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_pt_abbreviation_versus_paren_number(self) -> None:
        item = _make_item(
            title="Poltergeist Pt. 2",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E02 - Poltergeist (2).mkv"),
            series_name="Show",
            season_number=1,
            episode_number=2,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_leading_article_difference(self) -> None:
        item = _make_item(
            title="The Murdering Cowboy",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Murdering Cowboy.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_mid_title_article_difference(self) -> None:
        item = _make_item(
            title="A Trip to the Moon",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Trip to Moon.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_british_versus_american_spelling(self) -> None:
        item = _make_item(
            title="Encyclopaedia",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Encyclopedia.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_british_spelling_combined_with_leading_article(self) -> None:
        item = _make_item(
            title="The Colour of Money",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Color of Money.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_title_with_embedded_period_and_no_space(self) -> None:
        item = _make_item(
            title="Mr.Robot",
            is_movie=False,
            is_episode=True,
            path=Path("Show S01E01 Mr Robot.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_ampersand_versus_and(self) -> None:
        item = _make_item(
            title="Salt & Pepper",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Salt and Pepper.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_plus_versus_slash(self) -> None:
        item = _make_item(
            title="Trick / Treat",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Trick + Treat.mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_ellipsis_versus_literal_periods(self) -> None:
        item = _make_item(
            title="Once Upon a Time…",
            is_movie=False,
            is_episode=True,
            path=Path("Show - S01E01 - Once Upon a Time....mkv"),
            series_name="Show",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_when_filename_omits_episode_title(self) -> None:
        item = _make_item(
            title="Ozymandias",
            is_movie=False,
            is_episode=True,
            path=Path("Breaking Bad - S01E01.mkv"),
            series_name="Breaking Bad",
            season_number=1,
            episode_number=1,
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))

    def test_does_not_flag_movies(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Alien - S01E01 - Something.mkv"),
        )

        self.assertIsNone(audit.mismatched_episode_filename_title(item))


class ExpectedMovieTitleFromFilenameTests(unittest.TestCase):
    def test_returns_title_preceding_year_marker(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025).mp4"),
            year=2025,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "Installing The Dock")

    def test_ignores_edition_suffix_after_year(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025) - Timelapse.mp4"),
            year=2025,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "Installing The Dock")

    def test_strips_release_tags_and_dot_separators(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("The.Matrix.1999.1080p.WEB-DL.x264-GROUP.mkv"),
            year=1999,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "The Matrix")

    def test_prefers_parenthesized_year_when_title_contains_matching_number(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Fantasia 2000 (2000).mkv"),
            year=2000,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "Fantasia 2000")

    def test_preserves_leading_parenthesized_title_text(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("(500) Days of Summer (2009).mp4"),
            year=2009,
        )

        self.assertEqual(media.expected_movie_title_from_filename(item), "(500) Days of Summer")

    def test_returns_none_when_filename_has_no_year_marker(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock.mp4"),
            year=2025,
        )

        self.assertIsNone(media.expected_movie_title_from_filename(item))

    def test_returns_none_when_year_is_missing(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025).mp4"),
            year=None,
        )

        self.assertIsNone(media.expected_movie_title_from_filename(item))

    def test_returns_none_for_non_movie_items(self) -> None:
        item = _make_item(
            title="Pilot",
            is_movie=False,
            is_episode=True,
            path=Path("Show (2025).mkv"),
            year=2025,
        )

        self.assertIsNone(media.expected_movie_title_from_filename(item))


class MismatchedMovieFilenameTitleTests(unittest.TestCase):
    def test_flags_movie_when_metadata_title_differs_from_filename(self) -> None:
        item = _make_item(
            title="Wrong Title",
            path=Path("Installing The Dock (2025) - Timelapse.mp4"),
            year=2025,
        )

        finding = audit.mismatched_movie_filename_title(item)

        self.assertIsNotNone(finding)
        self.assertEqual(finding.check_name, "mismatched_movie_filename_title")
        self.assertEqual(finding.category, AuditCategory.METADATA)
        self.assertEqual(finding.severity, AuditSeverity.WARNING)
        self.assertIn("Installing The Dock", finding.message)
        self.assertIn("Wrong Title", finding.message)

    def test_does_not_flag_matching_title_case_insensitively(self) -> None:
        item = _make_item(
            title="installing the dock",
            path=Path("Installing The Dock (2025).mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_ampersand_versus_and(self) -> None:
        item = _make_item(
            title="Salt & Pepper",
            path=Path("Salt and Pepper (2025).mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_plus_versus_slash(self) -> None:
        item = _make_item(
            title="Trick / Treat",
            path=Path("Trick + Treat (2025).mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_when_filename_omits_year(self) -> None:
        item = _make_item(
            title="Installing The Dock",
            path=Path("Installing The Dock.mp4"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_episodes(self) -> None:
        item = _make_item(
            title="Wrong Title",
            is_movie=False,
            is_episode=True,
            path=Path("Show (2025).mkv"),
            year=2025,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))

    def test_does_not_flag_year_in_parens(self) -> None:
        item = _make_item(
            title="It (2016)",
            is_movie=True,
            is_episode=False,
            path=Path("It (2016).mkv"),
            year=2016,
        )

        self.assertIsNone(audit.mismatched_movie_filename_title(item))


class RelativeToMediaRootTests(unittest.TestCase):
    def test_strips_everything_through_the_media_segment(self) -> None:
        result = media.relative_to_media_root(
            Path("/mnt/left/media/TV Shows/Show/Season 01/Show.S01E09.mkv")
        )

        self.assertEqual(result, "TV Shows/Show/Season 01/Show.S01E09.mkv")

    def test_matches_the_media_segment_case_insensitively(self) -> None:
        result = media.relative_to_media_root(Path("D:/Media/Movies/Movie (2024)/Movie (2024).mkv"))

        self.assertEqual(result, "Movies/Movie (2024)/Movie (2024).mkv")

    def test_uses_the_last_media_segment_when_more_than_one_exists(self) -> None:
        result = media.relative_to_media_root(Path("/srv/media/archive/media/Movies/Movie.mkv"))

        self.assertEqual(result, "Movies/Movie.mkv")

    def test_returns_path_unchanged_when_no_media_segment_exists(self) -> None:
        result = media.relative_to_media_root(Path("/mnt/library/Movies/Movie.mkv"))

        self.assertEqual(result, str(Path("/mnt/library/Movies/Movie.mkv")))


class GetDisplayPathTests(unittest.TestCase):
    def test_trims_to_the_library_folder_automatically(self) -> None:
        item = _make_item(
            title="Movie",
            library="Movies",
            path=Path("/mnt/left/media/Movies/Movie (2024)/Movie (2024).mkv"),
        )

        self.assertEqual(media.get_display_path(item), "Movies/Movie (2024)/Movie (2024).mkv")

    def test_falls_back_to_configured_prefix_without_a_media_segment(self) -> None:
        item = _make_item(
            title="Movie",
            library="Movies",
            path=Path("/mnt/library/Movies/Movie.mkv"),
        )

        with patch("media.get_config", return_value=_make_app_config_with_prefix("/mnt/library")):
            display_path = media.get_display_path(item)

        self.assertEqual(display_path, str(Path("Movies/Movie.mkv")))


class GetDisplayBaseDirectoryAndFilenameTests(unittest.TestCase):
    """The Path column shown throughout the app's reports is split into
    Base Directory (the one directory right below the library) and Base
    Filename (everything after the last "/") - these mirror
    GetDisplayPathTests' own fixtures, since both are built from
    get_display_path()'s already-trimmed value.
    """

    def test_splits_a_movie_stored_one_folder_per_movie(self) -> None:
        item = _make_item(
            title="Movie",
            library="Movies",
            path=Path("/mnt/left/media/Movies/Movie (2024)/Movie (2024).mkv"),
        )

        self.assertEqual(media.get_display_base_directory(item), "Movie (2024)")
        self.assertEqual(media.get_display_base_filename(item), "Movie (2024).mkv")

    def test_splits_a_tv_episode_dropping_the_season_folder(self) -> None:
        """The season folder itself isn't the base directory - the audit
        already tracks season number in its own Season column, so the base
        directory is the series folder one level below that."""
        item = _make_item(
            title="Episode",
            library="TV Shows",
            is_movie=False,
            is_episode=True,
            path=Path("/mnt/left/media/TV Shows/Show/Season 01/Show.S01E09.mkv"),
        )

        self.assertEqual(media.get_display_base_directory(item), "Show")
        self.assertEqual(media.get_display_base_filename(item), "Show.S01E09.mkv")

    def test_base_directory_is_empty_when_file_sits_directly_in_the_library_folder(self) -> None:
        """A movie library with no per-movie folder has nothing "right
        below the library" to report."""
        item = _make_item(
            title="Movie",
            library="Movies",
            path=Path("/mnt/left/media/Movies/Movie.mkv"),
        )

        self.assertEqual(media.get_display_base_directory(item), "")
        self.assertEqual(media.get_display_base_filename(item), "Movie.mkv")

    def test_normalizes_backslashes_from_the_configured_prefix_fallback(self) -> None:
        """The configured-prefix fallback branch of get_display_path()
        returns OS-native separators (backslash on Windows), unlike the
        media-root-trimmed branch, which always uses "/" - both must split
        the same way."""
        item = _make_item(
            title="Movie",
            library="Movies",
            path=Path("/mnt/library/Movies/Movie (2024)/Movie (2024).mkv"),
        )

        with patch("media.get_config", return_value=_make_app_config_with_prefix("/mnt/library")):
            base_directory = media.get_display_base_directory(item)
            base_filename = media.get_display_base_filename(item)

        self.assertEqual(base_directory, "Movie (2024)")
        self.assertEqual(base_filename, "Movie (2024).mkv")


class MediaItemHashabilityTests(unittest.TestCase):
    """Regression tests for MediaItem's frozen=True hashability contract.

    MediaItem is @dataclass(frozen=True, slots=True), which promises a
    working __hash__ - but its image_tags field is a plain dict, and the
    dataclass-generated __hash__ would try to hash it along with every
    other field, raising TypeError. image_tags is marked field(hash=False)
    to exclude it from hashing while still fully participating in __eq__.
    """

    def test_media_item_is_hashable(self) -> None:
        item = _make_item(title="Movie", image_tags={"Primary": "tag"})

        # Must not raise TypeError: unhashable type: 'dict'.
        hash(item)

    def test_differing_image_tags_alone_still_compare_unequal(self) -> None:
        first = _make_item(title="Movie", item_id="same-id", image_tags={"Primary": "tag"})
        second = _make_item(title="Movie", item_id="same-id", image_tags={})

        self.assertNotEqual(first, second)
        self.assertEqual(hash(first), hash(second))


def _make_app_config_with_prefix(media_path_prefix: str):
    """Return an AppConfig with a configured REPORT_MEDIA_PATH_PREFIX, for the fallback test."""
    app_config = _make_app_config()
    return config.AppConfig(
        reporting=config.ReportingConfig(
            media_path_prefix=media_path_prefix,
            csv_output=app_config.reporting.csv_output,
            output=app_config.reporting.output,
            english_language_codes=app_config.reporting.english_language_codes,
        ),
        processing=app_config.processing,
        servers=app_config.servers,
        tvdb=app_config.tvdb,
    )
