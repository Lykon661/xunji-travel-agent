"""Configuration regression cases; no CLI execution or remote requests."""
import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from app.adapters import FlyAI, ToolError, flyai_failure, redact_diagnostic


class FlyAIConfiguration(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.package = self.root / "model-runtime" / "node_modules" / "@fly-ai" / "flyai-cli"
        self.package.mkdir(parents=True)
        (self.package / "package.json").write_text(json.dumps({"bin":{"flyai":"cli.cjs"}}), encoding="utf-8")
        (self.package / "cli.cjs").write_text("", encoding="utf-8")
        (self.root / "model-runtime" / "flyai-exit.cjs").write_text("", encoding="utf-8")
        self.settings = SimpleNamespace(data=self.root / "data", flyai_key="environment-key", flyai_command="flyai", flyai_enabled=True)
        for replacement in (patch("app.adapters.PROJECT_ROOT", self.root),
                            patch("app.adapters.settings", self.settings),
                            patch("app.adapters.shutil.which", side_effect=lambda name: "node.exe" if name == "node" else None)):
            replacement.start()
            self.addCleanup(replacement.stop)

    def test_default_uses_project_cli_without_global_install(self):
        expected = ["node.exe"]
        if os.name == "nt":
            expected += ["--require", str(self.root / "model-runtime" / "flyai-exit.cjs")]
        expected.append(str((self.package / "cli.cjs").resolve()))
        self.assertEqual(FlyAI(None).command(), expected)

    def test_custom_executable_does_not_receive_node_preload(self):
        self.settings.flyai_command = "custom-flyai.exe"
        with patch("app.adapters.shutil.which", return_value="custom-flyai.exe"):
            self.assertEqual(FlyAI(None).command(), ["custom-flyai.exe"])

    def test_explicit_missing_command_does_not_fall_back(self):
        self.settings.flyai_command = str(self.root / "missing.exe")
        with self.assertRaises(ToolError):
            FlyAI(None).command()
        self.assertFalse(FlyAI(None).availability()["ready"])

    def test_package_entry_cannot_escape_package_directory(self):
        (self.package.parent / "outside.cjs").write_text("", encoding="utf-8")
        (self.package / "package.json").write_text(json.dumps({"bin":"../outside.cjs"}), encoding="utf-8")
        self.assertFalse(FlyAI(None).availability()["ready"])

    def test_saved_key_survives_restart_without_public_secret(self):
        flyai = FlyAI(None)
        public = flyai.save_key(" saved-key ")
        self.assertTrue(public["configured"])
        self.assertNotIn("api_key", public)
        self.assertNotIn("saved-key", json.dumps(public))
        self.assertEqual(FlyAI(None).api_key, "saved-key")

    def test_clear_overrides_environment_after_restart(self):
        flyai = FlyAI(None)
        flyai.save_key("")
        restored = FlyAI(None)
        self.assertEqual(restored.api_key, "")
        self.assertFalse(restored.public()["configured"])
        self.assertFalse(restored.availability()["ready"])

    def test_failed_save_preserves_existing_key(self):
        flyai = FlyAI(None)
        flyai.save_key("original-key")
        with patch("app.adapters.os.replace", side_effect=OSError("cannot save")):
            with self.assertRaises(OSError):
                flyai.save_key("replacement-key")
        self.assertEqual(flyai.api_key, "original-key")
        self.assertEqual(FlyAI(None).api_key, "original-key")

    def test_invalid_key_does_not_change_configuration(self):
        flyai = FlyAI(None)
        for value in ("https://example.com/key", "key with spaces", "x" * 4097):
            with self.assertRaises(ToolError):
                flyai.save_key(value)
        self.assertEqual(flyai.api_key, "environment-key")


class FlyAIQueryCredentials(unittest.IsolatedAsyncioTestCase):
    async def test_runtime_crash_is_not_classified_from_quote_stdout(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder),flyai_key="environment-key",flyai_enabled=True)
            assertion = "Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), file src\\win\\async.c, line 94"
            process = AsyncMock(return_value=(1,'{"data":{"itemList":[{"id":"429","price":429}]}}',assertion))
            store = Mock()
            with patch("app.adapters.settings",settings),patch.object(FlyAI,"command",return_value=["flyai"]),patch("app.adapters.process",process):
                with self.assertRaises(ToolError) as raised:
                    await FlyAI(store).search("trip","hotel",{},{"dest-name":"广州"})
            self.assertEqual(raised.exception.code,"flyai_runtime")
            result = store.query.call_args.args[-1]
            self.assertEqual(result["code"],"flyai_runtime")
            self.assertEqual(result["diagnostic"],assertion)

    async def test_provider_failure_diagnostics_hide_query_credentials(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder),flyai_key="environment-key",flyai_enabled=True)
            process = AsyncMock(return_value=(1,"","MCP HTTP 403 Forbidden: api_key=environment-key Authorization: Bearer test-token"))
            store = Mock()
            with patch("app.adapters.settings",settings),patch.object(FlyAI,"command",return_value=["flyai"]),patch("app.adapters.process",process):
                with self.assertRaises(ToolError) as raised:
                    await FlyAI(store).search("trip","hotel",{},{"dest-name":"广州"})
            self.assertEqual(raised.exception.code,"flyai_auth")
            for diagnostic in (str(raised.exception),str(store.query.call_args)):
                self.assertNotIn("environment-key",diagnostic)
                self.assertNotIn("test-token",diagnostic)

    async def test_provider_failure_status_is_not_treated_as_missing_inventory(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder),flyai_key="environment-key",flyai_enabled=True)
            process = AsyncMock(return_value=(0,'{"status":403,"data":{"itemList":[]}}',""))
            with patch("app.adapters.settings",settings),patch.object(FlyAI,"command",return_value=["flyai"]),patch("app.adapters.process",process):
                with self.assertRaises(ToolError) as raised:
                    await FlyAI(Mock()).search("trip","hotel",{},{"dest-name":"广州"})
                self.assertEqual(raised.exception.code,"flyai_auth")

    async def test_saved_key_is_passed_to_cli_and_clear_stops_queries(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder), flyai_key="environment-key", flyai_enabled=True)
            store = Mock()
            process = AsyncMock(return_value=(0, "[]", ""))
            with patch("app.adapters.settings", settings), patch.object(FlyAI, "command", return_value=["flyai"]), patch("app.adapters.process", process):
                flyai = FlyAI(store)
                flyai.save_key("saved-key")
                await flyai.search("trip", "hotel", {}, {"dest-name":"广州"})
                self.assertEqual(process.call_args.kwargs["env"]["FLYAI_API_KEY"], "saved-key")
                self.assertNotIn("saved-key", str(store.query.call_args))
                flyai.save_key("")
                process.reset_mock()
                result = await flyai.search("trip", "hotel", {}, {"dest-name":"广州"})
                self.assertEqual(result["status"], "not_configured")
                process.assert_not_awaited()


