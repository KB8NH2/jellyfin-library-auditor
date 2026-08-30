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
                "Library,Path,Episode\nTV Shows,/media/show.mkv,'5-7\n",
                encoding="utf-8",
            )

            header, rows = compare_csv_files.read_rows(csv_path)

        self.assertEqual(header, ("Library", "Path", "Episode"))
        self.assertEqual(rows, (("TV Shows", "/media/show.mkv", "5-7"),))

    def test_read_rows_leaves_plain_episode_numbers_unchanged(self) -> None:
        with TemporaryDirectory() as temp_dir:
            csv_path = Path(temp_dir) / "audit.csv"
            csv_path.write_text(
                "Library,Path,Episode\nTV Shows,/media/show.mkv,5\n",
                encoding="utf-8",
            )

            _, rows = compare_csv_files.read_rows(csv_path)

        self.assertEqual(rows, (("TV Shows", "/media/show.mkv", "5"),))

    def test_combine_row_reapplies_excel_guard_for_matching_episode_range(self) -> None:
        header = ("Library", "Path", "Episode")
        row_a = ("TV Shows", "/media/a/show.mkv", "5-7")
        row_b = ("TV Shows", "/media/b/show.mkv", "5-7")

        combined = compare_csv_files.combine_row(header, "show.mkv", row_a, row_b)

        self.assertEqual(combined, ("TV Shows", "show.mkv", "'5-7"))

    def test_write_diff_csv_round_trips_episode_range_through_the_excel_guard(self) -> None:
        with TemporaryDirectory() as temp_dir:
            temp_dir_path = Path(temp_dir)
            csv_a = temp_dir_path / "left_audit.csv"
            csv_b = temp_dir_path / "right_audit.csv"
            output_path = temp_dir_path / "diffs.csv"

            csv_a.write_text(
                "Library,Path,Episode,Missing Subtitles\n"
                "TV Shows,/left/show.mkv,'5-7,Yes\n",
                encoding="utf-8",
            )
            csv_b.write_text(
                "Library,Path,Episode,Missing Subtitles\n"
                "TV Shows,/right/show.mkv,'5-7,No\n",
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
