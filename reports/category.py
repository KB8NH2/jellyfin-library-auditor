"""Category page generation for static audit reports."""

from __future__ import annotations

import audit_types
from . import templates


def render_category_page(
    category: audit_types.AuditCategory,
    findings: tuple[audit_types.AuditFinding, ...],
    *,
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
) -> str:
    """Return one category report page."""
    errors, warnings, info = templates.finding_count_summary(findings)
    affected_libraries = len(templates.group_findings_by_library(findings))
    cards = (
        templates.SummaryCard(
            title="Category",
            value=category.value.title(),
            accent="category",
            href=None,
            subtitle="Current audit category",
            search_text=category.value,
        ),
        templates.SummaryCard(
            title="Findings",
            value=str(len(findings)),
            accent="findings",
            subtitle="Findings in this category",
        ),
        templates.SummaryCard(
            title="Libraries",
            value=str(affected_libraries),
            accent="libraries",
            subtitle="Affected libraries",
        ),
        templates.SummaryCard(title="Errors", value=errors, accent="error"),
        templates.SummaryCard(title="Warnings", value=warnings, accent="warning"),
        templates.SummaryCard(title="Information", value=info, accent="info"),
    )

    grouped = templates.group_findings_by_library(findings)
    library_sections = []
    for library_name, library_findings in templates.sort_named_groups(grouped):
        library_sections.append(
            templates.render_details_group(
                title=library_name,
                count=len(library_findings),
                search_text=library_name,
                body=templates.render_findings_table(
                    templates.sort_findings_by_title(library_findings),
                    library_slug_map=library_slug_map,
                    finding_id_map=finding_id_map,
                    relative_prefix="../",
                ),
            )
        )

    section_lines = [
        templates.render_summary_cards(cards),
        '  <section class="section-card">',
        f"    <h2>{category.value.title()}</h2>",
        "    <p class=\"muted-text\">Findings are grouped by library and sorted by title.</p>",
        *(library_sections or ['    <p class="muted-text">No findings in this category.</p>']),
        "  </section>",
    ]
    content = "\n".join(section_lines)

    return templates.render_page(
        title=f"{category.value.title()} Findings",
        current_nav="Categories",
        relative_prefix="../",
        heading=f"{category.value.title()} Findings",
        intro=f"Detailed findings for the {category.value.title()} audit category.",
        content=content,
    )
