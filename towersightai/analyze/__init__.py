"""Offline analysis dashboard for TowerSightAI raw data (development/verification only).

Reads the NAS raw archive (JSONL shards, snapshots, clips), rebuilds camera and radar person
episodes with engine-equivalent rules, lets a reviewer label each episode against the evidence,
and reports per-source accuracy. Read-only with respect to the product: this package must never
import the process engine, state machine, PLC adapters, or the operator UI, and nothing here can
influence ``can_show_final_ok``.
"""

from __future__ import annotations

__all__: list[str] = []
