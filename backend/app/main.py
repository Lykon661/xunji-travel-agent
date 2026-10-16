import asyncio
import hashlib
import logging
import os
import re
import sqlite3
from contextlib import asynccontextmanager
from pathlib import Path
from time import perf_counter
from uuid import uuid4

from fastapi import FastAPI, File, Form, HTTPException, Query, Request, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, JSONResponse, StreamingResponse
from langgraph.checkpoint.sqlite.aio import AsyncSqliteSaver
from pydantic import ValidationError

from .adapters import ToolError, image_data, source_identity, xhs_url
from .config import CRAWLER_REVISION, PROJECT_ROOT, settings
from .llm import worker as model_worker
from .crawler_control import CrawlerControl, router as crawler_router
from .models import (AmapKeyInput, CandidateRevisionInput, CreateTrip, FlyAIKeyInput, GuideSearchInput, RequirementsSaveInput,
                     ItinerarySaveInput, LockInput, MessageInput, SelectionInput, ResourceSelectionInput,
                     ProductSearchInput, ProductSelectionInput, TaxiInput, WorkflowBackInput, HotelAreaInput, DecisionReopenInput)
from .model_oauth import ModelOAuthManager
from .model_routes import router as model_router
from .map_preview import MapPreviewConfig, router as map_router
from .planner import Planner
from .store import Conflict, Store, compact_sqlite, dump, now
from .web import mount_frontend
from .routing import coordinates
from .resource_planning import recommended_hotel_areas


@asynccontextmanager
async def lifespan(app: FastAPI):
    logger = logging.getLogger("uvicorn.error")
    started = perf_counter()
    store = Store(settings.data)
    store.recover()
    for name in ("trips.sqlite","checkpoints.sqlite"):
        try:
            if reclaimed := compact_sqlite(settings.data / name):
                logger.info("[startup] reclaimed %.1f MB of free space in %s",reclaimed/1024/1024,name)
        except sqlite3.Error as exc:
            logger.warning("[startup] could not compact %s: %s",name,exc)
    logger.info("[startup] database initialization and recovery: %.3fs",perf_counter()-started)
    phase = perf_counter()
    async with AsyncSqliteSaver.from_conn_string(str(settings.data / "checkpoints.sqlite")) as checkpointer:
        await checkpointer.setup()
        logger.info("[startup] checkpoint database initialization: %.3fs",perf_counter()-phase)
        phase = perf_counter()
        app.state.store = store
        app.state.planner = Planner(store,checkpointer)
        app.state.map_preview = MapPreviewConfig(settings.data)
        app.state.model_oauth = ModelOAuthManager(app.state.planner.models.config_store)
        app.state.crawler_control = CrawlerControl(app.state.planner.crawler)
        logger.info("[startup] local services initialized (planning graph deferred): %.3fs",perf_counter()-phase)
        logger.info("[startup] application initialization complete: %.3fs",perf_counter()-started)
        yield
        await app.state.planner.close()
        control = app.state.crawler_control
        if control.task and not control.task.done():
            control.task.cancel()
            await asyncio.gather(control.task, return_exceptions=True)
        for identity in list(app.state.model_oauth.sessions):
            app.state.model_oauth.cancel(identity)
        model_worker.close()


app = FastAPI(title="循迹旅行规划",version="0.1.0",lifespan=lifespan)
app.add_middleware(CORSMiddleware,allow_origins=["http://localhost:5173","http://127.0.0.1:5173",
                                              "http://localhost:8000","http://127.0.0.1:8000"],
                   allow_methods=["GET","POST","PATCH","PUT","DELETE"],allow_headers=["Content-Type","Last-Event-ID"])
app.include_router(model_router, prefix="/api")
app.include_router(crawler_router, prefix="/api")
app.include_router(map_router)


@app.middleware("http")
async def local_access(request: Request, call_next):
    # Local-only app: reject DNS rebinding and cross-site writes to this unauthenticated service.
    host = request.headers.get("host","").split(":")[0]
    if host not in {"localhost","127.0.0.1","testserver"}:
        return JSONResponse({"detail":"仅允许本机访问"},status_code=403)
    origin = request.headers.get("origin")
    if request.method not in {"GET","HEAD","OPTIONS"} and origin and origin not in {"http://localhost:5173","http://127.0.0.1:5173","http://localhost:8000","http://127.0.0.1:8000"}:
        return JSONResponse({"detail":"不允许此来源修改本机数据"},status_code=403)
    return await call_next(request)


