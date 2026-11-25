import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from fc27.errors import FC27Error
from fc27.gallery import GallerySync


class GallerySyncTests(unittest.TestCase):
    @patch("fc27.gallery.time.sleep")
    def test_paginates_and_preserves_snapshot_after_failed_refresh(self, _sleep):
        read = Mock(side_effect=[
            {"cards": [
                {"card_ea_id": 11, "is_collected": False},
                {"card_ea_id": 12, "is_collected": True, "grading_score": 500},
            ]},
            FC27Error("BRIDGE_TIMEOUT", "Browser stopped responding.", retryable=True),
            FC27Error("BRIDGE_NOT_CONNECTED", "Browser is reconnecting.", retryable=True),
            {"cards": [{"card_ea_id": 13, "is_collected": True}]},
            {"cards": []},
            FC27Error("EA_PERSONA_CHANGED", "EA Persona changed."),
        ])
        sync = GallerySync(read)
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(persona_id="123", path=Path(directory) / "runtime.sqlite")
            with patch("fc27.gallery.threading.Thread") as thread:
                self.assertEqual(sync.start(runtime)["state"], "running")
                sync.start(runtime)
                thread.assert_called_once()
            sync._sync(runtime)
            first = sync.query(runtime, 0, 1)
            self.assertEqual(first["sync"]["state"], "complete")
            self.assertEqual(first["scanned"], 3)
            self.assertEqual(first["total"], 2)
            self.assertEqual(first["cards"][0]["grading_score"], 500)
            self.assertEqual(first["next_offset"], 1)
            self.assertEqual([call.args[1]["offset"] for call in read.call_args_list], [0, 2, 2, 2, 3])
            self.assertNotIn("retrying_offset", first["sync"])

            sync._sync(runtime)
            saved = sync.query(runtime, 1, 1)
            self.assertEqual(saved["sync"]["error"]["code"], "EA_PERSONA_CHANGED")
            self.assertEqual(saved["snapshot_at"], first["snapshot_at"])
            self.assertEqual(saved["cards"][0]["card_ea_id"], 13)

    @patch("fc27.gallery.time.sleep")
    def test_accepts_confirmed_repeated_tail_after_pagination_advances(self, _sleep):
        first = {"cards": [{"card_ea_id": 11, "is_collected": False}]}
        second = {"cards": [{"card_ea_id": 12, "is_collected": True}]}
        read = Mock(side_effect=[first, second, second, second])
        sync = GallerySync(read)
        cards, scanned = sync._read("123")
        self.assertEqual(scanned, 2)
        self.assertEqual([card["card_ea_id"] for card in cards], [12])
        self.assertEqual([call.args[1]["offset"] for call in read.call_args_list], [0, 1, 2, 3])

    @patch("fc27.gallery.time.sleep")
    def test_rejects_search_that_never_moves_past_the_first_page(self, _sleep):
        page = {"cards": [{"card_ea_id": 11, "is_collected": True}]}
        sync = GallerySync(Mock(return_value=page))
        with self.assertRaises(FC27Error) as raised:
            sync._read("123")
        self.assertEqual(raised.exception.details["reason"], "pagination_did_not_advance")

    @patch("fc27.gallery.time.sleep")
    def test_does_not_commit_when_collection_flags_are_missing(self, _sleep):
        sync = GallerySync(Mock(side_effect=[
            {"cards": [{"card_ea_id": 11, "is_collected": None}]},
            {"cards": []},
        ]))
        with tempfile.TemporaryDirectory() as directory:
            runtime = SimpleNamespace(persona_id="123", path=Path(directory) / "runtime.sqlite")
            with patch("fc27.gallery.threading.Thread"):
                sync.start(runtime)
            sync._sync(runtime)
            result = sync.query(runtime, 0, 50)
            self.assertEqual(result["sync"]["error"]["code"], "GALLERY_SCAN_INCOMPLETE")
            self.assertIsNone(result["snapshot_at"])
