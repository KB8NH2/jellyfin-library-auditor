"""Combined Excel workbook generation for audit results.

Writes one worksheet per audited server, each holding the same rows as that
server's own audit CSV in a named Excel Table, plus an optional "diffs"
worksheet when two servers were compared. Yes/No columns on a server sheet
get a yellow background for "Yes" cells; diff cells (any "left|right" value
where the two sides differ) get the same treatment on the diffs sheet. The
Episode column is formatted as text (rather than CSV's leading-apostrophe
guard) so a merged range like "19-20" isn't misread as a date, and every
column from Season onward is wrapped and center-aligned. A server sheet
also gets a "Problems" column (a per-row count of that row's "Yes" cells)
and a "Totals" row below the table (a per-column count of "Yes" cells, plus
the sum of "Problems") - neither exists in the server's own audit CSV,
which stays a plain per-item export.
"""

from __future__ import annotations

import re
from pathlib import Path

from openpyxl import Workbook
from openpyxl.formatting.rule import CellIsRule
from openpyxl.formatting.rule import FormulaRule
from openpyxl.styles import Alignment
from openpyxl.styles import Font
from openpyxl.styles import PatternFill
from openpyxl.utils import get_column_letter
from openpyxl.worksheet.table import Table
from openpyxl.worksheet.table import TableStyleInfo
from openpyxl.worksheet.worksheet import Worksheet

import compare_csv_files
from output_layout import audit_results_xlsx_path
from reports.generator import CSV_HEADER
from reports.generator import _csv_rows
from results import AuditServerResult


DIFFS_SHEET_LABEL = "diffs"
PROBLEMS_COLUMN_LABEL = "Problems"
TOTALS_ROW_LABEL = "Totals"

_SERVER_IDENTITY_COLUMNS = frozenset({"Library", "Path", "Series", "Title", "Season", "Episode"})
_YELLOW_FILL = PatternFill(start_color="FFFFFF00", end_color="FFFFFF00", fill_type="solid")
_INVALID_SHEET_NAME_CHARS = re.compile(r"[:\\/?*\[\]]")
_INVALID_TABLE_NAME_CHARS = re.compile(r"[^A-Za-z0-9_]+")
_MAX_SHEET_NAME_LENGTH = 31
_MAX_COLUMN_WIDTH = 60
_PROBLEMS_COLUMN_WIDTH = 10
_TEXT_NUMBER_FORMAT = "@"
_WRAP_CENTER_ALIGNMENT = Alignment(wrap_text=True, horizontal="center", vertical="center")
_TOTALS_ROW_FONT = Font(bold=True)
# Column from which "Season, Episode and all subsequent columns" (per-column
# wrap/center formatting) starts. Both CSV_HEADER and the diffs sheet's
# header (compare_csv_files.build_header(CSV_HEADER)) keep "Season" at the
# same position, so one column name lookup covers both sheet shapes.
_WRAP_CENTER_START_COLUMN = "Season"


