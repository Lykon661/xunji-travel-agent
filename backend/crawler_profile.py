"""Reproducible optional-import patches for the pinned Xiaohongshu/JSONL profile.

Upstream files retain their copyright/license headers. Apply only during installation,
never while importing the crawler or polling its status.
"""
import argparse
import hashlib
import json
from pathlib import Path


PROFILE = "xhs-jsonl-lite-v1"
ROOT = Path(__file__).resolve().parent
REQUIREMENTS = ROOT / "crawler-requirements.txt"

# These packages belong to optional upstream features, not the declared lite profile.
REMOVE_PACKAGES = (
    "opencv-python", "opencv-python-headless", "numpy", "pandas", "jieba", "wordcloud", "matplotlib",
    "fonttools", "contourpy", "cycler", "kiwisolver", "pyparsing", "pytz", "python-dateutil", "tzdata",
    "aiomysql", "PyMySQL", "asyncmy", "asyncpg", "aiosqlite", "SQLAlchemy", "alembic", "Mako",
    "motor", "pymongo", "dnspython", "redis", "openpyxl", "et-xmlfile",
    "pytest", "pytest-asyncio", "iniconfig", "pluggy",
    "fastapi", "uvicorn", "starlette", "typer", "shellingham", "rich", "markdown-it-py", "mdurl", "Pygments",
    "parsel", "cssselect", "lxml", "w3lib", "pyexecjs", "cryptography",
)

OPTIONAL_STORES = '''def load_optional_stores():
    # Xunji lite profile: database drivers are loaded only for database storage.
    global select, update, delete, AsyncSession, Session, get_session, XhsNote, XhsNoteComment, MongoDBStoreBase
    from sqlalchemy import select, update, delete
    from sqlalchemy.ext.asyncio import AsyncSession
    from sqlalchemy.orm import Session
    from database.db_session import get_session
    from database.models import XhsNote, XhsNoteComment
    from database.mongodb_store_base import MongoDBStoreBase


'''

PATCHES = {
    "proxy/proxy_ip_pool.py": [
        ("from typing import Dict, List\n", "from typing import Callable, Dict, List\n"),
        ("IpProxyProvider: Dict[str, ProxyProvider] = {\n"
         "    ProviderNameEnum.KUAI_DAILI_PROVIDER.value: new_kuai_daili_proxy(),\n"
         "    ProviderNameEnum.WANDOU_HTTP_PROVIDER.value: new_wandou_http_proxy(),\n"
         "    ProviderNameEnum.STATIC_PROVIDER.value: StaticProxyProvider(),\n"
         "}\n",
         "# Xunji lite profile: do not create Redis-backed providers while importing.\n"
         "IpProxyProvider: Dict[str, Callable[[], ProxyProvider]] = {\n"
         "    ProviderNameEnum.KUAI_DAILI_PROVIDER.value: new_kuai_daili_proxy,\n"
         "    ProviderNameEnum.WANDOU_HTTP_PROVIDER.value: new_wandou_http_proxy,\n"
         "    ProviderNameEnum.STATIC_PROVIDER.value: StaticProxyProvider,\n"
         "}\n"),
        ("        ip_provider=ip_provider,\n", "        ip_provider=ip_provider(),\n"),
    ],
    "tools/utils.py": [
        ("from .slider_util import *\n",
         "# Xunji lite profile: image/slider utilities are outside the supported collection flow.\n"),
    ],
    "tools/async_file_writer.py": [
        ("from tools.words import AsyncWordCloudGenerator\n", "# Xunji lite profile: wordcloud is an optional import.\n"),
        ("        self.wordcloud_generator = AsyncWordCloudGenerator() if config.ENABLE_GET_WORDCLOUD else None\n",
         "        self.wordcloud_generator = None\n"
         "        if config.ENABLE_GET_WORDCLOUD:\n"
         "            from tools.words import AsyncWordCloudGenerator\n"
         "            self.wordcloud_generator = AsyncWordCloudGenerator()\n"),
    ],
    "var.py": [
        ("import aiomysql\n", "from typing import TYPE_CHECKING\n\nif TYPE_CHECKING:\n    import aiomysql\n"),
        ("db_conn_pool_var: ContextVar[aiomysql.Pool]", "db_conn_pool_var: ContextVar[\"aiomysql.Pool\"]"),
    ],
    "store/xhs/_store_impl.py": [
        ("import json\n", "from __future__ import annotations\n\nimport json\n"),
        ("from sqlalchemy import select, update, delete\nfrom sqlalchemy.ext.asyncio import AsyncSession\nfrom sqlalchemy.orm import Session\n",
         "# Xunji lite profile: SQL imports are deferred to load_optional_stores().\n"),
        ("from database.db_session import get_session\nfrom database.models import XhsNote, XhsNoteComment\n",
         "# Xunji lite profile: database sessions are optional.\n"),
        ("from database.mongodb_store_base import MongoDBStoreBase\n", "# Xunji lite profile: MongoDB is optional.\n"),
        ("from store.excel_store_base import ExcelStoreBase\n\nclass XhsCsvStoreImplement",
         OPTIONAL_STORES + "class XhsCsvStoreImplement"),
    ],
    "store/xhs/__init__.py": [
        ("        store_class = XhsStoreFactory.STORES.get(config.SAVE_DATA_OPTION)\n",
         "        if config.SAVE_DATA_OPTION not in {\"jsonl\", \"json\", \"csv\", \"excel\"}:\n"
         "            load_optional_stores()\n"
         "        store_class = XhsStoreFactory.STORES.get(config.SAVE_DATA_OPTION)\n"),
    ],
}


