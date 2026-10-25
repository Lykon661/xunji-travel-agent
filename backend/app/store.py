import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

from .models import WISH_PREFIX


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, default=str)


def amap_request_key(request):
    def normalized(key, value):
        if key in {"origin","destination","location"} and isinstance(value,str):
            try:
                return ",".join(f"{float(part):.6f}" for part in value.split(","))
            except ValueError:
                pass
        if isinstance(value,bool):
            return "true" if value else "false"
        return str(value) if not isinstance(value,(dict,list)) else json.dumps(value,sort_keys=True,ensure_ascii=False)
    return tuple(sorted((key,normalized(key,value)) for key,value in request.items() if value is not None))


def dialogue_defaults(document):
    return {"coordinator_messages":[],"system_assumptions":[],
            "coordinator_confirmation":None,"unmet":[],"draft_revision":0,"decision_history":[],**document}


class Conflict(Exception):
    pass


def compact_sqlite(file: Path, min_free_ratio: float = 0.25, min_free_bytes: int = 1024 * 1024) -> int:
    """Reclaim pages freed by deleted trips, runs and checkpoints; returns reclaimed bytes.

    SQLite never shrinks a file on DELETE. Call this only at startup, before the service
    shares the database, and only when enough of the file is free to be worth rewriting.
    """
    if not file.is_file():
        return 0
    db = sqlite3.connect(file, timeout=20)
    try:
        page_size = db.execute("PRAGMA page_size").fetchone()[0]
        pages = db.execute("PRAGMA page_count").fetchone()[0]
        free = db.execute("PRAGMA freelist_count").fetchone()[0]
        if not pages or free / pages < min_free_ratio or free * page_size < min_free_bytes:
            return 0
        db.execute("VACUUM")
        # In WAL mode the rewritten pages land in the log; checkpoint so the main file shrinks now.
        db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
        return free * page_size
    finally:
        db.close()


