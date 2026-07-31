"""Report writers for Jellyfin Library Auditor.

This module formats already-computed audit results into report files. It does
not communicate with Jellyfin, perform audits, or inspect the filesystem except
when writing report files.
"""

from __future__ import annotations

import csv
from html import escape
from pathlib import Path

import audit_types
from config import get_config
from media import get_display_path
from results import AuditServerResult


CSV_HEADER = ("Category", "Severity", "Check", "Title", "Path", "Message")
SEVERITY_SORT_ORDER = {
    audit_types.AuditSeverity.ERROR: 0,
    audit_types.AuditSeverity.WARNING: 1,
    audit_types.AuditSeverity.INFO: 2,
}


def write_csv_report(result: AuditServerResult) -> Path:
    """Write a CSV report containing one row per finding.

    Args:
        result: Audit results to serialize.

    Returns:
        The CSV report path that was written.
    """
    output_path = get_config().reporting.output.audit_csv
    _ensure_parent_directory(output_path)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(_csv_rows(result))

    return output_path


def write_html_report(result: AuditServerResult) -> Path:
    """Write a standalone HTML report for the supplied audit results.

    Args:
        result: Audit results to serialize.

    Returns:
        The HTML report path that was written.
    """
    output_path = get_config().reporting.output.audit_html
    _ensure_parent_directory(output_path)

    document = "\n".join(
        (
            "<!DOCTYPE html>",
            '<html lang="en">',
            "<head>",
            '  <meta charset="utf-8">',
            "  <title>Jellyfin Library Auditor Report</title>",
            "  <style>",
            _embedded_css(),
            "  </style>",
            "</head>",
            "<body>",
            '  <main class="container">',
            "    <h1>Jellyfin Library Auditor Report</h1>",
            _html_summary(result),
            _html_table(sort_findings(result.findings)),
            "  </main>",
            "  <script>",
            _embedded_sort_script(),
            "  </script>",
            "</body>",
            "</html>",
        )
    )

    output_path.write_text(document, encoding="utf-8")
    return output_path


