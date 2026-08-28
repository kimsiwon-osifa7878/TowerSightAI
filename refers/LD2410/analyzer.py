from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Deque, Dict, Iterable, List, Optional

import pandas as pd


GATES = list(range(9))
ENERGY_TYPES = ("moving", "motionless")


@dataclass
class DetectionState:
    state: str
    active_gates: List[int]
    hit_count: int
    sample_count: int
    last_score: float


def gate_column(energy_type: str, gate: int) -> str:
    return f"gate{gate}_{energy_type}"


def create_profile(df: pd.DataFrame, profile_name: str) -> pd.DataFrame:
    subset = df[df["profile"] == profile_name].copy()
    if subset.empty:
        return pd.DataFrame()

    rows = []
    for energy_type in ENERGY_TYPES:
        for gate in GATES:
            col = gate_column(energy_type, gate)
            if col not in subset.columns:
                continue
            series = pd.to_numeric(subset[col], errors="coerce").dropna()
            if series.empty:
                continue
            rows.append(
                {
                    "profile": profile_name,
                    "energy_type": energy_type,
                    "gate": gate,
                    "avg": float(series.mean()),
                    "max": float(series.max()),
                    "std": float(series.std(ddof=0)),
                    "samples": int(series.count()),
                }
            )
    return pd.DataFrame(rows)


def create_all_profiles(df: pd.DataFrame) -> pd.DataFrame:
    frames = [create_profile(df, name) for name in sorted(df["profile"].dropna().unique())]
    frames = [frame for frame in frames if not frame.empty]
    return pd.concat(frames, ignore_index=True) if frames else pd.DataFrame()


def compare_empty_vs_person_still(df: pd.DataFrame) -> pd.DataFrame:
    empty = create_profile(df, "empty_car")
    still = create_profile(df, "person_still")
    if empty.empty or still.empty:
        return pd.DataFrame()

    merged = empty.merge(
        still,
        on=["energy_type", "gate"],
        suffixes=("_empty", "_person_still"),
    )
    merged["avg_diff"] = merged["avg_person_still"] - merged["avg_empty"]
    merged["separation_score"] = merged["avg_person_still"] - merged["max_empty"]
    return merged[
        [
            "energy_type",
            "gate",
            "avg_empty",
            "max_empty",
            "std_empty",
            "avg_person_still",
            "max_person_still",
            "std_person_still",
            "avg_diff",
            "separation_score",
            "samples_empty",
            "samples_person_still",
        ]
    ]


def recommend_thresholds(
    df: pd.DataFrame,
    margin: int = 5,
    interest_gates: Optional[Iterable[int]] = None,
) -> pd.DataFrame:
    interest = set(interest_gates if interest_gates is not None else [3, 4, 5])
    empty = create_profile(df, "empty_car")
    rows = []
    for energy_type in ENERGY_TYPES:
        for gate in GATES:
            match = empty[(empty["energy_type"] == energy_type) & (empty["gate"] == gate)]
            empty_max = float(match["max"].iloc[0]) if not match.empty else 0.0
            recommended = 100 if gate not in interest else min(100, int(round(empty_max + margin)))
            rows.append(
                {
                    "energy_type": energy_type,
                    "gate": gate,
                    "empty_max": empty_max,
                    "margin": margin if gate in interest else None,
                    "interest_gate": gate in interest,
                    "recommended_threshold": recommended,
                }
            )
    return pd.DataFrame(rows)


class RealtimeBaselineComparator:
    def __init__(self, window_seconds: int = 8) -> None:
        self.window = timedelta(seconds=window_seconds)
        self.history: Deque[tuple[datetime, List[int], float]] = deque()
        self.thresholds: Dict[tuple[str, int], float] = {}
        self.interest_gates = {3, 4, 5}

    def configure(
        self,
        baseline_profile: pd.DataFrame,
        margin: int = 5,
        interest_gates: Optional[Iterable[int]] = None,
    ) -> None:
        self.interest_gates = set(interest_gates if interest_gates is not None else [3, 4, 5])
        self.thresholds.clear()
        if baseline_profile.empty:
            return
        for _, row in baseline_profile.iterrows():
            gate = int(row["gate"])
            if gate not in self.interest_gates:
                threshold = 100
            else:
                threshold = min(100, float(row["max"]) + margin)
            self.thresholds[(str(row["energy_type"]), gate)] = threshold

    def update(self, frame) -> DetectionState:
        now = frame.timestamp
        active = []
        max_score = 0.0
        for gate in self.interest_gates:
            moving_score = frame.moving_gate_energy[gate] - self.thresholds.get(("moving", gate), 100)
            motionless_score = frame.motionless_gate_energy[gate] - self.thresholds.get(("motionless", gate), 100)
            gate_score = max(moving_score, motionless_score)
            if gate_score > 0:
                active.append(gate)
                max_score = max(max_score, gate_score)

        self.history.append((now, active, max_score))
        cutoff = now - self.window
        while self.history and self.history[0][0] < cutoff:
            self.history.popleft()

        sample_count = len(self.history)
        hit_count = sum(1 for _, gates, _ in self.history if gates)
        active_gates = sorted({gate for _, gates, _ in self.history for gate in gates})

        if sample_count == 0 or hit_count == 0:
            state = "CLEAR"
        else:
            ratio = hit_count / sample_count
            if hit_count >= 4 and ratio >= 0.35:
                state = "DETECTED"
            else:
                state = "SUSPECT"

        return DetectionState(
            state=state,
            active_gates=active_gates,
            hit_count=hit_count,
            sample_count=sample_count,
            last_score=max_score,
        )
