"""Tests for xlsx_report.py."""

from __future__ import annotations

from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import openpyxl

import xlsx_report
from reports.generator import CSV_HEADER

from tests.helpers import _make_item
from tests.helpers import _make_single_library_result


class WriteAuditResultsWorkbookTests(unittest.TestCase):
    def test_title_column_matches_the_items_current_title(self) -> None:
        """Regression test: the workbook's Title column must reflect each
        item's current title (the same value the CSV/HTML reports show),
        not a stale or independently-derived one - the server sheet is
        built from the exact same rows reports.generator._csv_rows()
        produces for the CSV.
        """
        item = _make_item("Correct New Title", is_movie=True, is_episode=False)
        result = _make_single_library_result(
            (item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Primary",
            server_key="primary",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            output_path = xlsx_report.write_audit_results_workbook(root_dir, (result,))

            workbook = openpyxl.load_workbook(output_path)
            sheet = workbook["Primary"]
            header = [cell.value for cell in sheet[1]]
            title_column = header.index("Title") + 1

            self.assertEqual(sheet.cell(row=2, column=title_column).value, "Correct New Title")

    def test_path_is_split_into_base_directory_and_base_filename_columns(self) -> None:
        item = _make_item(
            "Movie One",
            is_movie=True,
            is_episode=False,
            path=Path("Movies/Movie One (2024)/Movie One (2024).mkv"),
        )
        result = _make_single_library_result(
            (item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Primary",
            server_key="primary",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            output_path = xlsx_report.write_audit_results_workbook(root_dir, (result,))

            workbook = openpyxl.load_workbook(output_path)
            sheet = workbook["Primary"]
            header = [cell.value for cell in sheet[1]]

        self.assertNotIn("Path", header)
        base_directory_column = header.index("Base Directory") + 1
        base_filename_column = header.index("Base Filename") + 1
        self.assertEqual(sheet.cell(row=2, column=base_directory_column).value, "Movie One (2024)")
        self.assertEqual(
            sheet.cell(row=2, column=base_filename_column).value, "Movie One (2024).mkv"
        )

    def test_permission_error_on_save_is_reraised_with_a_clearer_message(self) -> None:
        """Regression test: if the xlsx is open in Excel (or another program
        holding it locked), Workbook.save() raises a plain PermissionError
        with no indication of why - that must be surfaced as an actionable
        message instead of a bare stack trace, since the previous run's
        stale file is left in place on disk when this happens."""
        item = _make_item("Some Title")
        result = _make_single_library_result(
            (item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Primary",
            server_key="primary",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            with patch.object(
                xlsx_report.Workbook, "save", side_effect=PermissionError("locked")
            ):
                with self.assertRaises(PermissionError) as context:
                    xlsx_report.write_audit_results_workbook(root_dir, (result,))

        self.assertIn("open in Excel", str(context.exception))

    def test_every_cell_is_wrapped_and_center_aligned(self) -> None:
        item = _make_item("Some Title", library="Movies")
        result = _make_single_library_result(
            (item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Primary",
            server_key="primary",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            output_path = xlsx_report.write_audit_results_workbook(root_dir, (result,))

            workbook = openpyxl.load_workbook(output_path)
            sheet = workbook["Primary"]
            header = [cell.value for cell in sheet[1]]

        # "Library" is the very first column - before Title, and well
        # before the old "Season onward" cutoff - confirming wrap/center
        # now applies to every column, not just a trailing subset.
        library_column = header.index("Library") + 1
        for row in (1, 2):
            cell = sheet.cell(row=row, column=library_column)
            self.assertTrue(cell.alignment.wrap_text)
            self.assertEqual(cell.alignment.horizontal, "center")
            self.assertEqual(cell.alignment.vertical, "center")

    def test_columns_after_title_have_a_fixed_width_of_eleven(self) -> None:
        item = _make_item(
            "A Title Much Longer Than Eleven Characters",
            library="Movies",
        )
        result = _make_single_library_result(
            (item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Primary",
            server_key="primary",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            output_path = xlsx_report.write_audit_results_workbook(root_dir, (result,))

            workbook = openpyxl.load_workbook(output_path)
            sheet = workbook["Primary"]
            header = [cell.value for cell in sheet[1]]

        title_index = header.index("Title")
        # Title itself keeps its content-fitted width, which for this
        # deliberately long title is wider than the fixed width every
        # later column gets.
        title_letter = openpyxl.utils.get_column_letter(title_index + 1)
        self.assertGreater(sheet.column_dimensions[title_letter].width, 11)
        for index in range(title_index + 1, len(header)):
            column_letter = openpyxl.utils.get_column_letter(index + 1)
            self.assertEqual(sheet.column_dimensions[column_letter].width, 11)

    def test_diffs_sheet_also_gets_wrap_center_and_fixed_width(self) -> None:
        left_item = _make_item("Same Title", library="Movies")
        right_item = _make_item("Same Title", library="Movies")
        left_result = _make_single_library_result(
            (left_item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Left",
            server_key="left",
        )
        right_result = _make_single_library_result(
            (right_item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Right",
            server_key="right",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            output_path = xlsx_report.write_audit_results_workbook(
                root_dir, (left_result, right_result), diff_results=(left_result, right_result)
            )

            workbook = openpyxl.load_workbook(output_path)
            sheet = workbook["diffs"]
            header = [cell.value for cell in sheet[1]]

        library_column = header.index("Library") + 1
        self.assertTrue(sheet.cell(row=1, column=library_column).alignment.wrap_text)
        title_index = header.index("Title")
        after_title_letter = openpyxl.utils.get_column_letter(title_index + 2)
        self.assertEqual(sheet.column_dimensions[after_title_letter].width, 11)

    def test_header_matches_csv_header_plus_problems_column(self) -> None:
        item = _make_item("Some Title")
        result = _make_single_library_result(
            (item,),
            library_id="lib1",
            library_name="Movies",
            collection_type="movies",
            server_name="Primary",
            server_key="primary",
        )

        with TemporaryDirectory() as temp_dir:
            root_dir = Path(temp_dir) / "audit_results"
            output_path = xlsx_report.write_audit_results_workbook(root_dir, (result,))

            workbook = openpyxl.load_workbook(output_path)
            sheet = workbook["Primary"]
            header = tuple(cell.value for cell in sheet[1])

        self.assertEqual(header, CSV_HEADER + ("Problems",))


if __name__ == "__main__":
    unittest.main()
