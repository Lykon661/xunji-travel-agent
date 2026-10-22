"""Lazy planner startup with mocked constructors and graph compilation."""
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch, sentinel

from app.planner import Planner, TripState


class LazyPlannerStartup(unittest.TestCase):
    def planner(self):
        with patch("app.planner.Models",return_value=SimpleNamespace()),patch("app.planner.Crawler"), \
             patch("app.planner.FlyAI"),patch("app.planner.Amap"):
            return Planner(Mock(),sentinel.checkpointer)

    def test_local_services_do_not_compile_a_planning_graph_at_startup(self):
        with patch("langgraph.graph.StateGraph") as factory:
            planner = self.planner()
            self.assertEqual(planner.tasks,{})
            self.assertEqual(planner.gates,{})
            self.assertIsNotNone(planner.models)
            self.assertIsNotNone(planner.crawler)
            self.assertNotIn("graph",planner.__dict__)
            factory.assert_not_called()

    def test_first_planning_access_compiles_once_with_the_original_checkpointer(self):
        with patch("langgraph.graph.StateGraph") as factory:
            factory.return_value.compile.return_value = sentinel.compiled_graph
            planner = self.planner()
            self.assertIs(planner.graph,sentinel.compiled_graph)
            self.assertIs(planner.graph,sentinel.compiled_graph)
            factory.assert_called_once_with(TripState)
            factory.return_value.compile.assert_called_once_with(checkpointer=sentinel.checkpointer)

    def test_failed_graph_initialization_can_be_retried(self):
        with patch("langgraph.graph.StateGraph") as factory:
            factory.return_value.compile.side_effect = [RuntimeError("synthetic compile failure"),sentinel.compiled_graph]
            planner = self.planner()
            with self.assertRaises(RuntimeError):
                _ = planner.graph
            self.assertNotIn("graph",planner.__dict__)
            self.assertIs(planner.graph,sentinel.compiled_graph)
            self.assertEqual(factory.return_value.compile.call_count,2)

    def test_mock_graph_assignment_remains_supported_for_existing_callers(self):
        planner = Planner.__new__(Planner)
        planner.graph = sentinel.mock_graph
        self.assertIs(planner.graph,sentinel.mock_graph)
