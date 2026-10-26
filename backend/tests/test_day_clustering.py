import random
import unittest
from copy import deepcopy
from unittest.mock import patch

from app import day_clustering as clustering
from app.conflicts import day_demand, visit_minutes
from app.constraints import merge, trip_dates
from app.models import ConstraintDraft, Requirements
from app.places import canonicalize_stops
from app.routing import coordinates, distance

A, B, C, D = "2026-11-10", "2026-11-11", "2026-11-12", "2026-11-13"


def requirement(end=B, cities=None, **values):
    return Requirements(cities=cities or ["杭州"],start_date=A,end_date=end,**values).model_dump(mode="json")


def constraint(kind, **values):
    return merge([],[ConstraintDraft(kind=kind,quote="用户要求",**values).model_dump(mode="json")],"form")


def stop(identity, minutes=90, city="杭州", location=0, **values):
    place = {"id":"poi-"+identity,"location":f"{120+location/100},30"} if location is not None else None
    return {"id":identity,"name":"景点"+identity,"city":city,"category":"sight","source_id":"guide",
            "duration_minutes":minutes,"map_place":place,**values}


def state(stops, req=None, required=(), locks=(), **values):
    return {"requirements":req or requirement(),"stops":stops,"locks":list(locks),
            "stop_constraints":{"required":list(required),"excluded":[]},**values}


def selected(plan):
    return {i:d["date"] for d in plan["days"] for i in d["stop_ids"]}


def geometry(plan, stops):
    centers,dispersion = [],0
    for day in plan["days"]:
        ids = [i for i in day["stop_ids"] if coordinates(stops[i].get("map_place"))]
        def meters(i, j):
            return round(distance(coordinates(stops[i]["map_place"]),coordinates(stops[j]["map_place"])))
        center = min(ids,key=lambda i:(sum(meters(i,j) for j in ids),i),default=None)
        centers.append(center)
        dispersion += sum(meters(i,center) for i in ids)
    edges = sum(round(distance(coordinates(stops[a]["map_place"]),coordinates(stops[b]["map_place"])))
                for k,(a,b) in enumerate(zip(centers,centers[1:])) if a and b and plan["days"][k]["city"] == plan["days"][k+1]["city"])
    return 4*dispersion+edges


class VisitDuration(unittest.TestCase):
    def test_priority_date_and_first_valid_constraint(self):
        item = stop("a",120)
        records = constraint("visit_duration",minutes=180,scope={"dates":[A],"stop_ids":["a"]})
        req = requirement(constraints=records)
        self.assertEqual(visit_minutes(item,req,A,60),180)
        self.assertEqual(visit_minutes(item,req,B,60),60)
        self.assertEqual(visit_minutes(item,req,B),120)
        self.assertEqual(visit_minutes({**item,"duration_minutes":None},req,B),90)
        self.assertEqual(visit_minutes(item,{"constraints":[*records,{**records[0],"minutes":240}]},A),180)
        records[0]["strength"] = "soft"
        self.assertEqual(visit_minutes(item,{"constraints":records},A),180)

    def test_day_demand_preserves_schedule_and_internal_lunch(self):
        item = stop("a",120)
        day = {"date":A,"city":"杭州","activities":[{"id":"a","kind":"visit","start":"09:00","end":"12:00",
                "meal_break":{"start":"10:00","end":"11:00"}}]}
        [demand] = day_demand(state({"a":item}),{"days":[day]},day,{"stop_ids":["a"]})
        self.assertEqual(demand["visit"],120)


