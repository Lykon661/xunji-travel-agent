"""Temporary SQLite regression for user execution; no production data is opened."""
import sqlite3
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest.mock import patch

from app.store import Store


class ReviewQueryLoading(unittest.TestCase):
    def test_loads_only_referenced_and_latest_matching_results_in_original_order(self):
        with tempfile.TemporaryDirectory() as folder:
            store = Store(Path(folder))
            request = {"tool":"maps_weather","city":"杭州"}
            oldest = store.query("trip","amap",request,{"raw":{"forecast":"旧天气"}})
            store.query("trip","amap",request,{"raw":{"forecast":"未被引用的中间天气"}})
            store.query("trip","amap",{"tool":"maps_text_search","city":"杭州","keywords":"其他景点"},
                        {"raw":{"large_irrelevant_result":"合成大段原始数据"*5000}})
            store.query("trip","other-provider",request,{"raw":{"forecast":"另一供应商"}})
            newest = store.query("trip","amap",request,{"raw":{"forecast":"新天气"}})
            foreign = store.query("other-trip","amap",request,{"raw":{"forecast":"另一旅行"}})
            original_connect = store.connect
            loaded_full_result_ids = []
            @contextmanager
            def track_materialized_results():
                with original_connect() as db:
                    def track(cursor,row):
                        columns = [column[0] for column in cursor.description]
                        if "result" in columns:
                            loaded_full_result_ids.append(row[columns.index("id")])
                        return sqlite3.Row(cursor,row)
                    db.row_factory = track
                    yield db
            with patch.object(store,"connect",track_materialized_results):
                result = store.review_queries("trip",[f"query:{oldest}",f"query:{oldest}",f"query:{foreign}","requirements"])
            self.assertEqual([row["evidence_id"] for row in result],[f"query:{oldest}",f"query:{newest}"])
            self.assertEqual([row["result"]["raw"]["forecast"] for row in result],["旧天气","新天气"])
            self.assertEqual(set(loaded_full_result_ids),{oldest,newest})
            self.assertEqual(len(loaded_full_result_ids),2)
            self.assertTrue(all(row["request"] == request and row["provider"] == "amap" for row in result))
