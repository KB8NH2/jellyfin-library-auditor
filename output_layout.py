"""Shared output layout helpers for generated audit artifacts."""

from __future__ import annotations

from datetime import datetime
from html import escape
import json
from pathlib import Path
import re

from report_theme import render_theme_bootstrap_script
from report_theme import render_theme_toggle
from results import AuditServerResult


COMPARISON_RESULTS_DIRNAME = "comparison_results"
AUDIT_RESULTS_XLSX_FILENAME = "audit_results.xlsx"


def audit_results_root(configured_path: Path) -> Path:
    """Return the audit-results root directory for a configured output path."""
    return configured_path.with_suffix("") if configured_path.suffix else configured_path


def reset_audit_results_root(root_dir: Path) -> None:
    """Ensure the shared audit-results root exists without removing prior runs."""
    root_dir.mkdir(parents=True, exist_ok=True)


def server_directory_name(result: AuditServerResult) -> str:
    """Return the stable directory name for one server result."""
    for candidate in (result.server_key, result.server_name, result.server_url):
        if candidate is None:
            continue
        normalized = candidate.strip()
        if normalized:
            return slugify(normalized)
    return "server"


def server_output_dir(root_dir: Path, result: AuditServerResult) -> Path:
    """Return the output directory for one server site."""
    return root_dir / server_directory_name(result)


def server_csv_path(
    root_dir: Path,
    result: AuditServerResult,
    configured_csv_path: Path,
) -> Path:
    """Return the CSV output path for one server result."""
    return server_output_dir(root_dir, result) / server_csv_filename(
        result, configured_csv_path
    )


def server_csv_filename(result: AuditServerResult, configured_csv_path: Path) -> str:
    """Return the server-prefixed CSV filename, e.g. ``MyServer_audit.csv``.

    Prefixing with the server name keeps CSVs from multiple servers from
    colliding when downloaded from a browser to the same folder.
    """
    return f"{_server_filename_label(result)}_{configured_csv_path.name}"


def _server_filename_label(result: AuditServerResult) -> str:
    """Return a filesystem-safe, human-readable server label for filenames."""
    label = _server_display_label(result)
    normalized = re.sub(r"[^A-Za-z0-9]+", "_", label.strip())
    return normalized.strip("_") or "server"


def comparison_output_dir(root_dir: Path) -> Path:
    """Return the output directory for comparison pages."""
    return root_dir / COMPARISON_RESULTS_DIRNAME


def audit_results_xlsx_path(root_dir: Path) -> Path:
    """Return the path of the combined audit-results Excel workbook."""
    return root_dir / AUDIT_RESULTS_XLSX_FILENAME


def shared_css_path(root_dir: Path) -> Path:
    """Return the shared stylesheet path."""
    return root_dir / "css" / "style.css"


def shared_js_path(root_dir: Path) -> Path:
    """Return the shared JavaScript path."""
    return root_dir / "js" / "report.js"


def write_audit_results_index(
    root_dir: Path,
    results: tuple[AuditServerResult, ...],
    *,
    include_comparison: bool,
) -> Path:
    """Write the top-level audit-results index page."""
    del results
    del include_comparison
    asset_version = datetime.now().strftime("%Y%m%d%H%M%S")
    server_cards = tuple(
        _server_card(entry)
        for entry in _discover_server_entries(root_dir)
    )
    comparison_card = _comparison_card() if _comparison_index_exists(root_dir) else ""
    workbook_card = _workbook_card() if audit_results_xlsx_path(root_dir).is_file() else ""
    cards = (
        server_cards
        + ((comparison_card,) if comparison_card else ())
        + ((workbook_card,) if workbook_card else ())
    )
    cards_html = "\n".join(cards) or (
        '      <p class="muted-text">No HTML reports were generated.</p>'
    )
    index_path = root_dir / "index.html"
    index_path.write_text(
        "\n".join(
            (
                "<!DOCTYPE html>",
                '<html lang="en">',
                "<head>",
                '  <meta charset="utf-8">',
                '  <meta name="viewport" content="width=device-width, initial-scale=1">',
                '  <meta http-equiv="Cache-Control" content="no-cache, no-store, must-revalidate">',
                '  <meta http-equiv="Pragma" content="no-cache">',
                '  <meta http-equiv="Expires" content="0">',
                "  <title>Audit Results</title>",
                f"  {render_theme_bootstrap_script()}",
                f'  <link rel="stylesheet" href="css/style.css?v={asset_version}">',
                "</head>",
                "<body>",
                '  <main class="page-shell">',
                '    <section class="page-header-card">',
                '      <div class="page-header-actions">',
                f"        {render_theme_toggle()}",
                "      </div>",
                "      <h1>Audit Results</h1>",
                "      <p class=\"page-intro\">Open a server audit dashboard or the comparison results.</p>",
                "    </section>",
                '    <section class="section-card">',
                "      <h2>Available Reports</h2>",
                '      <section class="summary-card-grid">',
                cards_html,
                "      </section>",
                "    </section>",
                "  </main>",
                f'  <script src="js/report.js?v={asset_version}"></script>',
                "</body>",
                "</html>",
            )
        ),
        encoding="utf-8",
    )
    return index_path


