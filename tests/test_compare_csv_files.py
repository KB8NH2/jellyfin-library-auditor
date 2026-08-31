"""Tests for compare_csv_files.py."""

from __future__ import annotations

import csv
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

import compare_csv_files


class CompareCsvFilesExcelGuardTests(unittest.TestCase):
    """reports/generator.py guards a hyphenated Episode range (e.g. "5-7")

    against Excel's automatic type detection misreading it as a date, by
    prefixing it with a leading apostrophe. compare_csv_files.py reads
    those same audit CSVs back in, so it must strip that guard off before
    using the value for anything, and re-apply it to whatever ends up in
    its own diff output.
    """

    def test_read_rows_strips_excel_guard_from_episode_column(self) -> None:
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "audit.csv"
            csv_path.write_text(
                "Library,Base Filename,Episode\nTV Shows,show.mkv,'5-7\n",
                encoding="utf-8",
            )

            header, rows = compare_csv_files.read_rows(csv_path)

        self.assertEqual(header, ("Library", "Base Filename", "Episode"))
        self.assertEqual(rows, (("TV Shows", "show.mkv", "5-7"),))

    def test_read_rows_leaves_plain_episode_numbers_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "audit.csv"
            csv_path.write_text(
                "Library,Base Filename,Episode\nTV Shows,show.mkv,5\n",
                encoding="utf-8",
            )

            _, rows = compare_csv_files.read_rows(csv_path)

        self.assertEqual(rows, (("TV Shows", "show.mkv", "5"),))

    def test_combine_row_reapplies_excel_guard_for_matching_episode_range(self) -> None:
        header = ("Library", "Base Filename", "Episode")
        row_a = ("TV Shows", "show.mkv", "5-7")
        row_b = ("TV Shows", "show.mkv", "5-7")

        combined = compare_csv_files.combine_row(header, row_a, row_b)

        self.assertEqual(combined, ("TV Shows", "show.mkv", "'5-7"))

    def test_write_diff_csv_round_trips_episode_range_through_the_excel_guard(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            csv_a = temp_dir_path / "left_audit.csv"
            csv_b = temp_dir_path / "right_audit.csv"
            output_path = temp_dir_path / "diffs.csv"

            csv_a.write_text(
                "Library,Base Filename,Episode,Missing Subtitles\n"
                "TV Shows,show.mkv,'5-7,Yes\n",
                encoding="utf-8",
            )
            csv_b.write_text(
                "Library,Base Filename,Episode,Missing Subtitles\n"
                "TV Shows,show.mkv,'5-7,No\n",
                encoding="utf-8",
            )

            diff_count = compare_csv_files.write_diff_csv(csv_a, csv_b, output_path)
            with output_path.open("r", encoding="utf-8", newline="") as handle:
                # write_diff_csv's own output has a leading "left|right"
                # identifier line before the real header, unlike an audit
                # CSV - read it with a plain csv.reader rather than
                # read_rows(), which assumes the first line is the header.
                output_rows = [tuple(row) for row in csv.reader(handle)]

        self.assertEqual(diff_count, 1)
        header = output_rows[1]
        data_row = output_rows[2]
        episode_index = header.index("Episode")
        # The Episode value still carries its Excel guard in the raw
        # output file, same as a real audit CSV would.
        self.assertEqual(data_row[episode_index], "'5-7")


class CompareCsvFilesBaseDirectoryAndFilenameTests(unittest.TestCase):
    """Regression tests for matching/diffing on the split Base Directory/
    Base Filename columns that replaced the old single Path column - two
    servers never share a mount point or drive letter, but the base
    filename an item is stored under should still line up between them.
    """

    def test_matches_rows_by_base_filename_despite_different_directories(self) -> None:
        header_a = ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        rows_a = (("TV Shows", "Show Name", "Show.S01E01.mkv", "Yes"),)
        header_b = ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        rows_b = (("TV Shows", "Show Name (2008)", "Show.S01E01.mkv", "No"),)

        diff_header, diff_rows = compare_csv_files.diff_header_and_rows(
            header_a, rows_a, header_b, rows_b
        )

        self.assertEqual(len(diff_rows), 1)
        self.assertEqual(diff_rows[0][diff_header.index("Base Filename")], "Show.S01E01.mkv")

    def test_differing_base_directory_shows_as_left_right(self) -> None:
        """A genuine folder-organization mismatch between the two servers
        is worth surfacing, unlike the old Path column (which always
        differed trivially due to unrelated mount-point differences and so
        was excluded from the diff entirely)."""
        header_a = ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        rows_a = (("TV Shows", "Show Name", "Show.S01E01.mkv", "Yes"),)
        header_b = ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        rows_b = (("TV Shows", "Show Name (2008)", "Show.S01E01.mkv", "Yes"),)

        _, diff_rows = compare_csv_files.diff_header_and_rows(header_a, rows_a, header_b, rows_b)

        self.assertEqual(len(diff_rows), 1)
        self.assertEqual(diff_rows[0][1], "Show Name|Show Name (2008)")

    def test_matching_base_directory_is_not_reported_as_a_diff(self) -> None:
        header_a = ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        rows_a = (("TV Shows", "Show Name", "Show.S01E01.mkv", "Yes"),)
        header_b = ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        rows_b = (("TV Shows", "Show Name", "Show.S01E01.mkv", "No"),)

        diff_header, diff_rows = compare_csv_files.diff_header_and_rows(
            header_a, rows_a, header_b, rows_b
        )

        self.assertEqual(diff_rows[0][diff_header.index("Base Directory")], "Show Name")

    def test_no_input_column_named_base_filename_is_an_error(self) -> None:
        header_a = ("Library", "Base Directory", "Missing Subtitles")
        rows_a = (("TV Shows", "Show Name", "Yes"),)

        with self.assertRaises(ValueError):
            compare_csv_files.diff_header_and_rows(header_a, rows_a, header_a, rows_a)

    def test_build_header_never_marks_identity_columns_l_r(self) -> None:
        header = compare_csv_files.build_header(
            ("Library", "Base Directory", "Base Filename", "Missing Subtitles")
        )

        self.assertEqual(
            header,
            ("Library", "Base Directory", "Base Filename", "Missing Subtitles (L|R)"),
        )


if __name__ == "__main__":
    unittest.main()
