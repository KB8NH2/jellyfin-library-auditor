"""Compare two audit CSV files from a --compare operation and report real diffs.

The two CSVs come from auditing the same libraries on two different Jellyfin
servers, so every "Path" value is expected to differ (different mount points),
and the two files won't necessarily have the same number of rows (one server
may be missing items the other has). This script matches rows between the two
files by the base filename of their Path column and writes one line per
differing item: identity columns (Library, Title, Season, Episode, and Path
replaced with Base Filename) show a single value, and every test-criteria
column shows "L|R" ("y"/"n" for the left/right file, or "-" if the row is
missing from that file). The output starts with a "left|right" line naming
which input file (by filename, extension stripped) is L and which is R,
e.g. "FELIX_audit|Jellyfin_audit".

All comparisons (deciding whether a row differs, and whether an identity
column differs enough to show as "L|R") are case-insensitive, matching the
case-insensitive title/episode-name comparison the app's own HTML "Mismatched
Metadata" report already uses. A pure capitalization difference between two
servers' metadata (e.g. "The Search For..." vs "The Search for...") is not
treated as a difference.
"""

from __future__ import annotations

import argparse
import csv
import posixpath
import sys
from collections import defaultdict
from pathlib import Path

PATH_COLUMN_NAME = "Path"
EPISODE_COLUMN_NAME = "Episode"
IDENTITY_COLUMNS = frozenset({"Library", "Series", "Title", "Season", EPISODE_COLUMN_NAME})


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("csv_a", type=Path, help="First (left) audit CSV file")
    parser.add_argument("csv_b", type=Path, help="Second (right) audit CSV file")
    parser.add_argument(
        "-o",
        "--output",
        type=Path,
        default=Path("diffs.csv"),
        help="Path to write the diff CSV to (default: diffs.csv)",
    )
    return parser.parse_args(argv)


def read_rows(csv_path: Path) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    with csv_path.open("r", encoding="utf-8", newline="") as handle:
        reader = csv.reader(handle)
        rows = [tuple(row) for row in reader]
    if not rows:
        raise SystemExit(f"{csv_path} is empty")

    header, data_rows = rows[0], rows[1:]
    return header, strip_episode_guard(header, tuple(data_rows))


def strip_episode_guard(
    header: tuple[str, ...], rows: tuple[tuple[str, ...], ...]
) -> tuple[tuple[str, ...], ...]:
    """Return ``rows`` with read_rows()'s Excel text-guard removed from the Episode column.

    A no-op when ``header`` has no Episode column. Exposed separately from
    :func:`read_rows` so callers holding audit rows already in memory (rather
    than freshly read from a CSV file) can still get diff_header_and_rows()'s
    expected un-guarded input.
    """
    if EPISODE_COLUMN_NAME not in header:
        return rows
    episode_index = header.index(EPISODE_COLUMN_NAME)
    return tuple(_without_excel_text_guard(row, episode_index) for row in rows)


def _without_excel_text_guard(row: tuple[str, ...], column_index: int) -> tuple[str, ...]:
    """Return ``row`` with a leading Excel text-guard apostrophe removed from one column.

    The audit CSV writer (reports/generator.py's _csv_episode_number)
    prefixes a hyphenated Episode range like "19-20" with a leading
    apostrophe so Excel's automatic type detection doesn't reinterpret it
    as a date - that apostrophe means nothing to this tool's own plain CSV
    reading, so it's stripped back off here before the value is compared or
    written to this tool's own output.
    """
    value = row[column_index]
    if not value.startswith("'"):
        return row
    return row[:column_index] + (value[1:],) + row[column_index + 1 :]


def without_column(row: tuple[str, ...], index: int) -> tuple[str, ...]:
    return row[:index] + row[index + 1 :]


def normalized(row: tuple[str, ...]) -> tuple[str, ...]:
    """Casefold every value so comparisons are case-insensitive.

    Matches the case-insensitive title/episode-name comparison already used
    by the app's HTML "Mismatched Metadata" report, so a pure capitalization
    difference between two servers' metadata isn't flagged here either.
    """
    return tuple(value.casefold() for value in row)


def basename_key(path_value: str) -> str:
    """Return the base filename from a (always POSIX-style) media path."""
    return posixpath.basename(path_value.rstrip("/"))


def yn(value: str) -> str:
    """Normalize a Yes/No test-criteria value down to a single letter."""
    normalized = value.strip().lower()
    if normalized in ("yes", "y", "true", "1"):
        return "y"
    if normalized in ("no", "n", "false", "0"):
        return "n"
    return value