def write_audit_results_workbook(
    root_dir: Path,
    server_results: tuple[AuditServerResult, ...],
    *,
    diff_results: tuple[AuditServerResult, AuditServerResult] | None = None,
) -> Path:
    """Write the combined audit-results Excel workbook to ``root_dir``.

    Args:
        root_dir: Audit-results root directory (same directory as the
            top-level index.html).
        server_results: One worksheet is written per entry, named after the
            server and containing the same rows as that server's own audit
            CSV (see reports.generator.write_csv_report).
        diff_results: An optional (left, right) pair of already-audited
            server results to diff into a "diffs" worksheet, in the same
            row shape compare_csv_files.py produces for its CSV output.

    Returns:
        The written workbook's path.

    Raises:
        PermissionError: If ``output_path`` couldn't be overwritten - most
            often because it's currently open in Excel or another program,
            which locks the file. Re-raised with a clearer message; the
            previous run's xlsx is left in place on disk in that case, even
            though this run's CSV/HTML reports (written earlier) did get
            updated - the two can briefly disagree until the xlsx write
            succeeds on a later run.
    """
    workbook = Workbook()
    workbook.remove(workbook.active)

    used_sheet_titles: set[str] = set()
    used_table_names: set[str] = set()

    for result in server_results:
        label = _server_label(result)
        _write_server_sheet(
            workbook,
            label=label,
            rows=_csv_rows(result),
            used_sheet_titles=used_sheet_titles,
            used_table_names=used_table_names,
        )

    if diff_results is not None:
        left_result, right_result = diff_results
        _write_diffs_sheet(
            workbook,
            left_result=left_result,
            right_result=right_result,
            used_sheet_titles=used_sheet_titles,
            used_table_names=used_table_names,
        )

    output_path = audit_results_xlsx_path(root_dir)
    output_path.parent.mkdir(parents=True, exist_ok=True)
    try:
        workbook.save(output_path)
    except PermissionError as error:
        raise PermissionError(
            f"Could not write {output_path} - it may be open in Excel or another "
            "program. Close it and re-run. The CSV/HTML reports were written "
            "successfully and reflect this run; only the previous run's xlsx "
            "file is left in place."
        ) from error
    return output_path


def _write_server_sheet(
    workbook: Workbook,
    *,
    label: str,
    rows: tuple[tuple[str, ...], ...],
    used_sheet_titles: set[str],
    used_table_names: set[str],
) -> None:
    """Write one server's audit rows to a new named-table worksheet."""
    rows = compare_csv_files.strip_episode_guard(CSV_HEADER, rows)
    sheet = workbook.create_sheet(_unique_sheet_title(label, used_sheet_titles))

    yes_no_indices = _yes_no_column_indices(CSV_HEADER)
    first_yes_no_letter = get_column_letter(yes_no_indices[0] + 1)
    last_yes_no_letter = get_column_letter(yes_no_indices[-1] + 1)
    header = CSV_HEADER + (PROBLEMS_COLUMN_LABEL,)
    rows_with_problems = tuple(
        row + (f'=COUNTIF({first_yes_no_letter}{row_number}:{last_yes_no_letter}{row_number},"Yes")',)
        for row_number, row in enumerate(rows, start=2)
    )

    last_row = _write_table(
        sheet,
        header=header,
        rows=rows_with_problems,
        table_name=_unique_table_name(label, used_table_names),
    )
    # Overrides _write_table()'s generic content-fitted width, which would
    # otherwise size this column to the Problems formula's own text length
    # rather than the short count it actually displays.
    sheet.column_dimensions[get_column_letter(len(header))].width = _PROBLEMS_COLUMN_WIDTH

    _add_yes_no_conditional_formatting(sheet, CSV_HEADER, last_row)
    _add_episode_text_format(sheet, CSV_HEADER, last_row)
    _add_wrap_center_alignment(sheet, header, last_row)
    _add_totals_row(
        sheet,
        yes_no_indices=yes_no_indices,
        problems_column_index=len(CSV_HEADER),
        last_row=last_row,
    )


def _write_diffs_sheet(
    workbook: Workbook,
    *,
    left_result: AuditServerResult,
    right_result: AuditServerResult,
    used_sheet_titles: set[str],
    used_table_names: set[str],
) -> None:
    """Write the left/right diff rows to a new "diffs" worksheet."""
    left_rows = compare_csv_files.strip_episode_guard(CSV_HEADER, _csv_rows(left_result))
    right_rows = compare_csv_files.strip_episode_guard(CSV_HEADER, _csv_rows(right_result))
    diff_header, diff_rows = compare_csv_files.diff_header_and_rows(
        CSV_HEADER, left_rows, CSV_HEADER, right_rows
    )
    # diff_header_and_rows() re-applies its own CSV text guard (a leading
    # apostrophe) to a non-differing Episode value, matching
    # compare_csv_files.py's own CSV output - not wanted here, since the
    # Episode column instead gets a real text number format below.
    diff_rows = compare_csv_files.strip_episode_guard(diff_header, diff_rows)

    sheet = workbook.create_sheet(_unique_sheet_title(DIFFS_SHEET_LABEL, used_sheet_titles))
    last_row = _write_table(
        sheet,
        header=diff_header,
        rows=diff_rows,
        table_name=_unique_table_name(DIFFS_SHEET_LABEL, used_table_names),
    )
    _add_diff_conditional_formatting(sheet, diff_header, last_row)
    _add_episode_text_format(sheet, diff_header, last_row)
    _add_wrap_center_alignment(sheet, diff_header, last_row)