@app.exception_handler(KeyError)
async def not_found(request,exc):
    return JSONResponse({"detail":str(exc).strip("'")},status_code=404)


@app.exception_handler(Conflict)
async def conflict(request,exc):
    return JSONResponse({"detail":str(exc)},status_code=409)


@app.exception_handler(ToolError)
async def tool_error(request,exc):
    return JSONResponse({"detail":str(exc),"code":exc.code},status_code=400)


@app.exception_handler(ValidationError)
async def invalid(request,exc):
    return JSONResponse({"detail":"输入不符合要求："+"；".join(e["msg"] for e in exc.errors())},status_code=422)


@app.get("/api/capabilities")
async def capabilities():
    models = app.state.planner.models.config_store
    registry = models.public_registry()
    current = registry["current"]
    configured = current["configured"] or any(provider["configured"] and any(model["id"].strip() for model in provider["models"]) for provider in registry["providers"])
    flyai = app.state.planner.flyai.public()
    return {"model_configured":bool(configured),"model_active":current["configured"],
            "model_name":" / ".join(item for item in (current["provider_name"], current["model_name"] or current["model"]) if item),
            "crawler_enabled":app.state.planner.crawler.enabled,"crawler_revision":CRAWLER_REVISION,
            "flyai_enabled":flyai["enabled"],"flyai_configured":flyai["configured"],"flyai_ready":flyai["ready"],"flyai_message":flyai["message"],
            "amap_configured":app.state.planner.amap.public()["configured"]}


@app.get("/api/system/amap")
async def amap_configuration():
    return app.state.planner.amap.public()


def save_amap_key(value: str):
    planner = app.state.planner
    if any(not task.done() for task in planner.tasks.values()) or any(lock.locked() for lock in planner.gates.values()):
        raise HTTPException(409, "正在处理旅行任务，请结束后再修改高德 Key")
    try:
        return planner.amap.save_key(value)
    except OSError as exc:
        raise HTTPException(500, "无法保存高德配置，请检查数据目录权限") from exc


@app.put("/api/system/amap")
async def save_amap_configuration(body: AmapKeyInput):
    value = body.api_key.get_secret_value().strip()
    if not value:
        raise HTTPException(400, "请填写高德 Web 服务 Key")
    return save_amap_key(value)


@app.delete("/api/system/amap")
async def clear_amap_configuration():
    return save_amap_key("")


@app.get("/api/system/flyai")
async def flyai_configuration():
    return app.state.planner.flyai.public()


def save_flyai_key(value: str):
    planner = app.state.planner
    if any(not task.done() for task in planner.tasks.values()) or any(lock.locked() for lock in planner.gates.values()):
        raise HTTPException(409, "正在处理旅行任务，请结束后再修改飞猪 API Key")
    try:
        return planner.flyai.save_key(value)
    except OSError as exc:
        raise HTTPException(500, "无法保存飞猪配置，请检查数据目录权限") from exc


@app.put("/api/system/flyai")
async def save_flyai_configuration(body: FlyAIKeyInput):
    value = body.api_key.get_secret_value().strip()
    if not value:
        raise HTTPException(400, "请填写飞猪 API Key")
    return save_flyai_key(value)


@app.delete("/api/system/flyai")
async def clear_flyai_configuration():
    return save_flyai_key("")


@app.get("/api/health", include_in_schema=False)
async def health():
    return {"status":"ready", "service":"travel-planner", "instance":os.getenv("TRAVEL_SERVER_INSTANCE", "")}


@app.post("/api/trips",status_code=201)
async def create(body: CreateTrip):
    return app.state.store.create_trip(body.title,body.requirements.model_dump(mode="json"))


@app.get("/api/trips")
async def trips():
    return app.state.store.trips()


@app.get("/api/trips/{trip_id}")
async def trip(trip_id: str):
    result = app.state.store.trip(trip_id)
    if result["active_run"]:
        result["run"] = app.state.store.run(result["active_run"])
    return app.state.planner.refresh_review_status(result)


