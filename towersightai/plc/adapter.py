from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Protocol


@dataclass
class PLCEvent:
    name: str
    payload: dict[str, object] = field(default_factory=dict)
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class PLCAdapter(Protocol):
    def send(self, event: str, payload: dict[str, object] | None = None) -> None:
        ...


@dataclass
class FakePLCAdapter:
    events: list[PLCEvent] = field(default_factory=list)

    def send(self, event: str, payload: dict[str, object] | None = None) -> None:
        self.events.append(PLCEvent(event, payload or {}))


@dataclass
class SimulatorPLCAdapter:
    connected: bool = True
    events: list[PLCEvent] = field(default_factory=list)

    def send(self, event: str, payload: dict[str, object] | None = None) -> None:
        if not self.connected:
            raise ConnectionError("PLC simulator is disconnected")
        self.events.append(PLCEvent(event, payload or {}))

    @property
    def event_names(self) -> tuple[str, ...]:
        return tuple(event.name for event in self.events)
        self.events.append(event)
