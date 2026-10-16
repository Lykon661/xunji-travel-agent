"""Synthetic temporary databases only; no service, model or provider calls."""
import sqlite3
import tempfile
import unittest
from pathlib import Path

from app.store import Store, compact_sqlite


class DatabaseCompaction(unittest.TestCase):
    def setUp(self):
        folder = tempfile.TemporaryDirectory()
        self.addCleanup(folder.cleanup)
        self.folder = Path(folder.name)

    def filled_store(self):
        store = Store(self.folder)
        trips = [store.create_trip(f"合成旅行{index}",{"cities":["杭州"]}) for index in range(40)]
        for trip in trips:
            store.update(trip["id"],{**trip["document"],"padding":"行程" * 20000})
        return store,trips

    def test_deleted_trips_are_reclaimed_without_losing_the_remaining_data(self):
        store,trips = self.filled_store()
        for trip in trips[1:]:
            store.delete_trip(trip["id"],0)
        file = self.folder / "trips.sqlite"
        before = file.stat().st_size
        self.assertGreater(compact_sqlite(file),0)
        self.assertLess(file.stat().st_size,before / 4)
        self.assertEqual(store.trip(trips[0]["id"])["document"]["padding"],"行程" * 20000)
        with sqlite3.connect(file) as db:
            self.assertEqual(db.execute("PRAGMA integrity_check").fetchone()[0],"ok")
        db.close()

    def test_a_mostly_used_or_missing_file_is_left_alone(self):
        self.filled_store()
        file = self.folder / "trips.sqlite"
        before = file.stat().st_size
        self.assertEqual(compact_sqlite(file),0)
        self.assertEqual(file.stat().st_size,before)
        self.assertEqual(compact_sqlite(self.folder / "missing.sqlite"),0)


if __name__ == "__main__":
    unittest.main()