@app.patch("/api/trips/{trip_id}/requirements")
async def save_requirements(trip_id: str, body: RequirementsSaveInput):
    planner = app.state.planner
    async with planner.gate(trip_id):
        planner.active(trip_id)
        if missing := body.requirements.missing():
            raise HTTPException(422,"请先填写："+"、".join(missing))
        return app.state.store.save_requirements(trip_id,body.requirements.model_dump(mode="json"),body.base_version,body.updated_at)


@app.delete("/api/trips/{trip_id}")
async def delete_trip(trip_id: str, base_version: int = Query(ge=0)):
    await app.state.planner.delete_trip(trip_id,base_version)
    return {"deleted":True}


@app.post("/api/trips/{trip_id}/sources/search")
async def search_guides(trip_id: str, body: GuideSearchInput):
    # Raw guide search only needs a city/keyword, not complete itinerary requirements or a model.
    async with app.state.planner.gate(trip_id):
        trip = app.state.planner.active(trip_id)
        if trip["version"] != body.base_version:
            raise Conflict("行程已更新，请刷新后重新搜索")
        city, keywords = body.city.strip(), body.keywords.strip()
        if not city and not keywords:
            raise HTTPException(422, "请填写目的地城市或搜索关键词")
        query = " ".join(item for item in (city, keywords or "旅行攻略") if item)
        try:
            sources = await app.state.planner.crawler.collect(trip_id, city, [query])
        except ToolError as exc:
            app.state.store.query(trip_id, "xiaohongshu", {"city":city,"keywords":[query]},
                                  {"status":"error","code":exc.code,"message":str(exc)})
            raise
        return {"sources":sources,"count":len(sources),"message":f"找到 {len(sources)} 篇攻略，已加入当前旅行" if sources else "没有找到攻略，试试更换关键词或手动导入"}


@app.post("/api/trips/{trip_id}/sources",status_code=201)
async def source(trip_id: str, title: str = Form("导入的攻略"), url: str = Form(""), text: str = Form(""),
                 city_hint: str = Form(""), files: list[UploadFile] = File(default=[]), base_version: int = Form(...)):
    async with app.state.planner.gate(trip_id):
        trip = app.state.planner.active(trip_id)
        if trip["version"] != base_version:
            raise Conflict("行程已更新，请刷新后导入")
        if not text.strip() and not url.strip() and not files:
            raise HTTPException(422,"请提供攻略链接、文字或图片")
        if len(text) > 30000 or len(title) > 200 or len(files) > 18:
            raise HTTPException(422,"正文最多 30000 字，标题最多 200 字，最多导入 18 张图片")
        url = xhs_url(url.strip()) if url.strip() else ""
        normalized, hashes = [],[]
        for file in files:
            raw = await file.read(10*1024*1024+1)
            if len(raw) > 10*1024*1024:
                raise HTTPException(422,"每张图片不得超过 10 MB")
            encoded = image_data(raw)
            import base64
            jpeg = base64.b64decode(encoded.split(",",1)[1])
            digest = hashlib.sha256(jpeg).hexdigest()
            hashes.append(digest); normalized.append((digest,jpeg))
        folder = settings.data / "uploads"; folder.mkdir(parents=True,exist_ok=True)
        image_paths = []
        for digest,jpeg in normalized:
            filename = digest + ".jpg"
            (folder / filename).write_bytes(jpeg)
            image_paths.append("local:"+filename)
        identity = source_identity(url,text,hashes)
        doc = {"id":uuid4().hex,"identity":identity,"title":title.strip() or "导入的攻略","url":url,
               "text":text.strip(),"images":image_paths,"city_hint":city_hint.strip(),"likes":0,"favorites":0,
               "published_at":None,"collected_at":now(),"kind":"manual" if text.strip() or files else "url",
               "imported":True,"extraction":None,"error":None}
        return app.state.store.add_source(trip_id,doc)


@app.get("/api/uploads/{filename}")
async def upload(filename: str):
    if not re.fullmatch(r"[0-9a-f]{64}\.jpg",filename):
        raise HTTPException(404,"图片不存在")
    file = settings.data / "uploads" / filename
    if not file.is_file():
        raise HTTPException(404,"图片不存在")
    return FileResponse(file,media_type="image/jpeg")


