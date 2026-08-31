"""Tests for the standalone transfer_metadata.py/transfer_images.py CLIs."""

from __future__ import annotations

import contextlib
import io
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch

import config
import transfer_images
import transfer_metadata

from tests.helpers import _make_left_right_app_config


class TransferMetadataMergeTests(unittest.TestCase):
    def test_overwrites_only_transferable_fields(self) -> None:
        source_dto = {
            "Id": "source-id",
            "Name": "Correct Title",
            "Overview": "Correct overview",
            "Genres": ["Drama"],
            "ImageTags": {"Primary": "sourcetag"},
        }
        destination_dto = {
            "Id": "dest-id",
            "ServerId": "dest-server",
            "Path": "/media/dest/file.mkv",
            "Name": "Wrong Title",
            "Overview": "Wrong overview",
            "Genres": [],
            "ImageTags": {"Primary": "desttag"},
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["Name"], "Correct Title")
        self.assertEqual(merged["Overview"], "Correct overview")
        self.assertEqual(merged["Genres"], ["Drama"])
        self.assertEqual(merged["Id"], "dest-id")
        self.assertEqual(merged["ServerId"], "dest-server")
        self.assertEqual(merged["Path"], "/media/dest/file.mkv")
        self.assertEqual(merged["ImageTags"], {"Primary": "desttag"})

    def test_omits_non_editable_fields_even_when_present_on_destination(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title"}
        destination_dto = {
            "Id": "dest-id",
            "Name": "Wrong Title",
            "Trickplay": {"abc": {"1": {"whatever": True}}},
            "MediaSources": [{"Id": "media-source"}],
            "UserData": {"Played": True},
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertNotIn("Trickplay", merged)
        self.assertNotIn("MediaSources", merged)
        self.assertNotIn("UserData", merged)

    def test_preserves_identity_and_hierarchy_fields_not_in_transferable_set(self) -> None:
        """Regression test: fields like IndexNumber must never be dropped from the
        outgoing payload, since Jellyfin's update endpoint clears any field the
        request omits rather than leaving it untouched."""
        source_dto = {"Id": "source-id", "Name": "Correct Title"}
        destination_dto = {
            "Id": "dest-id",
            "Name": "Wrong Title",
            "Type": "Episode",
            "SeriesId": "series-id",
            "SeasonId": "season-id",
            "IndexNumber": 6,
            "ParentIndexNumber": 1,
            "Path": "/media/dest/Show/Season 01/Show.S01E06.mkv",
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["Type"], "Episode")
        self.assertEqual(merged["SeriesId"], "series-id")
        self.assertEqual(merged["SeasonId"], "season-id")
        self.assertEqual(merged["IndexNumber"], 6)
        self.assertEqual(merged["ParentIndexNumber"], 1)
        self.assertEqual(merged["Path"], "/media/dest/Show/Season 01/Show.S01E06.mkv")

    def test_explicit_null_on_source_does_not_clobber_destination_value(self) -> None:
        """Regression test: Jellyfin returns explicit nulls for unset fields (e.g. an
        episode with no standalone ProductionYear), and that null must not overwrite
        a real value the destination already has."""
        source_dto = {"Id": "source-id", "Name": "Correct Title", "ProductionYear": None}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title", "ProductionYear": 2019}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["ProductionYear"], 2019)

    def test_transfers_episode_and_season_number_when_source_has_a_value(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title", "IndexNumber": 9, "ParentIndexNumber": 1}
        destination_dto = {
            "Id": "dest-id",
            "Name": "Wrong Title",
            "IndexNumber": 10,
            "ParentIndexNumber": 2,
        }

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["IndexNumber"], 9)
        self.assertEqual(merged["ParentIndexNumber"], 1)

    def test_null_episode_number_on_source_does_not_clobber_destination_value(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title", "IndexNumber": None}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title", "IndexNumber": 10}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["IndexNumber"], 10)

    def test_leaves_destination_value_when_source_field_missing(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Only Name"}
        destination_dto = {"Id": "dest-id", "Name": "Old Name", "Overview": "Kept overview"}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["Name"], "Only Name")
        self.assertEqual(merged["Overview"], "Kept overview")

    def test_locks_name_when_it_actually_changes(self) -> None:
        """Regression test: without locking a changed Name, a library with an
        internet metadata provider enabled can silently revert the transfer
        on its next refresh, and (separately) a subsequent audit's
        whole-library listing can keep showing the destination's pre-
        transfer title for a while even though the transfer succeeded."""
        source_dto = {"Id": "source-id", "Name": "Correct Title"}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title"}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["LockedFields"], ["Name"])

    def test_does_not_lock_name_when_it_already_matches(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Same Title", "Overview": "New overview"}
        destination_dto = {"Id": "dest-id", "Name": "Same Title", "Overview": "Old overview"}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertNotIn("LockedFields", merged)

    def test_preserves_existing_locked_fields_when_locking_name(self) -> None:
        source_dto = {"Id": "source-id", "Name": "Correct Title"}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title", "LockedFields": ["Genres"]}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertEqual(merged["LockedFields"], ["Genres", "Name"])

    def test_never_locks_original_title(self) -> None:
        """Regression test: Jellyfin's LockedFields enum has no OriginalTitle
        member - locking it fails the entire update with a 400."""
        source_dto = {"Id": "source-id", "Name": "Correct Title", "OriginalTitle": "Original"}
        destination_dto = {"Id": "dest-id", "Name": "Wrong Title", "OriginalTitle": "Different"}

        merged = transfer_metadata.build_merged_item_dto(source_dto, destination_dto)

        self.assertNotIn("OriginalTitle", merged["LockedFields"])


class SkippedNullSourceFieldsTests(unittest.TestCase):
    def test_reports_fields_with_no_source_value(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title", "IndexNumber": None}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "IndexNumber": 10}

        skipped = transfer_metadata._skipped_null_source_fields(source_dto, destination_dto)

        self.assertEqual(skipped, ("IndexNumber",))

    def test_omits_fields_where_destination_also_has_no_value(self) -> None:
        source_dto = {"Id": "src", "IndexNumber": None}
        destination_dto = {"Id": "dst", "IndexNumber": None}

        skipped = transfer_metadata._skipped_null_source_fields(source_dto, destination_dto)

        self.assertEqual(skipped, ())

    def test_omits_fields_the_source_actually_has_a_value_for(self) -> None:
        source_dto = {"Id": "src", "IndexNumber": 9}
        destination_dto = {"Id": "dst", "IndexNumber": 10}

        skipped = transfer_metadata._skipped_null_source_fields(source_dto, destination_dto)

        self.assertEqual(skipped, ())


class TransferImagesPlanTests(unittest.TestCase):
    class _FakeClient:
        def __init__(self, images: dict[tuple[str, str], tuple[bytes, str]]):
            self._images = images
            self.uploaded: list[tuple[str, str, bytes, str]] = []

        def get_item_image(self, item_id: str, image_type: str):
            return self._images.get((item_id, image_type))

        def upload_item_image(self, item_id: str, image_type: str, image_bytes: bytes, content_type: str) -> None:
            self.uploaded.append((item_id, image_type, image_bytes, content_type))

    def test_plan_reads_source_image(self) -> None:
        from_client = self._FakeClient({("src", "Primary"): (b"bytes", "image/jpeg")})
        to_client = self._FakeClient({})

        plan = transfer_images.plan_image_transfer(from_client, to_client, "src", "dst", "Primary")

        self.assertTrue(plan.has_image)
        self.assertEqual(plan.image_bytes, b"bytes")
        self.assertEqual(plan.content_type, "image/jpeg")

    def test_plan_has_no_image_when_source_lacks_one(self) -> None:
        from_client = self._FakeClient({})
        to_client = self._FakeClient({})

        plan = transfer_images.plan_image_transfer(from_client, to_client, "src", "dst", "Backdrop")

        self.assertFalse(plan.has_image)
        self.assertIsNone(plan.image_bytes)
        self.assertIsNone(plan.content_type)

    def test_apply_uploads_planned_image_to_destination(self) -> None:
        from_client = self._FakeClient({("src", "Primary"): (b"bytes", "image/jpeg")})
        to_client = self._FakeClient({})
        plan = transfer_images.plan_image_transfer(from_client, to_client, "src", "dst", "Primary")

        transfer_images.apply_image_transfer(to_client, plan)

        self.assertEqual(to_client.uploaded, [("dst", "Primary", b"bytes", "image/jpeg")])


class TransferImageCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "transfer_images.IMAGE_TRANSFER_LOG_FILE",
            Path(temp_dir.name) / "image_transfer.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
        return _make_left_right_app_config()

    def _make_fake_client(self, source_dto, destination_dtos_by_call, images, upload_calls):
        """``destination_dtos_by_call`` lets get_item("dst") return a different
        dto before vs. after the upload, so tests can assert the post-upload
        ImageTags re-check actually reflects the new value."""
        calls = {"get_destination": 0}

        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_item(self, item_id):
                if self.server.key == "left":
                    return source_dto
                index = min(calls["get_destination"], len(destination_dtos_by_call) - 1)
                calls["get_destination"] += 1
                return destination_dtos_by_call[index]

            def get_item_image(self, item_id, image_type):
                return images.get((item_id, image_type))

            def upload_item_image(self, item_id, image_type, image_bytes, content_type):
                upload_calls.append((self.server.key, item_id, image_type, image_bytes, content_type))

        return FakeClient

    def test_transfers_image_after_confirmation_and_reports_new_tag(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_before = {"Id": "dst", "Name": "Correct Title", "ImageTags": {}}
        destination_after = {"Id": "dst", "Name": "Correct Title", "ImageTags": {"Primary": "new-tag"}}
        images = {("src-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(
            source_dto, [destination_before, destination_after], images, upload_calls
        )

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = transfer_images.transfer_image(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        image_type="Primary",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [("right", "dst-id", "Primary", b"bytes", "image/jpeg")])

    def test_reports_when_source_has_no_image(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "ImageTags": {}}
        upload_calls: list = []
        fake_client = self._make_fake_client(source_dto, [destination_dto], {}, upload_calls)

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                exit_code = transfer_images.transfer_image(
                    from_server_key="left",
                    from_item_id="src-id",
                    to_server_key="right",
                    to_item_id="dst-id",
                    image_type="Backdrop",
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 0)
        self.assertEqual(upload_calls, [])

    def test_aborts_when_confirmation_declined(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "ImageTags": {}}
        images = {("src-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(source_dto, [destination_dto], images, upload_calls)

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="n"):
                    exit_code = transfer_images.transfer_image(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        image_type="Primary",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 1)
        self.assertEqual(upload_calls, [])

    def test_writes_transfer_details_to_log_file(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Correct Title", "ImageTags": {"Primary": "new-tag"}}
        images = {("src-id", "Primary"): (b"bytes", "image/jpeg")}
        upload_calls: list = []
        fake_client = self._make_fake_client(source_dto, [destination_dto], images, upload_calls)

        with patch("transfer_images.get_config", return_value=self._make_config()):
            with patch("transfer_images.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    transfer_images.transfer_image(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        image_type="Primary",
                        assume_yes=False,
                    )

        log_text = transfer_images.IMAGE_TRANSFER_LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("Upload complete", log_text)
        self.assertIn("new-tag", log_text)


class TransferMetadataCommandTests(unittest.TestCase):
    def setUp(self) -> None:
        temp_dir = TemporaryDirectory()
        self.addCleanup(temp_dir.cleanup)
        log_patch = patch(
            "transfer_metadata.METADATA_TRANSFER_LOG_FILE",
            Path(temp_dir.name) / "metadata_transfer.log",
        )
        log_patch.start()
        self.addCleanup(log_patch.stop)

    def _make_config(self) -> config.AppConfig:
        return _make_left_right_app_config()

    def _make_fake_client(self, source_dto, destination_dto, update_calls):
        class FakeClient:
            def __init__(self, server, **kwargs):
                self.server = server

            def __enter__(self):
                return self

            def __exit__(self, *args):
                return False

            def get_item(self, item_id):
                return source_dto if self.server.key == "left" else destination_dto

            def update_item(self, item_id, item_dto):
                update_calls.append((item_id, item_dto))

        return FakeClient

    def test_transfers_metadata_after_confirmation(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)
        updated_item_id, updated_dto = update_calls[0]
        self.assertEqual(updated_item_id, "dst-id")
        self.assertEqual(updated_dto["Name"], "Correct Title")
        self.assertEqual(updated_dto["Id"], "dst")

    def test_writes_transfer_details_to_log_file(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        log_contents = transfer_metadata.METADATA_TRANSFER_LOG_FILE.read_text(encoding="utf-8")
        self.assertIn("Transfer metadata: Left -> Right", log_contents)
        self.assertIn("Name: 'Wrong Title' -> 'Correct Title'", log_contents)
        self.assertIn("Metadata transfer complete.", log_contents)

    def test_appends_across_multiple_runs_instead_of_truncating(self) -> None:
        source_dto = {"Id": "src", "Name": "Same Title"}
        destination_dto = {"Id": "dst", "Name": "Same Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                transfer_metadata.transfer_metadata(
                    from_server_key="left", from_item_id="src-id",
                    to_server_key="right", to_item_id="dst-id", assume_yes=True,
                )
                transfer_metadata.transfer_metadata(
                    from_server_key="left", from_item_id="src-id",
                    to_server_key="right", to_item_id="dst-id", assume_yes=True,
                )

        log_contents = transfer_metadata.METADATA_TRANSFER_LOG_FILE.read_text(encoding="utf-8")
        self.assertEqual(log_contents.count("Transfer metadata: Left -> Right"), 2)

    def test_aborts_when_user_declines_confirmation(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="n"):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])

    def test_assume_yes_skips_confirmation_prompt(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called when assume_yes=True")

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", side_effect=_unexpected_input):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=True,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(len(update_calls), 1)

    def test_no_prompt_when_nothing_to_transfer(self) -> None:
        source_dto = {"Id": "src", "Name": "Same Title"}
        destination_dto = {"Id": "dst", "Name": "Same Title", "Path": "/media/dst/file.mkv"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        def _unexpected_input(prompt: str = "") -> str:
            raise AssertionError("input() should not be called when there is nothing to transfer")

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", side_effect=_unexpected_input):
                    exit_code = transfer_metadata.transfer_metadata(
                        from_server_key="left",
                        from_item_id="src-id",
                        to_server_key="right",
                        to_item_id="dst-id",
                        assume_yes=False,
                    )

        self.assertEqual(exit_code, 0)
        self.assertEqual(update_calls, [])

    def test_prints_note_when_source_has_no_value_for_a_field(self) -> None:
        source_dto = {"Id": "src", "Name": "Correct Title", "IndexNumber": None}
        destination_dto = {
            "Id": "dst",
            "Name": "Wrong Title",
            "Path": "/media/dst/file.mkv",
            "IndexNumber": 10,
        }
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        captured_stdout = io.StringIO()
        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                with patch("builtins.input", return_value="y"):
                    with contextlib.redirect_stdout(captured_stdout):
                        exit_code = transfer_metadata.transfer_metadata(
                            from_server_key="left",
                            from_item_id="src-id",
                            to_server_key="right",
                            to_item_id="dst-id",
                            assume_yes=False,
                        )

        self.assertEqual(exit_code, 0)
        output = captured_stdout.getvalue()
        self.assertIn("no value for these fields", output)
        self.assertIn("IndexNumber", output)
        self.assertEqual(len(update_calls), 1)
        self.assertEqual(update_calls[0][1]["IndexNumber"], 10)

    def test_refuses_to_update_when_destination_path_missing(self) -> None:
        """Regression test: Jellyfin cleared Path on an update whose payload omitted
        it, turning a real episode into a pathless "virtual" item that Jellyfin's
        library scanner then deleted. The transfer must refuse rather than repeat
        that."""
        source_dto = {"Id": "src", "Name": "Correct Title"}
        destination_dto = {"Id": "dst", "Name": "Wrong Title"}
        update_calls: list = []
        fake_client = self._make_fake_client(source_dto, destination_dto, update_calls)

        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            with patch("transfer_metadata.JellyfinClient", fake_client):
                exit_code = transfer_metadata.transfer_metadata(
                    from_server_key="left",
                    from_item_id="src-id",
                    to_server_key="right",
                    to_item_id="dst-id",
                    assume_yes=True,
                )

        self.assertEqual(exit_code, 1)
        self.assertEqual(update_calls, [])

    def test_unknown_server_key_returns_usage_error(self) -> None:
        with patch("transfer_metadata.get_config", return_value=self._make_config()):
            exit_code = transfer_metadata.transfer_metadata(
                from_server_key="missing",
                from_item_id="x",
                to_server_key="right",
                to_item_id="y",
                assume_yes=True,
            )

        self.assertEqual(exit_code, 2)
