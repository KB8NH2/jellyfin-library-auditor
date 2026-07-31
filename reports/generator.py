"""Top-level static site generation orchestration."""

from __future__ import annotations

import csv
from datetime import datetime
import shutil
from pathlib import Path
from urllib.parse import urlsplit

import audit_types
from config import get_config
from media import get_display_path
from media import has_jellyfin_backdrop
from media import has_jellyfin_logo
from media import has_jellyfin_primary_image
from media import has_jellyfin_thumb
from media import local_backdrop_exists
from media import local_poster_exists
from .category import render_category_page
from .css import write_css
from .dashboard import render_dashboard_page
from .javascript import write_javascript
from .library import render_library_page
from . import templates
from results import AuditServerResult


CSV_HEADER = (
    "Category",
    "Severity",
    "Check",
    "Title",
    "Path",
    "Local Poster",
    "Local Backdrop",
    "Jellyfin Primary",
    "Jellyfin Backdrop",
    "Jellyfin Logo",
    "Jellyfin Thumb",
    "Artwork Source",
    "Message",
)


def write_csv_report(result: AuditServerResult) -> Path:
    """Write a CSV report containing one row per finding."""
    output_path = get_config().reporting.output.audit_csv
    output_path.parent.mkdir(parents=True, exist_ok=True)

    with output_path.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.writer(handle)
        writer.writerow(CSV_HEADER)
        writer.writerows(_csv_rows(result))

    return output_path


def write_html_report(result: AuditServerResult) -> Path:
    """Write a static HTML site for the supplied audit results."""
    return write_reports(result)


def write_reports(result: AuditServerResult) -> Path:
    """Generate the full static site and return the dashboard path."""
    config = get_config()
    site_paths = _site_paths(config.reporting.output.audit_html)
    _prepare_site_root(site_paths.root_dir)
    generated_at_text = _generated_at_text()
    server_display_name = result.server_name or _server_display_name(
        config.jellyfin.server_url
    )

    library_slug_map = _library_slug_map(result)
    category_filenames = {
        category: f"{templates.slugify(category.value)}.html"
        for category in audit_types.AuditCategory
    }
    finding_id_map = {
        id(finding): f"finding-{index}"
        for index, finding in enumerate(templates.sort_findings(result.findings), start=1)
    }

    write_css(site_paths.css_path)
    write_javascript(site_paths.js_path)
    write_dashboard(
        result,
        site_paths=site_paths,
        category_filenames=category_filenames,
        library_slug_map=library_slug_map,
        generated_at_text=generated_at_text,
        server_display_name=server_display_name,
    )
    write_category_pages(
        result,
        site_paths=site_paths,
        category_filenames=category_filenames,
        library_slug_map=library_slug_map,
        finding_id_map=finding_id_map,
    )
    write_library_pages(
        result,
        site_paths=site_paths,
        library_slug_map=library_slug_map,
        finding_id_map=finding_id_map,
    )

    return site_paths.index_path


def write_dashboard(
    result: AuditServerResult,
    *,
    site_paths: templates.SitePaths,
    category_filenames: dict[audit_types.AuditCategory, str],
    library_slug_map: dict[str, str],
    generated_at_text: str,
    server_display_name: str,
) -> None:
    """Write the dashboard page."""
    body = render_dashboard_page(
        result,
        category_filenames=category_filenames,
        library_slug_map=library_slug_map,
        generated_at_text=generated_at_text,
        server_display_name=server_display_name,
    )
    site_paths.index_path.write_text(
        templates.page_document(
            title="Jellyfin Library Auditor Dashboard",
            relative_prefix="",
            body=body,
        ),
        encoding="utf-8",
    )


def write_category_pages(
    result: AuditServerResult,
    *,
    site_paths: templates.SitePaths,
    category_filenames: dict[audit_types.AuditCategory, str],
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
) -> None:
    """Write one page per audit category."""
    grouped = templates.group_findings_by_category(result.findings)
    for category in audit_types.AuditCategory:
        page_path = site_paths.categories_dir / category_filenames[category]
        body = render_category_page(
            category,
            grouped.get(category, ()),
            library_slug_map=library_slug_map,
            finding_id_map=finding_id_map,
        )
        page_path.write_text(
            templates.page_document(
                title=f"{category.value.title()} Findings",
                relative_prefix="../",
                body=body,
            ),
            encoding="utf-8",
        )