@app.post("/api/trips/{trip_id}/messages",status_code=202)
async def message(trip_id: str,body: MessageInput):
    run_id = await app.state.planner.submit(trip_id,body.base_version,body.content,body.requirements,body.retry,
                                          resume=body.resume,
                                          refresh_resources=body.refresh_resources,
                                          resource_ids=body.resource_ids,
                                          updated_at=body.updated_at,
                                          selected_source_ids=body.selected_source_ids,
                                          interview_response=body.interview_response.model_dump(mode="json") if body.interview_response else None)
    return {"run_id":run_id}


@app.post("/api/trips/{trip_id}/selection",status_code=202)
async def selection(trip_id: str,body: SelectionInput):
    run_id = await app.state.planner.submit(trip_id,body.base_version,candidate_id=body.candidate_id,updated_at=body.updated_at,
                                          candidate_revision=body.draft_revision,candidate_hash=body.plan_hash)
    return {"run_id":run_id}


@app.post("/api/trips/{trip_id}/decisions/reopen",status_code=202)
async def reopen_decision(trip_id: str,body: DecisionReopenInput):
    run_id = await app.state.planner.reopen_decision(trip_id,body.base_version,body.updated_at,body.history_index)
    return {"run_id":run_id}


@app.post("/api/trips/{trip_id}/workflow/back")
async def back_workflow(trip_id: str,body: WorkflowBackInput):
    run_id = await app.state.planner.back_to_step(trip_id,body.base_version,body.updated_at,body.step)
    return JSONResponse({"run_id":run_id},status_code=202 if run_id else 200)


@app.post("/api/trips/{trip_id}/hotels/area",status_code=202)
async def change_hotel_area(trip_id: str,body: HotelAreaInput):
    run_id = await app.state.planner.submit(trip_id,body.base_version,updated_at=body.updated_at,hotel_area_choice=body.model_dump())
    return {"run_id":run_id}


@app.post("/api/trips/{trip_id}/resources/select",status_code=202)
async def select_resource(trip_id: str,body: ResourceSelectionInput):
    run_id = await app.state.planner.submit(trip_id,body.base_version,resource_choice=body.model_dump(mode="json"))
    return {"run_id":run_id}


