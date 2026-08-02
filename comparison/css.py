"""Shared CSS asset writer for static comparison reports."""

from __future__ import annotations

from pathlib import Path


def write_css(path: Path) -> None:
    """Write the shared comparison stylesheet."""
    path.write_text(_stylesheet(), encoding="utf-8")


def _stylesheet() -> str:
    """Return the shared comparison stylesheet."""
    return "\n".join(
        (
            ":root {",
            "  --bg: #f6f8fa;",
            "  --surface: #ffffff;",
            "  --surface-alt: #f8fafc;",
            "  --border: #d0d7de;",
            "  --text: #1f2328;",
            "  --muted: #57606a;",
            "  --shadow: 0 8px 24px rgba(31, 35, 40, 0.08);",
            "  --accent: #0969da;",
            "}",
            "* { box-sizing: border-box; }",
            "html { font-size: 80%; }",
            "body {",
            "  margin: 0;",
            "  background: var(--bg);",
            "  color: var(--text);",
            "  font-family: Arial, sans-serif;",
            "  line-height: 1.45;",
            "}",
            "a { color: var(--accent); text-decoration: none; }",
            "a:hover { text-decoration: underline; }",
            ".page-shell { max-width: 1600px; margin: 0 auto; padding: 24px; }",
            ".site-nav { display: flex; flex-wrap: wrap; gap: 10px; margin-bottom: 12px; }",
            ".nav-link { display: inline-flex; align-items: center; padding: 10px 14px; border-radius: 999px; border: 1px solid var(--border); background: var(--surface); box-shadow: var(--shadow); color: var(--text); font-weight: 600; }",
            ".nav-link.is-active { background: var(--accent); border-color: var(--accent); color: #ffffff; }",
            ".page-header-card, .toolbar-card, .section-card { background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); padding: 16px; margin-bottom: 16px; }",
            ".page-header-card h1 { margin: 0 0 8px; font-size: 2rem; }",
            ".page-intro, .muted-text { color: var(--muted); }",
            ".toolbar-card { display: flex; flex-wrap: wrap; gap: 16px; align-items: end; justify-content: space-between; }",
            ".toolbar-controls { display: flex; flex-wrap: wrap; gap: 10px; align-items: end; }",
            ".search-field { display: flex; flex-direction: column; gap: 6px; min-width: 280px; font-weight: 600; }",
            ".search-field input { padding: 10px 12px; border: 1px solid var(--border); border-radius: 10px; font: inherit; }",
            ".summary-card-grid { display: grid; gap: 14px; grid-template-columns: repeat(auto-fit, minmax(190px, 1fr)); margin-bottom: 16px; }",
            ".summary-card { display: block; background: var(--surface); border: 1px solid var(--border); border-radius: 16px; box-shadow: var(--shadow); padding: 16px; }",
            ".summary-card h3 { margin: 0 0 8px; }",
            ".summary-card-value { margin: 0; font-size: 1.55rem; font-weight: 700; }",
            ".table-shell { overflow-x: auto; }",
            ".data-table { width: 100%; border-collapse: separate; border-spacing: 0; table-layout: fixed; background: var(--surface); border: 1px solid var(--border); border-radius: 12px; }",
            ".data-table th, .data-table td { padding: 8px 10px; text-align: left; vertical-align: top; border-bottom: 1px solid var(--border); word-break: break-word; }",
            ".data-table thead th { position: sticky; top: 0; z-index: 1; background: #eef4fb; }",
            ".data-table tbody tr:nth-child(even) { background: #fbfdff; }",
            ".data-table tbody tr:last-child td { border-bottom: none; }",
            ".comparison-diff { background: #fff8c5; color: #7d4e00; font-weight: 700; }",
            ".sort-button { width: 100%; border: none; background: transparent; padding: 0; text-align: left; font: inherit; font-weight: 700; cursor: pointer; }",
            ".empty-row td { color: var(--muted); }",
        )
    )
