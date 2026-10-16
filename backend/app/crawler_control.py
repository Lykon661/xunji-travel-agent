"""Local UI control for the existing isolated MediaCrawler installation."""
import asyncio
import json
import time
from uuid import uuid4

from fastapi import APIRouter, Request

from .adapters import ToolError, process
from .config import CRAWLER_REVISION, PROJECT_ROOT, ROOT, settings
from .crawler_installation import installation_ready
from .store import Conflict, dump

router = APIRouter(prefix="/system/crawler")


class CrawlerControl:
    def __init__(self, crawler):
        self.crawler = crawler
        self.task = None
        self.stage = "idle"
        self.message = "可从这里安装、启动采集器，或打开小红书登录窗口。"

    def public(self):
        installed = installation_ready()
        present = (settings.crawler_dir / "main.py").is_file()
        busy = bool(self.task and not self.task.done())
        return {"installed":installed,"present":present,"enabled":self.crawler.enabled and installed,"busy":busy,
                "stage":self.stage,"message":self.crawler.blocked or self.message,"collecting":self.crawler.lock.locked() and not busy,
                "blocked":bool(self.crawler.blocked),"cooldown_seconds":max(0, int(self.crawler.next_allowed_at - time.time()) + 1)}

    def enable(self, value):
        self.crawler.set_enabled(value)

    async def check_installation(self):
        if not self.public()["installed"]:
            raise ToolError("crawler_config", "采集器依赖尚未安装完成，请先点击“精简并修复安装”，完成后再启动或登录")
        code, revision, _ = await process(["git","rev-parse","HEAD"], cwd=settings.crawler_dir)
        if code or revision.strip() != CRAWLER_REVISION:
            raise ToolError("crawler_revision", "采集器版本不匹配，请按 README 使用固定版本")
        code, output, errors = await process([str(settings.crawler_python),str(ROOT / "crawler_health.py"),
                                             str(settings.crawler_dir)], cwd=settings.crawler_dir, timeout=60)
        (settings.data / "crawler-start.log").write_text(output + "\n" + errors, encoding="utf-8")
        try:
            status = json.loads(output.strip().splitlines()[-1])
        except (ValueError, IndexError):
            status = {}
        if code or not status.get("ready"):
            raise ToolError("crawler_config", status.get("message") or "采集器运行依赖无法加载，请修复安装；详情见 backend/data/crawler-start.log")

    def idle(self):
        if self.public()["busy"] or self.crawler.lock.locked():
            raise Conflict("采集器正在安装、登录或搜索，请等待结束后再操作")

    def launch(self, operation):
        self.idle()
        if operation == "login":
            self.crawler.reserve_round()
        if operation == "install" and settings.crawler_python != (settings.crawler_dir / ".venv/Scripts/python.exe").resolve():
            raise ToolError("crawler_config", "当前使用自定义 Python 路径，请按 README 安装到该环境，或恢复默认 MEDIACRAWLER_PYTHON")
        self.enable(False)
        self.stage = "installing" if operation == "install" else "logging_in"
        self.message = "正在准备 Python 3.11、小红书轻量依赖和 Chromium；已有环境会移除可选大包，登录资料保留。" if operation == "install" else "正在打开小红书登录窗口，请在弹出的窗口扫码并完成验证。"
        self.task = asyncio.create_task(self.run(operation))
        return self.public()

    async def run(self, operation):
        try:
            async with self.crawler.lock:
                if operation == "install":
                    code, output, errors = await process([
                        "powershell.exe","-NoProfile","-ExecutionPolicy","Bypass","-File",
                        str(PROJECT_ROOT / "scripts/setup.ps1"),"-WithCrawler","-CrawlerOnly",
                        "-CrawlerDirectory",str(settings.crawler_dir)], cwd=PROJECT_ROOT, timeout=1800)
                    # Installation diagnostics remain on this machine, outside the public API.
                    (settings.data / "crawler-install.log").write_text(output + "\n" + errors, encoding="utf-8")
                    if code:
                        message = ("无法准备 Python 3.11，请安装 Python Install Manager 或 Python 3.11 后重试。"
                                   if "TRAVEL_CRAWLER_PYTHON_311" in output + errors else "采集器依赖安装未完成，请点击“精简并修复安装”重试。")
                        raise ToolError("crawler_install", message + "详情见 backend/data/crawler-install.log")
                    await self.check_installation()
                    self.stage = "idle"
                    self.message = "小红书轻量采集器已安装。点击“打开登录窗口”完成首次登录，或已有登录状态时直接启动。"
                else:
                    await self.check_installation()
                    folder = settings.data / "crawler" / uuid4().hex
                    folder.mkdir(parents=True, exist_ok=True)
                    request = folder / "request.json"
                    result = folder / "login-result.json"
                    request.write_text(dump({"action":"login","keywords":[],"url":None,
                                             "output":str(folder),"result":str(result)}), encoding="utf-8")
                    code, output, errors = await process([str(settings.crawler_python),str(ROOT / "crawler_bridge.py"),
                                               str(settings.crawler_dir),str(request)], cwd=settings.crawler_dir, timeout=600)
                    (settings.data / "crawler-login.log").write_text(output + "\n" + errors, encoding="utf-8")
                    self.crawler.check_result(folder)
                    if code or not result.is_file():
                        raise ToolError("crawler_login", "登录未完成或窗口无法打开，已保持停用；请先检查账号状态，不要连续扫码。详情见 backend/data/crawler-login.log")
                    self.stage = "idle"
                    self.message = "登录完成，采集器保持停用；账号正常且冷却结束后，可手动启动并搜索。"
        except asyncio.CancelledError:
            self.stage = "idle"
            self.message = "操作已取消，采集器已停用。"
            raise
        except (ToolError, OSError, TimeoutError) as exc:
            self.stage = "failed"
            self.message = str(exc) if isinstance(exc, ToolError) else "操作失败或超时，请检查安装环境和网络后重试。"
        except Exception:
            self.stage = "failed"
            self.message = "采集器操作失败，请重新安装或打开登录窗口后重试。"

    async def start(self):
        self.idle()
        if self.crawler.blocked:
            raise ToolError("crawler_protected", self.crawler.blocked)
        async with self.crawler.lock:
            await self.check_installation()
            self.enable(True)
        self.stage = "ready"
        self.message = "采集器已启动，将在搜索时采集攻略；首次使用或登录过期时请打开登录窗口。"
        return self.public()

    async def stop(self):
        self.enable(False)
        if self.task and not self.task.done():
            self.task.cancel()
            await asyncio.gather(self.task, return_exceptions=True)
        if self.crawler.collection_task and not self.crawler.collection_task.done():
            self.crawler.collection_task.cancel()
            await asyncio.gather(self.crawler.collection_task, return_exceptions=True)
        self.stage = "idle"
        self.message = "采集器已停用，进行中的采集请求已取消。"
        return self.public()

    async def unblock(self):
        self.idle()
        self.crawler.blocked = ""
        self.enable(False)
        self.stage = "idle"
        self.message = "已手动解除保护，采集器保持停用；低频采集也可能触发账号限制。"
        return self.public()


@router.get("")
async def state(request: Request):
    return request.app.state.crawler_control.public()


@router.post("/install", status_code=202)
async def install(request: Request):
    return request.app.state.crawler_control.launch("install")


@router.post("/login", status_code=202)
async def login(request: Request):
    return request.app.state.crawler_control.launch("login")


@router.post("/start")
async def start(request: Request):
    return await request.app.state.crawler_control.start()


@router.post("/stop")
async def stop(request: Request):
    return await request.app.state.crawler_control.stop()


@router.post("/unblock")
async def unblock(request: Request):
    return await request.app.state.crawler_control.unblock()