@app.post("/api/trips/{trip_id}/products/search")
async def search_products(trip_id: str,body: ProductSearchInput):
    planner = app.state.planner
    async with planner.gate(trip_id):
        trip = planner.active(trip_id)
        if trip["version"] != body.base_version:
            raise Conflict("旅行已变化，请刷新后再搜索")
        doc, query = trip["document"],body.query.strip()
        req = doc["requirements"]
        city = body.city.strip() or next(iter(req["cities"]),"")
        hotel_search = body.mode == "hotel"
        price_range = {"min_price":body.min_price,"max_price":body.max_price,"hotel_location":body.hotel_location,"hotel_area":body.hotel_area}
        areas = [None]
        context = {"city":city,"query":query,"mode":body.mode}
        if hotel_search:
            if not city:
                raise HTTPException(422,"酒店查询需要目的地城市")
            hotel_context = next((entry["context"] for entry in (doc.get("itinerary") or {}).get("hotels",[]) if entry["city"] == city),{}) if not doc.get("itinerary_outdated") else {}
            check_in = body.check_in.isoformat() if body.check_in else hotel_context.get("check_in") or req.get("start_date")
            check_out = body.check_out.isoformat() if body.check_out else hotel_context.get("check_out") or req.get("end_date")
            from datetime import date
            if not check_in or not check_out or not 1 <= (date.fromisoformat(check_out)-date.fromisoformat(check_in)).days <= 30:
                raise HTTPException(422,"请填写入住和退房日期，支持1–30晚，退房必须晚于入住")
            context.update(check_in=check_in,check_out=check_out)
            context.update({key:str(value) for key,value in price_range.items() if value is not None})
            if body.hotel_location == "custom":
                areas = [body.hotel_area]
            elif body.hotel_location == "route":
                recommended = recommended_hotel_areas(doc,city)
                if body.hotel_area and body.hotel_area not in recommended:
                    raise HTTPException(422,"住宿推荐区域已变化，请重新选择，或切换为自己指定区域")
                areas = [body.hotel_area] if body.hotel_area else recommended or [None]
            flags = {"dest-name":city,"key-words":query or None,"hotel-bed-types":body.bed_type or None,"hotel-stars":body.hotel_stars or None,
                     "sort":"price_asc","check-in-date":check_in,"check-out-date":check_out,"max-price":body.max_price}
        elif body.mode == "poi":
            if not city:
                raise HTTPException(422,"景点门票查询需要城市")
            context["date"] = body.visit_date.isoformat() if body.visit_date else req.get("start_date")
            flags = {"city-name":city,"keyword":query or None}
        try:
            if hotel_search:
                responses, failures, found = [],[],{}
                for area in areas:
                    try:
                        response = await planner.flyai.search(trip_id,body.mode,{**context,"search_area":area or ""},{**flags,"poi-name":area},fresh=True)
                        responses.append(response)
                        for option in response["options"]:
                            found.setdefault(str(option.get("supplier_id") or option["id"]),{**option,"search_area":area})
                    except ToolError as exc:
                        failures.append(exc)
                        break
                if not responses:
                    raise failures[0]
                result = {**responses[-1],"options":list(found.values()),
                          "status":"ok" if found else next((response.get("status") for response in responses if response.get("status") not in {"ok","empty"}),"empty"),
                          "message":"；".join(dict.fromkeys([response.get("message") for response in responses if response.get("message")]+[str(failure) for failure in failures]))}
            else:
                result = await planner.flyai.search(trip_id,body.mode,context,flags,fresh=True)
        except ToolError as exc:
            doc["product_search"] = {"query":query,"city":city,"mode":body.mode,**price_range,"status":"error","code":exc.code,"message":str(exc),"queried_at":now(),"result_ids":[]}
            app.state.store.update(trip_id,doc)
            raise
        if hotel_search and (body.min_price is not None or body.max_price is not None):
            # shortcut: the minimum filters returned candidates; use a provider lower bound when available.
            options = [option for option in result["options"] if option.get("price") is not None and option.get("currency") == "CNY"
                       and (option.get("price_basis") == "room_night" or (option.get("price_basis") == "unknown" and option.get("reference_unit") == "room_night"))
                       and (body.min_price is None or option["price"] >= body.min_price)
                       and (body.max_price is None or option["price"] <= body.max_price)]
            if len(options) != len(result["options"]):
                result = {**result,"options":options,"status":"ok" if options else "empty",
                          "message":(result.get("message") or "")+"价格筛选已排除区间外、未报价及币种或每晚价格口径不明的候选。"}
        products = []
        for option in result["options"]:
            # Search results remain references; itinerary resources use their own explicit selection flow.
            products.append({**option,"city":(option.get("raw") or {}).get("cityName") or city,"search_mode":body.mode,
                             "reference_only":True,"map_place":None})
        previous = {item["id"]:item for item in doc.get("products",[])}
        for item in products:
            previous[item["id"]] = {**previous.get(item["id"],{}),**item}
        doc["products"] = list(previous.values())[-48:]
        doc["product_search"] = {"query":query,"city":city,"mode":body.mode,**price_range,"summary":result.get("summary", ""),"platform_hint":result.get("platform_hint", ""),
                                 "queried_at":result["queried_at"],"message":result.get("message", ""),"status":result.get("status"),"code":result.get("code"),"result_ids":[item["id"] for item in products]}
        app.state.store.update(trip_id,doc)
        return {"products":products,**doc["product_search"],"updated_at":app.state.store.trip(trip_id)["updated_at"]}


@app.post("/api/trips/{trip_id}/products/select")
async def select_product(trip_id: str,body: ProductSelectionInput):
    async with app.state.planner.gate(trip_id):
        trip = app.state.planner.active(trip_id)
        if trip["version"] != body.base_version or trip["updated_at"] != body.updated_at:
            raise Conflict("旅行已变化，请刷新后再收藏酒店或门票参考")
        doc = trip["document"]
        if not any(item["id"] == body.product_id and item.get("search_mode") in {"hotel","poi","marriott_hotel"}
                   for item in doc.get("products",[])):
            raise Conflict("酒店或门票参考已不存在，请重新查询")
        doc["reference_product_ids"] = list(dict.fromkeys([*doc.get("reference_product_ids",[]),body.product_id]))[-48:]
        app.state.store.update(trip_id,doc)
        return {"saved":True,"updated_at":app.state.store.trip(trip_id)["updated_at"]}


