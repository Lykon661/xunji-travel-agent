"""One local background server, with a file-based graceful stop request."""
from time import perf_counter

RUNNER_STARTED = perf_counter()

import asyncio
import json
import os
import sys
import traceback
from pathlib import Path

from app.service_lock import service_lock

ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
DATA.mkdir(parents=True, exist_ok=True)
LOG = (DATA / "server.log").open("a", encoding="utf-8", buffering=1)
sys.stdout = sys.stderr = LOG


async def serve(instance: str):
    phase = perf_counter()
    import uvicorn
    print(f"[startup] uvicorn import: {perf_counter()-phase:.3f}s",flush=True)

    os.environ["TRAVEL_SERVER_INSTANCE"] = instance
    state_file, stop_file = DATA / "server.json", DATA / "server.stop"
    server = uvicorn.Server(uvicorn.Config(
        "app.main:app", host="127.0.0.1", port=8000, timeout_graceful_shutdown=20,
    ))
    temporary = state_file.with_suffix(".tmp")
    temporary.write_text(json.dumps({"pid":os.getpid(), "instance":instance}), encoding="utf-8")
    temporary.replace(state_file)

    async def watch_stop():
        while True:
            if stop_file.is_file() and stop_file.read_text(encoding="utf-8-sig").strip() == instance:
                server.should_exit = True
                return
            await asyncio.sleep(0.5)

    watcher = asyncio.create_task(watch_stop())
    try:
        phase = perf_counter()
        server.config.load()
        print(f"[startup] application imports and route registration: {perf_counter()-phase:.3f}s",flush=True)
        print(f"[startup] runner elapsed before application initialization: {perf_counter()-RUNNER_STARTED:.3f}s",flush=True)
        await server.serve()
    finally:
        watcher.cancel()
        await asyncio.gather(watcher, return_exceptions=True)


if __name__ == "__main__":
    try:
        with service_lock(DATA / "server.lock"):
            try:
                asyncio.run(serve(sys.argv[1]))
            finally:
                # asyncio.run also finishes pending tasks and executor threads before we advertise a stop.
                (DATA / "server.json").unlink(missing_ok=True)
                (DATA / "server.stop").unlink(missing_ok=True)
    except BaseException:
        traceback.print_exc()
        sys.exit(1)
