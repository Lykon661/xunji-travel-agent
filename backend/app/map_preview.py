import io
import json
import os
import re

import httpx
from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import Response

from .adapters import map_urls
from .models import AmapWebKeyInput


class MapPreviewConfig:
    def __init__(self, folder):
        self.path = folder / "amap-web-config.json"
        self.js_key = ""
        self.security_code = ""
        if self.path.is_file():
            value = json.loads(self.path.read_text(encoding="utf-8"))
            self.js_key = value.get("js_key","")
            self.security_code = value.get("security_code","")

    def public(self):
        return {"configured":bool(self.js_key and self.security_code),
                "has_js_key":bool(self.js_key),"has_security_code":bool(self.security_code)}

    def save(self, js_key, security_code):
        key = js_key.strip()
        code = security_code.strip() or (self.security_code if key == self.js_key else "")
        if key and (not code or not all(re.fullmatch(r"[A-Za-z0-9_-]{1,256}",value) for value in (key,code))):
            raise HTTPException(422,"请同时填写对应的高德 Web端(JSAPI) Key 与安全密钥")
        if not key:
            code = ""
        self.path.parent.mkdir(parents=True,exist_ok=True)
        temporary = self.path.with_suffix(".json.tmp")
        try:
            with temporary.open("w",encoding="utf-8") as output:
                os.chmod(temporary,0o600)
                json.dump({"js_key":key,"security_code":code},output)
                output.flush()
                os.fsync(output.fileno())
            os.replace(temporary,self.path)
        finally:
            temporary.unlink(missing_ok=True)
        self.js_key,self.security_code = key,code
        return self.public()


router = APIRouter()


@router.get("/api/system/amap/web")
async def configuration(request: Request):
    return request.app.state.map_preview.public()


def editable(request):
    planner = request.app.state.planner
    if any(not task.done() for task in planner.tasks.values()) or any(lock.locked() for lock in planner.gates.values()):
        raise HTTPException(409,"正在处理旅行任务，请结束后再修改地图配置")


@router.put("/api/system/amap/web")
async def save_configuration(request: Request, body: AmapWebKeyInput):
    editable(request)
    if not body.js_key.get_secret_value().strip():
        raise HTTPException(422,"请填写高德 Web端(JSAPI) Key")
    try:
        return request.app.state.map_preview.save(body.js_key.get_secret_value(),body.security_code.get_secret_value())
    except OSError as exc:
        raise HTTPException(500,"无法保存网页地图配置，请检查数据目录权限") from exc


@router.delete("/api/system/amap/web")
async def clear_configuration(request: Request):
    editable(request)
    try:
        return request.app.state.map_preview.save("","")
    except OSError as exc:
        raise HTTPException(500,"无法清除网页地图配置") from exc


@router.get("/api/maps/web-config")
async def browser_configuration(request: Request):
    config = request.app.state.map_preview
    # JSAPI's browser key is public by design; jscode stays on the server.
    return {**config.public(),"js_key":config.js_key if config.public()["configured"] else ""}


def proxy_target(path):
    if not re.fullmatch(r"v[345]/[A-Za-z0-9_/-]+",path) or ".." in path or "//" in path:
        raise HTTPException(404,"不支持的地图服务路径")
    if path == "v4/map/styles":
        return "https://webapi.amap.com/"+path
    if path == "v3/vectormap":
        return "https://fmap01.amap.com/"+path
    return "https://restapi.amap.com/"+path


@router.get("/_AMapService/{path:path}")
async def proxy(request: Request, path: str):
    config = request.app.state.map_preview
    if not config.public()["configured"]:
        raise HTTPException(400,"网页地图尚未配置 JSAPI Key 和安全密钥")
    allowed = {"http://localhost:8000","http://127.0.0.1:8000","http://localhost:5173","http://127.0.0.1:5173"}
    if request.headers.get("sec-fetch-site") == "cross-site" or (request.headers.get("origin") and request.headers["origin"] not in allowed):
        raise HTTPException(403,"不允许此来源调用本机地图代理")
    target = proxy_target(path)
    parameters = [(key,value) for key,value in request.query_params.multi_items() if key.lower() not in {"key","jscode","securityjscode"}]
    parameters += [("key",config.js_key),("jscode",config.security_code)]
    try:
        async with httpx.AsyncClient(timeout=25,follow_redirects=False) as client:
            async with client.stream("GET",target,params=parameters) as upstream:
                if upstream.status_code != 200:
                    raise HTTPException(502,"高德地图服务暂不可用，请稍后重试")
                content = bytearray()
                async for chunk in upstream.aiter_bytes():
                    content.extend(chunk)
                    if len(content) > 4 * 1024 * 1024:
                        raise HTTPException(502,"地图服务响应过大")
                return Response(bytes(content),headers={"Content-Type":upstream.headers.get("content-type","application/json"),"Cache-Control":"no-store"})
    except httpx.HTTPError as exc:
        raise HTTPException(502,"无法连接高德网页地图服务，请检查网络后重试") from exc


@router.get("/api/trips/{trip_id}/maps/qrcode")
async def map_qrcode(request: Request, trip_id: str, index: int = Query(default=0,ge=0), version: int | None = Query(default=None,ge=1), at: str = ""):
    store = request.app.state.store
    trip = store.trip(trip_id)
    doc = trip["document"]
    if version:
        try:
            saved = store.version(trip_id,version)
        except KeyError:
            raise HTTPException(404,"该行程版本已不存在")
        doc = saved["document"]
    personal_map = (doc.get("itinerary") or {}).get("personal_map") or {}
    if at and personal_map.get("queried_at") != at:
        raise HTTPException(409,"地图导出已变化，请刷新后重试")
    links = map_urls(personal_map.get("links",[]))
    if index >= len(links):
        raise HTTPException(404,"地图导出链接已变化，请刷新后重试")
    if len(links[index].encode("utf-8")) > 2000:
        raise HTTPException(400,"地图链接过长，请使用复制地址入口")
    try:
        import qrcode
    except ImportError as exc:
        raise HTTPException(503,"二维码组件尚未安装，请停止服务后运行 build.bat 更新依赖") from exc
    output = io.BytesIO()
    qrcode.make(links[index],box_size=6,border=4).save(output,format="PNG")
    return Response(output.getvalue(),media_type="image/png",headers={"Cache-Control":"no-store"})