class Store:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.file = folder / "trips.sqlite"
        with self.connect() as db:
            db.executescript("""
                PRAGMA journal_mode=WAL;
                CREATE TABLE IF NOT EXISTS trips(id TEXT PRIMARY KEY, title TEXT NOT NULL,
                  version INTEGER NOT NULL DEFAULT 0, document TEXT NOT NULL, active_thread TEXT,
                  active_run TEXT, updated_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS sources(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL,
                  identity TEXT NOT NULL, document TEXT NOT NULL, UNIQUE(trip_id,identity));
                CREATE TABLE IF NOT EXISTS versions(trip_id TEXT NOT NULL, version INTEGER NOT NULL,
                  document TEXT NOT NULL, created_at TEXT NOT NULL, generation TEXT, PRIMARY KEY(trip_id,version));
                CREATE TABLE IF NOT EXISTS runs(id TEXT PRIMARY KEY, trip_id TEXT NOT NULL,
                  thread_id TEXT NOT NULL, base_version INTEGER NOT NULL, status TEXT NOT NULL,
                  document TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS events(id INTEGER PRIMARY KEY AUTOINCREMENT,
                  run_id TEXT NOT NULL, document TEXT NOT NULL);
                CREATE INDEX IF NOT EXISTS events_run ON events(run_id,id);
                CREATE TABLE IF NOT EXISTS agent_results(trip_id TEXT NOT NULL, cache_key TEXT NOT NULL,
                  document TEXT NOT NULL, PRIMARY KEY(trip_id,cache_key));
                CREATE TABLE IF NOT EXISTS queries(id INTEGER PRIMARY KEY AUTOINCREMENT,
                  trip_id TEXT NOT NULL, provider TEXT NOT NULL, request TEXT NOT NULL,
                  result TEXT NOT NULL, created_at TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS search_cache(keyword TEXT PRIMARY KEY,
                  document TEXT NOT NULL, created_at TEXT NOT NULL);
            """)

    @contextmanager
    def connect(self):
        db = sqlite3.connect(self.file, timeout=20)
        db.row_factory = sqlite3.Row
        try:
            with db:
                yield db
        finally:
            db.close()

    def create_trip(self, title: str, requirements: dict) -> dict:
        trip_id = uuid4().hex
        doc = {"requirements": requirements, "stage": "draft", "messages": [], "locks": [],
               "candidates": [], "selected_source_ids": [], "interview": None, "itinerary": None,
               "itinerary_saved_version": None, "warnings": [], "pending": None, "system_assumptions": []}
        doc = dialogue_defaults(doc)
        with self.connect() as db:
            db.execute("INSERT INTO trips(id,title,document,updated_at) VALUES(?,?,?,?)", (trip_id,title,dump(doc),now()))
        return self.trip(trip_id)

    def trip(self, trip_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
        if not row:
            raise KeyError("旅行不存在")
        result = dict(row)
        result["document"] = json.loads(result["document"])
        result["document"] = dialogue_defaults(result["document"])
        # Older releases automatically saved generated itineraries.
        result["document"].setdefault("itinerary_saved_version", result["version"] if result["document"].get("itinerary") and result["version"] else None)
        result["sources"] = self.sources(trip_id)
        return result

    def trips(self) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT id,title,version,updated_at,document,active_run FROM trips ORDER BY updated_at DESC").fetchall()
        return [{"id": r["id"], "title": r["title"], "version": r["version"], "updated_at": r["updated_at"],
                 "stage": json.loads(r["document"]).get("stage"), "active_run": r["active_run"]} for r in rows]

    def trip_threads(self, trip_id: str) -> list[str]:
        trip = self.trip(trip_id)
        with self.connect() as db:
            rows = db.execute("SELECT DISTINCT thread_id FROM runs WHERE trip_id=?", (trip_id,)).fetchall()
        # Clear the currently resumable checkpoint last.
        threads = [row["thread_id"] for row in rows if row["thread_id"] != trip["active_thread"]]
        if trip["active_thread"]:
            threads.append(trip["active_thread"])
        return threads

    def delete_trip(self, trip_id: str, base: int):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            trip = db.execute("SELECT version FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            if trip["version"] != base:
                raise Conflict("行程已更新，请刷新后重新删除")
            if db.execute("SELECT 1 FROM runs WHERE trip_id=? AND status='running'", (trip_id,)).fetchone():
                raise Conflict("这趟旅行正在处理，请等待本次运行结束后再删除")
            db.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE trip_id=?)", (trip_id,))
            db.execute("DELETE FROM queries WHERE trip_id=?", (trip_id,))
            db.execute("DELETE FROM versions WHERE trip_id=?", (trip_id,))
            db.execute("DELETE FROM sources WHERE trip_id=?", (trip_id,))
            db.execute("DELETE FROM runs WHERE trip_id=?", (trip_id,))
            db.execute("DELETE FROM trips WHERE id=?", (trip_id,))
            db.execute("DELETE FROM agent_results WHERE trip_id=?", (trip_id,))

    def update(self, trip_id: str, document: dict):
        document = dialogue_defaults(document)
        with self.connect() as db:
            db.execute("UPDATE trips SET document=?,updated_at=? WHERE id=?", (dump(document),now(),trip_id))

    def save_requirements(self, trip_id: str, requirements: dict, base: int, updated_at: str):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM trips WHERE id=?",(trip_id,)).fetchone()
            if row is None:
                raise KeyError("旅行不存在")
            if row["version"] != base or row["updated_at"] != updated_at:
                raise Conflict("旅行已变化，请刷新后再保存需求")
            if row["active_run"]:
                run = db.execute("SELECT status FROM runs WHERE id=?",(row["active_run"],)).fetchone()
                if run and run[0] == "running":
                    raise Conflict("正在处理旅行，请结束后再保存需求")
            doc = json.loads(row["document"])
            walk_changed = doc["requirements"].get("max_walk_per_day",8000) != requirements.get("max_walk_per_day",8000)
            if walk_changed:
                requirements = {**requirements,"walking_limit_exempt_dates":[]}
            if (walk_changed or doc["requirements"].get("walking_limit_exempt_dates",[]) != requirements.get("walking_limit_exempt_dates",[])) and doc.get("decision_history"):
                doc["decision_history"] = [*doc["decision_history"],{"questions":[],"answers":{},
                    "note":"已在需求表更新每日步行限制，以本次设置为准","summary":"最新需求表设置已替代之前的步行限制选择"}]
            if doc["requirements"] != requirements:
                doc.update(requirements=requirements,candidates=[],selected_route={},interview=None,
                           pending=None,stage="draft",progress_stage="draft",workflow_step="guides",warnings=[],
                           itinerary_outdated=bool(doc.get("itinerary")),review=None)
                # A changed request must not resume an older graph checkpoint.
                db.execute("UPDATE trips SET document=?,updated_at=?,active_run=NULL,active_thread=NULL WHERE id=?",
                           (dump(doc),now(),trip_id))
        return self.trip(trip_id)

    def add_source(self, trip_id: str, doc: dict) -> dict:
        identity = doc.get("note_id") or doc.get("identity") or doc["id"]
        with self.connect() as db:
            old = db.execute("SELECT id,document FROM sources WHERE trip_id=? AND identity=?", (trip_id,identity)).fetchone()
            if old:
                # Preserve a completed extraction when its content has not changed.
                previous = json.loads(old["document"])
                if old["id"] != doc["id"]:
                    db.execute("DELETE FROM sources WHERE id=? AND trip_id=?",(doc["id"],trip_id))
                doc["id"] = old["id"]
                if doc.get("extraction") is None and previous.get("text") == doc.get("text") and previous.get("images") == doc.get("images"):
                    doc["extraction"] = previous.get("extraction")
                db.execute("UPDATE sources SET document=? WHERE id=?", (dump(doc),old["id"]))
            else:
                db.execute("INSERT INTO sources VALUES(?,?,?,?)", (doc["id"],trip_id,identity,dump(doc)))
        return doc

    def back_to_step(self, trip_id: str, base: int, updated_at: str, step: str):
        steps = ["requirements","guides","interview","routes","itinerary"]
        if step not in steps:
            raise Conflict("不支持返回该规划步骤")
        index = steps.index(step)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT * FROM trips WHERE id=?",(trip_id,)).fetchone()
            if row is None:
                raise KeyError("旅行不存在")
            if row["version"] != base or row["updated_at"] != updated_at:
                raise Conflict("旅行已变化，请刷新后再返回上一步")
            if db.execute("SELECT 1 FROM runs WHERE trip_id=? AND status='running'",(trip_id,)).fetchone():
                raise Conflict("这趟旅行正在处理，请等待本次运行结束")
            doc = json.loads(row["document"])
            doc.update(workflow_step=step,stage="draft",progress_stage="draft",progress_message="",
                       pending=None,warnings=[],changes={},messages=[],products=[],reference_product_ids=[],product_search=None)
            if index < 4:
                doc.update(itinerary=None,itinerary_saved_version=None,itinerary_generation=None,
                           itinerary_outdated=False,confirmed_plan=None,sources_snapshot=[],locks=[])
                db.execute("DELETE FROM versions WHERE trip_id=?",(trip_id,))
            if index < 3:
                doc.update(candidates=[],selected_route={},route_chat=[],draft_revision=0,supplements=[])
                source_ids = set(doc.get("selected_source_ids",[]))
                allowed = {stop["id"] for source in db.execute("SELECT id,document FROM sources WHERE trip_id=?",(trip_id,))
                           if source["id"] in source_ids for stop in (json.loads(source["document"]).get("extraction") or {}).get("stops",[])}
                for field in ("required_stop_ids","excluded_stop_ids"):
                    doc["requirements"][field] = [identity for identity in doc["requirements"].get(field,[])
                                                  if identity in allowed or field == "required_stop_ids" and identity.startswith(WISH_PREFIX)]
                doc["stop_constraints"] = {"required":doc["requirements"]["required_stop_ids"],"excluded":doc["requirements"]["excluded_stop_ids"]}
                db.execute("DELETE FROM queries WHERE trip_id=?",(trip_id,))
            else:
                doc["selected_route"] = {} if index == 3 else doc.get("selected_route",{})
            if index < 2:
                required = [identity for identity in doc["requirements"].get("required_stop_ids",[]) if identity.startswith(WISH_PREFIX)]
                doc.update(interview=None,decision_history=[],stop_constraints={"required":required,"excluded":[]})
                doc["requirements"].update(required_stop_ids=required,excluded_stop_ids=[],walking_limit_exempt_dates=[])
            elif index == 2 and doc.get("interview"):
                doc["interview"]["confirmed"] = False
            if index == 0:
                doc["selected_source_ids"] = []
                db.execute("DELETE FROM sources WHERE trip_id=?",(trip_id,))
            elif index == 4 and doc.get("itinerary"):
                doc["stage"] = "completed" if doc.get("itinerary_saved_version") is not None else "ready"
                doc["progress_stage"] = doc["stage"]
            db.execute("DELETE FROM events WHERE run_id IN (SELECT id FROM runs WHERE trip_id=?)",(trip_id,))
            db.execute("DELETE FROM runs WHERE trip_id=?",(trip_id,))
            db.execute("DELETE FROM agent_results WHERE trip_id=?",(trip_id,))
            # Saved version numbers are never reused after clearing later steps.
            db.execute("UPDATE trips SET document=?,updated_at=?,active_thread=NULL,active_run=NULL WHERE id=?",
                       (dump(doc),now(),trip_id))
        return self.trip(trip_id)

    def sources(self, trip_id: str) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT document FROM sources WHERE trip_id=? ORDER BY rowid", (trip_id,)).fetchall()
        return [json.loads(r[0]) for r in rows]

    def query(self, trip_id: str, provider: str, request: dict, result: dict):
        with self.connect() as db:
            cursor = db.execute("INSERT INTO queries(trip_id,provider,request,result,created_at) VALUES(?,?,?,?,?)",
                                (trip_id,provider,dump(request),dump(result),now()))
            return cursor.lastrowid

    def agent_result(self, trip_id: str, cache_key: str):
        with self.connect() as db:
            row = db.execute("SELECT document FROM agent_results WHERE trip_id=? AND cache_key=?",(trip_id,cache_key)).fetchone()
        return json.loads(row[0]) if row else None

    def amap_result(self, trip_id: str, request: dict, hours: int):
        cutoff = (datetime.now(timezone.utc)-timedelta(hours=hours)).isoformat()
        key = amap_request_key(request)
        with self.connect() as db:
            rows = db.execute("SELECT id,request,result,created_at FROM queries WHERE trip_id=? AND provider='amap' "
                              "AND created_at>=? AND json_extract(request,'$.tool')=? ORDER BY id DESC",
                              (trip_id,cutoff,request["tool"])).fetchall()
        for row in rows:
            result = json.loads(row["result"])
            if result.get("status") == "error" or "raw" not in result or amap_request_key(json.loads(row["request"])) != key:
                continue
            return {"id":row["id"],"raw":result["raw"],"created_at":row["created_at"]}
        return None

    def review_queries(self, trip_id: str, evidence_ids: list[str]):
        identities = {int(value.split(":",1)[1]) for value in evidence_ids
                      if value.startswith("query:") and value.split(":",1)[1].isdigit()}
        if not identities:
            return []
        with self.connect() as db:
            rows = db.execute("SELECT id,provider,request FROM queries WHERE trip_id=? ORDER BY id DESC",(trip_id,)).fetchall()
            referenced = [row for row in rows if row["id"] in identities]
            requests = {(row["provider"],dump(json.loads(row["request"]))) for row in referenced}
            latest, selected = set(),{row["id"] for row in referenced}
            for row in rows:
                key = (row["provider"],dump(json.loads(row["request"])))
                if key in requests and key not in latest:
                    latest.add(key)
                    selected.add(row["id"])
            documents, selected = [],sorted(selected)
            for start in range(0,len(selected),900):
                chunk = selected[start:start+900]
                placeholders = ",".join("?" for _ in chunk)
                documents.extend(db.execute(f"SELECT * FROM queries WHERE trip_id=? AND id IN ({placeholders})",
                                             (trip_id,*chunk)).fetchall())
        return [{"evidence_id":f"query:{row['id']}","provider":row["provider"],
                 "request":json.loads(row["request"]),"result":json.loads(row["result"]),"created_at":row["created_at"]}
                for row in sorted(documents,key=lambda row:row["id"])]

    def save_agent_result(self, trip_id: str, cache_key: str, document: dict):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO agent_results VALUES(?,?,?)",(trip_id,cache_key,dump(document)))

    def cached(self, keyword: str, hours: int) -> list[dict] | None:
        with self.connect() as db:
            row = db.execute("SELECT * FROM search_cache WHERE keyword=?", (keyword,)).fetchone()
        if row and (datetime.now(timezone.utc)-datetime.fromisoformat(row["created_at"])).total_seconds() < hours * 3600:
            return json.loads(row["document"])
        return None

    def cache(self, keyword: str, documents: list[dict]):
        with self.connect() as db:
            db.execute("INSERT OR REPLACE INTO search_cache VALUES(?,?,?)", (keyword,dump(documents),now()))

    def start_run(self, trip_id: str, base: int, thread: str, initial: dict) -> str:
        run_id = uuid4().hex
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            row = db.execute("SELECT version,active_run FROM trips WHERE id=?", (trip_id,)).fetchone()
            if row["version"] != base:
                raise Conflict("行程已经更新，请刷新后重新提交")
            if row["active_run"]:
                old = db.execute("SELECT status FROM runs WHERE id=?", (row["active_run"],)).fetchone()
                if old and old[0] == "running":
                    raise Conflict("这趟旅行正在处理，请等待本次运行结束")
            db.execute("INSERT INTO runs VALUES(?,?,?,?,?,?,?)", (run_id,trip_id,thread,base,"running",dump(initial),now()))
            db.execute("UPDATE trips SET active_run=?,active_thread=? WHERE id=?", (run_id,thread,trip_id))
        return run_id

    def run(self, run_id: str) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT * FROM runs WHERE id=?", (run_id,)).fetchone()
        if not row:
            raise KeyError("任务不存在")
        result = dict(row)
        result["document"] = json.loads(result["document"])
        return result

    def finish_run(self, run_id: str, status: str, details: dict):
        with self.connect() as db:
            db.execute("UPDATE runs SET status=?,document=? WHERE id=?", (status,dump(details),run_id))

    def event(self, run_id: str, kind: str, message: str, **details):
        with self.connect() as db:
            db.execute("INSERT INTO events(run_id,document) VALUES(?,?)", (run_id,dump({"kind":kind,"message":message,"time":now(),**details})))

    def events(self, run_id: str, after: int) -> list[dict]:
        with self.connect() as db:
            rows = db.execute("SELECT * FROM events WHERE run_id=? AND id>? ORDER BY id LIMIT 200", (run_id,after)).fetchall()
        return [{"id":r["id"], **json.loads(r["document"])} for r in rows]

    def generation_version(self,trip_id: str,generation: str) -> int | None:
        with self.connect() as db:
            row = db.execute("SELECT version FROM versions WHERE trip_id=? AND generation=?",(trip_id,generation)).fetchone()
        return row[0] if row else None

    def commit(self, trip_id: str, base: int, doc: dict, generation: str | None = None,
               updated_at: str | None = None) -> int:
        doc = dialogue_defaults(doc)
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            if generation:
                completed = db.execute("SELECT version FROM versions WHERE trip_id=? AND generation=?",(trip_id,generation)).fetchone()
                if completed:
                    return completed[0]
            row = db.execute("SELECT version,updated_at FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not row:
                raise KeyError("旅行不存在")
            if row[0] != base:
                raise Conflict("基础版本已改变，本次结果未覆盖当前行程")
            if updated_at is not None and row["updated_at"] != updated_at:
                raise Conflict("行程草稿已变化，请刷新后重新保存")
            version = base + 1
            db.execute("INSERT INTO versions(trip_id,version,document,created_at,generation) VALUES(?,?,?,?,?)", (trip_id,version,dump(doc),now(),generation))
            db.execute("UPDATE trips SET version=?,document=?,updated_at=? WHERE id=?", (version,dump(doc),now(),trip_id))
        return version

    def delete_itinerary(self, trip_id: str, base: int, updated_at: str, version: int | None = None):
        with self.connect() as db:
            db.execute("BEGIN IMMEDIATE")
            trip = db.execute("SELECT * FROM trips WHERE id=?", (trip_id,)).fetchone()
            if not trip:
                raise KeyError("旅行不存在")
            if trip["version"] != base or trip["updated_at"] != updated_at:
                raise Conflict("行程已变化，请刷新后重新删除")
            if db.execute("SELECT 1 FROM runs WHERE trip_id=? AND status='running'", (trip_id,)).fetchone():
                raise Conflict("这趟旅行正在处理，请等待本次运行结束后再删除")
            doc = json.loads(trip["document"])
            saved_version = doc.get("itinerary_saved_version", trip["version"] if doc.get("itinerary") and trip["version"] else None)
            doc["itinerary_saved_version"] = saved_version
            if version is not None:
                if not db.execute("SELECT 1 FROM versions WHERE trip_id=? AND version=?", (trip_id,version)).fetchone():
                    raise KeyError("行程版本不存在")
                db.execute("DELETE FROM versions WHERE trip_id=? AND version=?", (trip_id,version))
            clear_current = version is None or version == saved_version
            if clear_current:
                if not doc.get("itinerary"):
                    raise Conflict("当前没有可删除的行程")
                doc.update(itinerary=None, itinerary_saved_version=None, itinerary_outdated=False,
                           itinerary_generation=None, locks=[], selected_route={}, candidates=[], warnings=[],
                           pending=None, stage="draft", progress_stage="draft", changes={}, sources_snapshot=[])
            # Keep the version counter monotonic so deleted numbers are never reused.
            db.execute("UPDATE trips SET document=?,updated_at=?,active_thread=?,active_run=? WHERE id=?",
                       (dump(doc),now(),None if clear_current else trip["active_thread"],
                        None if clear_current else trip["active_run"],trip_id))

    def versions(self, trip_id: str, include_documents: bool = True) -> list[dict]:
        with self.connect() as db:
            if not include_documents:
                return [dict(row) for row in db.execute("SELECT version,created_at FROM versions WHERE trip_id=? ORDER BY version DESC",(trip_id,))]
            rows = db.execute("SELECT version,document,created_at FROM versions WHERE trip_id=? ORDER BY version DESC", (trip_id,)).fetchall()
        return [{"version":r[0],"document":dialogue_defaults(json.loads(r[1])),"created_at":r[2]} for r in rows]

    def version(self, trip_id: str, version: int) -> dict:
        with self.connect() as db:
            row = db.execute("SELECT version,document,created_at FROM versions WHERE trip_id=? AND version=?",(trip_id,version)).fetchone()
        if row is None:
            raise KeyError("行程版本不存在")
        return {"version":row["version"],"document":dialogue_defaults(json.loads(row["document"])),"created_at":row["created_at"]}

    def recover(self):
        with self.connect() as db:
            rows = db.execute("SELECT id,trip_id FROM runs WHERE status='running'").fetchall()
            db.execute("UPDATE runs SET status='recoverable' WHERE status='running'")
            for r in rows:
                trip = db.execute("SELECT document FROM trips WHERE id=?", (r["trip_id"],)).fetchone()
                doc = json.loads(trip[0]); doc.setdefault("progress_stage", doc["stage"]); doc["stage"] = "recoverable"
                db.execute("UPDATE trips SET document=? WHERE id=?", (dump(doc),r["trip_id"]))
