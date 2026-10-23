"""Guide filtering and request budgets with synthetic data; no network or browser."""
import asyncio
import json
import sys
import tempfile
import unittest
from contextlib import ExitStack
from contextvars import ContextVar
from pathlib import Path
from types import ModuleType, SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch

import httpx

import crawler_bridge
from app.adapters import Crawler
from app.config import CRAWLER_REVISION
from app.main import app, search_guides
from app.models import GuideSearchInput


def search_item(note_id="note", title="旅行攻略", note_type="normal", **extra):
    return {"id":note_id, "model_type":"note", "xsec_source":"pc_search", "xsec_token":"token",
            "note_card":{"type":note_type, "display_title":title}, **extra}


class GuideFilters(unittest.TestCase):
    def test_video_is_rejected_even_with_city_and_guide_title(self):
        self.assertFalse(crawler_bridge.is_travel_guide_item(search_item(title="广州攻略", note_type="video"), "广州"))

    def test_query_suggestions_are_rejected(self):
        for model_type in ("rec_query", "hot_query"):
            with self.subTest(model_type=model_type):
                self.assertFalse(crawler_bridge.is_travel_guide_item(search_item(model_type=model_type), "广州"))

    def test_unrelated_and_missing_titles_are_rejected(self):
        for item in (search_item(title="WiFi密码分享"), search_item(title="探店vlog"),
                     search_item(title="来广州必须要喝凉茶非常好喝"), search_item(title="广州"),
                     search_item(title="today开心"), search_item(title=""), search_item(title=None),
                     {"id":"missing"}, {"note_card":None}):
            with self.subTest(item=item):
                self.assertFalse(crawler_bridge.is_travel_guide_item(item, "广州"))

    def test_guide_terms_are_kept(self):
        for title in ("广州景点合集", "旅行攻略", "步行路线", "行程分享", "CityWalk", "CITY WALK",
                      "旅途Day1", "DAY 2", "三天两晚", "3天2晚", "几天几晚", "5天", "一日游", "2日游",
                      "广州旅游完整版攻略", "在广州！这9个室内好去处", "周末自由行", "3日安排", "三日安排",
                      "周末必去", "周末必打卡", "周末打卡", "游玩推荐", "旅游推荐", "旅行分享", "玩法大全", "逛吃合集"):
            with self.subTest(title=title):
                self.assertTrue(crawler_bridge.is_travel_guide_item(search_item(title=title), " 广州 "))
        self.assertTrue(crawler_bridge.is_travel_guide_item(search_item(), ""))
        self.assertFalse(crawler_bridge.is_travel_guide_item(search_item(title="普通日记"), ""))

    def test_employment_and_housing_terms_override_guide_terms(self):
        for word in ("找工作", "租房", "求职", "面试", "上班", "通勤", "搬家", "落户"):
            with self.subTest(word=word):
                self.assertFalse(crawler_bridge.is_travel_guide_item(search_item(title=f"广州{word}攻略"), "广州"))
        self.assertFalse(crawler_bridge.is_travel_guide_item(
            search_item(title="五天时间在广州，找工作和租房全搞定的思路"), "广州"))

    def test_short_text_and_few_images_are_rejected_after_removing_topics(self):
        for desc in ("短正文", "字" * 79, "短正文" + "#广州旅行攻略[话题]#" * 20, " \n" * 80, None):
            with self.subTest(desc=desc):
                self.assertFalse(crawler_bridge.has_guide_content({"desc":desc, "image_list":[{}, {}]}))
        self.assertFalse(crawler_bridge.has_guide_content({}))

    def test_text_and_image_thresholds_are_inclusive(self):
        self.assertTrue(crawler_bridge.has_guide_content({"desc":"字" * 80, "image_list":[]}))
        self.assertTrue(crawler_bridge.has_guide_content({"desc":"#话题#", "image_list":[{}, {}, {}]}))