def _with_excel_text_guard(value: str) -> str:
    """Return ``value`` prefixed with a leading apostrophe when it's a hyphenated range.

    Mirrors reports/generator.py's _csv_episode_number: a value like
    "19-20" is exactly the shape Excel's automatic type detection likes to
    reinterpret as a date when this diff CSV is opened by double-clicking
    it. read_rows() already strips this same guard off the two input
    files' Episode columns, so it needs to be re-applied here to whatever
    ends up in the *output* Episode column. A plain single number is never
    ambiguous this way and is left alone.
    """
    return f"'{value}" if "-" in value else value


def build_header(header: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(
        "Base Filename"
        if column == PATH_COLUMN_NAME
        else column
        if column in IDENTITY_COLUMNS
        else f"{column} (L|R)"
        for column in header
    )


def combine_row(
    header: tuple[str, ...],
    key: str,
    row_a: tuple[str, ...] | None,
    row_b: tuple[str, ...] | None,
) -> tuple[str, ...]:
    """Merge a matched (or half-missing) pair of rows into a single diff row."""
    identity_source = row_a if row_a is not None else row_b
    assert identity_source is not None
    combined = []
    for index, column in enumerate(header):
        if column == PATH_COLUMN_NAME:
            combined.append(key)
        elif column in IDENTITY_COLUMNS:
            if (
                row_a is not None
                and row_b is not None
                and row_a[index].casefold() != row_b[index].casefold()
            ):
                combined.append(f"{row_a[index]}|{row_b[index]}")
            elif column == EPISODE_COLUMN_NAME:
                combined.append(_with_excel_text_guard(identity_source[index]))
            else:
                combined.append(identity_source[index])
        else:
            left = yn(row_a[index]) if row_a is not None else "-"
            right = yn(row_b[index]) if row_b is not None else "-"
            combined.append(f"{left}|{right}")
    return tuple(combined)


def diff_header_and_rows(
    header_a: tuple[str, ...],
    rows_a: tuple[tuple[str, ...], ...],
    header_b: tuple[str, ...],
    rows_b: tuple[tuple[str, ...], ...],
) -> tuple[tuple[str, ...], tuple[tuple[str, ...], ...]]:
    """Return the diff header and rows for two audit row sets sharing one header shape.

    ``rows_a``/``rows_b`` are expected already un-guarded (see
    :func:`strip_episode_guard`) - matching what :func:`read_rows` returns.
    Uses ``header_a`` as the output header shape, same as :func:`write_diff_csv`.
    """
    try:
        path_index = header_a.index(PATH_COLUMN_NAME)
    except ValueError:
        raise ValueError(f"'{PATH_COLUMN_NAME}' column not found in header") from None

    # Bucket file B's rows by base filename so file A's rows can be realigned
    # to their counterpart even when the two files don't have matching row
    # counts or ordering.
    remaining_b: dict[str, list[tuple[str, ...]]] = defaultdict(list)
    for row in rows_b:
        remaining_b[basename_key(row[path_index])].append(row)

    diff_rows: list[tuple[str, ...]] = []
    for row_a in rows_a:
        key = basename_key(row_a[path_index])
        bucket = remaining_b.get(key)
        if bucket:
            row_b = bucket.pop(0)
            if not bucket:
                del remaining_b[key]
            if normalized(without_column(row_a, path_index)) != normalized(
                without_column(row_b, path_index)
            ):
                diff_rows.append(combine_row(header_a, key, row_a, row_b))
        else:
            # No counterpart in file B: this row only exists in file A.
            diff_rows.append(combine_row(header_a, key, row_a, None))

    # Anything left in remaining_b only exists in file B.
    for key, bucket in remaining_b.items():
        diff_rows.extend(combine_row(header_a, key, None, row_b) for row_b in bucket)

    return build_header(header_a), tuple(diff_rows)


def write_diff_csv(csv_a: Path, csv_b: Path, output_path: Path) -> int:
    """Diff two audit CSVs and write the result to output_path.

    Returns the number of differing rows written.
    """
    header_a, rows_a = read_rows(csv_a)
    header_b, rows_b = read_rows(csv_b)

    if header_a != header_b:
        print(
            f"Warning: headers differ between {csv_a} and {csv_b}; "
            "using the first file's header for output.",
            file=sys.stderr,
        )

    header, diff_rows = diff_header_and_rows(header_a, rows_a, header_b, rows_b)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow([f"{csv_a.stem}|{csv_b.stem}"])
        writer.writerow(header)
        writer.writerows(diff_rows)

    return len(diff_rows)


def main(argv: list[str] | None = None) -> None:
    args = parse_args(argv)
    diff_count = write_diff_csv(args.csv_a, args.csv_b, args.output)
    print(f"Wrote {diff_count} differing row(s) to {args.output}")


if __name__ == "__main__":
    main()