def _write_table(
    sheet: Worksheet,
    *,
    header: tuple[str, ...],
    rows: tuple[tuple[str, ...], ...],
    table_name: str,
) -> int:
    """Write a header/rows table to ``sheet`` as a named Excel Table.

    Returns the 1-indexed row number of the last written row (the header
    row when ``rows`` is empty).
    """
    sheet.append(header)
    for row in rows:
        sheet.append(row)
    sheet.freeze_panes = "A2"

    last_row = len(rows) + 1
    last_column_letter = get_column_letter(len(header))
    table = Table(displayName=table_name, ref=f"A1:{last_column_letter}{last_row}")
    table.tableStyleInfo = TableStyleInfo(name="TableStyleMedium9", showRowStripes=True)
    sheet.add_table(table)

    for index, column_name in enumerate(header):
        column_letter = get_column_letter(index + 1)
        sheet.column_dimensions[column_letter].width = _column_width(column_name, rows, index)

    return last_row


def _yes_no_column_indices(header: tuple[str, ...]) -> tuple[int, ...]:
    """Return the 0-indexed positions of every non-identity (Yes/No) column."""
    return tuple(index for index, name in enumerate(header) if name not in _SERVER_IDENTITY_COLUMNS)


def _add_totals_row(
    sheet: Worksheet,
    *,
    yes_no_indices: tuple[int, ...],
    problems_column_index: int,
    last_row: int,
) -> None:
    """Write a "Totals" row directly below the table.

    Each Yes/No column gets a count of its own "Yes" cells; the Problems
    column gets the sum of its per-row counts (so it equals the same total
    either way). Deliberately left out of the table's own ref - a row
    inside the table would participate in the table's own sort/filter,
    which could otherwise scatter this row away from the bottom of the
    sheet.
    """
    totals_row = last_row + 1
    sheet.cell(row=totals_row, column=1, value=TOTALS_ROW_LABEL)

    # With no data rows (last_row == 1, just the header), the totals row is
    # row 2 - the same row a "2:{last_row}" formula would reference, which
    # would make the cell reference itself. Write a literal 0 instead in
    # that case rather than a self-referential formula.
    has_data_rows = last_row >= 2

    for index in yes_no_indices:
        column_letter = get_column_letter(index + 1)
        value = (
            f'=COUNTIF({column_letter}2:{column_letter}{last_row},"Yes")' if has_data_rows else 0
        )
        sheet.cell(row=totals_row, column=index + 1, value=value)

    problems_column_letter = get_column_letter(problems_column_index + 1)
    problems_value = (
        f"=SUM({problems_column_letter}2:{problems_column_letter}{last_row})"
        if has_data_rows
        else 0
    )
    sheet.cell(row=totals_row, column=problems_column_index + 1, value=problems_value)

    for cell in sheet[totals_row]:
        cell.font = _TOTALS_ROW_FONT
        cell.alignment = _WRAP_CENTER_ALIGNMENT


def _add_yes_no_conditional_formatting(
    sheet: Worksheet, header: tuple[str, ...], last_row: int
) -> None:
    """Highlight "Yes" cells in every non-identity column with a yellow background."""
    if last_row < 2:
        return
    for index in _yes_no_column_indices(header):
        column_letter = get_column_letter(index + 1)
        sheet.conditional_formatting.add(
            f"{column_letter}2:{column_letter}{last_row}",
            CellIsRule(operator="equal", formula=['"Yes"'], fill=_YELLOW_FILL),
        )


