"""Dashboard page generation for static audit reports."""

from __future__ import annotations

import audit_types
from html import escape
from results import AuditServerResult
from . import templates


def render_dashboard_page(
    result: AuditServerResult,
    *,
    category_filenames: dict[audit_types.AuditCategory, str],
    library_slug_map: dict[str, str],
    generated_at_text: str,
) -> str:
    """Return the dashboard page HTML body."""
    error_count, warning_count, info_count = templates.finding_count_summary(result.findings)
    summary_cards = (
        templates.SummaryCard(
            title="Libraries",
            value=str(result.libraries_audited),
            accent="libraries",
            href="#libraries-overview",
            subtitle="Audited libraries",
            search_text="libraries audited",
        ),
        templates.SummaryCard(
            title="Media Items",
            value=str(result.media_items_processed),
            accent="media",
            subtitle="Items processed",
            search_text="media items processed",
        ),
        templates.SummaryCard(
            title="Total Findings",
            value=str(result.findings_count),
            accent="findings",
            subtitle="All findings",
            search_text="total findings",
        ),
        templates.SummaryCard(
            title="Errors",
            value=error_count,
            accent="error",
            subtitle="Error findings",
            search_text="errors",
        ),
        templates.SummaryCard(
            title="Warnings",
            value=warning_count,
            accent="warning",
            subtitle="Warning findings",
            search_text="warnings",
        ),
        templates.SummaryCard(
            title="Information",
            value=info_count,
            accent="info",
            subtitle="Info findings",
            search_text="information",
        ),
    )

    return templates.render_page(
        title="Dashboard",
        current_nav="Dashboard",
        relative_prefix="",
        heading="Jellyfin Library Auditor",
        intro=(
            "Dashboard overview for the latest Jellyfin library audit run. "
            f"Generated {generated_at_text}."
        ),
        content="\n".join(
            (
                templates.render_summary_cards(summary_cards),
                _summary_panels(result, category_filenames),
                _category_cards(result, category_filenames),
                _library_cards(result, library_slug_map),
            )
        ),
    )


def _summary_panels(
    result: AuditServerResult,
    category_filenames: dict[audit_types.AuditCategory, str],
) -> str:
    """Return dashboard summary panels."""
    category_rows = []
    grouped_categories = templates.group_findings_by_category(result.findings)
    for category in audit_types.AuditCategory:
        findings = grouped_categories.get(category, ())
        filename = category_filenames[category]
        category_rows.append(
            "      <li>"
            f'<a href="categories/{filename}">{escape(category.value.title())}</a>: {len(findings)}'
            "</li>"
        )

    severity_rows = []
    grouped_severity = templates.group_findings_by_severity(result.findings)
    for severity in sorted(
        audit_types.AuditSeverity,
        key=lambda item: templates.SEVERITY_SORT_ORDER[item],
    ):
        severity_rows.append(
            "      <li>"
            f"{severity.value.title()}: {len(grouped_severity.get(severity, ()))}"
            "</li>"
        )

    return "\n".join(
        (
            '  <section class="panel-grid">',
            '    <article class="panel-card">',
            "      <h2>Server Summary</h2>",
            '      <div class="metric-pairs">',
            f"        <div><span>Libraries audited</span><strong>{result.libraries_audited}</strong></div>",
            f"        <div><span>Media items processed</span><strong>{result.media_items_processed}</strong></div>",
            f"        <div><span>Total findings</span><strong>{result.findings_count}</strong></div>",
            "      </div>",
            "    </article>",
            '    <article class="panel-card">',
            "      <h2>Findings by Severity</h2>",
            "      <ul class=\"summary-list\">",
            *severity_rows,
            "      </ul>",
            "    </article>",
            '    <article class="panel-card">',
            "      <h2>Findings by Category</h2>",
            "      <ul class=\"summary-list\">",
            *category_rows,
            "      </ul>",
            "    </article>",
            "  </section>",
        )
    )


def _category_cards(
    result: AuditServerResult,
    category_filenames: dict[audit_types.AuditCategory, str],
) -> str:
    """Return large category links on the dashboard."""
    grouped = templates.group_findings_by_category(result.findings)
    cards = []
    for category in audit_types.AuditCategory:
        findings = grouped.get(category, ())
        cards.append(
            templates.SummaryCard(
                title=category.value.title(),
                value=str(len(findings)),
                accent="category",
                href=f"categories/{category_filenames[category]}",
                subtitle="Category findings",
                search_text=f"category {category.value}",
            )
        )

    return "\n".join(
        (
            '  <section class="section-card" id="categories-overview">',
            "    <h2>Categories</h2>",
            "    <p class=\"muted-text\">Open one page per audit category.</p>",
            templates.render_summary_cards(tuple(cards)),
            "  </section>",
        )
    )


def _library_cards(
    result: AuditServerResult,
    library_slug_map: dict[str, str],
) -> str:
    """Return large library links on the dashboard."""
    grouped = templates.group_findings_by_library(result.findings)
    cards = []
    for library_result in sorted(
        result.library_results,
        key=lambda item: item.library.name.casefold(),
    ):
        library_name = library_result.library.name
        cards.append(
            templates.SummaryCard(
                title=library_name,
                value=str(len(grouped.get(library_name, ()))),
                accent="library",
                href=f"libraries/{library_slug_map[library_name]}.html",
                subtitle=f"{library_result.media_items_processed} items processed",
                search_text=f"library {library_name}",
            )
        )

    return "\n".join(
        (
            '  <section class="section-card" id="libraries-overview">',
            "    <h2>Libraries</h2>",
            "    <p class=\"muted-text\">Open one page per audited library.</p>",
            templates.render_summary_cards(tuple(cards)),
            "  </section>",
        )
    )
