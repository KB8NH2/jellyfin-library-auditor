"""Dashboard landing page generation for static audit reports."""

from __future__ import annotations

from . import templates


def render_dashboard_page(
    *,
    server_display_name: str,
    generated_at_text: str,
    csv_report_href: str,
    libraries_audited: int,
    media_items_processed: int,
    actionable_findings_count: int,
    library_cards: tuple[templates.SummaryCard, ...],
    check_cards: tuple[templates.SummaryCard, ...],
) -> str:
    """Return the dashboard landing page HTML body."""
    summary_cards = (
        templates.SummaryCard("Server Name", server_display_name, "server", subtitle="Jellyfin server"),
        templates.SummaryCard("Newest Report", generated_at_text, "timestamp", subtitle="Generated timestamp"),
        templates.SummaryCard("Download CSV", "Open", "check", href=csv_report_href, subtitle="Spreadsheet export"),
        templates.SummaryCard("Libraries Audited", str(libraries_audited), "libraries"),
        templates.SummaryCard("Media Items", str(media_items_processed), "media"),
        templates.SummaryCard("Actionable Findings", str(actionable_findings_count), "findings"),
    )
    return templates.render_page(
        title="Dashboard",
        current_nav="Dashboard",
        relative_prefix="",
        heading=f"Jellyfin Library Auditor ({server_display_name})",
        intro="Use these pages to find missing artwork, subtitles, and metadata.",
        breadcrumbs=(templates.Breadcrumb("Dashboard"),),
        include_search=False,
        include_expand_controls=False,
        server_display_name=server_display_name,
        content="\n".join(
            (
                templates.render_summary_cards(summary_cards),
                _card_section("Libraries", "Open a compact maintenance view for each library.", library_cards, section_id="libraries-overview"),
                _card_section("Audit Checks", "Fix one class of issue at a time.", check_cards, section_id="checks-overview"),
            )
        ),
    )


def _card_section(title: str, subtitle: str, cards: tuple[templates.SummaryCard, ...], *, section_id: str) -> str:
    """Return a dashboard card section."""
    body = templates.render_summary_cards(cards) if cards else '    <p class="muted-text">No actionable items.</p>'
    return "\n".join(
        (
            f'  <section class="section-card" id="{section_id}">',
            f"    <h2>{title}</h2>",
            f'    <p class="muted-text">{subtitle}</p>',
            body,
            "  </section>",
        )
    )
