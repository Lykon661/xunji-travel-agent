"""Run inside MediaCrawler's isolated environment with the installer-managed lite profile."""
import asyncio
import json
import re
import sys
import time
from pathlib import Path


class CollectionProtected(RuntimeError):
    pass


class LoginRequired(RuntimeError):
    pass


def is_travel_guide_item(item: dict, city: str = "") -> bool:
    """Require guide terms before a detail request; city alone is insufficient."""
    if item.get("model_type") in {"rec_query", "hot_query"}:
        return False
    card = item.get("note_card")
    if not isinstance(card, dict) or card.get("type") == "video":
        return False
    title = card.get("display_title")
    if not isinstance(title, str) or not title.strip():
        return False
    if re.search(r"找工作|租房|求职|面试|上班|通勤|搬家|落户", title):
        return False
    return bool(re.search(
        r"攻略|路线|行程|city\s*walk|(?<![a-z])day(?![a-z])|自由行|一日游|"
        r"[0-9零一二两三四五六七八九十百几]+\s*(?:天|日)|几天几晚|"
        r"景点|必去|必打卡|打卡|好去处|游玩|旅游|旅行|玩法|逛吃",
        title, re.IGNORECASE))


def has_guide_content(note: dict) -> bool:
    """Keep substantial text or image guides; topic tags do not count as text."""
    text = re.sub(r"#[^#]*#", "", note.get("desc") or "")
    text = re.sub(r"\s+", "", text)
    return len(text) >= 80 or len(note.get("image_list") or []) >= 3