class CapacityAndIdentity(unittest.TestCase):
    def test_empty_short_previous_window_has_zero_load(self):
        stops = {"a":stop("a",60)}
        req = requirement(constraints=constraint("daily_min_stops",count=3,strength="soft"))
        old = {"days":[{"date":A,"city":"杭州","available_start":780,"tail_deadline":810}]}
        plan = clustering.build_spatial_plan(state(stops,req,old_requirements=req,old_itinerary=old),stops)
        self.assertEqual(plan["status"],"ready",plan["issues"])
        self.assertEqual(plan["days"][0]["stop_ids"],[])
        self.assertEqual(plan["day_budgets"][A]["load"],0)
        self.assertEqual(clustering.spatial_errors(plan,plan,stops,req),[])

    def test_minutes_lunch_and_twenty_minute_connection(self):
        stops = {i:stop(i,90,location=n) for n,i in enumerate("abcd")}
        req = requirement(A,earliest_departure="14:30")
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertEqual(len(selected(plan)),2)
        self.assertEqual(plan["day_budgets"][A],{"city":"杭州","start":870,"end":1200,"lunch":0,"minimum":0,"load":230,"basis":"default_estimate"})
        req = requirement(A,earliest_departure="12:00")
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(plan["day_budgets"][A]["lunch"],60)
        self.assertEqual(plan["day_budgets"][A]["load"],430)

    def test_lunch_boundaries_and_negative_window(self):
        stops = {"a":stop("a",30)}
        for start,end,lunch in ((839,750,60),(840,900,0),(540,749,0)):
            old = {"days":[{"date":A,"city":"杭州","available_start":start,"tail_deadline":end}]}
            req = requirement(A)
            plan = clustering.build_spatial_plan(state(stops,req,old_requirements=req,old_itinerary=old),stops)
            self.assertEqual(plan["day_budgets"][A]["lunch"],lunch)
            if start > end:
                self.assertEqual(plan["status"],"partial")
                self.assertEqual((plan["day_budgets"][A]["start"],plan["day_budgets"][A]["end"]),(start,end))

    def test_old_windows_require_exact_requirements_and_city(self):
        stops = {"a":stop("a",120)}
        req = requirement(A,latest_arrival="15:00",latest_return="16:00")
        old = {"days":[{"date":A,"city":"杭州","available_start":960,"tail_deadline":1020}]}
        plan = clustering.build_spatial_plan(state(stops,req,old_requirements={**req,"preferences":"旧偏好"},old_itinerary=old),stops)
        self.assertEqual((plan["day_budgets"][A]["start"],plan["day_budgets"][A]["end"]),(540,1200))
        old["hotels"] = [{"city":"杭州","selected_by_user":True,"selected":{"name":"酒店"}}]
        plan = clustering.build_spatial_plan(state(stops,req,old_itinerary=old),stops)
        self.assertEqual(plan["day_budgets"][A]["end"],960)
        old["days"][0]["city"] = "苏州"
        plan = clustering.build_spatial_plan(state(stops,req,old_itinerary=old,old_requirements=req),stops)
        self.assertEqual(plan["day_budgets"][A]["basis"],"default_estimate")

    def test_required_locked_assigned_and_resource_locks(self):
        stops = canonicalize_stops({"a":stop("a",assigned_date=A),"b":stop("b",location=2),"c":stop("c",location=4)})
        old = {"days":[{"date":B,"city":"杭州","activities":[{"id":"b","kind":"visit"}]}]}
        plan = clustering.build_spatial_plan(state(stops,required=["c"],locks=["b","hotel:1","train:1"],old_itinerary=old),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertEqual(plan["required_ids"],["a","b","c"])
        self.assertEqual(plan["fixed_dates"],{"a":A,"b":B})
        self.assertEqual((selected(plan)["a"],selected(plan)["b"]),(A,B))
        partial = clustering.build_spatial_plan(state(stops,locks=["b"]),stops)
        self.assertEqual(partial["status"],"partial")
        self.assertTrue(any("无法恢复" in issue for issue in partial["issues"]))

    def test_assigned_lock_date_mismatch_and_out_of_range(self):
        stops = {"a":stop("a",assigned_date=A)}
        old = {"days":[{"date":B,"activities":[{"id":"a","kind":"visit"}]}]}
        mismatch = clustering.build_spatial_plan(state(stops,locks=["a"],old_itinerary=old),stops)
        self.assertEqual(mismatch["status"],"conflict")
        self.assertIn("a",str(mismatch["issues"]))
        stops["a"]["assigned_date"] = C
        out = clustering.build_spatial_plan(state(stops),stops)
        self.assertEqual(out["status"],"conflict")
        self.assertIn(C,str(out["issues"]))

    def test_full_day_text_counts_at_least_360_and_is_exclusive(self):
        stops = {"a":stop("a",90,evidence={"quote":"景点a需要一整天"}),"b":stop("b",90,location=10)}
        req = requirement(constraints=constraint("daily_min_stops",count=1))
        plan = clustering.build_spatial_plan(state(stops,req,required=["a"]),stops)
        self.assertEqual(plan["status"],"ready")
        day = next(day for day in plan["days"] if "a" in day["stop_ids"])
        self.assertEqual(day["stop_ids"],["a"])
        self.assertEqual(plan["visit_minutes"]["a"],360)
        self.assertEqual(plan["day_budgets"][day["date"]]["load"],420)
        req["constraints"] = constraint("daily_min_stops",count=2)
        partial = clustering.build_spatial_plan(state(stops,req,required=["a"]),stops)
        self.assertEqual(partial["status"],"partial")
        self.assertTrue(any("数量" in issue for issue in partial["issues"]))

    def test_closed_and_unknown_opening_and_date_limited_full_day(self):
        stops = {"a":stop("a",opening_claim="闭馆"),"b":stop("b",location=3)}
        plan = clustering.build_spatial_plan(state(stops,required=["a"]),stops)
        self.assertEqual(plan["status"],"partial")
        self.assertNotIn("a",selected(plan))
        self.assertIn("b",selected(plan))
        stops["a"].pop("opening_claim")
        with patch.object(clustering,"reference_opening",side_effect=lambda s,d: {"closed":True} if s["id"] == "a" and d == A else None):
            plan = clustering.build_spatial_plan(state(stops,required=["a"]),stops)
        self.assertEqual(selected(plan)["a"],B)
        stops["a"]["evidence"] = {"quote":"景点a需要全天"}
        with patch.object(clustering,"reference_opening",side_effect=lambda s,d: {"closed":True} if s["id"] == "a" and d == A else None):
            plan = clustering.build_spatial_plan(state(stops,required=["a"]),stops)
        self.assertEqual(selected(plan)["a"],B)
        self.assertEqual(next(d["stop_ids"] for d in plan["days"] if d["date"] == B),["a"])

    def test_unlocated_fixed_required_and_optional(self):
        stops = {"a":stop("a",location=None,assigned_date=A),"b":stop("b",location=None),"c":stop("c",location=1)}
        plan = clustering.build_spatial_plan(state(stops),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertEqual(selected(plan)["a"],A)
        self.assertNotIn("b",selected(plan))
        self.assertEqual(plan["unlocated_stop_ids"],["a","b"])
        self.assertEqual(plan["visit_minutes"]["a"],90)
        only = clustering.build_spatial_plan(state({"a":stops["a"]},requirement(A)),{"a":stops["a"]})
        self.assertEqual(only["days"][0]["adjustment"],"空间分日：按候选地点、用户固定日期和每日可用时间形成初始分日建议。保留用户固定日期，位置尚未核实，未据此判断远近。")
        partial = clustering.build_spatial_plan(state(stops,required=["b"]),stops)
        self.assertEqual(partial["status"],"partial")
        self.assertTrue(any("b" in issue for issue in partial["issues"]))

    def test_alias_representative_priority_and_excluded_coverage(self):
        first = stop("a",location=0)
        stops = canonicalize_stops({"a":first,"z":{**first,"id":"z"}})
        plan = clustering.build_spatial_plan(state(stops,requirement(A),required=["z"]),stops)
        self.assertEqual(list(selected(plan)),["z"])
        old = {"days":[{"date":A,"activities":[{"id":"a","kind":"visit"}]}]}
        plan = clustering.build_spatial_plan(state(stops,requirement(A),required=["z"],locks=["a"],old_itinerary=old),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertEqual(list(selected(plan)),["a"])
        excluded = state(stops,requirement(A))
        excluded["stop_constraints"]["excluded"] = ["z"]
        plan = clustering.build_spatial_plan(excluded,stops)
        self.assertEqual(selected(plan),{})

    def test_authorized_fixed_repeat_and_ambiguous_protection(self):
        first = stop("a",assigned_date=A)
        stops = canonicalize_stops({"a":first,"b":{**first,"id":"b","assigned_date":B}})
        permission = [{"place_id":stops["a"]["canonical_place_id"],"role":"visit","dates":[A,B],"authorization":"允许两天各去一次"}]
        plan = clustering.build_spatial_plan(state(stops,requirement(repeat_exceptions=permission)),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertEqual(selected(plan),{"a":A,"b":B})
        partial = clustering.build_spatial_plan(state(stops),stops)
        self.assertEqual(partial["status"],"partial")
        self.assertEqual(selected(partial),{"a":A,"b":B})
        self.assertTrue(any("唯一访问实例" in issue for issue in partial["issues"]))

    def test_no_mutation_and_dictionary_order_determinism(self):
        stops = canonicalize_stops({i:stop(i,60,location=n*5) for n,i in enumerate("dcba")})
        current = state(stops,requirement(constraints=constraint("daily_min_stops",count=1)))
        snapshot = deepcopy(current)
        plan = clustering.build_spatial_plan(current,stops)
        reverse = clustering.build_spatial_plan(current,dict(reversed(list(stops.items()))))
        self.assertEqual(current,snapshot)
        self.assertEqual(plan,reverse)
        for day in plan["days"]:
            self.assertTrue(day["adjustment"].startswith("空间分日："))
            self.assertNotIn("最短",day["adjustment"])

    def test_soft_minimum_avoids_empty_days_and_retains_shortage(self):
        stops = {i:stop(i,60,location=n) for n,i in enumerate("ab")}
        req = requirement(constraints=constraint("daily_min_stops",count=3,strength="soft"))
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertTrue(all(len(day["stop_ids"]) == 1 for day in plan["days"]))
        self.assertTrue(all("参考数量3个" in day["adjustment"] for day in plan["days"]))
        plan = clustering.build_spatial_plan(state({"a":stops["a"]},req),{"a":stops["a"]})
        self.assertEqual(plan["status"],"ready")

    def test_soft_minimum_allows_empty_day_with_shortage_issue(self):
        stops = {"a":stop("a",60)}
        req = requirement(constraints=constraint("daily_min_stops",count=3,strength="soft"))
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(clustering.spatial_errors(plan,plan,stops,req),[])
        self.assertEqual(plan["status"],"ready")
        self.assertEqual(sorted(len(day["stop_ids"]) for day in plan["days"]),[0,1])
        self.assertTrue(any("参考数量3个" in issue for issue in plan["issues"]))
        self.assertTrue(all(budget["minimum"] == 0 for budget in plan["day_budgets"].values()))

    def test_soft_minimum_local_optimization_keeps_three_days_nonempty(self):
        stops = {i:stop(i,90,location=location) for i,location in zip("abc",(0,100,101))}
        req = requirement(C,constraints=constraint("daily_min_stops",count=1,strength="soft"))
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(plan["status"],"ready",plan["issues"])
        self.assertEqual([len(day["stop_ids"]) for day in plan["days"]],[1,1,1])
        self.assertEqual(set(selected(plan)),set(stops))
        self.assertEqual(clustering.spatial_errors(plan,plan,stops,req),[])


class CityAllocationAndSearch(unittest.TestCase):
    def test_partial_city_change_uses_default_window_and_same_city_keeps_previous_window(self):
        stops = {str(n):stop(str(n),90,location=n) for n in range(12)}
        stops.update({i:stop(i,300,city="苏州",location=n) for n,i in enumerate("bcd")})
        req = requirement(D,["杭州","苏州"],earliest_departure="10:00",latest_return="17:00")
        old = {"days":[{"date":B,"city":"杭州","available_start":780,"tail_deadline":810}],
               "hotels":[{"city":"苏州","selected_by_user":True,"selected":{"name":"苏州酒店"}}]}
        plan = clustering.build_spatial_plan(state(stops,req,required=list("bcd"),old_requirements=req,old_itinerary=old),stops)
        self.assertEqual(plan["status"],"partial",plan["issues"])
        self.assertEqual(plan["city_days"],{"杭州":2,"苏州":2})
        self.assertEqual(plan["day_budgets"][B]["city"],"杭州")
        self.assertEqual(plan["day_budgets"][B]["basis"],"previous_window")
        candidate = {"days":[{"date":A,"city":"杭州","stop_ids":["0"]},
                             *[{"date":day,"city":"苏州","stop_ids":[i]} for day,i in zip((B,C,D),"bcd")]]}
        snapshot = deepcopy(plan)
        self.assertEqual(clustering.spatial_errors(plan,candidate,stops,req),[])
        candidate["days"][0]["stop_ids"] = []
        candidate["days"][1] = {"date":B,"city":"杭州","stop_ids":["0"]}
        candidate["days"][2]["stop_ids"].append("b")
        self.assertIn(B+" 超出每日估计时间容量",clustering.spatial_errors(plan,candidate,stops,req))
        self.assertEqual(plan,snapshot)

    def test_changed_city_default_window_respects_its_confirmed_hotel(self):
        stops = {str(n):stop(str(n),90,location=n) for n in range(12)}
        stops.update({i:stop(i,300,city="苏州",location=n) for n,i in enumerate("bcd")})
        req = requirement(D,["杭州","苏州"],latest_return="14:00")
        candidate = {"days":[{"date":A,"city":"杭州","stop_ids":["0"]},
                             *[{"date":day,"city":"苏州","stop_ids":[i]} for day,i in zip((B,C,D),"bcd")]]}
        for city in ("杭州","苏州"):
            with self.subTest(hotel_city=city):
                old = {"hotels":[{"city":city,"selected_by_user":True,"selected":{"name":"酒店"},
                                  "context":{"check_in":B,"check_out":B}}]}
                plan = clustering.build_spatial_plan(state(stops,req,required=list("bcd"),old_itinerary=old),stops)
                self.assertEqual(plan["day_budgets"][B]["city"],"杭州")
                errors = clustering.spatial_errors(plan,candidate,stops,req)
                self.assertEqual(B+" 超出每日估计时间容量" in errors,city == "苏州")
                self.assertFalse(any(day+" 超出每日估计时间容量" in errors for day in (A,C,D)))

    def test_required_full_days_override_city_proportion_including_fixed_items(self):
        for assigned in (None,C):
            with self.subTest(assigned=assigned):
                stops = {str(n):stop(str(n),90,location=n) for n in range(12)}
                stops.update(b=stop("b",360,city="苏州"),c=stop("c",360,city="苏州",assigned_date=assigned))
                req = requirement(C,["杭州","苏州"],required_stop_ids=["b"])
                current = state(stops,req,required=[] if assigned else ["c"])
                plan = clustering.build_spatial_plan(current,stops)
                self.assertEqual(plan["city_days"],{"杭州":1,"苏州":2})
                self.assertEqual(plan["status"],"ready",plan["issues"])
                self.assertEqual([d["stop_ids"] for d in plan["days"][1:]],[["b"],["c"]])
                self.assertEqual(clustering.spatial_errors(plan,plan,stops,req),[])

    def test_required_full_day_lower_bounds_without_enough_days_conflict(self):
        stops = {"a":stop("a"),"b":stop("b",360,city="苏州"),"c":stop("c",360,city="苏州")}
        req = requirement(B,["杭州","苏州"])
        plan = clustering.build_spatial_plan(state(stops,req,required=["b","c"]),stops)
        self.assertEqual(plan["status"],"conflict")
        self.assertTrue(any("城市连续日期段冲突" in issue for issue in plan["issues"]))

    def test_single_city_and_90_to_270_split(self):
        stops = {"a":stop("a",90),"b":stop("b",270,city="苏州")}
        plan = clustering.build_spatial_plan(state(stops,requirement(D,["杭州","苏州"])),stops)
        self.assertEqual(plan["city_days"],{"杭州":1,"苏州":3})
        self.assertEqual([d["city"] for d in plan["days"]],["杭州","苏州","苏州","苏州"])
        one = clustering.build_spatial_plan(state({"a":stops["a"]},requirement(D)),{"a":stops["a"]})
        self.assertEqual(one["city_days"],{"杭州":4})

    def test_each_city_one_zero_weights_and_tie_goes_to_earlier_city(self):
        stops = {"a":stop("a",90)}
        plan = clustering.build_spatial_plan(state(stops,requirement(D,["杭州","苏州"])),stops)
        self.assertEqual(plan["city_days"],{"杭州":3,"苏州":1})
        zero = clustering.build_spatial_plan(state({},requirement(C,["杭州","苏州"])),{})
        self.assertEqual(zero["city_days"],{"杭州":2,"苏州":1})
        self.assertEqual(zero["status"],"partial")
        stops["b"] = stop("b",90,city="苏州")
        tie = clustering.build_spatial_plan(state(stops,requirement(C,["杭州","苏州"])),stops)
        self.assertEqual(tie["city_days"],{"杭州":2,"苏州":1})

    def test_unlocated_weights_aliases_and_date_scoped_maximum(self):
        stops = canonicalize_stops({"a":stop("a",30,location=None),"b":stop("b",90,city="苏州",location=None)})
        req = requirement(D,["杭州","苏州"],constraints=constraint("visit_duration",minutes=270,scope={"dates":[A],"stop_ids":["a"]}))
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(plan["city_days"],{"杭州":3,"苏州":1})
        stops["alias"] = {**stops["b"],"id":"alias"}
        alias = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(alias["city_days"],plan["city_days"])

    def test_fixed_dates_override_proportion_and_city_conflict_is_explicit(self):
        stops = {"a":stop("a",90,assigned_date=C),"b":stop("b",270,city="苏州",assigned_date=D)}
        req = requirement(D,["杭州","苏州"])
        plan = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(plan["city_days"],{"杭州":3,"苏州":1})
        stops["b"]["assigned_date"] = A
        conflict = clustering.build_spatial_plan(state(stops,req),stops)
        self.assertEqual(conflict["status"],"conflict")
        self.assertIn(A,str(conflict["issues"]))
        self.assertIn("b",str(conflict["issues"]))

    def test_nearest_neighbor_with_unlocated_last(self):
        stops = {"a":stop("a",30,location=0),"b":stop("b",30,location=20),"c":stop("c",30,location=1),
                 "0":stop("0",30,location=None,assigned_date=A)}
        plan = clustering.build_spatial_plan(state(stops,requirement(A)),stops)
        self.assertEqual(plan["days"][0]["stop_ids"],["a","c","b","0"])

    def test_day_order_and_local_improvements_preserve_membership_and_budget(self):
        self.assertEqual(clustering.MAX_LOCAL_PASSES,20)
        self.assertEqual(clustering.ORDINARY_LOCATION_LIMIT,48)
        generator = random.Random(13)
        req = requirement(C,constraints=constraint("daily_min_stops",count=1))
        improved = False
        for _ in range(6):
            stops = {str(n):stop(str(n),90,location=generator.randrange(200)) for n in range(10)}
            with patch.object(clustering,"MAX_LOCAL_PASSES",0):
                before = clustering.build_spatial_plan(state(stops,req),stops)
            after = clustering.build_spatial_plan(state(stops,req),stops)
            self.assertEqual(set(selected(before)),set(selected(after)))
            self.assertLessEqual(geometry(after,stops),geometry(before,stops))
            self.assertEqual(clustering.spatial_errors(after,after,stops,req),[])
            improved |= geometry(after,stops) < geometry(before,stops)
            previous = geometry(before,stops)
            for limit in (1,2,3,20):
                with patch.object(clustering,"MAX_LOCAL_PASSES",limit):
                    result = clustering.build_spatial_plan(state(stops,req),stops)
                self.assertLessEqual(geometry(result,stops),previous)
                previous = geometry(result,stops)
        self.assertTrue(improved)

    def test_different_day_windows_reject_illegal_cluster_swaps(self):
        stops = {i:stop(i,90,location=n*20) for n,i in enumerate("abc")}
        req = requirement(constraints=constraint("daily_min_stops",count=1))
        old = {"days":[{"date":A,"city":"杭州","available_start":540,"tail_deadline":690},
                       {"date":B,"city":"杭州","available_start":540,"tail_deadline":840}]}
        plan = clustering.build_spatial_plan(state(stops,req,old_requirements=req,old_itinerary=old),stops)
        self.assertEqual(plan["status"],"ready")
        self.assertEqual([len(day["stop_ids"]) for day in plan["days"]],[1,2])
        illegal = deepcopy(plan)
        illegal["days"][0]["stop_ids"],illegal["days"][1]["stop_ids"] = illegal["days"][1]["stop_ids"],illegal["days"][0]["stop_ids"]
        self.assertTrue(any("容量" in issue for issue in clustering.spatial_errors(plan,illegal,stops,req)))

    def test_required_over_capacity_is_partial_with_located_suggestions(self):
        stops = {i:stop(i,300,location=n) for n,i in enumerate("abc")}
        plan = clustering.build_spatial_plan(state(stops,requirement(A),required=list(stops)),stops)
        self.assertEqual(plan["status"],"partial")
        self.assertTrue(selected(plan))
        self.assertTrue(plan["reserve_stop_ids"])
        self.assertTrue(any("缺少" in issue for issue in plan["issues"]))

    def test_day_clusters_follow_nearest_centers_and_respect_calendar(self):
        stops = {i:stop(i,90,location=p) for i,p in zip("abcdef",(0,1,100,101,50,51))}
        req = requirement(C,constraints=constraint("daily_min_stops",count=1))
        old = {"days":[{"date":d,"city":"杭州","available_start":540,"tail_deadline":840} for d in (A,B,C)]}
        current = state(stops,req,old_requirements=req,old_itinerary=old)
        with patch.object(clustering,"MAX_LOCAL_PASSES",0):
            ordered = clustering.build_spatial_plan(current,stops)
        self.assertEqual([d["stop_ids"] for d in ordered["days"]],[["a","b"],["e","f"],["c","d"]])
        with patch.object(clustering,"MAX_LOCAL_PASSES",0),patch.object(clustering,"reference_opening",side_effect=lambda s,d: {"closed":True} if s["id"] in {"e","f"} and d == B else None):
            restricted = clustering.build_spatial_plan(current,stops)
        self.assertEqual([d["stop_ids"] for d in restricted["days"]],[["a","b"],["c","d"],["e","f"]])
        self.assertLess(geometry(ordered,stops),geometry(restricted,stops))

    def test_count_limit_and_missing_locked_stop(self):
        stops = {str(i):stop(str(i),1,location=i) for i in range(20)}
        req = requirement(A)
        old = {"days":[{"date":A,"city":"杭州","available_start":0,"tail_deadline":1440}]}
        plan = clustering.build_spatial_plan(state(stops,req,old_requirements=req,old_itinerary=old),stops)
        self.assertEqual(len(selected(plan)),12)
        self.assertEqual(len(plan["reserve_stop_ids"]),8)
        current = state(stops,req,locks=["0"])
        current["stop_constraints"]["excluded"] = ["0"]
        plan = clustering.build_spatial_plan(current,stops)
        self.assertEqual(plan["status"],"partial")
        self.assertIn("0",plan["required_ids"])

    def test_spatial_hard_errors_cover_skeleton_fixed_excluded_closed_and_full_day(self):
        stops = {"a":stop("a",assigned_date=A),"b":stop("b",location=2),"c":stop("c",location=3)}
        req = requirement(constraints=constraint("daily_min_stops",count=1))
        plan = clustering.build_spatial_plan(state(stops,req,required=["b"]),stops)
        self.assertEqual(clustering.spatial_errors(plan,plan,stops,req),[])
        changed = deepcopy(plan)
        changed["days"][0]["city"] = "苏州"
        self.assertTrue(any("城市日期" in error for error in clustering.spatial_errors(plan,changed,stops,req)))
        candidate = {"days":[{"date":A,"city":"杭州","stop_ids":[]},{"date":B,"city":"杭州","stop_ids":["a","c"]}]}
        errors = clustering.spatial_errors(plan,candidate,stops,{**req,"excluded_stop_ids":["c"]})
        self.assertTrue(any("必去" in error for error in errors))
        self.assertTrue(any("固定" in error for error in errors))
        self.assertTrue(any("排除" in error for error in errors))
        stops["c"]["opening_claim"] = "闭馆"
        stops["a"]["evidence"] = {"quote":"景点a玩一整天"}
        errors = clustering.spatial_errors(plan,candidate,stops,req)
        self.assertTrue(any("闭馆" in error for error in errors))
        self.assertTrue(any("独占" in error for error in errors))


if __name__ == "__main__":
    unittest.main()
