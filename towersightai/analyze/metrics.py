"""Accuracy metrics over labeled episodes, plus parameter sweeps.

Absolute recall is unobservable (nobody logs the people neither sensor saw), so recall here is
*mutual*: among presence groups any reviewer confirmed as a person, how many did each source
catch. The dashboard must always say so next to the number.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from datetime import datetime
from typing import Any, Iterable, Mapping, Sequence
from zoneinfo import ZoneInfo

from towersightai.analyze.digest import coverage_seconds
from towersightai.analyze.episodes import (
    SOURCE_CAMERA,
    SOURCE_RADAR,
    Episode,
    EpisodeParams,
    build_camera_episodes,
    build_radar_episodes,
)

SOURCES = (SOURCE_CAMERA, SOURCE_RADAR)


def _ratio(numerator: int, denominator: int) -> float | None:
    return round(numerator / denominator, 4) if denominator else None


def summarize(
    episodes: Sequence[Episode],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    coverage: Mapping[str, float],
    timezone_name: str = "Asia/Seoul",
) -> dict[str, Any]:
    tz = ZoneInfo(timezone_name)
    per_source: dict[str, dict[str, Any]] = {}
    groups: dict[str, list[Episode]] = defaultdict(list)
    for ep in episodes:
        groups[ep.group_id or ep.id].append(ep)

    # A presence group is "true" when any labeled episode in it says person.
    group_truth: dict[str, str | None] = {}
    for gid, members in groups.items():
        verdicts = {labels[m.id]["verdict"] for m in members if m.id in labels}
        if "person" in verdicts:
            group_truth[gid] = "person"
        elif "no_person" in verdicts and not verdicts - {"no_person", "unsure"}:
            group_truth[gid] = "no_person" if "no_person" in verdicts else None
        elif verdicts:
            group_truth[gid] = "unsure"
        else:
            group_truth[gid] = None
    true_groups = {gid for gid, truth in group_truth.items() if truth == "person"}

    for source in SOURCES:
        items = [ep for ep in episodes if ep.source == source]
        labeled = [ep for ep in items if ep.id in labels]
        verdicts = Counter(labels[ep.id]["verdict"] for ep in labeled)
        tp = verdicts.get("person", 0)
        fp = verdicts.get("no_person", 0)
        caught = {ep.group_id for ep in items if ep.group_id in true_groups}
        cov_seconds = float(coverage.get(source, 0.0))
        durations = sorted(ep.duration for ep in items)
        per_source[source] = {
            "episodes": len(items),
            "labeled": len(labeled),
            "verdicts": {"person": tp, "no_person": fp, "unsure": verdicts.get("unsure", 0)},
            "precision": _ratio(tp, tp + fp),
            "mutual_recall": _ratio(len(caught), len(true_groups)),
            "true_groups": len(true_groups),
            "caught_groups": len(caught),
            "false_per_hour": round(fp / (cov_seconds / 3600.0), 3) if cov_seconds > 0 else None,
            "coverage_seconds": round(cov_seconds, 1),
            "agreement": dict(Counter(ep.agreement for ep in items)),
            "duration": _quantiles(durations),
            "by_hour": _by_hour(items, tz),
            "by_camera": dict(Counter(camera for ep in items for camera in ep.cameras)) if source == SOURCE_CAMERA else {},
            "partial_radar": any(ep.partial_radar for ep in items) if source == SOURCE_RADAR else False,
        }

    matrix: dict[str, dict[str, int]] = {}
    for ep in episodes:
        verdict = labels[ep.id]["verdict"] if ep.id in labels else "unlabeled"
        matrix.setdefault(ep.agreement, {})
        matrix[ep.agreement][verdict] = matrix[ep.agreement].get(verdict, 0) + 1

    latencies: list[float] = []
    by_id = {ep.id: ep for ep in episodes}
    for ep in episodes:
        if ep.source != SOURCE_CAMERA:
            continue
        for rid in ep.paired_ids:
            radar = by_id.get(rid)
            if radar is not None:
                latencies.append(round(radar.start - ep.start, 2))

    radar_by_verdict: dict[str, dict[str, list[float]]] = {}
    for ep in episodes:
        if ep.source != SOURCE_RADAR or not ep.radar:
            continue
        verdict = labels[ep.id]["verdict"] if ep.id in labels else "unlabeled"
        bucket = radar_by_verdict.setdefault(verdict, {"distance": [], "energy": [], "moving_fraction": []})
        if ep.radar.get("median_distance_cm") is not None:
            bucket["distance"].append(ep.radar["median_distance_cm"])
        energy = max(ep.radar.get("max_moving_energy") or 0, ep.radar.get("max_motionless_energy") or 0)
        bucket["energy"].append(energy)
        bucket["moving_fraction"].append(ep.radar.get("moving_fraction") or 0.0)

    return {
        "per_source": per_source,
        "agreement_matrix": matrix,
        "latency_seconds": sorted(latencies),
        "radar_by_verdict": radar_by_verdict,
        "labeled_total": sum(1 for ep in episodes if ep.id in labels),
        "episodes_total": len(episodes),
        "groups_total": len(groups),
        "true_groups": len(true_groups),
        "note": "재현율은 상호 재현율(두 센서 중 하나라도 잡고 사람이 확인한 사례 대비)입니다. 둘 다 놓친 사람은 로그에 없어 절대 재현율은 계산할 수 없습니다.",
    }


def _quantiles(values: Sequence[float]) -> dict[str, float | None]:
    if not values:
        return {"min": None, "p50": None, "p90": None, "max": None, "mean": None}
    n = len(values)
    return {
        "min": round(values[0], 2),
        "p50": round(values[n // 2], 2),
        "p90": round(values[min(n - 1, int(n * 0.9))], 2),
        "max": round(values[-1], 2),
        "mean": round(sum(values) / n, 2),
    }


def _by_hour(items: Iterable[Episode], tz: ZoneInfo) -> list[int]:
    hours = [0] * 24
    for ep in items:
        hours[datetime.fromtimestamp(ep.start, tz).hour] += 1
    return hours


def coverage_for_sources(digests: Iterable[Mapping[str, Any]]) -> dict[str, float]:
    result = {SOURCE_CAMERA: 0.0, SOURCE_RADAR: 0.0, "app": 0.0}
    for digest in digests:
        cov = digest.get("coverage") or {}
        result[SOURCE_CAMERA] += coverage_seconds(cov.get("monitoring") or [])
        result[SOURCE_RADAR] += coverage_seconds(cov.get("radar") or [])
        result["app"] += coverage_seconds(cov.get("app") or [])
    return result


def evaluate_against_reference(
    candidates: Sequence[Episode],
    reference: Sequence[Episode],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    tolerance: float,
) -> dict[str, Any]:
    """Score a candidate episode set against labeled reference episodes by overlap.

    A candidate overlapping a reference labeled ``person`` counts as a true positive; one that
    overlaps only ``no_person`` references is a false positive; candidates touching no labeled
    reference are ignored (unknown truth). Recall = labeled-true references hit by any candidate.
    """
    labeled_refs = [ref for ref in reference if ref.id in labels and labels[ref.id]["verdict"] in ("person", "no_person")]
    true_refs = [ref for ref in labeled_refs if labels[ref.id]["verdict"] == "person"]
    tp = fp = 0
    hit_true: set[str] = set()
    for cand in candidates:
        touched_true = False
        touched_false = False
        for ref in labeled_refs:
            if ref.start - tolerance <= cand.end and cand.start - tolerance <= ref.end:
                if labels[ref.id]["verdict"] == "person":
                    touched_true = True
                    hit_true.add(ref.id)
                else:
                    touched_false = True
        if touched_true:
            tp += 1
        elif touched_false:
            fp += 1
    return {
        "candidates": len(candidates),
        "tp": tp,
        "fp": fp,
        "precision": _ratio(tp, tp + fp),
        "recall": _ratio(len(hit_true), len(true_refs)),
        "true_reference": len(true_refs),
        "labeled_reference": len(labeled_refs),
    }


def sweep_camera(
    digests: Sequence[Mapping[str, Any]],
    base: EpisodeParams,
    reference: Sequence[Episode],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    confidences: Sequence[float] = (0.2, 0.3, 0.4, 0.5, 0.6, 0.7, 0.8),
    frames: Sequence[int] = (1, 2, 3, 5),
) -> list[dict[str, Any]]:
    camera_refs = [ref for ref in reference if ref.source == SOURCE_CAMERA]
    rows: list[dict[str, Any]] = []
    for conf in confidences:
        for n in frames:
            params = EpisodeParams(**{**base.to_dict(), "min_confidence": conf, "consecutive_frames": n})
            candidates = [ep for digest in digests for ep in build_camera_episodes(digest, params)]
            score = evaluate_against_reference(candidates, camera_refs, labels, tolerance=base.pair_tolerance_seconds)
            rows.append({"min_confidence": conf, "consecutive_frames": n, **score})
    return rows


def sweep_radar(
    digests: Sequence[Mapping[str, Any]],
    base: EpisodeParams,
    reference: Sequence[Episode],
    labels: Mapping[str, Mapping[str, Any]],
    *,
    confirm_seconds: Sequence[float] = (1.0, 2.0, 3.0, 5.0, 10.0),
    moving_only: Sequence[bool] = (False, True),
    max_distance_cm: Sequence[int] = (0, 300, 400),
) -> list[dict[str, Any]]:
    radar_refs = [ref for ref in reference if ref.source == SOURCE_RADAR]
    rows: list[dict[str, Any]] = []
    for confirm in confirm_seconds:
        for moving in moving_only:
            for distance in max_distance_cm:
                params = EpisodeParams(
                    **{
                        **base.to_dict(),
                        "radar_confirm_seconds": confirm,
                        "radar_moving_only": moving,
                        "radar_max_distance_cm": distance,
                    }
                )
                candidates = [ep for digest in digests for ep in build_radar_episodes(digest, params)]
                score = evaluate_against_reference(candidates, radar_refs, labels, tolerance=base.pair_tolerance_seconds)
                rows.append(
                    {"radar_confirm_seconds": confirm, "radar_moving_only": moving, "radar_max_distance_cm": distance, **score}
                )
    return rows


__all__ = ["coverage_for_sources", "evaluate_against_reference", "summarize", "sweep_camera", "sweep_radar"]
