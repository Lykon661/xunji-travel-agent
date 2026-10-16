"""Static-page delivery only; no planner, model, crawler or network calls."""
import tempfile
import unittest
from pathlib import Path

import httpx
from fastapi import FastAPI

from app.web import mount_frontend


class WebDelivery(unittest.IsolatedAsyncioTestCase):
    async def test_page_assets_and_api_do_not_shadow_each_other(self):
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            (root / "index.html").write_text("<html>travel</html>", encoding="utf-8")
            (root / "assets").mkdir()
            (root / "assets" / "app.js").write_text("console.log('travel')", encoding="utf-8")
            app = FastAPI()

            @app.get("/api/example")
            async def example():
                return {"ok":True}

            mount_frontend(app, root)
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                page = await client.get("/")
                self.assertIn("travel",page.text)
                self.assertEqual(page.headers["cache-control"],"no-store")
                for path in ("/","/index.html"):
                    refreshed = await client.get(path,headers={"If-None-Match":page.headers["etag"],
                                                               "If-Modified-Since":page.headers["last-modified"]})
                    self.assertEqual(refreshed.status_code,200)
                    self.assertIn("travel",refreshed.text)
                self.assertEqual((await client.get("/assets/app.js")).status_code, 200)
                self.assertEqual((await client.get("/api/example")).json(), {"ok":True})
                self.assertEqual((await client.get("/api/missing")).status_code, 404)
                self.assertEqual((await client.get("/assets/missing.js")).status_code, 404)
                self.assertEqual((await client.get("/assets/%2e%2e/%2e%2e/private.txt")).status_code, 404)

    async def test_missing_build_has_actionable_error(self):
        with tempfile.TemporaryDirectory() as folder:
            app = FastAPI()
            mount_frontend(app, Path(folder))
            async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://testserver") as client:
                response = await client.get("/")
                self.assertEqual(response.status_code, 503)
                self.assertIn("build.bat", response.json()["detail"])
