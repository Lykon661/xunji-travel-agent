"""Local credential persistence cases; no MCP connection is opened."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.adapters import Amap, ToolError


class AmapConfiguration(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        replacement = patch("app.adapters.settings", SimpleNamespace(data=Path(self.folder.name), amap_key="environment-key"))
        replacement.start()
        self.addCleanup(replacement.stop)

    def test_saved_key_survives_restart_without_public_secret(self):
        amap = Amap(None)
        amap.cache["old"] = {"name":"old place"}
        self.assertEqual(amap.save_key(" saved-key "), {"configured":True})
        self.assertEqual(amap.cache, {})
        self.assertEqual(Amap(None).api_key, "saved-key")
        self.assertEqual(Amap(None).public(), {"configured":True})

    def test_clear_overrides_environment_after_restart(self):
        self.assertTrue(Amap(None).public()["configured"])
        Amap(None).save_key("")
        self.assertEqual(Amap(None).public(), {"configured":False})

    def test_failed_save_preserves_existing_key_and_cache(self):
        amap = Amap(None)
        amap.save_key("original-key")
        amap.cache["old"] = None
        with patch("app.adapters.os.replace", side_effect=OSError("cannot save")):
            with self.assertRaises(OSError):
                amap.save_key("replacement-key")
        self.assertEqual(amap.api_key, "original-key")
        self.assertIn("old", amap.cache)
        self.assertEqual(Amap(None).api_key, "original-key")

    def test_invalid_key_does_not_change_configuration(self):
        amap = Amap(None)
        with self.assertRaises(ToolError):
            amap.save_key("https://mcp.amap.com/mcp?key=example")
        self.assertEqual(amap.api_key, "environment-key")
