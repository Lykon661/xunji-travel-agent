"""Local state regression checks; no browser, subprocess or platform request is started."""
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from app.adapters import Crawler, ToolError


class AccountProtection(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        settings = SimpleNamespace(data=Path(self.folder.name), crawler_enabled=False)
        settings_patch = patch("app.adapters.settings", settings)
        settings_patch.start()
        self.addCleanup(settings_patch.stop)
        installation_patch = patch("app.adapters.installation_ready", return_value=True)
        installation_patch.start()
        self.addCleanup(installation_patch.stop)

    async def test_restart_keeps_protection_and_prevents_remote_collection(self):
        crawler = Crawler(None)
        crawler.set_enabled(True)
        crawler.protect("account restricted")
        restarted = Crawler(None)
        self.assertFalse(restarted.enabled)
        with self.assertRaises(ToolError):
            restarted.set_enabled(True)
        with patch("app.adapters.process", new_callable=AsyncMock) as remote:
            with self.assertRaises(ToolError):
                await restarted.collect("trip", "city", ["query"])
            remote.assert_not_awaited()

    async def test_restart_and_toggle_do_not_reset_round_cooldown(self):
        with patch("app.adapters.time.time", return_value=1000):
            crawler = Crawler(None)
            crawler.reserve_round()
        with patch("app.adapters.time.time", return_value=1059):
            restarted = Crawler(None)
            restarted.set_enabled(False)
            restarted.set_enabled(True)
            with self.assertRaises(ToolError):
                restarted.reserve_round()
        with patch("app.adapters.time.time", return_value=1060):
            restarted.reserve_round()
        self.assertEqual(restarted.next_allowed_at, 1120)
