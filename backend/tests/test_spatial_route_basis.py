import copy
import unittest
from unittest.mock import AsyncMock, Mock, patch

from app import ablation
from app.models import OrderChoice, Requirements
from app.planner import Planner


class SpatialRouteBasis(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.map_order = AsyncMock(side_effect=lambda state,city,ids,*anchors:(list(ids),"逐段核实日内交通；保留日内说明"))
        self.planner.movement = AsyncMock(return_value=({"minutes":15,"mode":"transit"},None))
        self.state = {"requirements":Requirements(cities=["杭州"],start_date="2026-11-10",end_date="2026-11-10",people=1).model_dump(mode="json"),
            "locks":[],"stops":{i:{"id":i,"name":"合成景点"+i,"city":"杭州","duration_minutes":90,
                "source_id":"guide","source_title":"合成攻略","evidence":{"quote":i}}
                for i in ("guide:a","guide:b")}}
        self.day = {"date":"2026-11-10","city":"杭州","stop_ids":list(self.state["stops"])}
        self.order = OrderChoice(stop_ids=list(self.state["stops"]),explanation="保留活动级调整说明")
        self.window = (540,1200,[],[],{"name":"合成酒店","label":"返回酒店","start_name":"合成酒店"})

    async def schedule(self, note=""):
        return await self.planner.schedule_activity_day(copy.deepcopy(self.state),{**self.day,"adjustment":note},
            self.order.model_copy(deep=True),set(),copy.deepcopy(self.window),{"weather":"晴"})

    async def test_spatial_and_daily_basis_survive_without_changing_other_results(self):
        original = await self.schedule()
        note = "空间分日：初始按已定位景点集中安排；开放信息仍需核实"
        result = await self.schedule(note)
        basis = result.pop("route_basis")
        self.assertEqual(result,{k:v for k,v in original.items() if k != "route_basis"})
        self.assertTrue(basis.startswith(note))
        self.assertIn(original["route_basis"],basis)
        visits = [a for a in result["activities"] if a["kind"] == "visit"]
        self.assertEqual([a["adjustment"] for a in visits],[self.order.explanation]*2)
        self.assertEqual(result["adjustment"],self.order.explanation)

    async def test_unmarked_notes_leave_output_unchanged(self):
        self.assertEqual(await self.schedule(),await self.schedule("用户调整：按预约安排"))

    async def test_no_clustering_leaves_output_unchanged_even_with_spatial_marker(self):
        original = await self.schedule()
        with ablation.use("no_clustering"):
            self.assertEqual(original,await self.schedule("空间分日：初始按坐标安排"))

    async def test_repeated_basis_segments_are_deduplicated(self):
        note = "空间分日：初始按坐标安排；保留日内说明；空间分日：初始按坐标安排"
        result = await self.schedule(note)
        self.assertEqual(result["route_basis"],"空间分日：初始按坐标安排；保留日内说明；逐段核实日内交通")

    async def test_move_lines_are_shown_as_sentences(self):
        note = "空间分日：初始按坐标安排\n移日：合成景点guide:a|2026-11-11|2026-11-10|与当天景点更近"
        result = await self.schedule(note)
        self.assertEqual(result["route_basis"],"空间分日：初始按坐标安排；跨天调整：将合成景点guide:a从2026-11-11移至2026-11-10，"
                         "原因：与当天景点更近；逐段核实日内交通；保留日内说明")
        self.assertNotIn("|",result["route_basis"])

    async def test_scheduling_does_not_rebuild_or_validate_spatial_plan(self):
        with patch("app.day_clustering.build_spatial_plan",side_effect=AssertionError()),patch(
                "app.day_clustering.cluster_deviation",side_effect=AssertionError()),patch(
                "app.day_clustering.spatial_errors",side_effect=AssertionError()):
            result = await self.schedule("空间分日：初始按坐标安排")
        self.assertEqual([a["id"] for a in result["activities"] if a["kind"] == "visit"],self.day["stop_ids"])

    async def test_move_line_stop_id_is_displayed_as_stop_name(self):
        self.state["stops"]["guide:a"]["name"] = "湖边公园"
        result = await self.schedule("空间分日：初始按坐标安排\n移日：guide:a|2026-11-11|2026-11-10|配合预约时段")
        self.assertIn("跨天调整：将湖边公园从2026-11-11移至2026-11-10，原因：配合预约时段",result["route_basis"])
        self.assertNotIn("guide:a",result["route_basis"])

    async def test_move_line_unknown_reference_is_omitted(self):
        result = await self.schedule("空间分日：初始按坐标安排\n移日：其他公园|2026-11-11|2026-11-10|配合预约时段")
        self.assertNotIn("其他公园",result["route_basis"])

    async def test_skipped_visit_loses_entire_move_reason_with_semicolon(self):
        self.state["stops"]["guide:b"]["duration_minutes"] = 900
        result = await self.schedule("空间分日：初始按坐标安排\n移日：guide:b|2026-11-11|2026-11-10|配合山边预约时段；山边景点仅该日开放")
        self.assertEqual([a["id"] for a in result["activities"] if a["kind"] == "visit"],["guide:a"])
        self.assertNotIn("配合山边预约时段",result["route_basis"])
        self.assertNotIn("仅该日开放",result["route_basis"])
        self.assertNotIn("跨天调整：",result["route_basis"])

    async def test_kept_visit_preserves_entire_move_reason_with_semicolon(self):
        result = await self.schedule("空间分日：初始按坐标安排\n移日：guide:b|2026-11-11|2026-11-10|配合山边预约时段；山边景点仅该日开放")
        self.assertEqual([a["id"] for a in result["activities"] if a["kind"] == "visit"],self.day["stop_ids"])
        self.assertIn("跨天调整：将合成景点guide:b从2026-11-11移至2026-11-10，原因：配合山边预约时段；山边景点仅该日开放",result["route_basis"])

    async def test_skipped_visits_lose_move_lines_and_stale_count_sentences(self):
        self.state["stops"]["guide:b"]["duration_minutes"] = 900
        self.state["stops"]["guide:a"]["alias_names"] = ["湖边别名"]
        self.state["stops"]["guide:b"]["alias_names"] = ["山边别名"]
        for first,second in (("guide:a","guide:b"),("合成景点guide:a","合成景点guide:b"),("湖边别名","山边别名")):
            with self.subTest(first=first,second=second):
                note = ("空间分日：初始按坐标安排。当日安排2个景点，低于已接受不足的参考数量3个。保留预约提醒。"
                        f"\n移日：{first}|2026-11-11|2026-11-10|配合湖边预约时段"
                        f"\n移日：{second}|2026-11-11|2026-11-10|配合山边预约时段")
                result = await self.schedule(note)
                self.assertEqual([a["id"] for a in result["activities"] if a["kind"] == "visit"],["guide:a"])
                self.assertNotIn("当日安排",result["route_basis"])
                self.assertNotIn("低于已接受不足",result["route_basis"])
                self.assertEqual(result["route_basis"].count("跨天调整："),1)
                self.assertIn("跨天调整：将合成景点guide:a从2026-11-11移至2026-11-10，原因：配合湖边预约时段",result["route_basis"])
                self.assertNotIn("配合山边预约时段",result["route_basis"])
                self.assertIn("保留预约提醒",result["route_basis"])
