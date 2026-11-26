from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.player_diagnostics import player_diagnostics_router


def client():
    app = FastAPI()
    app.include_router(player_diagnostics_router("test"))
    return TestClient(app)


def event(**changes):
    return {"event": "loadedmetadata", "at": 1000, "media_time": 6,
            "paused": False, "ready_state": 4, "network_state": 2,
            "hidden": False, "proxy_id": "proxy", "detail": "tracks=1", **changes}


def test_trace_is_bounded_and_isolated_between_app_instances():
    a, b = client(), client()
    for batch in range(14):
        r = a.post("/api/player/diagnostics", json={"client_id": "phone",
            "events": [event(at=batch*32+i) for i in range(32)]})
        assert r.status_code == 200 and r.json()["accepted"] == 32
    result = a.get("/api/player/diagnostics").json()
    assert result["build"] == "test"
    assert len(result["events"]) == 400
    assert result["events"][0]["at"] == 48
    assert result["events"][-1]["at"] == 447
    assert result["events"][0]["client_id"] == "phone"
    assert b.get("/api/player/diagnostics").json()["events"] == []


def test_trace_rejects_oversized_batches_and_invalid_fields():
    c = client()
    for events in ([event()] * 33, [event(event="x" * 49)],
                   [event(detail="x" * 161)], [event(ready_state=9)]):
        response = c.post("/api/player/diagnostics", json={"client_id":"phone", "events":events})
        assert response.status_code == 422
    assert c.get("/api/player/diagnostics").json()["events"] == []
