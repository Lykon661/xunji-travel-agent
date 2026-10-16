"""Date fields must not shadow their annotation type, including on Python 3.14."""
import unittest
from datetime import date as Date

from pydantic import ValidationError

from app.models import CandidateDay, ProductSelectionInput, TaxiInput


class RequestDates(unittest.TestCase):
    def test_query_reference_cannot_add_itinerary_dates(self):
        values = {"product_id":"synthetic","base_version":0,"updated_at":"synthetic"}
        self.assertEqual(ProductSelectionInput(**values).model_dump(),values)
        for extra in ({"date":"2026-11-10"},{"city":"杭州"},{"place_name":"体验地点"},{"duration_minutes":90}):
            with self.subTest(extra=extra),self.assertRaises(ValidationError):
                ProductSelectionInput(**values,**extra)

    def test_candidate_and_taxi_dates_keep_the_date_type(self):
        candidate = CandidateDay(date="2026-11-10",city="杭州")
        taxi = TaxiInput(date="2026-11-10",activity_id="synthetic",base_version=0,updated_at="synthetic")
        self.assertIsInstance(candidate.date,Date)
        self.assertIsInstance(taxi.date,Date)
        with self.assertRaises(ValidationError):
            TaxiInput(date="2026-02-30",activity_id="synthetic",base_version=0,updated_at="synthetic")
