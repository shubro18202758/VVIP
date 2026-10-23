"""Read-only MCP city tools, canvas verbs and POST /mcp (wave 3, contract C-D2).

What this suite pins:
    R2 rail     no tool name approves, decides, earmarks, requests a dispatch,
                withdraws or transitions anything; the executor POSTs only to
                the compute allow-list (predict / optimize / evaluate /
                traffic-live / simulate) — every tool is exercised over an
                httpx MockTransport that records each request
    city tools  each hits its C-C GET path with validated parameters; ids are
                checked against the oracle's grammar before they reach a path;
                a 404 is {ok:false, not_found | not_deployed}; results are
                compacted for model context and never carry a raw plate
    events      get_active_corridor_events honours event_id, treats a
                movement_id that IS an event id as the event filter, widens
                to the event history for a closed event, and answers a filter
                that hides every active event with the whole board + a note
    history     get_historical_pattern sends hours=168 + day_of_week/hour and
                keeps a pre-C-P oracle honest (rows filtered to the IST hour)
    canvas      the six verbs validate into directives and never call HTTP
    /mcp        JSON-RPC 2.0 over HTTP: tools/list >= 20 with none of the
                forbidden verbs, tools/call, resources, prompts, parse /
                invalid-request errors, notifications, the body cap
    regressions asyncio is imported by mcp/server.py (the stdio transport) and
                the segment-history resource URI is split('/')[3]
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import re
from pathlib import Path

import httpx
import pytest

from convoy_brain.mcp import server as mcp_server
from convoy_brain.mcp.server import (
    CANVAS_VERBS,
    FORBIDDEN_TOOL_VERBS,
    POST_ALLOWLIST,
    TOOL_DEFINITIONS,
    ToolExecutionError,
    ToolExecutor,
    canvas_directive,
    mask_plates,
    parse_urn,
    plate_label,
)

VEHICLE = "1b94e9d2-7e11-4174-a735-7dd3be3189cf"
PURSUIT = "4508d86d-566b-438f-8414-b3d67fd7e6e4"
CLOSED = "9a28fc47-4991-4745-8f0e-462c2a87b515"

ENTITIES = {
    "as_of": "2026-10-22T21:00:00Z",
    "entities": [
        {"urn": f"vehicle:{VEHICLE}", "kind": "vehicle", "id": VEHICLE, "label": "GJ27EM•••1",
         "lon": 72.575356, "lat": 23.02503, "state": "flagged", "source": "emulated",
         "t": "2026-10-22T16:56:21.000Z", "age_sec": 120.0, "links": [],
         "props": {"plate": "GJ27EM9001", "plate_masked": "GJ27EM•••1",
                   "earmark_state": "flagged", "lawful_basis": "stolen_vehicle_fir",
                   "earmark_reason": "STOLEN VEHICLE reported — FIR 4401/2026, GJ27EM9001"}},
        {"urn": "vehicle:2b94e9d2-7e11-4174-a735-7dd3be3189cf", "kind": "vehicle",
         "id": "2b94e9d2-7e11-4174-a735-7dd3be3189cf", "label": "GJ01AB•••4", "lon": 72.58,
         "lat": 23.03, "state": "pending_fine", "source": "live", "links": [],
         "props": {"plate": "GJ01AB1234", "earmark_state": "pending_fine"}},
        {"urn": f"event:{PURSUIT}", "kind": "event", "id": PURSUIT,
         "label": "PURSUIT — stolen GJ27EM•••1", "lon": 72.58, "lat": 23.02, "state": "planning",
         "source": "live", "links": [],
         "props": {"event_kind": "vehicle_pursuit", "title": "PURSUIT — stolen GJ27EM9001",
                   "status": "planning",
                   "route_spines": [{"label": "sweep", "segment_ids": [1012, 1015]}],
                   "metadata": {"vehicle_id": VEHICLE, "fir": "FIR 4401/2026"}}},
    ],
    "feeds": {"vehicle": {"count": 2, "status": "ok"}, "event": {"count": 1, "status": "ok"}},
}
EVENTS_ACTIVE = {"events": [{
    "event_id": PURSUIT, "event_kind": "vehicle_pursuit", "status": "planning",
    "title": "PURSUIT — stolen GJ27EM9001 (FIR 4401/2026)", "protection_class": "none",
    "movement_id": None, "routes": [{"label": "old-city sweep belt", "segment_ids": [1012, 1015]}],
    "phases": [], "metadata": {"vehicle_id": VEHICLE},
}]}
EVENTS_HISTORY = {"events": [
    {"event_id": CLOSED, "event_kind": "protected_movement", "status": "cancelled",
     "title": "Z protected movement · Relief Road", "routes": [], "phases": [], "metadata": {}},
]}


class Recorder:
    """MockTransport handler: routes by path, records (method, path, params, body)."""

    def __init__(self, routes: dict[tuple[str, str], object] | None = None) -> None:
        self.routes = routes or {}
        self.seen: list[tuple[str, str, dict, object]] = []

    def __call__(self, request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content) if request.content else None
        self.seen.append((request.method, request.url.path, dict(request.url.params), body))
        handler = self.routes.get((request.method, request.url.path))
        if handler is None:
            # FastAPI's own answer for a route that does not exist
            return httpx.Response(404, json={"detail": "Not Found"})
        if callable(handler):
            return handler(request)
        if isinstance(handler, httpx.Response):
            return handler
        return httpx.Response(200, json=handler)

    def posts(self) -> set[str]:
        return {path for method, path, _p, _b in self.seen if method == "POST"}


def executor_with(recorder: Recorder) -> ToolExecutor:
    return ToolExecutor(
        traffic_oracle_url="http://127.0.0.1:8114",
        client=httpx.AsyncClient(base_url="http://127.0.0.1:8114",
                                 transport=httpx.MockTransport(recorder)),
    )


#: One plausible call per tool (the arguments a drafting tier would send).
SAMPLE_ARGS = {
    "predict_traffic_flow": {"segment_ids": [1012]},
    "find_convoy_routes": {"origin_segment": 1001, "destination_segment": 1040},
    "plan_diversions": {"route_segment_ids": [1012, 1015]},
    "evaluate_scenarios": {"scenarios": [{"scenario_id": "a", "route_segment_ids": [1012]}]},
    "predict_eta": {"route_length_m": 5000, "num_segments": 4, "avg_predicted_speed": 40,
                    "avg_predicted_congestion": 0.3, "hour": 9, "day_of_week": 1},
    "query_shortest_path": {"source_segment": 1001, "target_segment": 1040},
    "query_k_shortest_paths": {"source_segment": 1001, "target_segment": 1040},
    "query_segments_in_bbox": {"min_lon": 72.5, "min_lat": 23.0, "max_lon": 72.6, "max_lat": 23.1},
    "query_segment_details": {"segment_id": 1012},
    "get_live_traffic": {"segment_ids": [1012]},
    "get_historical_pattern": {"segment_id": 1012, "day_of_week": 3, "hour": 10},
    "simulate_twin": {"route_segment_ids": [1012]},
    "get_active_corridor_events": {},
    "city_query_entities": {"kinds": ["vehicle"]},
    "city_get_dossier": {"urn": f"vehicle:{VEHICLE}"},
    "city_feed": {"limit": 5},
    "get_signal_state": {},
    "list_units": {},
    "list_dispatches": {"event_id": PURSUIT},
    "get_interception_plan": {"event_id": PURSUIT},
    "get_situation_log": {"event_id": PURSUIT},
    "list_enforcement_recommendations": {"status": "pending"},
    "canvas_fly_to": {"urn": f"vehicle:{VEHICLE}"},
    "canvas_track_entity": {"urn": f"vehicle:{VEHICLE}"},
    "canvas_highlight": {"urns": [f"vehicle:{VEHICLE}"]},
    "canvas_annotate": {"urn": f"vehicle:{VEHICLE}", "text": "last lawful read",
                        "severity": "warn"},
    "canvas_set_sensor": {"mode": "flir-iron"},
    "canvas_toggle_layer": {"layer_id": "cameras", "on": False},
}


class TestReadOnlyRail:
    def test_no_tool_name_makes_the_ai_an_actor(self):
        names = [t["name"] for t in TOOL_DEFINITIONS]
        forbidden = re.compile(r"approve|decide|earmark|dispatch_request|withdraw|transition")
        assert [n for n in names if forbidden.search(n)] == []
        assert [n for n in names if FORBIDDEN_TOOL_VERBS.search(n)] == []
        assert len(names) >= 20

    def test_post_allow_list_is_compute_only(self):
        verbs = re.compile(r"/(predict|optimize|evaluate|simulate)/|/traffic/live$")
        assert POST_ALLOWLIST and all(verbs.search(p) for p in POST_ALLOWLIST)

    async def test_every_tool_posts_only_to_the_allow_list(self):
        recorder = Recorder()  # every path 404s: the transport is what we watch
        executor = executor_with(recorder)
        assert set(SAMPLE_ARGS) == {t["name"] for t in TOOL_DEFINITIONS}
        for name, args in SAMPLE_ARGS.items():
            with contextlib.suppress(ToolExecutionError):
                await executor.execute(name, args)
        assert recorder.posts() <= POST_ALLOWLIST
        # the city tools read: not one of them POSTs
        city = {"city_query_entities", "city_get_dossier", "city_feed", "get_signal_state",
                "list_units", "list_dispatches", "get_interception_plan", "get_situation_log",
                "list_enforcement_recommendations"}
        for name in city:
            recorder.seen.clear()
            with contextlib.suppress(ToolExecutionError):
                await executor.execute(name, SAMPLE_ARGS[name])
            assert recorder.seen and {m for m, *_ in recorder.seen} == {"GET"}, name

    async def test_a_post_off_the_allow_list_is_refused_without_a_request(self):
        recorder = Recorder()
        executor = executor_with(recorder)
        await executor._ensure_client()
        with pytest.raises(ToolExecutionError) as exc:
            await executor._post("/api/v1/events/x/status", {"to_status": "cancelled"})
        assert exc.value.retryable is False and recorder.seen == []

    async def test_canvas_verbs_never_call_http(self):
        recorder = Recorder()
        executor = executor_with(recorder)
        for verb in CANVAS_VERBS:
            result = await executor.execute(verb, SAMPLE_ARGS[verb])
            assert result["ok"] is True and result["directive"]["verb"] == verb
        assert recorder.seen == []


class TestCityTools:
    async def test_query_entities_compacts_masks_and_matches_plates(self):
        recorder = Recorder({("GET", "/api/v1/city/entities"): ENTITIES})
        executor = executor_with(recorder)
        result = await executor.execute(
            "city_query_entities", {"kinds": ["vehicle", "nonsense"], "match": "gj 27 em 9001",
                                    "bbox": [72.0, 22.0, 72.7, 23.1]})
        method, path, params, _ = recorder.seen[-1]
        assert params["kinds"] == "vehicle"
        assert params["bbox"] == "72.400000,22.900000,72.700000,23.100000"  # clipped to the city
        assert result["ignored_kinds"] == ["nonsense"]
        assert result["count"] == 1 and result["entities"][0]["urn"] == f"vehicle:{VEHICLE}"
        row = result["entities"][0]
        assert "plate" not in json.dumps(row["props"]).lower().replace("plate_masked", "")
        assert "GJ27EM9001" not in json.dumps(result)
        assert row["props"]["lawful_basis"] == "stolen_vehicle_fir"
        assert row["props"]["earmark_reason"].endswith("GJ27EM•••1")
        # the entity's OWN time in IST; the query's time is labelled as such
        # (live: the model quoted the query's as_of as "last seen at …")
        assert row["seen_ist"] == "22:26 IST, 22 Oct"
        assert result["queried_at"] == "2026-10-22T21:00:00Z" and "as_of" not in result

    async def test_event_rows_carry_the_vehicle_link(self):
        recorder = Recorder({("GET", "/api/v1/city/entities"): ENTITIES})
        result = await executor_with(recorder).execute("city_query_entities", {"kinds": ["event"]})
        event = next(r for r in result["entities"] if r["kind"] == "event")
        assert event["props"]["vehicle_urn"] == f"vehicle:{VEHICLE}"
        assert event["props"]["spines"] == [{"label": "sweep", "segment_ids": [1012, 1015]}]

    async def test_dossier_validates_the_urn_before_any_path(self):
        recorder = Recorder()
        executor = executor_with(recorder)
        for bad in ("vehicle:../../health", "vehicle:not-a-uuid", "junction:-3", "camera:/x",
                    "segment:", "person:42", "nonsense", ""):
            with pytest.raises(ToolExecutionError) as exc:
                await executor.execute("city_get_dossier", {"urn": bad})
            assert exc.value.retryable is False
        assert recorder.seen == []

    async def test_dossier_404_is_not_found_and_a_missing_route_is_not_deployed(self):
        recorder = Recorder({
            ("GET", f"/api/v1/city/entity/vehicle/{VEHICLE}"):
                httpx.Response(404, json={"detail": f"vehicle:{VEHICLE} not found"}),
        })
        executor = executor_with(recorder)
        missing = await executor.execute("city_get_dossier", {"urn": f"vehicle:{VEHICLE.upper()}"})
        assert missing == {"ok": False, "not_found": True, "subject": f"vehicle:{VEHICLE}",
                           "detail": f"vehicle:{VEHICLE} not found"}
        absent = await executor.execute("get_signal_state", {})
        assert absent["ok"] is False and absent["not_deployed"] is True

    async def test_dossier_compacts_related_and_audit(self):
        dossier = {
            "entity": ENTITIES["entities"][0],
            "related": [{"relation": "pursuit", "entity": ENTITIES["entities"][2]}],
            "audit": [{"t": "2026-10-22T16:56:21Z", "kind": "anpr", "actor": "EM-CAM-02",
                       "actor_kind": "machine", "text": "ANPR hit · STOLEN VEHICLE · GJ27EM9001",
                       "source": "emulated"}],
        }
        recorder = Recorder({("GET", f"/api/v1/city/entity/vehicle/{VEHICLE}"): dossier})
        result = await executor_with(recorder).execute(
            "city_get_dossier", {"urn": f"vehicle:{VEHICLE}"})
        assert recorder.seen[-1][2] == {"audit_limit": "10"}
        assert result["entity"]["urn"] == f"vehicle:{VEHICLE}"
        assert result["related"][0]["relation"] == "pursuit"
        assert result["audit"][0]["text"].endswith("GJ27EM•••1")

    async def test_event_scoped_tools_validate_their_ids(self):
        recorder = Recorder({
            ("GET", f"/api/v1/events/{PURSUIT}/interception-plan"): {
                "event_id": PURSUIT, "vehicle_id": VEHICLE,
                "latest_sighting": {"sighting_id": "12a102e0-628a-56c2-ad6d-a350fce94c70",
                                    "plate": "GJ27EM9001", "camera_id": "EM-CAM-02",
                                    "position": [72.575, 23.025], "source": "emulated_anpr"},
                "sighting_age_sec": 900.0, "stale": True,
                "direction_of_travel": {"basis": "heading", "towards_junction_ids": [3]},
                "interception_junctions": [{"junction_id": 3, "name": "Lal Darwaja",
                                            "graph_cost_sec": 27.8}],
            },
            ("GET", "/api/v1/dispatches"): {"dispatches": [], "count": 0},
        })
        executor = executor_with(recorder)
        plan = await executor.execute("get_interception_plan", {"event_id": PURSUIT})
        assert plan["vehicle_urn"] == f"vehicle:{VEHICLE}"
        assert plan["chokes"][0]["urn"] == "junction:3"
        assert "GJ27EM9001" not in json.dumps(plan)  # the sighting's plate never travels
        for tool in ("get_interception_plan", "list_dispatches"):
            with pytest.raises(ToolExecutionError):
                await executor.execute(tool, {"event_id": "../../events/x"})
        await executor.execute("list_dispatches", {"event_id": PURSUIT, "status": "requested"})
        assert recorder.seen[-1][2] == {"limit": "20", "event_id": PURSUIT, "status": "requested"}

    async def test_recommendations_are_masked_and_say_who_decides(self):
        recorder = Recorder({("GET", "/api/v1/enforcement/recommendations"): {
            "recommendations": [{
                "recommendation_id": "da273ccf-fb0d-49f1-9b62-854520d3be56",
                "vehicle_id": VEHICLE, "plate": "GJ27EM9001", "rule_id": "R_SPD_SEGMENT",
                "recommendation": "e-challan recommended: GJ27EM9001 read at 92 km/h",
                "evidence": {"plate": "GJ27EM9001", "speed_kmh": 92.0, "speed_limit_kmh": 40.0},
                "status": "pending"}],
            "status": "pending"}})
        executor = executor_with(recorder)
        result = await executor.execute("list_enforcement_recommendations", {})
        assert "GJ27EM9001" not in json.dumps(result)
        evidence = result["recommendations"][0]["evidence"]
        assert evidence == {"speed_kmh": 92.0, "speed_limit_kmh": 40.0}
        assert "named officer" in result["rail"]
        with pytest.raises(ToolExecutionError):
            await executor.execute("list_enforcement_recommendations", {"status": "approve_all"})


class TestActiveEvents:
    def recorder(self) -> Recorder:
        return Recorder({
            ("GET", "/api/v1/events/active"): EVENTS_ACTIVE,
            ("GET", "/api/v1/events"): EVENTS_HISTORY,
        })

    async def test_event_id_filters(self):
        result = await executor_with(self.recorder()).execute(
            "get_active_corridor_events", {"event_id": PURSUIT})
        assert result["count"] == 1 and result["events"][0]["status"] == "planning"
        assert result["events"][0]["urn"] == f"event:{PURSUIT}"
        assert result["events"][0]["vehicle_urn"] == f"vehicle:{VEHICLE}"
        # the linked vehicle is named by its masked plate, never its uuid
        assert result["events"][0]["vehicle_label"] == "vehicle GJ27EM•••1"
        assert "GJ27EM9001" not in json.dumps(result)

    async def test_a_movement_id_that_is_an_event_id_filters_the_event(self):
        """The pursuit has no movement: the deck's old path sent its event id
        as movement_id and every event was dropped (live #60)."""
        result = await executor_with(self.recorder()).execute(
            "get_active_corridor_events", {"movement_id": PURSUIT})
        assert result["count"] == 1 and result["events"][0]["event_id"] == PURSUIT

    async def test_a_filter_that_hides_every_event_answers_the_board_with_a_note(self):
        result = await executor_with(self.recorder()).execute(
            "get_active_corridor_events", {"event_kind": "protected_movement"})
        assert result["count"] == 1 and result["total_active"] == 1
        assert "no active event matches" in result["note"]

    async def test_a_closed_event_is_found_in_the_history(self):
        result = await executor_with(self.recorder()).execute(
            "get_active_corridor_events", {"event_id": CLOSED})
        assert result["count"] == 1 and result["events"][0]["status"] == "cancelled"
        assert "no longer active" in result["note"]


class TestHistoricalBaseline:
    async def test_asks_for_a_week_of_the_same_weekday_and_hour(self):
        rows = [
            {"hour_utc": "2026-10-22T04:00:00+00:00", "avg_speed_kmh": 40.0},  # Thu 09:30 IST
            {"hour_utc": "2026-10-22T05:00:00+00:00", "avg_speed_kmh": 44.0},  # Thu 10:30 IST
            {"hour_utc": "2026-10-22T08:00:00+00:00", "avg_speed_kmh": 20.0},  # Thu 13:30 IST
            {"hour_utc": "2026-10-21T04:00:00+00:00", "avg_speed_kmh": 10.0},  # Wed
        ]
        recorder = Recorder({("GET", "/api/v1/traffic/historical"):
                             {"segment_id": 1012, "aggregates": rows}})
        result = await executor_with(recorder).execute(
            "get_historical_pattern", {"segment_id": 1012, "day_of_week": 3, "hour": 10})
        assert recorder.seen[-1][2] == {"segment_id": "1012", "hours": "168",
                                        "day_of_week": "3", "hour": "10"}
        # pre-C-P oracle: the brain keeps only the buckets overlapping Thu 10-11 IST
        assert [r["avg_speed_kmh"] for r in result["aggregates"]] == [40.0, 44.0]
        assert result["filtered_by"] == "brain" and result["provenance"] == "real"
        assert result["synthetic_aggregates"] == []

    async def test_a_c_p_oracle_is_trusted_and_synthetic_rows_are_labelled(self):
        recorder = Recorder({("GET", "/api/v1/traffic/historical"): {
            "segment_id": 1001, "aggregates": [], "provenance": "synthetic",
            "synthetic_aggregates": [
                {"hour_utc": "2026-10-15T04:00:00+00:00", "avg_speed_kmh": 33.0}],
            "basis": {"kind": "same_ist_weekday_hour", "day_of_week": 3, "hour": 10,
                      "lookback_hours": 168, "ist_bucket_offset_min": 30}}})
        result = await executor_with(recorder).execute(
            "get_historical_pattern", {"segment_id": 1001, "day_of_week": 3, "hour": 10})
        assert result["filtered_by"] == "oracle" and result["provenance"] == "synthetic"
        assert result["synthetic_aggregates"][0]["data_quality"] == "synthetic"
        assert "never a calibration input" in result["note"]

    def test_the_description_states_real_and_labelled_synthetic(self):
        tool = next(t for t in TOOL_DEFINITIONS if t["name"] == "get_historical_pattern")
        text = tool["description"]
        assert "IST weekday and hour" in text and "REAL hourly aggregates" in text
        assert "synthetic" in text and "never a calibration input" in text


class TestCanvasDirectives:
    def test_valid_verbs_become_directives(self):
        fly = canvas_directive("canvas_fly_to", {"lon": 72.58, "lat": 23.03, "range_m": 10})
        assert fly["directive"]["args"] == {"lon": 72.58, "lat": 23.03, "range_m": 100}
        track = canvas_directive("canvas_track_entity", {"urn": f"VEHICLE:{VEHICLE}".lower()})
        assert track["directive"]["args"]["urn"] == f"vehicle:{VEHICLE}"
        note = canvas_directive("canvas_annotate", {"urn": "junction:3", "text": "GJ27EM9001 seen"})
        assert note["directive"]["args"] == {"urn": "junction:3", "text": "GJ27EM•••1 seen",
                                              "severity": "info"}
        assert canvas_directive("canvas_toggle_layer", {"layer_id": "photoreal-3d", "on": True})

    @pytest.mark.parametrize("verb,args", [
        ("canvas_fly_to", {"lon": 77.2, "lat": 28.6}),             # Delhi: outside the city
        ("canvas_fly_to", {}),
        ("canvas_track_entity", {"urn": "vehicle:stolen"}),
        ("canvas_highlight", {"urns": []}),
        ("canvas_highlight", {"urns": [f"junction:{i}" for i in range(1, 23)]}),
        ("canvas_annotate", {"urn": "junction:3", "text": "x" * 141}),
        ("canvas_annotate", {"urn": "junction:3", "text": "ok", "severity": "panic"}),
        ("canvas_set_sensor", {"mode": "thermal"}),
        ("canvas_toggle_layer", {"layer_id": "faces", "on": True}),
        ("canvas_toggle_layer", {"layer_id": "units", "on": "yes"}),
    ])
    def test_invalid_arguments_are_refused(self, verb, args):
        with pytest.raises(ToolExecutionError) as exc:
            canvas_directive(verb, args)
        assert exc.value.retryable is False

    def test_urn_grammar_mirrors_the_oracle(self):
        assert parse_urn("camera:EM-CAM-01") == ("camera", "EM-CAM-01")
        assert parse_urn("camera:osm:123") == ("camera", "osm:123")
        assert parse_urn("junction:0") is None and parse_urn("junction:007") == ("junction", "7")
        assert parse_urn("event:../x") is None and parse_urn("unit:a b") is None

    def test_plates_are_masked_in_free_text(self):
        masked = mask_plates("stolen GJ27EM9001 and GJ 01 AB 1234")
        assert masked == "stolen GJ27EM•••1 and GJ 01 AB •••4"

    def test_plate_label_names_the_first_mark_masked(self):
        assert plate_label("PURSUIT — stolen GJ27EM9001 (FIR 4401/2026)") == "vehicle GJ27EM•••1"
        assert plate_label("Z protected movement · Relief Road") is None
        assert plate_label(None) is None


class TestRegressions:
    def test_asyncio_is_imported_for_the_stdio_transport(self):
        """Original-plan defect: run_stdio_transport used asyncio unimported."""
        assert mcp_server.asyncio is asyncio
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        assert re.search(r"^import asyncio$", source, re.MULTILINE)

    async def test_segment_history_uri_reads_the_fourth_part(self):
        """Original-plan defect: split('/')[2] read 'segments' as the id."""
        recorder = Recorder({("GET", "/api/v1/traffic/history/1012"):
                             {"segment_id": 1012, "observations": []}})
        result = await executor_with(recorder).read_resource("traffic://segments/1012/history")
        assert result["segment_id"] == 1012
        source = Path(mcp_server.__file__).read_text(encoding="utf-8")
        assert 'uri.split("/")[3]' in source

    async def test_a_segment_resource_id_must_be_an_integer(self):
        # POST /mcp serves resources over HTTP: nothing but an integer id
        # reaches the oracle path.
        recorder = Recorder({})
        executor = executor_with(recorder)
        for uri in ("traffic://segments/1012%2F..%2Fx/history",
                    "traffic://segments/../history", "traffic://segments/1/2/history",
                    "traffic://segments/0/history", "traffic://segments/１２/history"):
            with pytest.raises(ToolExecutionError) as caught:
                await executor.read_resource(uri)
            assert caught.value.retryable is False
        assert recorder.seen == []


@pytest.fixture
def http_app(monkeypatch):
    from conftest import FakeContextStore

    from convoy_brain import __main__ as brain_main

    monkeypatch.setenv("MODEL_HOST_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("LLAMA_BASE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("TRAFFIC_ORACLE_URL", "http://127.0.0.1:1")
    monkeypatch.setenv("EMBEDDING_SERVICE_URL", "http://127.0.0.1:1")
    monkeypatch.setattr(brain_main, "ConvoyContextStore", lambda valkey_url: FakeContextStore())
    return brain_main.create_app()


class TestMcpOverHttp:
    def rpc(self, client, body, raw: bytes | None = None):
        if raw is not None:
            return client.post("/mcp", content=raw, headers={"content-type": "application/json"})
        return client.post("/mcp", json=body)

    def test_tools_list_and_call(self, http_app):
        from starlette.testclient import TestClient

        with TestClient(http_app) as client:
            listed = self.rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/list"}).json()
            names = [t["name"] for t in listed["result"]["tools"]]
            assert len(names) >= 20
            assert not [n for n in names
                        if re.search(r"approve|decide|earmark|dispatch_request|withdraw", n)]
            called = self.rpc(client, {
                "jsonrpc": "2.0", "id": "c-1", "method": "tools/call",
                "params": {"name": "canvas_set_sensor", "arguments": {"mode": "raw"}},
            }).json()
            payload = json.loads(called["result"]["content"][0]["text"])
            assert called["id"] == "c-1" and payload["directive"]["verb"] == "canvas_set_sensor"
            init = self.rpc(client, {"jsonrpc": "2.0", "id": 2, "method": "initialize",
                                     "params": {"clientInfo": {"name": "test"}}}).json()
            assert init["result"]["serverInfo"]["name"] == "vvip-convoy-mcp"
            for method in ("resources/list", "prompts/list"):
                listed = self.rpc(client, {"jsonrpc": "2.0", "id": 3, "method": method})
                assert "result" in listed.json()
            prompt = self.rpc(client, {"jsonrpc": "2.0", "id": 4, "method": "prompts/get",
                                       "params": {"name": "vvip_security_protocol",
                                                  "arguments": {"vvip_class": "Z+"}}}).json()
            assert "6 lanes" in prompt["result"]["messages"][0]["content"]["text"]

    def test_bad_input_answers_json_rpc_errors(self, http_app):
        from starlette.testclient import TestClient

        with TestClient(http_app) as client:
            assert self.rpc(client, None, raw=b"{not json").json()["error"]["code"] == -32700
            assert self.rpc(client, [{"jsonrpc": "2.0", "id": 1, "method": "ping"}]).json()[
                "error"]["code"] == -32600
            assert self.rpc(client, {"id": 1, "method": "ping"}).json()["error"]["code"] == -32600
            assert self.rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "nope"}).json()[
                "error"]["code"] == -32601
            assert self.rpc(client, {"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                                     "params": ["x"]}).json()["error"]["code"] == -32602
            bad_urn = self.rpc(client, {"jsonrpc": "2.0", "id": 9, "method": "tools/call",
                                        "params": {"name": "canvas_track_entity",
                                                   "arguments": {"urn": "person:1"}}}).json()
            assert bad_urn["error"]["code"] == -32000
            note = self.rpc(client, {"jsonrpc": "2.0", "method": "initialized"})
            assert note.status_code == 204
            big = b'{"jsonrpc":"2.0","id":1,"method":"ping","pad":"' + b"a" * 300_000 + b'"}'
            capped = self.rpc(client, None, raw=big)
            assert capped.status_code == 413 and capped.json()["error"]["code"] == -32600