def current_map_trip(trip_id,body):
    trip = app.state.planner.active(trip_id)
    if trip["version"] != body.base_version or trip["updated_at"] != body.updated_at:
        raise Conflict("行程已变化，请刷新后再打开地图")
    if trip["document"].get("itinerary_outdated") or trip["document"].get("pending") or not trip["document"].get("itinerary"):
        raise Conflict("请先完成当前行程，再生成地图入口")
    return trip


@app.post("/api/trips/{trip_id}/maps")
async def export_map(trip_id: str,body: ItinerarySaveInput):
    planner = app.state.planner
    async with planner.gate(trip_id):
        trip = current_map_trip(trip_id,body)
        doc,lines,skipped = trip["document"],[],[]
        itinerary = doc["itinerary"]
        for day in itinerary["days"]:
            places = []
            first_move = next((activity.get("move_before",{}) for activity in day["activities"] if activity["kind"] == "visit"),{})
            if first_move.get("origin_place"):
                places.append(first_move["origin_place"])
            for activity in day["activities"]:
                if activity["kind"] != "visit":
                    continue
                place = activity.get("map_place")
                if not coordinates(place):
                    skipped.append(day["date"]+" "+activity["name"])
                else:
                    places.append(place)
            if day.get("return_move",{}).get("destination_place"):
                places.append(day["return_move"]["destination_place"])
            points = []
            for place in places:
                location = coordinates(place)
                if location and (not points or (points[-1]["lon"],points[-1]["lat"]) != location):
                    points.append({"name":place["name"],"lon":location[0],"lat":location[1],**({"poiId":place["id"]} if place.get("id") else {})})
            if points:
                skipped.extend(day["date"]+" "+point["name"]+"（超过单日16点导出限制）" for point in points[16:])
                lines.append({"title":day["date"]+" "+day["city"],"pointInfoList":points[:16]})
        if not lines:
            raise ToolError("map_empty","还没有已确认坐标的行程地点，请先补全地点")
        async with planner.amap.batch():
            links = await planner.amap.export_map(trip_id,trip["title"],lines)
        if not links:
            raise ToolError("map_link","高德未返回可识别的地图链接，未生成替代地址")
        app_uris = [link for link in links if link.startswith("amapuri:")]
        web_urls = [link for link in links if link.startswith(("https://","http://"))]
        result = {"links":links,"app_uri":next(iter(app_uris),None),"web_url":next(iter(web_urls),None),
                  "status":"app_only" if not web_urls else "web_entry","skipped":skipped,"queried_at":now(),
                  "note":"用于导入手机高德 App；电脑查看请使用上方网页地图。扫码唤起取决于手机扫描器及高德客户端。"}
        itinerary["personal_map"] = result
        app.state.store.update(trip_id,doc)
        return result


@app.post("/api/trips/{trip_id}/taxi")
async def taxi_link(trip_id: str,body: TaxiInput):
    planner = app.state.planner
    async with planner.gate(trip_id):
        trip = current_map_trip(trip_id,body)
        if not trip["document"]["requirements"].get("allow_taxi"):
            raise Conflict("当前需求未允许打车，请先修改旅行需求")
        day = next((day for day in trip["document"]["itinerary"]["days"] if day["date"] == body.date.isoformat()),None)
        activity = next((activity for activity in (day or {}).get("activities",[]) if activity["id"] == body.activity_id),None)
        move = (activity or {}).get("move_before",{})
        if not coordinates(move.get("origin_place")) or not coordinates(move.get("destination_place")):
            raise Conflict("该段起终点尚未定位，不能生成打车入口")
        async with planner.amap.batch():
            links = await planner.amap.taxi_link(trip_id,move["origin_place"],move["destination_place"])
        if not links:
            raise ToolError("taxi_link","高德未返回可识别的打车入口")
        return {"links":links,"note":"打开高德后由你确认叫车；未创建订单，也未确认车费"}


@app.post("/api/trips/{trip_id}/candidates/revise",status_code=202)
async def revise_candidate(trip_id: str,body: CandidateRevisionInput):
    run_id = await app.state.planner.submit(trip_id,body.base_version,route_edit=body.model_dump(mode="json",exclude={"base_version","updated_at"}),updated_at=body.updated_at)
    return {"run_id":run_id}


