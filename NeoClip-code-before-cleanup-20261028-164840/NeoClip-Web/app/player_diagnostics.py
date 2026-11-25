"""Bounded, in-memory browser events for diagnosing native player behavior."""
import time
from collections import deque

from fastapi import APIRouter
from pydantic import BaseModel, Field


class PlayerEvent(BaseModel):
    event: str = Field(max_length=48)
    at: float = Field(ge=0, allow_inf_nan=False)
    media_time: float = Field(ge=0, allow_inf_nan=False)
    paused: bool
    ready_state: int = Field(ge=0, le=4)
    network_state: int = Field(ge=0, le=3)
    hidden: bool
    proxy_id: str = Field(default="", max_length=64)
    detail: str = Field(default="", max_length=160)


class PlayerTrace(BaseModel):
    client_id: str = Field(min_length=1, max_length=64)
    events: list[PlayerEvent] = Field(max_length=32)


def player_diagnostics_router(build):
    router = APIRouter()
    events = deque(maxlen=400)

    @router.post("/api/player/diagnostics")
    async def record_player_events(trace: PlayerTrace):
        received_at = time.time()
        for event in trace.events:
            events.append({"client_id": trace.client_id, "received_at": received_at,
                           **event.model_dump()})
        return {"accepted": len(trace.events)}

    @router.get("/api/player/diagnostics")
    async def get_player_events():
        return {"build": build, "events": list(events)}

    return router
