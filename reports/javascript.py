"""Shared JavaScript asset writer for static audit reports."""

from __future__ import annotations

from pathlib import Path


def write_javascript(path: Path) -> None:
    """Write the shared JavaScript asset."""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_script(), encoding="utf-8")


def _script() -> str:
    """Return the shared report JavaScript."""
    return "\n".join(
        (
            "(function () {",
            "  const root = document.querySelector('[data-nav-current]');",
            "  if (!root) { return; }",
            "  document.querySelectorAll('.nav-link[data-nav]').forEach((link) => {",
            "    if (link.dataset.nav === root.dataset.navCurrent) {",
            "      link.classList.add('is-active');",
            "    }",
            "  });",
            "  const searchInput = document.getElementById('report-search');",
            "  function applySearch() {",
            "    if (!searchInput) { return; }",
            "    const query = searchInput.value.trim().toLowerCase();",
            "    document.querySelectorAll('[data-search-row]').forEach((row) => {",
            "      const matches = query === '' || row.dataset.search.includes(query);",
            "      row.hidden = !matches;",
            "    });",
            "  }",
            "  searchInput?.addEventListener('input', applySearch);",
            "  applySearch();",
            "})();",
            "",
            "function sortReportTable(button) {",
            "  const table = button.closest('table');",
            "  if (!table) { return; }",
            "  const body = table.tBodies[0];",
            "  const columnIndex = Number(button.dataset.column);",
            "  const rows = Array.from(body.rows);",
            "  const ascending = table.dataset.sortColumn !== String(columnIndex) || table.dataset.sortDirection !== 'asc';",
            "  rows.sort((left, right) => {",
            "    const leftValue = left.cells[columnIndex].textContent.trim().toLowerCase();",
            "    const rightValue = right.cells[columnIndex].textContent.trim().toLowerCase();",
            "    if (leftValue < rightValue) { return ascending ? -1 : 1; }",
            "    if (leftValue > rightValue) { return ascending ? 1 : -1; }",
            "    return 0;",
            "  });",
            "  rows.forEach((row) => body.appendChild(row));",
            "  table.dataset.sortColumn = String(columnIndex);",
            "  table.dataset.sortDirection = ascending ? 'asc' : 'desc';",
            "}",
        )
    )
