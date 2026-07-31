"""Library page generation for static audit reports."""

from __future__ import annotations

from . import templates


def render_library_page(
    library_name: str,
    findings: tuple[audit_types.AuditFinding, ...],
    *,
    library_slug_map: dict[str, str],
    finding_id_map: dict[int, str],
) -> str:
    """Return one library report page."""
    errors, warnings, info = templates.finding_count_summary(findings)
    cards = (
        templates.SummaryCard(
            title="Library",
            value=library_name,
            accent="library",
            subtitle="Current library",
            search_text=library_name,
        ),
        templates.SummaryCard(
            title="Findings",
            value=str(len(findings)),
            accent="findings",
            subtitle="Findings in this library",
        ),
        templates.SummaryCard(title="Errors", value=errors, accent="error"),
        templates.SummaryCard(title="Warnings", value=warnings, accent="warning"),
        templates.SummaryCard(title="Information", value=info, accent="info"),
    )

    movies = tuple(finding for finding in findings if not finding.media_item.is_episode)
    episodes = tuple(finding for finding in findings if finding.media_item.is_episode)
    sections: list[str] = []

    if movies:
        sections.append(
            templates.render_details_group(
                title="Movies",
                count=len(movies),
                search_text=f"{library_name} movies",
                body=templates.render_findings_table(
                    templates.sort_findings_by_title(movies),
                    library_slug_map=library_slug_map,
                    finding_id_map=finding_id_map,
                    relative_prefix="../",
                    current_library_name=library_name,
                ),
            )
        )

    for series_name, series_findings in templates.sort_named_groups(
        templates.group_findings_by_series(episodes)
    ):
        season_sections = []
        for season_label, season_findings in templates.sort_season_groups(
            templates.group_findings_by_season(series_findings)
        ):
            ordered_findings = tuple(
                sorted(
                    season_findings,
                    key=lambda finding: (
                        finding.media_item.episode_number is None,
                        finding.media_item.episode_number or 0,
                        finding.media_item.display_name.casefold(),
                    ),
                )
            )
            season_sections.append(
                templates.render_details_group(
                    title=season_label,
                    count=len(season_findings),
                    search_text=f"{series_name} {season_label}",
                    body=templates.render_findings_table(
                        ordered_findings,
                        library_slug_map=library_slug_map,
                        finding_id_map=finding_id_map,
                        relative_prefix="../",
                        current_library_name=library_name,
                    ),
                )
            )

        sections.append(
            templates.render_details_group(
                title=series_name,
                count=len(series_findings),
                search_text=f"{library_name} {series_name}",
                body="\n".join(season_sections),
            )
        )

    if not sections:
        sections.append('    <p class="muted-text">No findings in this library.</p>')

    content = "\n".join(
        (
            templates.render_summary_cards(cards),
            '  <section class="section-card">',
            f"    <h2>{library_name}</h2>",
            "    <p class=\"muted-text\">Episodes are grouped by series and season. Movies are listed alphabetically.</p>",
            *sections,
            "  </section>",
        )
    )

    return templates.render_page(
        title=f"{library_name} Findings",
        current_nav="Libraries",
        relative_prefix="../",
        heading=library_name,
        intro=f"Detailed findings for the {library_name} library.",
        content=content,
    )