class BridgeCollection(unittest.IsolatedAsyncioTestCase):
    def three_guide_pages(self):
        pages = [{"items":[search_item(str(i)) for i in ids], "has_more":True}
                 for ids in (range(5), range(5, 12), range(12, 21))]
        # Repeated IDs across pages must not spend any additional detail requests.
        pages[1]["items"].insert(0, search_item("0"))
        pages[2]["items"].insert(0, search_item("5"))
        return pages

    async def run_bridge(self, items, notes, *, city="广州", extra_request=False, restriction=None, url=None, pages=None):
        """Run the real bridge against module stubs and a mocked HTTP boundary."""
        with tempfile.TemporaryDirectory() as folder, ExitStack() as stack:
            output = Path(folder)
            request_file = output / "request.json"
            request = {"keywords":["广州 旅行攻略"], "url":url, "output":folder}
            if city is not None:
                request["city"] = city
            request_file.write_text(json.dumps(request), encoding="utf-8")
            config = ModuleType("config")
            stored = AsyncMock()
            details = AsyncMock(side_effect=lambda note_id, *args: notes.get(note_id))
            keyword_search = AsyncMock(side_effect=pages if pages is not None else [{"items":items, "has_more":False}])
            search_id_factory = Mock(return_value="round-search-id")

            async def send(client, http_request, **kwargs):
                return httpx.Response(200, json=restriction or {}, request=http_request)

            async def local_get(path):
                # AsyncClient.send is mocked before main installs its real guard around it.
                async with httpx.AsyncClient(trust_env=False) as client:
                    await client.get("https://testserver/" + path)

            class FakeClient:
                async def request(self):
                    pass

                request.__wrapped__ = request

                async def query_self(self):
                    await local_get("pong")
                    return {"data":{"result":{"success":True}}}

                async def get_note_by_keyword(self, **kwargs):
                    await local_get("search")
                    return await keyword_search(**kwargs)

                async def get_note_by_id(self, *args):
                    await local_get("detail/" + args[0])
                    return await details(*args)

            class FakeCrawler:
                def __init__(self):
                    self.xhs_client = FakeClient()

                async def start(self):
                    await self.xhs_client.pong()
                    if config.CRAWLER_TYPE == "detail":
                        await self.get_specified_notes()
                    else:
                        await self.search()
                    if extra_request:
                        await self.xhs_client.query_self()

            modules = {}
            for name in ("media_platform", "media_platform.xhs", "media_platform.xhs.client",
                         "media_platform.xhs.field", "media_platform.xhs.help", "store", "var"):
                modules[name] = ModuleType(name)
            modules["config"] = config
            modules["media_platform.xhs"].XiaoHongShuCrawler = FakeCrawler
            modules["media_platform.xhs.client"].XiaoHongShuClient = FakeClient
            modules["media_platform.xhs.field"].SearchSortType = SimpleNamespace(MOST_POPULAR="popular")
            modules["media_platform.xhs.field"].SearchNoteType = SimpleNamespace(IMAGE=2)
            modules["media_platform.xhs.help"].get_search_id = search_id_factory
            modules["media_platform.xhs.help"].parse_note_info_from_note_url = Mock(
                return_value=SimpleNamespace(note_id="note", xsec_source="pc_share", xsec_token="import-token"))
            modules["store"].xhs = SimpleNamespace(update_xhs_note=stored)
            modules["var"].source_keyword_var = ContextVar("test_source_keyword")
            stack.enter_context(patch.dict(sys.modules, modules))
            stack.enter_context(patch.object(sys, "argv", ["bridge", folder, str(request_file)]))
            stack.enter_context(patch.object(sys, "path", list(sys.path)))
            network = stack.enter_context(patch("httpx.AsyncClient.send", autospec=True, side_effect=send))
            sleep = stack.enter_context(patch("crawler_bridge.asyncio.sleep", new_callable=AsyncMock))
            stack.enter_context(patch("crawler_bridge.time.monotonic", return_value=100))
            error = None
            try:
                await crawler_bridge.main()
            except RuntimeError as exc:
                error = exc
            return SimpleNamespace(config=config, stored=stored, details=details, search=keyword_search,
                                   network=network, sleep=sleep, error=error, search_id_factory=search_id_factory,
                                   protected=(output / "account-protection.json").exists())

    async def test_twenty_guides_stop_before_another_page_with_local_prefilter_and_deduplication(self):
        items = [search_item("video", note_type="video"), search_item("wifi", title="WiFi密码"),
                 search_item("herbal-tea", title="来广州必须要喝凉茶非常好喝"),
                 search_item("career", title="广州找工作攻略"),
                 search_item("query", model_type="rec_query")]
        items += [search_item(str(i), title="广州景点合集") for i in range(20)]
        items.insert(5, search_item("0"))
        notes = {str(i):{"note_id":str(i), "desc":"字" * 80} for i in range(21)}
        result = await self.run_bridge(items, notes, pages=[
            {"items":items, "has_more":True}, {"items":[search_item("20")], "has_more":True}])
        self.assertIsNone(result.error)
        self.assertEqual(result.config.CRAWLER_MAX_NOTES_COUNT, 20)
        self.assertEqual([call.args[0] for call in result.details.await_args_list], list(map(str, range(20))))
        self.assertEqual(result.stored.await_count, 20)
        self.assertEqual(result.network.await_count, 22)
        self.assertEqual(result.sleep.await_count, 21)
        self.assertTrue(all(call.args == (1.5,) for call in result.sleep.await_args_list))
        result.search.assert_awaited_once_with(keyword="广州 旅行攻略", page=1, search_id="round-search-id",
                                              sort="popular", note_type=2)
        result.search_id_factory.assert_called_once_with()
        self.assertEqual(result.stored.await_args.args[0]["xsec_token"], "token")

    async def test_three_pages_share_one_search_id_and_fit_twenty_four_request_budget(self):
        notes = {str(i):{"note_id":str(i), "desc":"字" * 80} for i in range(21)}
        result = await self.run_bridge([], notes, pages=self.three_guide_pages())
        self.assertIsNone(result.error)
        result.search_id_factory.assert_called_once_with()
        self.assertEqual([call.kwargs for call in result.search.await_args_list], [
            {"keyword":"广州 旅行攻略", "page":page, "search_id":"round-search-id", "sort":"popular", "note_type":2}
            for page in (1, 2, 3)])
        self.assertEqual([call.args[0] for call in result.details.await_args_list], list(map(str, range(20))))
        self.assertEqual(result.stored.await_count, 20)
        self.assertEqual(result.network.await_count, 24)
        self.assertEqual(result.sleep.await_count, 23)
        self.assertTrue(all(call.args == (1.5,) for call in result.sleep.await_args_list))

    async def test_no_more_or_empty_items_stop_pagination(self):
        for response in ({"items":[search_item()], "has_more":False}, {"items":[search_item()]},
                         {"items":[], "has_more":True}, {"has_more":True}):
            with self.subTest(response=response):
                result = await self.run_bridge([], {"note":{"desc":"字" * 80}}, pages=[
                    response, {"items":[search_item("unexpected")], "has_more":True}])
                self.assertIsNone(result.error)
                self.assertEqual(result.search.await_count, 1)
                expected_details = 1 if response.get("items") else 0
                self.assertEqual(result.details.await_count, expected_details)
                self.assertEqual(result.network.await_count, 2 + expected_details)

    async def test_at_most_three_pages_even_when_all_items_are_filtered(self):
        pages = [{"items":[search_item(str(i), note_type="video")], "has_more":True} for i in range(4)]
        result = await self.run_bridge([], {}, pages=pages)
        self.assertIsNone(result.error)
        self.assertEqual([call.kwargs["page"] for call in result.search.await_args_list], [1, 2, 3])
        result.details.assert_not_awaited()
        self.assertEqual(result.network.await_count, 4)

    async def test_discarded_and_missing_details_still_consume_the_twenty_request_budget(self):
        notes = {str(i):{"note_id":str(i), "desc":"正文#旅行攻略[话题]#" * 5,
                        "image_list":[{}, {}, {}] if i else []} for i in range(21)}
        notes["1"] = None
        result = await self.run_bridge([], notes, pages=self.three_guide_pages())
        self.assertIsNone(result.error)
        self.assertEqual([call.args[0] for call in result.details.await_args_list], list(map(str, range(20))))
        self.assertEqual(result.stored.await_count, 18)
        self.assertEqual(result.network.await_count, 24)

    async def test_discarded_details_exhaust_budget_before_next_page(self):
        pages = [{"items":[search_item(str(i)) for i in range(20)], "has_more":True},
                 {"items":[search_item("20")], "has_more":True}]
        result = await self.run_bridge([], {str(i):{"desc":"短正文"} for i in range(21)}, pages=pages)
        self.assertIsNone(result.error)
        self.assertEqual(result.search.await_count, 1)
        self.assertEqual(result.details.await_count, 20)
        result.stored.assert_not_awaited()
        self.assertEqual(result.network.await_count, 22)

    async def test_city_field_is_optional(self):
        result = await self.run_bridge([search_item()], {"note":{"desc":"字" * 80}}, city=None)
        self.assertIsNone(result.error)
        self.assertEqual(result.stored.await_count, 1)

    async def test_specified_link_import_keeps_short_text_without_images(self):
        note = {"note_id":"note", "desc":"短正文", "image_list":[]}
        self.assertFalse(crawler_bridge.has_guide_content(note))
        url = "https://www.xiaohongshu.com/explore/note"
        result = await self.run_bridge([], {"note":note}, url=url)
        self.assertIsNone(result.error)
        self.assertEqual(result.config.CRAWLER_TYPE, "detail")
        self.assertEqual(result.config.XHS_SPECIFIED_NOTE_URL_LIST, [url])
        result.search.assert_not_awaited()
        result.details.assert_awaited_once_with("note", "pc_share", "import-token")
        result.stored.assert_awaited_once_with({**note, "xsec_source":"pc_share", "xsec_token":"import-token"})
        self.assertEqual(result.network.await_count, 2)

    async def test_http_guard_rejects_twenty_fifth_request(self):
        result = await self.run_bridge([], {str(i):{"desc":"字" * 80} for i in range(21)},
                                      pages=self.three_guide_pages(), extra_request=True)
        self.assertIsInstance(result.error, RuntimeError)
        self.assertIn("接口请求上限", str(result.error))
        self.assertEqual(result.network.await_count, 24)

    async def test_restriction_stops_requests_and_writes_protection_file(self):
        for restriction in ({"code":300011}, {"code":"300012"}, {"msg":"需要验证码"}):
            with self.subTest(restriction=restriction):
                result = await self.run_bridge([search_item()], {}, restriction=restriction)
                self.assertIsInstance(result.error, crawler_bridge.CollectionProtected)
                self.assertTrue(result.protected)
                self.assertEqual(result.network.await_count, 1)
                result.details.assert_not_awaited()