def write_library_pages(
    result: AuditServerResult,
    *,
    site_paths: templates.SitePaths,
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
) -> None:
    """Write one page per library."""
    grouped = templates.group_findings_by_library(result.findings)
    library_names = tuple(
        library_result.library.name
        for library_result in sorted(
            result.library_results,
            key=lambda item: item.library.name.casefold(),
        )
    )
    for library_name in library_names:
        page_path = site_paths.libraries_dir / f"{library_slug_map[library_name]}.html"
        body = render_library_page(
            library_name,
            grouped.get(library_name, ()),
            library_slug_map=library_slug_map,
            finding_id_map=finding_id_map,
        )
        page_path.write_text(
            templates.page_document(
                title=f"{library_name} Findings",
                relative_prefix="../",
                body=body,
            ),
            encoding="utf-8",
        )


def _csv_rows(result: AuditServerResult) -> tuple[tuple[str, ...], ...]:
    """Return CSV rows for the supplied audit results."""
    rows = []
    for finding in templates.sort_findings(result.findings):
        item = finding.media_item
        local_poster = local_poster_exists(item)
        local_backdrop = local_backdrop_exists(item)
        jellyfin_primary = has_jellyfin_primary_image(item)
        jellyfin_backdrop = has_jellyfin_backdrop(item)
        rows.append(
            (
                finding.category.value.title(),
                finding.severity.value.title(),
                finding.check_name,
                item.display_name,
                get_display_path(item),
                _yes_no(local_poster),
                _yes_no(local_backdrop),
                _yes_no(jellyfin_primary),
                _yes_no(jellyfin_backdrop),
                _yes_no(has_jellyfin_logo(item)),
                _yes_no(has_jellyfin_thumb(item)),
                _artwork_source(
                    has_local_artwork=local_poster or local_backdrop,
                    has_jellyfin_artwork=jellyfin_primary or jellyfin_backdrop,
                ),
                finding.message,
            )
        )
    return tuple(rows)


def _library_slug_map(result: AuditServerResult) -> dict[str, str]:
    """Return slugs for every audited library."""
    library_names = {
        library_result.library.name
        for library_result in result.library_results
    }
    library_names.update(finding.media_item.library for finding in result.findings)
    return templates.build_slug_map(tuple(library_names))


def _prepare_site_root(root_dir: Path) -> None:
    """Create a clean output root for the static site."""
    if root_dir.exists():
        shutil.rmtree(root_dir)
    root_dir.mkdir(parents=True, exist_ok=True)
    (root_dir / "categories").mkdir(parents=True, exist_ok=True)
    (root_dir / "libraries").mkdir(parents=True, exist_ok=True)
    (root_dir / "css").mkdir(parents=True, exist_ok=True)
    (root_dir / "js").mkdir(parents=True, exist_ok=True)


def _site_paths(configured_path: Path) -> templates.SitePaths:
    """Return normalized site output paths from the configured HTML path."""
    root_dir = configured_path.with_suffix("") if configured_path.suffix else configured_path
    return templates.SitePaths(
        root_dir=root_dir,
        index_path=root_dir / "index.html",
        css_path=root_dir / "css" / "style.css",
        js_path=root_dir / "js" / "report.js",
        categories_dir=root_dir / "categories",
        libraries_dir=root_dir / "libraries",
    )


def _yes_no(value: bool) -> str:
    """Return Yes or No for CSV fields."""
    return "Yes" if value else "No"


def _artwork_source(has_local_artwork: bool, has_jellyfin_artwork: bool) -> str:
    """Return the compact artwork source label for CSV output."""
    if has_local_artwork and has_jellyfin_artwork:
        return "Local + Jellyfin"
    if has_local_artwork:
        return "Local only"
    if has_jellyfin_artwork:
        return "Jellyfin metadata"
    return "Missing"


def _generated_at_text() -> str:
    """Return a display-friendly local timestamp for the dashboard."""
    return datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")


def _server_display_name(server_url: str) -> str:
    """Return a dashboard-friendly server name from the configured URL."""
    parsed_url = urlsplit(server_url.strip())
    if parsed_url.hostname:
        return parsed_url.hostname

    if parsed_url.netloc:
        return parsed_url.netloc

    stripped_url = server_url.strip().rstrip("/")
    return stripped_url or "unknown"