@app.patch("/api/trips/{trip_id}/locks")
async def lock(trip_id: str,body: LockInput):
    async with app.state.planner.gate(trip_id):
        trip = app.state.planner.active(trip_id)
        if trip["version"] != body.base_version:
            raise Conflict("行程已更新，请刷新后操作")
        doc = trip["document"]; itinerary = doc.get("itinerary") or {}
        ids = {x["id"] for group in ("transport","hotels") for x in itinerary.get(group,[])}
        ids |= {a["id"] for d in itinerary.get("days",[]) for a in d.get("activities",[])}
        if body.item_id not in ids:
            raise HTTPException(422,"当前行程中没有这个项目")
        locks = set(doc.get("locks",[]))
        if body.locked:
            locks.add(body.item_id)
        else:
            locks.discard(body.item_id)
        doc["locks"] = sorted(locks)
        if not doc.get("confirmed_plan"):
            doc["itinerary_saved_version"] = None
            if doc["stage"] == "completed":
                doc["stage"] = "ready"
        app.state.store.update(trip_id,doc)
        return {"version":trip["version"],"locks":doc["locks"]}


@app.post("/api/trips/{trip_id}/itinerary/save")
async def save_itinerary(trip_id: str,body: ItinerarySaveInput):
    async with app.state.planner.gate(trip_id):
        trip = app.state.planner.active(trip_id)
        if trip["version"] != body.base_version or trip["updated_at"] != body.updated_at:
            raise Conflict("行程已变化，请刷新后重新保存")
        doc = trip["document"]
        if not doc.get("itinerary") or doc.get("itinerary_outdated") or doc.get("pending") or doc["stage"] not in {"ready","completed","incomplete","conflicts"}:
            raise Conflict("请先完成当前行程的生成，再保存")
        app.state.planner.assert_document_review(trip)
        if doc.get("itinerary_saved_version") is not None:
            return {"version":doc["itinerary_saved_version"]}
        doc["itinerary_saved_version"] = trip["version"] + 1
        if doc["stage"] == "ready":
            doc["stage"] = "completed"
        version = app.state.store.commit(trip_id,body.base_version,doc,updated_at=body.updated_at)
        return {"version":version}


@app.delete("/api/trips/{trip_id}/itinerary")
async def delete_itinerary(trip_id: str,base_version: int = Query(ge=0),updated_at: str = Query(min_length=1),
                           version: int | None = Query(default=None,ge=1)):
    async with app.state.planner.gate(trip_id):
        trip = app.state.planner.active(trip_id)
        if version is None:
            version = trip["document"].get("itinerary_saved_version")
        app.state.store.delete_itinerary(trip_id,base_version,updated_at,version)
        return {"deleted":True}


@app.get("/api/trips/{trip_id}/versions")
async def versions(trip_id: str,include_documents: bool = True):
    app.state.store.trip(trip_id)
    return app.state.store.versions(trip_id,include_documents=include_documents)


@app.get("/api/trips/{trip_id}/versions/{version}")
async def saved_version(trip_id: str,version: int):
    app.state.store.trip(trip_id)
    return app.state.store.version(trip_id,version)


@app.get("/api/runs/{run_id}/events")
async def events(run_id: str,request: Request,after: int = 0):
    app.state.store.run(run_id)
    try:
        cursor = max(after,int(request.headers.get("last-event-id","0")))
    except ValueError:
        raise HTTPException(422,"事件序号必须为整数")

    async def stream():
        nonlocal cursor
        while not await request.is_disconnected():
            rows = app.state.store.events(run_id,cursor)
            for row in rows:
                cursor = row["id"]
                yield f"id: {cursor}\ndata: {dump(row)}\n\n"
            try:
                status = app.state.store.run(run_id)["status"]
            except KeyError:
                yield f"event: end\ndata: {dump({'status':'deleted'})}\n\n"
                break
            if not rows and status != "running":
                yield f"event: end\ndata: {dump({'status':status})}\n\n"
                break
            yield ": heartbeat\n\n"
            await asyncio.sleep(1)

    return StreamingResponse(stream(),media_type="text/event-stream",
                             headers={"Cache-Control":"no-cache","X-Accel-Buffering":"no"})


mount_frontend(app, PROJECT_ROOT / "frontend" / "dist")