def write_server_report_metadata(
    root_dir: Path,
    result: AuditServerResult,
    *,
    generated_at_text: str,
) -> Path:
    """Write the metadata used to rebuild the top-level index from disk."""
    metadata_path = server_output_dir(root_dir, result) / "report_info.json"
    metadata_path.write_text(
        json.dumps(
            {
                "server_directory": server_directory_name(result),
                "server_display_name": _server_display_label(result),
                "server_key": result.server_key,
                "libraries_audited": result.libraries_audited,
                "media_items_processed": result.media_items_processed,
                "findings_count": result.findings_count,
                "audited_at_text": generated_at_text,
            },
            indent=2,
            sort_keys=True,
        ),
        encoding="utf-8",
    )
    return metadata_path


def slugify(value: str) -> str:
    """Return a filesystem-safe slug for one display value."""
    normalized = re.sub(r"[^a-z0-9]+", "_", value.strip().casefold())
    collapsed = re.sub(r"_+", "_", normalized).strip("_")
    return collapsed or "item"


def _server_display_label(result: AuditServerResult) -> str:
    """Return the display label used for one server link."""
    if result.server_name:
        return result.server_name
    if result.server_key:
        return result.server_key
    return server_directory_name(result)


def _server_subtitle(entry: dict[str, object]) -> str:
    """Return the compact summary text for one server link."""
    parts = [
        f"{entry['libraries_audited']} libraries",
        f"{entry['media_items_processed']} media items",
        f"{entry['findings_count']} findings",
    ]
    if entry.get("server_key"):
        parts.append(f"folder: {entry['server_directory']}")
    return " | ".join(parts)


def _server_card(entry: dict[str, object]) -> str:
    """Return one top-level link card for a server report."""
    href = f"{entry['server_directory']}/index.html"
    heading = _server_card_heading(entry)
    return "\n".join(
        (
            f'        <a class="summary-card summary-card-server" href="{escape(href)}">',
            f"          <h3>{escape(heading)}</h3>",
            f'          <p class="summary-card-subtitle">{escape(_server_subtitle(entry))}</p>',
            "        </a>",
        )
    )


def _comparison_card() -> str:
    """Return the top-level link card for comparison results."""
    return "\n".join(
        (
            f'        <a class="summary-card summary-card-check" href="{escape(f"{COMPARISON_RESULTS_DIRNAME}/index.html")}">',
            "          <h3>Comparison Results</h3>",
            '          <p class="summary-card-subtitle">Cross-server library and media differences.</p>',
            "        </a>",
        )
    )


def _workbook_card() -> str:
    """Return the top-level link card for the combined Excel workbook."""
    return "\n".join(
        (
            f'        <a class="summary-card summary-card-check" href="{escape(AUDIT_RESULTS_XLSX_FILENAME)}">',
            "          <h3>Excel Workbook</h3>",
            '          <p class="summary-card-subtitle">All server and comparison data in one spreadsheet.</p>',
            "        </a>",
        )
    )


def _server_card_heading(entry: dict[str, object]) -> str:
    """Return the server card heading including the audit timestamp."""
    server_label = str(entry["server_display_name"])
    audited_at_text = entry.get("audited_at_text")
    if not audited_at_text:
        return server_label
    return f"{server_label} ({audited_at_text})"


def _discover_server_entries(root_dir: Path) -> tuple[dict[str, object], ...]:
    """Return server report entries discovered from the audit-results directory."""
    if not root_dir.exists():
        return ()

    entries: list[dict[str, object]] = []
    for child in root_dir.iterdir():
        if not child.is_dir() or child.name in {"css", "js", COMPARISON_RESULTS_DIRNAME}:
            continue
        entry = _load_server_entry(child)
        if entry is None:
            continue
        entries.append(entry)

    return tuple(
        sorted(entries, key=lambda item: str(item["server_display_name"]).casefold())
    )


def _load_server_entry(server_dir: Path) -> dict[str, object] | None:
    """Load one server entry from on-disk report metadata or fall back to HTML."""
    metadata_path = server_dir / "report_info.json"
    index_path = server_dir / "index.html"
    if metadata_path.is_file():
        try:
            payload = json.loads(metadata_path.read_text(encoding="utf-8"))
        except json.JSONDecodeError:
            payload = {}
        if isinstance(payload, dict):
            return {
                "server_directory": payload.get("server_directory") or server_dir.name,
                "server_display_name": payload.get("server_display_name") or server_dir.name,
                "server_key": payload.get("server_key"),
                "libraries_audited": payload.get("libraries_audited", 0),
                "media_items_processed": payload.get("media_items_processed", 0),
                "findings_count": payload.get("findings_count", 0),
                "audited_at_text": payload.get("audited_at_text")
                or _file_timestamp_text(index_path),
            }
    if not index_path.is_file():
        return None
    return {
        "server_directory": server_dir.name,
        "server_display_name": server_dir.name,
        "server_key": None,
        "libraries_audited": 0,
        "media_items_processed": 0,
        "findings_count": 0,
        "audited_at_text": _file_timestamp_text(index_path),
    }


def _comparison_index_exists(root_dir: Path) -> bool:
    """Return whether comparison results currently exist on disk."""
    return (comparison_output_dir(root_dir) / "index.html").is_file()


def _file_timestamp_text(path: Path) -> str | None:
    """Return a formatted local timestamp for one file path when it exists."""
    if not path.is_file():
        return None
    return datetime.fromtimestamp(path.stat().st_mtime).astimezone().strftime(
        "%Y-%m-%d %H:%M:%S %Z"
    )
