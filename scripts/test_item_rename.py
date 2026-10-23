from __future__ import annotations

import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from smart_desktop import OrganizerModel


class FakeStore:
    def __init__(self, desktop: Path) -> None:
        self.saved = 0
        self.lock = threading.RLock()
        self.data = {
            "desktop": str(desktop),
            "overrides": {}, "pending_inbox": {}, "seen": {}, "visual_assets": {},
            "markdown_summaries": {}, "document_summaries": {}, "folder_summaries": {},
            "tip_summaries": {}, "item_metadata": {}, "item_orders": {},
            "custom_blocks": [], "category_modes": {}, "category_sorting": {},
            "ui": {"category_names": {}},
        }

    def save(self) -> None:
        self.saved += 1


class ItemRenameTests(unittest.TestCase):
    def test_rename_preserves_extension_and_filename_keyed_metadata(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary)
            original = desktop / "报告.txt"
            original.write_text("content", encoding="utf-8")
            store = FakeStore(desktop)
            old_key = original.name.casefold()
            for field in (
                "overrides", "seen", "visual_assets", "markdown_summaries",
                "document_summaries", "folder_summaries", "tip_summaries", "item_metadata",
            ):
                store.data[field][old_key] = {"field": field}
            store.data["pending_inbox"][old_key] = {"path": str(original)}
            store.data["item_orders"]["work"] = ["other.txt", old_key]
            model = OrganizerModel(store)  # type: ignore[arg-type]

            with patch("smart_desktop.COMMON_DESKTOP", desktop / "missing"):
                result = model.rename_item(str(original), "最终报告.txt")

            renamed = desktop / "最终报告.txt"
            self.assertEqual("最终报告.txt", result["new_name"])
            self.assertTrue(renamed.exists())
            self.assertFalse(original.exists())
            new_key = renamed.name.casefold()
            for field in (
                "overrides", "pending_inbox", "seen", "visual_assets", "markdown_summaries",
                "document_summaries", "folder_summaries", "tip_summaries", "item_metadata",
            ):
                self.assertNotIn(old_key, store.data[field])
                self.assertIn(new_key, store.data[field])
            self.assertEqual(str(renamed), store.data["pending_inbox"][new_key]["path"])
            self.assertEqual(["other.txt", new_key], store.data["item_orders"]["work"])
            self.assertEqual(1, store.saved)

    def test_rename_rejects_existing_and_windows_reserved_names(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            desktop = Path(temporary)
            original = desktop / "one.txt"
            original.write_text("one", encoding="utf-8")
            (desktop / "two.txt").write_text("two", encoding="utf-8")
            model = OrganizerModel(FakeStore(desktop))  # type: ignore[arg-type]
            with patch("smart_desktop.COMMON_DESKTOP", desktop / "missing"):
                with self.assertRaisesRegex(ValueError, "已存在"):
                    model.rename_item(str(original), "two")
                with self.assertRaisesRegex(ValueError, "保留名称"):
                    model.rename_item(str(original), "CON")
            self.assertTrue(original.exists())


if __name__ == "__main__":
    unittest.main()
