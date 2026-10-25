"""Mocked ticket queries only; left for the user to run."""
import copy
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock

from app.adapters import ToolError
from app.planner import Planner
from app.store import now


class ConfirmedTickets(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.planner = Planner.__new__(Planner)
        self.planner.emit = Mock()
        self.planner.store = Mock()
        self.planner.flyai = SimpleNamespace(availability=lambda:{"ready":True},
            ticket=AsyncMock(return_value={"price":50,"status":"reference","queried_at":now(),"note":"参考价"}))
        async def resource_ticket(state,city,name,day_date,fresh=False):
            return await self.planner.flyai.ticket(state["trip_id"],city,name,day_date,fresh=fresh)
        self.planner.resource_agent = SimpleNamespace(ticket=AsyncMock(side_effect=resource_ticket))
        self.state = {"trip_id":"trip","stops":{},"change":{},"itinerary":{"days":[
            {"city":"杭州","date":"2026-11-10","activities":[
                {"id":"a","name":"景点一","kind":"visit","ticket_claim":"门票50元","ticket_quote":{"price":None,"status":"pending_confirmation"}},
                {"id":"rest","name":"用餐","kind":"rest"}]}]}}

    async def test_confirming_a_preview_queries_ticket_and_preserves_schedule(self):
        original = copy.deepcopy(self.state["itinerary"])
        result = await self.planner.tickets(self.state)
        activity = result["itinerary"]["days"][0]["activities"][0]
        self.assertEqual(activity["ticket_quote"]["price"],50)
        self.assertEqual(activity["ticket_quote"]["visit_date"],"2026-11-10")
        self.planner.flyai.ticket.assert_awaited_once_with("trip","杭州","景点一","2026-11-10",fresh=False)
        self.assertEqual(self.state["itinerary"],original)
        self.assertNotIn("ticket_quote",result["itinerary"]["days"][0]["activities"][1])

    async def test_current_quote_is_not_queried_twice(self):
        result = await self.planner.tickets(self.state)
        await self.planner.tickets({**self.state,"itinerary":result["itinerary"]})
        self.assertEqual(self.planner.flyai.ticket.await_count,1)

    async def test_changed_visit_date_queries_again(self):
        result = await self.planner.tickets(self.state)
        result["itinerary"]["days"][0]["date"] = "2026-11-11"
        await self.planner.tickets({**self.state,"itinerary":result["itinerary"]})
        self.assertEqual(self.planner.flyai.ticket.await_count,2)
        self.assertEqual(self.planner.flyai.ticket.call_args.args[3],"2026-11-11")
