"""Reviewer labels: append-only JSONL, latest record per (episode, reviewer) wins."""

from __future__ import annotations

import json
import os
import threading
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Mapping

VERDICTS = ("person", "no_person", "unsure")
VERDICT_LABELS = {"person": "사람 있음", "no_person": "사람 없음", "unsure": "판단 불가"}
DEFAULT_TAGS = ("문 밖 사람", "차량 내 탑승자", "작업자", "차량만 있음", "빈 공간", "화면 흐림/어두움", "스냅샷 없음")


class LabelStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._lock = threading.Lock()

    def append(
        self,
        *,
        episode_id: str,
        site: str,
        host: str,
        day: str,
        source: str,
        start: float,
        end: float,
        verdict: str,
        reviewer: str = "",
        tags: Iterable[str] = (),
        note: str = "",
        labeled_at: datetime | None = None,
    ) -> dict[str, Any]:
        if verdict not in VERDICTS:
            raise ValueError(f"verdict must be one of {VERDICTS}")
        record = {
            "episode_id": episode_id,
            "site": site,
            "host": host,
            "day": day,
            "source": source,
            "start": start,
            "end": end,
            "verdict": verdict,
            "reviewer": (reviewer or "").strip()[:64],
            "tags": sorted({str(tag).strip()[:40] for tag in tags if str(tag).strip()}),
            "note": (note or "").strip()[:500],
            "labeled_at": (labeled_at or datetime.now(timezone.utc)).isoformat(),
        }
        with self._lock:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            with self.path.open("a", encoding="utf-8") as fp:
                fp.write(json.dumps(record, ensure_ascii=False, sort_keys=True) + "\n")
                fp.flush()
                os.fsync(fp.fileno())
        return record

    def all_records(self) -> list[dict[str, Any]]:
        if not self.path.is_file():
            return []
        records: list[dict[str, Any]] = []
        with self.path.open("r", encoding="utf-8") as fp:
            for line in fp:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if isinstance(item, dict) and item.get("episode_id") and item.get("verdict") in VERDICTS:
                    records.append(item)
        return records

    def latest(self) -> dict[str, dict[str, Any]]:
        """Newest label per episode (any reviewer): line order is time order."""
        result: dict[str, dict[str, Any]] = {}
        for item in self.all_records():
            result[str(item["episode_id"])] = item
        return result

    def latest_by_reviewer(self) -> dict[str, dict[str, dict[str, Any]]]:
        result: dict[str, dict[str, dict[str, Any]]] = defaultdict(dict)
        for item in self.all_records():
            result[str(item["episode_id"])][str(item.get("reviewer") or "")] = item
        return dict(result)

    def history(self, episode_id: str) -> list[dict[str, Any]]:
        return [item for item in self.all_records() if item.get("episode_id") == episode_id]


def reviewer_agreement(by_reviewer: Mapping[str, Mapping[str, Mapping[str, Any]]]) -> dict[str, Any]:
    """Pairwise agreement across reviewers on episodes both labeled (simple percent + Cohen's κ)."""
    reviewers = sorted({name for labels in by_reviewer.values() for name in labels})
    pairs: list[dict[str, Any]] = []
    for i, a in enumerate(reviewers):
        for b in reviewers[i + 1 :]:
            shared = [(labels[a]["verdict"], labels[b]["verdict"]) for labels in by_reviewer.values() if a in labels and b in labels]
            if not shared:
                continue
            agree = sum(1 for x, y in shared if x == y)
            n = len(shared)
            po = agree / n
            pe = 0.0
            for verdict in VERDICTS:
                pa = sum(1 for x, _ in shared if x == verdict) / n
                pb = sum(1 for _, y in shared if y == verdict) / n
                pe += pa * pb
            kappa = (po - pe) / (1 - pe) if pe < 1 else 1.0
            conflicts = [eid for eid, labels in by_reviewer.items() if a in labels and b in labels and labels[a]["verdict"] != labels[b]["verdict"]]
            pairs.append({"a": a, "b": b, "shared": n, "agreement": round(po, 3), "kappa": round(kappa, 3), "conflicts": conflicts})
    return {"reviewers": reviewers, "pairs": pairs}


__all__ = ["DEFAULT_TAGS", "LabelStore", "VERDICTS", "VERDICT_LABELS", "reviewer_agreement"]
