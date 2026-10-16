from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles


class FrontendFiles(StaticFiles):
    def file_response(self, full_path, stat_result, scope, status_code=200):
        if Path(full_path).name == "index.html":
            # Always send the current asset links, including on conditional requests.
            return FileResponse(full_path, status_code=status_code, stat_result=stat_result,
                                headers={"Cache-Control":"no-store"})
        return super().file_response(full_path,stat_result,scope,status_code)


def mount_frontend(app: FastAPI, directory: Path):
    # Register last so /api and /docs retain their own handlers.
    if (directory / "index.html").is_file():
        app.mount("/", FrontendFiles(directory=directory, html=True), name="frontend")
    else:
        @app.get("/", include_in_schema=False)
        async def missing_build():
            raise HTTPException(503, "前端尚未打包，请先运行项目根目录的 build.bat")