def _add_diff_conditional_formatting(
    sheet: Worksheet, header: tuple[str, ...], last_row: int
) -> None:
    """Highlight any cell containing "left|right" values that differ.

    A single formula rule covers every data cell: it fires when the cell
    contains "|" and the text before it doesn't match the text after it,
    which is exactly compare_csv_files.py's own definition of a differing
    column (an identity column only ever shows "|" when the two sides
    differ; a test-criteria column always shows "left|right", so this
    still only lights up where "left" and "right" actually disagree).
    """
    if last_row < 2:
        return
    last_column_letter = get_column_letter(len(header))
    formula = (
        'AND(ISNUMBER(SEARCH("|",A2)),'
        'LEFT(A2,FIND("|",A2)-1)<>MID(A2,FIND("|",A2)+1,LEN(A2)))'
    )
    sheet.conditional_formatting.add(
        f"A2:{last_column_letter}{last_row}",
        FormulaRule(formula=[formula], fill=_YELLOW_FILL),
    )


def _add_episode_text_format(sheet: Worksheet, header: tuple[str, ...], last_row: int) -> None:
    """Format the Episode column's data cells as text.

    Excel's own auto-detection would otherwise read a merged-episode value
    like "19-20" as a date; a real text number format prevents that without
    needing compare_csv_files.py's CSV-only leading-apostrophe guard (which
    is stripped from these rows before they're written - see
    compare_csv_files.strip_episode_guard()).
    """
    if "Episode" not in header or last_row < 2:
        return
    column_letter = get_column_letter(header.index("Episode") + 1)
    for row in range(2, last_row + 1):
        sheet[f"{column_letter}{row}"].number_format = _TEXT_NUMBER_FORMAT


def _add_wrap_center_alignment(sheet: Worksheet, header: tuple[str, ...], last_row: int) -> None:
    """Wrap and center every "Season" column onward, header and data alike."""
    if _WRAP_CENTER_START_COLUMN not in header:
        return
    start_index = header.index(_WRAP_CENTER_START_COLUMN)
    for index in range(start_index, len(header)):
        column_letter = get_column_letter(index + 1)
        for row in range(1, last_row + 1):
            sheet[f"{column_letter}{row}"].alignment = _WRAP_CENTER_ALIGNMENT


def _server_label(result: AuditServerResult) -> str:
    """Return the display label used for one server's sheet/table name."""
    return result.server_name or result.server_key or result.server_url or "Server"


def _unique_sheet_title(label: str, used_titles: set[str]) -> str:
    """Return an Excel-legal, workbook-unique worksheet title derived from ``label``."""
    base = _INVALID_SHEET_NAME_CHARS.sub("_", label.strip())[:_MAX_SHEET_NAME_LENGTH] or "Sheet"
    title = base
    suffix = 2
    while title.casefold() in used_titles:
        suffix_text = f" ({suffix})"
        title = base[: _MAX_SHEET_NAME_LENGTH - len(suffix_text)] + suffix_text
        suffix += 1
    used_titles.add(title.casefold())
    return title


def _unique_table_name(label: str, used_names: set[str]) -> str:
    """Return an Excel-legal, workbook-unique table name derived from ``label``."""
    slug = _INVALID_TABLE_NAME_CHARS.sub("_", label.strip()).strip("_") or "Table"
    if slug[0].isdigit():
        slug = f"T_{slug}"
    name = slug
    suffix = 2
    while name.casefold() in used_names:
        name = f"{slug}_{suffix}"
        suffix += 1
    used_names.add(name.casefold())
    return name


def _column_width(column_name: str, rows: tuple[tuple[str, ...], ...], index: int) -> float:
    """Return a content-fitted column width, capped so long paths stay usable."""
    longest = max((len(row[index]) for row in rows), default=0)
    return min(max(len(column_name), longest) + 2, _MAX_COLUMN_WIDTH)


__all__ = ["write_audit_results_workbook"]
