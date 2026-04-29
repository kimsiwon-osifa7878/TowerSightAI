from __future__ import annotations

from dataclasses import dataclass, field


@dataclass
class FakePLCAdapter:
    events: list[str] = field(default_factory=list)

    def send(self, event: str) -> None:
        self.events.append(event)
