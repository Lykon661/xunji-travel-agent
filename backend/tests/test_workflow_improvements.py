"""Workflow regressions with temporary data and a mocked CLI."""
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

from fastapi import HTTPException

from app.adapters import FlyAI, ToolError, map_urls, normalize_pois, resource_status
from app.map_preview import MapPreviewConfig, proxy_target
from app.models import Requirements
from app.planner import Planner
from app.store import Conflict, Store


class RequirementPersistence(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.store = Store(Path(folder.name))
        self.req = Requirements(origin="北京",cities=["杭州"],start_date="2026-11-10",end_date="2026-11-12",people=2,rooms=1,budget=5000).model_dump(mode="json")
        self.trip = self.store.create_trip("需求保存",{})

    def test_saved_requirements_survive_reopen_without_starting_a_run(self):
        self.store.save_requirements(self.trip["id"],self.req,0,self.trip["updated_at"])
        restored = Store(self.store.file.parent).trip(self.trip["id"])
        self.assertEqual(restored["document"]["requirements"],self.req)
        self.assertIsNone(restored["active_run"])

    def test_changed_requirements_invalidate_old_checkpoint_but_keep_sources_and_itinerary(self):
        doc = self.trip["document"]
        doc.update(selected_source_ids=["guide"],candidates=[{"id":"old"}],interview={"summary":"old"},
                   pending={"kind":"interview"},itinerary={"days":[{"date":"2026-11-09"}]})
        self.store.update(self.trip["id"],doc)
        run = self.store.start_run(self.trip["id"],0,"old-thread",{})
        self.store.finish_run(run,"waiting",{})
        current = self.store.trip(self.trip["id"])
        result = self.store.save_requirements(current["id"],self.req,0,current["updated_at"])
        self.assertEqual(result["document"]["selected_source_ids"],["guide"])
        self.assertEqual(result["document"]["itinerary"],doc["itinerary"])
        self.assertTrue(result["document"]["itinerary_outdated"])
        self.assertEqual(result["document"]["candidates"],[])
        self.assertIsNone(result["active_thread"])

    def test_stale_timestamp_cannot_overwrite_saved_requirements(self):
        self.store.save_requirements(self.trip["id"],self.req,0,self.trip["updated_at"])
        with self.assertRaises(Conflict):
            self.store.save_requirements(self.trip["id"],{**self.req,"budget":100},0,self.trip["updated_at"])
        self.assertEqual(self.store.trip(self.trip["id"])["document"]["requirements"]["budget"],5000)


class QueryDiagnostics(unittest.IsolatedAsyncioTestCase):
    async def test_cli_auth_failure_is_distinct_and_secret_is_redacted(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder),flyai_key="private-key",flyai_enabled=True)
            store = Mock()
            process = AsyncMock(return_value=(1,"","HTTP 401 invalid API key private-key; https://example.com/?token=secret"))
            with patch("app.adapters.settings",settings),patch.object(FlyAI,"command",return_value=["flyai"]),patch("app.adapters.process",process):
                with self.assertRaises(ToolError) as raised:
                    await FlyAI(store).search("trip","hotel",{}, {"dest-name":"杭州"})
            self.assertEqual(raised.exception.code,"flyai_auth")
            self.assertNotIn("private-key",str(raised.exception))
            self.assertNotIn("token=secret",str(store.query.call_args))
            self.assertEqual(store.query.call_args.args[3]["exit_code"],1)

    async def test_missing_key_is_recorded_without_executing_cli(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder),flyai_key="",flyai_enabled=True)
            store = Mock()
            with patch("app.adapters.settings",settings),patch("app.adapters.process",new_callable=AsyncMock) as process:
                result = await FlyAI(store).search("trip","poi",{}, {"city-name":"杭州"})
                process.assert_not_awaited()
            self.assertEqual(result["status"],"not_configured")
            self.assertEqual(store.query.call_args.args[3]["code"],"flyai_key")



class StaleRunProtection(unittest.IsolatedAsyncioTestCase):
    async def test_old_page_cannot_submit_over_newer_trip_content(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            trip = store.create_trip("过期页面",Requirements().model_dump(mode="json"))
            planner = Planner.__new__(Planner)
            planner.store = store;planner.gates = {}
            planner.graph = SimpleNamespace(aget_state=AsyncMock())
            with self.assertRaises(Conflict):
                await planner.submit(trip["id"],0,updated_at="outdated-page")
            planner.graph.aget_state.assert_not_awaited()
            self.assertIsNone(store.trip(trip["id"])["active_run"])


class BrowserMapSecurity(unittest.TestCase):
    def test_web_key_storage_is_separate_and_does_not_expose_security_code(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder)
            (path / "amap-config.json").write_text('{"api_key":"service-key"}',encoding="utf-8")
            config = MapPreviewConfig(path)
            config.save("browser-key","private-jscode")
            self.assertNotIn("private-jscode",json.dumps(config.public()))
            self.assertEqual(MapPreviewConfig(path).security_code,"private-jscode")
            config.save("","")
            self.assertEqual(json.loads((path / "amap-config.json").read_text())["api_key"],"service-key")

    def test_proxy_cannot_be_redirected_to_arbitrary_hosts_or_parent_paths(self):
        for path in ("https://evil.example/v3/place/text","v3/../v4/map/styles","v3//evil.example"):
            with self.assertRaises(HTTPException):
                proxy_target(path)
        self.assertEqual(proxy_target("v3/place/text"),"https://restapi.amap.com/v3/place/text")

    def test_map_link_keeps_provider_uri_and_excludes_surrounding_punctuation(self):
        uri = "amapuri://workInAmap/createWithToken?polymericId=example&from=travel"
        self.assertEqual(map_urls("'"+uri+"'。 https://evil.example/map"),[uri])
