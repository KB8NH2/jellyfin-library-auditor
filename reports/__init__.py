"""Static report generation package for Jellyfin Library Auditor."""

from .generator import write_csv_report
from .generator import write_html_report

__all__ = [
    "write_csv_report",
    "write_html_report",
]
