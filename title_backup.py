"""Shared Name/OriginalTitle backup-and-rename logic for the apply_*_titles tools.

Both apply_episode_titles.py and apply_titles_from_filename.py rename an
item's Name and need the exact same LockedFields safety handling to keep the
rename from being silently reverted by an internet metadata provider's next
refresh - see transfer_metadata.lock_changed_fields() for why. Extracted
here so that logic, and the OriginalTitle backup/restore convention built on
top of it, is defined in exactly one place rather than copy-pasted per tool.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from transfer_metadata import NON_EDITABLE_ITEM_FIELDS
from transfer_metadata import lock_changed_fields as _lock_changed_fields


def build_title_merged_item_dto(
    destination_dto: Mapping[str, Any],
    target_name: str,
) -> dict[str, Any]:
    """Return the destination item document with its Name renamed.

    Mirrors transfer_metadata.build_merged_item_dto: starts from a full copy
    of the destination document minus NON_EDITABLE_ITEM_FIELDS. Before
    overwriting Name, the item's current Name is copied into OriginalTitle,
    the same backup convention apply_dvd_metadata.py uses - this does mean a
    genuine original-language title already stored in OriginalTitle is
    overwritten, since these tools repurpose that field as their own undo
    backup.

    Args:
        destination_dto: Full item document read from Jellyfin.
        target_name: Title to rename this item to.

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }
    merged_dto["OriginalTitle"] = destination_dto.get("Name")
    merged_dto["Name"] = target_name

    _lock_changed_fields(destination_dto, merged_dto, ["OriginalTitle", "Name"])
    return merged_dto


def build_title_restore_merged_item_dto(
    destination_dto: Mapping[str, Any],
    original_title: str,
) -> dict[str, Any]:
    """Return the destination item document with its Name restored from OriginalTitle.

    Sets Name back to the item's own OriginalTitle - the backup
    build_title_merged_item_dto writes there before an earlier rename -
    undoing that rename. OriginalTitle itself is left untouched: there is
    nothing further to preserve once Name is already back to what it held
    before.

    Args:
        destination_dto: Full item document read from Jellyfin.
        original_title: The item's own OriginalTitle backup value.

    Returns:
        A new item document ready to send back to the server.
    """
    merged_dto = {
        field: value
        for field, value in destination_dto.items()
        if field not in NON_EDITABLE_ITEM_FIELDS
    }
    merged_dto["Name"] = original_title

    _lock_changed_fields(destination_dto, merged_dto, ["Name"])
    return merged_dto


__all__ = [
    "build_title_merged_item_dto",
    "build_title_restore_merged_item_dto",
]