def profile_digest():
    return hashlib.sha256(Path(__file__).read_bytes()+b"\0"+REQUIREMENTS.read_bytes()).hexdigest()


def transform(relative, text):
    text = text.replace("\r\n", "\n")
    for before, after in PATCHES[relative]:
        if text.count(after) == 1:
            continue
        if text.count(before) != 1:
            raise ValueError(f"采集器轻量补丁不匹配：{relative}；请使用项目要求的固定版本，未覆盖该文件")
        text = text.replace(before, after, 1)
    return text


def profile_files(checkout):
    root = Path(checkout).resolve()
    result = {}
    for relative in PATCHES:
        target = root / relative
        resolved = target.resolve()
        resolved.relative_to(root)
        if target.is_symlink() or any(parent.is_symlink() for parent in target.parents if parent != root and root in parent.parents):
            raise ValueError("轻量补丁不能修改链接目录")
        result[relative] = target
    return result


def source_hashes(checkout):
    return {relative:hashlib.sha256(path.read_bytes()).hexdigest() for relative,path in profile_files(checkout).items()}


def apply_profile(checkout):
    updates = []
    # Resolve every patch before writing, so a different upstream layout changes no files.
    for relative, path in profile_files(checkout).items():
        original = path.read_bytes()
        updated = transform(relative, original.decode("utf-8")).encode("utf-8")
        if original != updated:
            updates.append((path, original, updated))
    written = []
    try:
        for path, original, updated in updates:
            temporary = path.with_name(path.name+".xunji.tmp")
            try:
                temporary.write_bytes(updated)
                temporary.replace(path)
            finally:
                temporary.unlink(missing_ok=True)
            written.append((path, original))
    except Exception:
        for path, original in reversed(written):
            path.write_bytes(original)
        raise
    return {"profile":PROFILE,"profile_sha256":profile_digest(),"source_sha256":source_hashes(checkout)}


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("checkout", type=Path)
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove-packages", action="store_true")
    arguments = parser.parse_args()
    if arguments.remove_packages:
        print("\n".join(REMOVE_PACKAGES))
    elif arguments.apply:
        print(json.dumps(apply_profile(arguments.checkout),ensure_ascii=False))
    else:
        print(json.dumps({"profile":PROFILE,"profile_sha256":profile_digest(),"source_sha256":source_hashes(arguments.checkout)}))