async def main():
    checkout = Path(sys.argv[1]).resolve()
    request = json.loads(Path(sys.argv[2]).read_text(encoding="utf-8"))
    sys.path.insert(0,str(checkout))
    import config
    login_only = request.get("action") == "login"
    config.ENABLE_CDP_MODE = False
    config.HEADLESS = not login_only
    config.SAVE_LOGIN_STATE = True
    config.ENABLE_IP_PROXY = False
    config.ENABLE_GET_COMMENTS = False
    config.ENABLE_GET_SUB_COMMENTS = False
    config.ENABLE_GET_MEIDAS = False
    config.ENABLE_GET_WORDCLOUD = False
    config.SORT_TYPE = "popularity_descending"
    config.MAX_CONCURRENCY_NUM = 1
    config.SAVE_DATA_OPTION = "jsonl"
    config.SAVE_DATA_PATH = request["output"]
    config.CRAWLER_MAX_NOTES_COUNT = 20
    config.PLATFORM = "xhs"
    config.LOGIN_TYPE = "qrcode"
    config.KEYWORDS = next(iter(request["keywords"]), "")
    config.CRAWLER_TYPE = "detail" if request["url"] else "search"
    if request["url"]:
        config.XHS_SPECIFIED_NOTE_URL_LIST = [request["url"]]
    from media_platform.xhs import XiaoHongShuCrawler
    from media_platform.xhs.client import XiaoHongShuClient
    from media_platform.xhs.field import SearchNoteType, SearchSortType
    from media_platform.xhs.help import get_search_id
    from store import xhs as xhs_store
    from var import source_keyword_var
    import httpx

    # Apply limits at the HTTP boundary, including the separate account-status request.
    original_send = httpx.AsyncClient.send
    last_finished, requests_sent = None, 0
    request_lock = asyncio.Lock()
    protection_file = Path(request["output"]) / "account-protection.json"

    async def guarded_send(client, http_request, **kwargs):
        nonlocal last_finished, requests_sent
        async with request_lock:
            if protection_file.exists():
                raise CollectionProtected("访问异常后已停止后续请求")
            if requests_sent >= 24:
                raise RuntimeError("本轮接口请求上限已到，停止采集")
            if last_finished is not None:
                await asyncio.sleep(max(0, 1.5 - (time.monotonic() - last_finished)))
            requests_sent += 1
            try:
                response = await original_send(client, http_request, **kwargs)
            finally:
                last_finished = time.monotonic()
            try:
                payload = response.json()
                payload = payload if isinstance(payload, dict) else {}
            except (ValueError, httpx.ResponseNotRead):
                payload = {}
            message = str(payload.get("msg", ""))
            restricted = (response.status_code in {401, 403, 429, 461, 471}
                          or str(payload.get("code")) in {"300011", "300012"}
                          or any(word in message.lower() for word in ("captcha", "验证码", "访问频繁", "访问受限", "账号异常", "账户异常")))
            if restricted:
                protection_file.write_text(json.dumps({"message":"检测到验证码或访问限制，采集已锁定；请先在官方客户端处理账号状态"}, ensure_ascii=False), encoding="utf-8")
                raise CollectionProtected("检测到验证码或访问限制，已立即停止")
            return response

    httpx.AsyncClient.send = guarded_send
    # The pinned client otherwise retries after one second; one failed request must end this round.
    XiaoHongShuClient.request = XiaoHongShuClient.request.__wrapped__

    async def checked_pong(client):
        info = await client.query_self()
        if info and info.get("data", {}).get("result", {}).get("success"):
            return True
        if login_only:
            return False
        raise LoginRequired("登录未生效，停止采集；请自行处理登录状态")

    XiaoHongShuClient.pong = checked_pong
    crawler = XiaoHongShuCrawler()

    async def search_small_batch():
        keyword = config.KEYWORDS
        if not keyword:
            return
        source_keyword_var.set(keyword)
        search_id = get_search_id()
        seen, collected = set(), 0
        for page in range(1, 4):
            if len(seen) >= 20 or collected >= 20:
                break
            response = await crawler.xhs_client.get_note_by_keyword(
                keyword=keyword, page=page, search_id=search_id,
                sort=SearchSortType.MOST_POPULAR, note_type=SearchNoteType.IMAGE)
            items = response.get("items") or []
            if not items:
                break
            for item in items:
                if len(seen) >= 20 or collected >= 20:
                    break
                note_id = item.get("id")
                if not note_id or note_id in seen or not is_travel_guide_item(item, request.get("city", "")):
                    continue
                # Rejected or missing details still consume the round's detail budget.
                seen.add(note_id)
                # Fetch details once, sequentially, without the upstream retry or HTML fallback.
                note = await crawler.xhs_client.get_note_by_id(note_id, item.get("xsec_source", ""), item.get("xsec_token", ""))
                if note and has_guide_content(note):
                    note.update(xsec_source=item.get("xsec_source", ""), xsec_token=item.get("xsec_token", ""))
                    await xhs_store.update_xhs_note(note)
                    collected += 1
            if not response.get("has_more", False):
                break

    crawler.search = search_small_batch
    if request["url"]:
        from media_platform.xhs.help import parse_note_info_from_note_url

        async def fetch_single_note():
            info = parse_note_info_from_note_url(request["url"])
            note = await crawler.xhs_client.get_note_by_id(info.note_id, info.xsec_source, info.xsec_token)
            if note:
                note.update(xsec_source=info.xsec_source, xsec_token=info.xsec_token)
                await xhs_store.update_xhs_note(note)

        crawler.get_specified_notes = fetch_single_note
    if login_only:
        async def confirm_login():
            if not await crawler.xhs_client.pong():
                raise RuntimeError("小红书登录未生效")
            Path(request["result"]).write_text(json.dumps({"logged_in":True}), encoding="utf-8")
        # The pinned crawler invokes search after login; replace it with a session check, without fetching posts.
        config.CRAWLER_TYPE = "search"
        crawler.search = confirm_login
    try:
        await crawler.start()
    except LoginRequired:
        (Path(request["output"]) / "login-required.json").write_text("{}", encoding="utf-8")
        raise
    finally:
        context = getattr(crawler,"browser_context",None)
        if context:
            try:
                await context.close()
            except Exception:
                pass  # Playwright may already have closed the context while leaving start().


if __name__ == "__main__":
    asyncio.run(main())
