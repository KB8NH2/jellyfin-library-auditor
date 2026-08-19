"""Static comparison report generation for Jellyfin audit results."""

from .generator import ImageTransferResult
from .generator import ImageTransferTarget
from .generator import MetadataTransferResult
from .generator import MetadataTransferTarget
from .generator import SubtitleTransferResult
from .generator import SubtitleTransferTarget
from .generator import comparison_summary_counts
from .generator import mismatched_metadata_transfer_targets
from .generator import missing_image_transfer_targets
from .generator import missing_subtitle_transfer_targets
from .generator import write_comparison_reports

__all__ = [
    "ImageTransferResult",
    "ImageTransferTarget",
    "MetadataTransferResult",
    "MetadataTransferTarget",
    "SubtitleTransferResult",
    "SubtitleTransferTarget",
    "comparison_summary_counts",
    "mismatched_metadata_transfer_targets",
    "missing_image_transfer_targets",
    "missing_subtitle_transfer_targets",
    "write_comparison_reports",
]