class AdapterCollection(unittest.IsolatedAsyncioTestCase):
    async def test_city_is_passed_and_only_twenty_unique_records_are_imported(self):
        with tempfile.TemporaryDirectory() as folder:
            settings = SimpleNamespace(data=Path(folder), crawler_enabled=True, cache_hours=24,
                                       crawler_dir=Path(folder), crawler_python=Path(folder) / "python.exe",
                                       crawler_timeout=240)
            store = Mock()
            store.cached.return_value = None
            store.add_source.side_effect = lambda trip_id, doc: doc
            requests = []

            async def process(args, **kwargs):
                if args[0] == "git":
                    return 0, CRAWLER_REVISION, ""
                request = json.loads(Path(args[-1]).read_text(encoding="utf-8"))
                requests.append(request)
                rows = [{"note_id":str(i), "desc":"字" * 80, "image_list":""} for i in range(21)]
                rows.insert(1, rows[0])
                (Path(request["output"]) / "notes.jsonl").write_text(
                    "\n".join(json.dumps(row) for row in rows), encoding="utf-8")
                return 0, "", ""

            with patch("app.adapters.settings", settings), patch("app.adapters.installation_ready", return_value=True), \
                 patch("app.adapters.process", new_callable=AsyncMock, side_effect=process):
                docs = await Crawler(store).collect("trip", "广州", ["广州 5天 攻略"])
            self.assertEqual(requests[0]["city"], "广州")
            self.assertEqual(requests[0]["keywords"], ["广州 5天 攻略"])
            self.assertEqual([doc["note_id"] for doc in docs], list(map(str, range(20))))
            store.cache.assert_called_once()
            self.assertEqual(len(store.cache.call_args.args[1]), 20)


class GuideQueries(unittest.IsolatedAsyncioTestCase):
    async def test_search_appends_guide_term_and_preserves_default_and_existing_term(self):
        crawler = SimpleNamespace(collect=AsyncMock(return_value=[]))
        planner = SimpleNamespace(gate=lambda trip_id: asyncio.Lock(),
                                  active=lambda trip_id: {"version":0}, crawler=crawler)
        cases = [("广州", "5天", "广州 5天 攻略"), (" 广州 ", " ", "广州 旅行攻略"),
                 ("广州", "美食攻略", "广州 美食攻略"), ("", "广州 5天", "广州 5天 攻略"),
                 ("广州", "旅行攻略", "广州 旅行攻略")]
        with patch.object(app.state, "planner", planner, create=True):
            for city, keywords, expected in cases:
                with self.subTest(city=city, keywords=keywords):
                    result = await search_guides("trip", GuideSearchInput(city=city, keywords=keywords, base_version=0))
                    crawler.collect.assert_awaited_with("trip", city.strip(), [expected])
                    self.assertEqual(result["count"], 0)
