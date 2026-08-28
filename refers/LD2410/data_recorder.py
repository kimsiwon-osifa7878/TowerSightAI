from __future__ import annotations

from pathlib import Path
from typing import List, Optional

import pandas as pd


CSV_COLUMNS = [
    "timestamp",
    "profile",
    "target_status",
    "target_status_text",
    "moving_distance_cm",
    "moving_energy",
    "motionless_distance_cm",
    "motionless_energy",
    "detection_distance_cm",
    "light",
    "out_pin",
] + [f"gate{i}_moving" for i in range(9)] + [f"gate{i}_motionless" for i in range(9)]


class DataRecorder:
    def __init__(self) -> None:
        self.rows: List[dict] = []
        self.active_profile: Optional[str] = None

    def start(self, profile: str) -> None:
        self.active_profile = profile

    def stop(self) -> None:
        self.active_profile = None

    def clear(self) -> None:
        self.rows.clear()

    def add_frame(self, frame) -> bool:
        if not self.active_profile:
            return False
        row = {
            "timestamp": frame.timestamp.isoformat(timespec="milliseconds"),
            "profile": self.active_profile,
            "target_status": frame.target_status,
            "target_status_text": frame.target_status_text,
            "moving_distance_cm": frame.moving_distance_cm,
            "moving_energy": frame.moving_energy,
            "motionless_distance_cm": frame.motionless_distance_cm,
            "motionless_energy": frame.motionless_energy,
            "detection_distance_cm": frame.detection_distance_cm,
            "light": frame.light,
            "out_pin": frame.out_pin,
        }
        for i in range(9):
            row[f"gate{i}_moving"] = frame.moving_gate_energy[i]
            row[f"gate{i}_motionless"] = frame.motionless_gate_energy[i]
        self.rows.append(row)
        return True

    def to_dataframe(self) -> pd.DataFrame:
        return pd.DataFrame(self.rows, columns=CSV_COLUMNS)

    def save_csv(self, path: str | Path) -> None:
        self.to_dataframe().to_csv(path, index=False, encoding="utf-8-sig")

    def load_csv(self, path: str | Path) -> pd.DataFrame:
        df = pd.read_csv(path)
        for col in CSV_COLUMNS:
            if col not in df.columns:
                df[col] = None
        self.rows = df[CSV_COLUMNS].to_dict("records")
        return self.to_dataframe()
