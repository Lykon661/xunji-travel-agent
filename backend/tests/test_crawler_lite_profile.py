"""User-run profile cases. No platform requests or browser launches; not run by Codex."""
import hashlib
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.crawler_installation import installation_ready
from app.config import CRAWLER_REVISION
from crawler_profile import PATCHES, PROFILE, REMOVE_PACKAGES, apply_profile, profile_digest, source_hashes, transform


class InstallationMarker(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.checkout = Path(folder.name)
        self.python = self.checkout / ".venv/Scripts/python.exe"
        self.python.parent.mkdir(parents=True)
        self.python.write_bytes(b"")
        (self.checkout/".venv/pyvenv.cfg").write_text("version = 3.11.9\n",encoding="utf-8")
        (self.checkout/"main.py").write_text("",encoding="utf-8")
        (self.checkout/"requirements.txt").write_text("upstream-full-requirements",encoding="utf-8")
        for relative in PATCHES:
            target = self.checkout/relative
            target.parent.mkdir(parents=True,exist_ok=True)
            target.write_text("# synthetic source fingerprint\n",encoding="utf-8")
        settings = SimpleNamespace(crawler_python=self.python,crawler_dir=self.checkout)
        self.enterContext(patch("app.crawler_installation.settings",settings))
        self.marker = {"complete":True,"revision":CRAWLER_REVISION,
            "requirements_sha256":hashlib.sha256((self.checkout/"requirements.txt").read_bytes()).hexdigest()}

    def write_marker(self):
        (self.checkout/".venv/travel-agent-ready.json").write_text(json.dumps(self.marker),encoding="utf-8")

    def lite_marker(self):
        self.marker.update(profile=PROFILE,profile_sha256=profile_digest(),source_sha256=source_hashes(self.checkout))
        self.write_marker()

    def test_old_full_environment_still_works_until_user_explicitly_migrates(self):
        self.write_marker()
        self.assertTrue(installation_ready())

    def test_lite_marker_binds_both_profile_recipe_and_patched_sources(self):
        self.lite_marker()
        self.assertTrue(installation_ready())
        (self.checkout/"tools/utils.py").write_text("# changed after installation",encoding="utf-8")
        self.assertFalse(installation_ready())
        self.lite_marker()
        self.marker["profile_sha256"] = "old-profile"
        self.write_marker()
        self.assertFalse(installation_ready())

    def test_unknown_profiles_and_incomplete_installations_are_not_ready(self):
        self.lite_marker()
        for value in ("unknown-profile", ""):
            self.marker["profile"] = value
            self.write_marker()
            self.assertFalse(installation_ready())
        self.lite_marker()
        self.marker["complete"] = False
        self.write_marker()
        self.assertFalse(installation_ready())


class PatchFailure(unittest.TestCase):
    def test_unexpected_source_layout_is_not_silently_patched(self):
        with self.assertRaisesRegex(ValueError,"补丁不匹配"):
            transform("tools/utils.py","# unrecognized upstream\n")

    def test_patch_mismatch_preserves_every_source_file(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder)
            before = {}
            for relative in PATCHES:
                target = checkout/relative
                target.parent.mkdir(parents=True,exist_ok=True)
                content = "".join(before for before, _ in PATCHES[relative]).encode("utf-8")
                if relative == "tools/utils.py":
                    content = b"# unrecognized\n"
                target.write_bytes(content)
                before[relative] = content
            with self.assertRaises(ValueError):
                apply_profile(checkout)
            self.assertEqual({relative:(checkout/relative).read_bytes() for relative in PATCHES},before)

    def test_core_browser_http_and_signing_packages_are_never_pruned(self):
        self.assertFalse(set(REMOVE_PACKAGES) & {"playwright","pyee","greenlet","pillow","httpx","httpcore","anyio","certifi","idna","h11","pydantic","pydantic_core","typing_extensions","tenacity","aiofiles","xhshow","pycryptodome"})


CHECKOUT = Path(__file__).resolve().parents[2]/"external/MediaCrawler"
CRAWLER_PYTHON = CHECKOUT/".venv"/("Scripts/python.exe" if sys.platform == "win32" else "bin/python")


@unittest.skipUnless(CRAWLER_PYTHON.is_file(),"Requires the user's existing crawler environment; no packages are installed")
class IsolatedLiteImport(unittest.TestCase):
    def test_real_xhs_import_and_jsonl_write_with_optional_packages_blocked(self):
        with tempfile.TemporaryDirectory() as folder:
            checkout = Path(folder)
            for name in ("base","cache","config","media_platform","model","proxy","store","tools"):
                shutil.copytree(CHECKOUT/name,checkout/name,ignore=shutil.ignore_patterns("__pycache__","*.pyc"))
            shutil.copy2(CHECKOUT/"var.py",checkout/"var.py")
            manifest = apply_profile(checkout)
            patched = {relative:(checkout/relative).read_bytes() for relative in PATCHES}
            for relative, hunks in PATCHES.items():
                for _, replacement in hunks:
                    self.assertIn(replacement,patched[relative].decode("utf-8"))
            self.assertEqual(apply_profile(checkout),manifest)
            self.assertEqual({relative:(checkout/relative).read_bytes() for relative in PATCHES},patched)
            program = r'''
import asyncio, importlib.abc, json, pathlib, sys
sys.path.insert(0,sys.argv[1])
blocked = {"cv2","numpy","pandas","jieba","wordcloud","matplotlib","sqlalchemy","aiomysql","asyncmy","motor","pymongo","redis","openpyxl","cryptography"}
class NoOptionalPackages(importlib.abc.MetaPathFinder):
    def find_spec(self,fullname,path=None,target=None):
        if fullname.split(".")[0] in blocked:
            raise ModuleNotFoundError("Optional dependency imported: "+fullname,name=fullname)
sys.meta_path.insert(0,NoOptionalPackages())
import config
config.SAVE_DATA_OPTION = "jsonl"
config.SAVE_DATA_PATH = str(pathlib.Path(sys.argv[1])/"output")
config.ENABLE_IP_PROXY = False
config.ENABLE_GET_WORDCLOUD = False
from media_platform.xhs import XiaoHongShuCrawler
from store import xhs
from xhshow import Xhshow
from proxy.proxy_ip_pool import IpProxyProvider
assert IpProxyProvider and all(callable(factory) for factory in IpProxyProvider.values())
assert "cache.redis_cache" not in sys.modules, "Disabled proxy eagerly loaded Redis"
asyncio.run(xhs.update_xhs_note({"note_id":"synthetic-note","title":"合成攻略","desc":"景点资料","image_list":[],"tag_list":[]}))
files = list(pathlib.Path(config.SAVE_DATA_PATH).rglob("*.jsonl"))
assert len(files) == 1
record = json.loads(files[0].read_text(encoding="utf-8").strip())
assert record["note_id"] == "synthetic-note" and record["title"] == "合成攻略"
assert not (blocked & set(sys.modules)), "Optional modules were loaded"
'''
            environment = {**os.environ,"PYTHONDONTWRITEBYTECODE":"1","PYTHONUTF8":"1"}
            result = subprocess.run([str(CRAWLER_PYTHON),"-c",program,str(checkout)],cwd=checkout,
                                    env=environment,capture_output=True,text=True,encoding="utf-8",timeout=30)
            self.assertEqual(result.returncode,0,result.stderr)
