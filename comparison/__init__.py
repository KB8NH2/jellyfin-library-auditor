"""Static comparison report generation for Jellyfin audit results."""

from .generator import MetadataTransferResult
from .generator import MetadataTransferTarget
from .generator import mismatched_metadata_transfer_targets
from .generator import write_comparison_reports

__all__ = [
    "MetadataTransferResult",
    "MetadataTransferTarget",
    "mismatched_metadata_transfer_targets",
    "write_comparison_reports",
]