def findings_by_category(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[audit_types.AuditCategory, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by audit category.

    Args:
        findings: Findings to group.

    Returns:
        A dictionary keyed by category containing grouped findings.
    """
    grouped: dict[audit_types.AuditCategory, list[audit_types.AuditFinding]] = {}

    for finding in findings:
        grouped.setdefault(finding.category, []).append(finding)

    return {category: tuple(items) for category, items in grouped.items()}


def findings_by_severity(
    findings: tuple[audit_types.AuditFinding, ...],
) -> dict[audit_types.AuditSeverity, tuple[audit_types.AuditFinding, ...]]:
    """Group findings by audit severity.

    Args:
        findings: Findings to group.

    Returns:
        A dictionary keyed by severity containing grouped findings.
    """
    grouped: dict[audit_types.AuditSeverity, list[audit_types.AuditFinding]] = {}

    for finding in findings:
        grouped.setdefault(finding.severity, []).append(finding)

    return {severity: tuple(items) for severity, items in grouped.items()}


def sort_findings(
    findings: tuple[audit_types.AuditFinding, ...],
) -> tuple[audit_types.AuditFinding, ...]:
    """Sort findings by severity, category, and title.

    Args:
        findings: Findings to sort.

    Returns:
        A sorted tuple of findings.
    """
    return tuple(
        sorted(
            findings,
            key=lambda finding: (
                SEVERITY_SORT_ORDER[finding.severity],
                finding.category.value,
                finding.media_item.display_name.casefold(),
            ),
        )
    )


def _csv_rows(result: AuditServerResult) -> tuple[tuple[str, ...], ...]:
    """Return CSV rows for the supplied audit results.

    Args:
        result: Audit results to serialize.

    Returns:
        CSV row values, excluding the header row.
    """
    rows: list[tuple[str, ...]] = []

    for finding in sort_findings(result.findings):
        row: tuple[str, ...] = (
            finding.category.value.title(),
            finding.severity.value.title(),
            finding.check_name,
            finding.media_item.display_name,
            get_display_path(finding.media_item),
            finding.message,
        )
        rows.append(row)

    return tuple(rows)


def _html_table(findings: tuple[audit_types.AuditFinding, ...]) -> str:
    """Return HTML for the findings table.

    Args:
        findings: Findings to render.

    Returns:
        A standalone HTML fragment containing the findings table.
    """
    header_cells = (
        "Category",
        "Severity",
        "Check",
        "Title",
        "Path",
        "Message",
    )
    header_html = "".join(
        f'<th><button type="button" onclick="sortReportTable({index})">{escape(label)}</button></th>'
        for index, label in enumerate(header_cells)
    )

    row_html: list[str] = []
    for finding in findings:
        cells = (
            finding.category.value.title(),
            finding.severity.value.title(),
            finding.check_name,
            finding.media_item.display_name,
            get_display_path(finding.media_item),
            finding.message,
        )
        row_html.append(
            "<tr>"
            + "".join(f"<td>{escape(cell)}</td>" for cell in cells)
            + "</tr>"
        )

    table_rows = "\n".join(row_html)
    return "\n".join(
        (
            '    <section class="report-section">',
            "      <h2>Findings</h2>",
            '      <table id="findings-table">',
            f"        <thead><tr>{header_html}</tr></thead>",
            f"        <tbody>{table_rows}</tbody>",
            "      </table>",
            "    </section>",
        )
    )


def _html_summary(result: AuditServerResult) -> str:
    """Return HTML for the report summary section.

    Args:
        result: Audit results to summarize.

    Returns:
        A standalone HTML fragment containing the summary section.
    """
    category_items = []
    for category, findings in sorted(
        findings_by_category(result.findings).items(),
        key=lambda entry: entry[0].value,
    ):
        label = escape(category.value.title())
        category_items.append(f"        <li>{label}: {len(findings)}</li>")

    severity_items = []
    for severity, findings in sorted(
        findings_by_severity(result.findings).items(),
        key=lambda entry: SEVERITY_SORT_ORDER[entry[0]],
    ):
        label = escape(severity.value.title())
        severity_items.append(f"        <li>{label}: {len(findings)}</li>")

    category_list = "\n".join(category_items) or "        <li>None: 0</li>"
    severity_list = "\n".join(severity_items) or "        <li>None: 0</li>"

    return "\n".join(
        (
            '    <section class="report-section summary-grid">',
            "      <div>",
            "        <h2>Server Summary</h2>",
            "        <ul>",
            f"          <li>Libraries audited: {result.libraries_audited}</li>",
            f"          <li>Media items processed: {result.media_items_processed}</li>",
            f"          <li>Total findings: {len(result.findings)}</li>",
            "        </ul>",
            "      </div>",
            "      <div>",
            "        <h2>Findings by Category</h2>",
            "        <ul>",
            category_list,
            "        </ul>",
            "      </div>",
            "      <div>",
            "        <h2>Findings by Severity</h2>",
            "        <ul>",
            severity_list,
            "        </ul>",
            "      </div>",
            "    </section>",
        )
    )


def _embedded_css() -> str:
    """Return embedded CSS for the HTML report."""
    return "\n".join(
        (
            "body {",
            "  margin: 0;",
            "  font-family: Arial, sans-serif;",
            "  background: #f5f7fa;",
            "  color: #1f2933;",
            "}",
            ".container {",
            "  max-width: 1200px;",
            "  margin: 0 auto;",
            "  padding: 24px;",
            "}",
            ".report-section {",
            "  background: #ffffff;",
            "  border-radius: 8px;",
            "  box-shadow: 0 1px 3px rgba(0, 0, 0, 0.1);",
            "  margin-bottom: 24px;",
            "  padding: 20px;",
            "}",
            ".summary-grid {",
            "  display: grid;",
            "  gap: 20px;",
            "  grid-template-columns: repeat(auto-fit, minmax(220px, 1fr));",
            "}",
            "h1, h2 {",
            "  margin-top: 0;",
            "}",
            "table {",
            "  width: 100%;",
            "  border-collapse: collapse;",
            "}",
            "th, td {",
            "  border: 1px solid #d2d6dc;",
            "  padding: 10px;",
            "  text-align: left;",
            "  vertical-align: top;",
            "}",
            "th {",
            "  background: #eef2f7;",
            "}",
            "th button {",
            "  width: 100%;",
            "  border: none;",
            "  background: transparent;",
            "  cursor: pointer;",
            "  font: inherit;",
            "  font-weight: 600;",
            "  padding: 0;",
            "  text-align: left;",
            "}",
            "tbody tr:nth-child(even) {",
            "  background: #f8fafc;",
            "}",
            "ul {",
            "  margin: 0;",
            "  padding-left: 20px;",
            "}",
        )
    )


def _embedded_sort_script() -> str:
    """Return a small script that enables table sorting."""
    return "\n".join(
        (
            "function sortReportTable(columnIndex) {",
            "  const table = document.getElementById('findings-table');",
            "  const body = table.tBodies[0];",
            "  const rows = Array.from(body.rows);",
            "  const ascending = table.dataset.sortColumn !== String(columnIndex)",
            "    || table.dataset.sortDirection !== 'asc';",
            "  rows.sort((left, right) => {",
            "    const leftValue = left.cells[columnIndex].textContent.trim().toLowerCase();",
            "    const rightValue = right.cells[columnIndex].textContent.trim().toLowerCase();",
            "    if (leftValue < rightValue) {",
            "      return ascending ? -1 : 1;",
            "    }",
            "    if (leftValue > rightValue) {",
            "      return ascending ? 1 : -1;",
            "    }",
            "    return 0;",
            "  });",
            "  rows.forEach((row) => body.appendChild(row));",
            "  table.dataset.sortColumn = String(columnIndex);",
            "  table.dataset.sortDirection = ascending ? 'asc' : 'desc';",
            "}",
        )
    )


def _ensure_parent_directory(path: Path) -> None:
    """Create the parent directory for a report path when needed.

    Args:
        path: Report path to prepare.
    """
    path.parent.mkdir(parents=True, exist_ok=True)
