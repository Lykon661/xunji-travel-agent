"""Dependency preflight used by an explicit installation/start action, without opening a browser."""
from contextlib import redirect_stdout
import json
from pathlib import Path
import sys
import traceback


def main():
    if sys.version_info[:2] != (3, 11):
        return {"ready":False,"message":"采集器需要 Python 3.11，请点击“精简并修复安装”重建独立环境"}
    checkout = Path(sys.argv[1]).resolve()
    sys.path.insert(0, str(checkout))
    try:
        with redirect_stdout(sys.stderr):
            import config
            config.SAVE_DATA_OPTION = "jsonl"
            config.ENABLE_GET_WORDCLOUD = False
            config.ENABLE_IP_PROXY = False
            from media_platform.xhs import XiaoHongShuCrawler
            from xhshow import Xhshow
            from playwright.sync_api import sync_playwright
            with sync_playwright() as runtime:
                executable = Path(runtime.chromium.executable_path)
                if not executable.is_file():
                    return {"ready":False,"message":"Chromium 尚未安装，请点击“精简并修复安装”补齐浏览器驱动"}
        return {"ready":True,"message":"采集器运行依赖已就绪"}
    except ModuleNotFoundError as exc:
        traceback.print_exc(file=sys.stderr)
        return {"ready":False,"message":f"采集器缺少依赖 {exc.name}，请点击“精简并修复安装”"}
    except Exception:
        traceback.print_exc(file=sys.stderr)
        return {"ready":False,"message":"采集器运行依赖无法加载，请修复安装；详情见 backend/data/crawler-start.log"}


if __name__ == "__main__":
    result = main()
    print(json.dumps(result, ensure_ascii=False))
    sys.exit(0 if result["ready"] else 1)