class FlyAIFailureClassification(unittest.TestCase):
    def test_explicit_http_and_provider_statuses_are_classified(self):
        for detail, expected in (
            ("MCP HTTP 429: Too Many Requests","flyai_rate_limit"),
            (json.dumps({"statusCode":429,"message":"service busy"}),"flyai_rate_limit"),
            ("MCP HTTP 403: Forbidden","flyai_auth"),
            (json.dumps({"status":403,"data":{"itemList":[]}}),"flyai_auth"),
        ):
            with self.subTest(detail=detail):
                self.assertEqual(flyai_failure(detail)[0],expected)

    def test_quotes_and_product_ids_are_not_http_errors(self):
        for detail in (
            json.dumps({"status":0,"data":{"itemList":[{"id":"429403","price":429}]}}),
            json.dumps({"data":{"id":"429","price":403,"name":"quota hotel"}}),
            "product 429403 costs 429 CNY",
        ):
            with self.subTest(detail=detail):
                self.assertEqual(flyai_failure(detail)[0],"flyai_failed")

    def test_runtime_assertion_has_priority_over_quote_numbers(self):
        detail = "Assertion failed: !(handle->flags & UV_HANDLE_CLOSING), file src\\win\\async.c, line 94\n{\"price\":429}"
        self.assertEqual(flyai_failure(detail)[0],"flyai_runtime")

    def test_diagnostic_redacts_secret_tokens_and_urls(self):
        diagnostic = redact_diagnostic("api_key=private-key Authorization: Bearer test-token https://example.test/query?key=private-key","private-key")
        self.assertNotIn("private-key",diagnostic)
        self.assertNotIn("test-token",diagnostic)
        self.assertNotIn("example.test",diagnostic)
