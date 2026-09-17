from __future__ import annotations

import argparse
import contextlib
import json
import logging
import os
import statistics
import sys
import time
from collections import Counter
import traceback
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from towersightai.runtime_logging import new_run_id, path_diagnostic, write_run_status


DETECTOR_MODEL = "yolo-v9-t-384-license-plate-end2end"
OCR_MODEL = "cct-xs-v2-global-model"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}
LOGGER = logging.getLogger("towersightai.ai.fast_alpr")


@dataclass(frozen=True)
class ImageInput:
    index: int
    path: Path


class FastAlprSession:
    """A FastALPR model held for repeated recognitions (e.g. the 1 Hz plate loop).

    ``run_fast_alpr_lpr`` constructs the model per call, which is fine for the
    one-shot image task but far too slow for periodic reads; this class pays the
    ONNX init cost once. CPU-only — no Hailo device or RTSP session involved.
    """

    def __init__(self, *, detector_model: str = DETECTOR_MODEL, ocr_model: str = OCR_MODEL) -> None:
        from fast_alpr import ALPR

        init_started = time.perf_counter()
        self._alpr = ALPR(detector_model=detector_model, ocr_model=ocr_model, ocr_device="cpu")
        self.init_ms = (time.perf_counter() - init_started) * 1000.0
        LOGGER.info("fast-alpr-session-ready init-ms=%.2f", self.init_ms)

    def recognize_image(self, path: Path, *, image_index: int = 0) -> dict[str, Any]:
        """Run one recognition; errors become an ``status=error`` attempt payload.

        Each detected plate is read twice: once whole (for the audit trail) and once from a crop
        of its right-hand side, which contains the four digits and no Hangul. Only the second read
        is trusted for the number.
        """
        image = ImageInput(index=image_index, path=Path(path))
        started = time.perf_counter()
        try:
            import cv2

            frame = cv2.imread(str(image.path))
            results = self._alpr.predict(frame if frame is not None else str(image.path))
            tails = self._read_tails(frame, results)
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            return _attempt_payload(image=image, results=results, elapsed_ms=elapsed_ms, tails=tails)
        except Exception as exc:  # noqa: BLE001 - loop must survive any predict failure
            elapsed_ms = (time.perf_counter() - started) * 1000.0
            payload = _error_payload(image=image, elapsed_ms=elapsed_ms, message=str(exc))
            payload["traceback"] = traceback.format_exc()
            return payload


    def _read_tails(self, frame: Any, results: list[Any]) -> dict[int, tuple[str, str]]:
        """Majority vote over several crops of each detected plate.

        Candidates: the whole-plate text, a padded whole-plate re-read, and the right-hand part of
        the box at several cut points (each excludes the Hangul the OCR model cannot read).
        """
        tails: dict[int, tuple[str, str]] = {}
        if frame is None:
            return tails
        for index, result in enumerate(results):
            detection = getattr(result, "detection", None)
            bbox = getattr(detection, "bounding_box", None)
            ocr_result = getattr(result, "ocr", None)
            candidates: list[str] = []
            whole = extract_tail_digits(getattr(ocr_result, "text", "") if ocr_result is not None else "")
            if whole:
                candidates.append(whole)
            for pad in TAIL_BOX_PADS:
                box = _plate_box(frame, bbox, pad)
                if box is None:
                    continue
                x1, y1, x2, y2 = box
                crops = [frame[y1:y2, x1:x2]] if pad else []
                for fraction in TAIL_CROP_FRACTIONS:
                    start = x1 + int((x2 - x1) * fraction)
                    if x2 - start >= 8:
                        crops.append(frame[y1:y2, start:x2])
                for crop in crops:
                    try:
                        ocr = self._alpr.ocr.predict(crop)
                    except Exception:  # noqa: BLE001 - one bad crop must not lose the read
                        LOGGER.debug("plate tail re-read failed", exc_info=True)
                        continue
                    tail = extract_tail_digits(getattr(ocr, "text", "") if ocr is not None else "")
                    if tail:
                        candidates.append(tail)
            if not candidates:
                continue
            counts = Counter(candidates)
            tail, votes = counts.most_common(1)[0]
            tails[index] = (tail, f"vote:{votes}/{len(candidates)}")
        return tails


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FastALPR over a directory of plate test images.")
    parser.add_argument("--image-dir", default="tmp/car_number-test", help="Directory containing plate images.")
    parser.add_argument("--event-path", required=True, help="JSONL output path.")
    parser.add_argument("--log-path", required=True, help="Human-readable log path.")
    parser.add_argument("--manifest-path", required=True, help="Image manifest JSON path.")
    parser.add_argument("--detector-model", default=DETECTOR_MODEL, help="FastALPR detector model name or path.")
    parser.add_argument("--ocr-model", default=OCR_MODEL, help="FastALPR OCR model name or path.")
    parser.add_argument("--run-id", default="", help="Run identifier used to correlate launcher and result logs.")
    parser.add_argument("--append-log", action="store_true", help="Append to an existing launcher log instead of overwriting.")
    args = parser.parse_args()
    logging.basicConfig(
        level=getattr(logging, os.environ.get("TOWERSIGHTAI_LOG_LEVEL", "INFO").upper(), logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s [%(threadName)s] %(message)s",
    )

    return run_fast_alpr_lpr(
        image_dir=Path(args.image_dir),
        event_path=Path(args.event_path),
        log_path=Path(args.log_path),
        manifest_path=Path(args.manifest_path),
        detector_model=args.detector_model,
        ocr_model=args.ocr_model,
        run_id=args.run_id,
        append_log=args.append_log,
    )


def run_fast_alpr_lpr(
    *,
    image_dir: Path,
    event_path: Path,
    log_path: Path,
    manifest_path: Path,
    detector_model: str = DETECTOR_MODEL,
    ocr_model: str = OCR_MODEL,
    run_id: str = "",
    status_path: Path | None = None,
    append_log: bool = False,
) -> int:
    run_id = run_id or new_run_id("fast-alpr")
    started_at = datetime.now(timezone.utc)
    started_monotonic = time.monotonic()
    images = _discover_images(image_dir)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if event_path.exists():
        event_path.unlink()

    _write_manifest(
        images=images,
        manifest_path=manifest_path,
        detector_model=detector_model,
        ocr_model=ocr_model,
    )
    log_context = contextlib.nullcontext(sys.stdout) if append_log else log_path.open("w", encoding="utf-8")
    with log_context as log_fp:
        log_fp.write("\nTowerSightAI FastALPR image LPR run\n")
        log_fp.write(f"run-id={run_id}\n")
        log_fp.write(f"cwd={Path.cwd()}\n")
        log_fp.write(f"python={sys.executable}\n")
        log_fp.write(f"detector-model={detector_model}\n")
        log_fp.write(f"ocr-model={ocr_model}\n")
        log_fp.write(f"image-dir={image_dir}\n")
        log_fp.write(f"image-count={len(images)}\n")
        for image in images:
            log_fp.write(f"input-image={path_diagnostic(image.path)}\n")
        log_fp.flush()
        LOGGER.info(
            "fast-alpr-start run-id=%s detector=%s ocr=%s image-dir=%s image-count=%s",
            run_id,
            detector_model,
            ocr_model,
            image_dir.resolve(strict=False),
            len(images),
        )
        _write_fast_alpr_status(
            status_path,
            run_id=run_id,
            status="starting",
            started_at=started_at,
            duration_seconds=0.0,
            image_count=len(images),
            recognized_images=0,
        )

        if not images:
            log_fp.write("status=error reason=no_images\n")
            LOGGER.error("fast-alpr-error run-id=%s reason=no_images image-dir=%s", run_id, image_dir.resolve(strict=False))
            _write_fast_alpr_status(
                status_path,
                run_id=run_id,
                status="failed",
                started_at=started_at,
                duration_seconds=time.monotonic() - started_monotonic,
                image_count=0,
                recognized_images=0,
                error="no_images",
            )
            return 2

        try:
            from fast_alpr import ALPR

            init_started = time.perf_counter()
            alpr = ALPR(detector_model=detector_model, ocr_model=ocr_model, ocr_device="cpu")
            init_ms = (time.perf_counter() - init_started) * 1000.0
            log_fp.write(f"model-init-ms={init_ms:.2f}\n")
            log_fp.write("model-init-status=ready\n")
            log_fp.flush()
            LOGGER.info("fast-alpr-model-ready run-id=%s init-ms=%.2f", run_id, init_ms)
        except Exception as exc:
            trace = traceback.format_exc()
            _write_error(
                log_fp,
                event_path=event_path,
                message=f"fast-alpr initialization failed: {exc}",
                traceback_text=trace,
            )
            LOGGER.exception("fast-alpr-init-failed run-id=%s", run_id)
            _write_fast_alpr_status(
                status_path,
                run_id=run_id,
                status="failed",
                started_at=started_at,
                duration_seconds=time.monotonic() - started_monotonic,
                image_count=len(images),
                recognized_images=0,
                error=f"fast-alpr initialization failed: {exc}",
            )
            return 1

        recognized_images = 0
        with event_path.open("a", encoding="utf-8") as event_fp:
            for image in images:
                started = time.perf_counter()
                try:
                    results = alpr.predict(str(image.path))
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    payload = _attempt_payload(image=image, results=results, elapsed_ms=elapsed_ms)
                except Exception as exc:
                    elapsed_ms = (time.perf_counter() - started) * 1000.0
                    payload = _error_payload(image=image, elapsed_ms=elapsed_ms, message=str(exc))
                    payload["traceback"] = traceback.format_exc()
                    LOGGER.exception(
                        "fast-alpr-predict-failed run-id=%s image-index=%s image=%s",
                        run_id,
                        image.index,
                        image.path.resolve(strict=False),
                    )

                if payload["status"] == "recognized":
                    recognized_images += 1
                    best_plate = payload["best_plate"]
                    event_fp.write(
                        json.dumps(
                            {
                                "type": "plate_ocr",
                                "plate_number": best_plate["plate_number"],
                                "confidence": best_plate["confidence"],
                                "timestamp": payload["timestamp"],
                                "source": "fast_alpr_image",
                            },
                            ensure_ascii=False,
                            sort_keys=True,
                        )
                        + "\n"
                    )
                event_fp.write(json.dumps(payload, ensure_ascii=False, sort_keys=True) + "\n")
                event_fp.flush()
                _write_attempt_log(log_fp, payload)
                LOGGER.info(
                    "fast-alpr-result run-id=%s image-index=%s status=%s elapsed-ms=%.2f detections=%s result=%s",
                    run_id,
                    payload["image_index"],
                    payload["status"],
                    payload["elapsed_ms"],
                    len(payload.get("detections") or ()),
                    (payload.get("best_plate") or {}).get("plate_number") or "no_result",
                )

        log_fp.write("\nTowerSightAI FastALPR image summary\n")
        for image in images:
            log_fp.write(f"image[{image.index}]={image.path}\n")
        log_fp.write(f"recognized-images={recognized_images}/{len(images)}\n")
        log_fp.write("status=recognized_all\n" if recognized_images == len(images) else "status=missing_results\n")
        duration_seconds = time.monotonic() - started_monotonic
        log_fp.write(f"duration-seconds={duration_seconds:.3f}\n")
        LOGGER.info(
            "fast-alpr-end run-id=%s status=%s duration-seconds=%.3f recognized-images=%s/%s",
            run_id,
            "recognized_all" if recognized_images == len(images) else "missing_results",
            duration_seconds,
            recognized_images,
            len(images),
        )
        _write_fast_alpr_status(
            status_path,
            run_id=run_id,
            status="completed" if recognized_images == len(images) else "no_result",
            started_at=started_at,
            duration_seconds=duration_seconds,
            image_count=len(images),
            recognized_images=recognized_images,
        )
    return 0


def _discover_images(image_dir: Path) -> tuple[ImageInput, ...]:
    if not image_dir.exists():
        return ()
    paths = tuple(sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS))
    return tuple(ImageInput(index=index, path=path) for index, path in enumerate(paths))


def _attempt_payload(*, image: ImageInput, results: list[Any], elapsed_ms: float, tails: dict[int, tuple[str, str]] | None = None) -> dict[str, Any]:
    tails = tails or {}
    detections = tuple(
        _result_payload(result, tail=tails.get(index, ("", ""))[0], tail_source=tails.get(index, ("", ""))[1])
        for index, result in enumerate(results)
    )
    recognized = tuple(item for item in detections if item.get("plate_number"))
    best_plate = max(recognized, key=lambda item: item.get("confidence") or 0.0, default=None)
    return {
        "type": "plate_ocr_attempt",
        "source": "fast_alpr_image",
        "source_image": str(image.path),
        "image_index": image.index,
        "status": "recognized" if best_plate else "no_result",
        "elapsed_ms": elapsed_ms,
        "detections": detections,
        "best_plate": best_plate,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _error_payload(*, image: ImageInput, elapsed_ms: float, message: str) -> dict[str, Any]:
    return {
        "type": "plate_ocr_attempt",
        "source": "fast_alpr_image",
        "source_image": str(image.path),
        "image_index": image.index,
        "status": "error",
        "elapsed_ms": elapsed_ms,
        "detections": (),
        "best_plate": None,
        "error": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def _result_payload(result: Any, *, tail: str = "", tail_source: str = "") -> dict[str, Any]:
    detection = getattr(result, "detection", None)
    ocr = getattr(result, "ocr", None)
    confidence = _ocr_confidence(getattr(ocr, "confidence", None))
    text = _clean_plate_text(getattr(ocr, "text", "")) if ocr is not None else ""
    if not tail:
        tail, tail_source = extract_tail_digits(text), "full_text" if extract_tail_digits(text) else ""
    return {
        "tail_digits": tail,
        "tail_source": tail_source,
        "plate_number": text,
        "confidence": confidence if confidence is not None else 0.0,
        "region": getattr(ocr, "region", None) if ocr is not None else None,
        "region_confidence": getattr(ocr, "region_confidence", None) if ocr is not None else None,
        "detection_label": getattr(detection, "label", None),
        "detection_confidence": getattr(detection, "confidence", None),
        "bbox": _bbox_payload(getattr(detection, "bounding_box", None)),
    }


# Korean plates read as "<2-3 digits><Hangul><4 digits>". The global OCR model has no Hangul in
# its alphabet, so it substitutes Latin look-alikes and sometimes drops or duplicates a character,
# which shifts the trailing digits too (field 2026-09-17: 213가9135 → "2137I913"). Only the last
# four digits are trusted, and they are read from a crop that excludes the Hangul entirely.
TAIL_DIGITS = 4
# The trailing digits are read many times from slightly different crops and the majority wins.
# One crop is not enough: on 2026-09-17 field frames a single right-crop fixed 213가9135 but broke
# 185다5007, while the majority of these candidates was correct on all eight plates. Measured cost
# is ~105 ms on top of the ~111 ms detect+read, well inside the 1 Hz loop.
TAIL_CROP_FRACTIONS = (0.30, 0.35, 0.40, 0.45, 0.50)
# A padded re-read recovers a digit the detector box clipped (field: 1757 → "757").
TAIL_BOX_PADS = (0.0, 0.10)


def extract_tail_digits(text: Any) -> str:
    """Trailing 4-digit group of an OCR string, or '' when it does not end in 4 digits."""
    digits = "".join(character for character in str(text or "") if character.isdigit())
    return digits[-TAIL_DIGITS:] if len(digits) >= TAIL_DIGITS else ""


def _plate_box(image: Any, bbox: Any, pad: float) -> tuple[int, int, int, int] | None:
    if image is None or bbox is None:
        return None
    height, width = image.shape[:2]
    raw_x1, raw_y1 = int(getattr(bbox, "x1", 0)), int(getattr(bbox, "y1", 0))
    raw_x2, raw_y2 = int(getattr(bbox, "x2", 0)), int(getattr(bbox, "y2", 0))
    pad_x = int((raw_x2 - raw_x1) * pad)
    pad_y = int((raw_y2 - raw_y1) * pad * 0.5)
    x1, y1 = max(raw_x1 - pad_x, 0), max(raw_y1 - pad_y, 0)
    x2, y2 = min(raw_x2 + pad_x, width), min(raw_y2 + pad_y, height)
    if x2 - x1 < 8 or y2 - y1 < 4:
        return None
    return x1, y1, x2, y2


def _clean_plate_text(text: Any) -> str:
    return str(text or "").strip().replace(" ", "")


def _ocr_confidence(value: Any) -> float | None:
    if isinstance(value, list):
        values = [float(item) for item in value if item is not None]
        return statistics.mean(values) if values else None
    if value is None:
        return None
    return float(value)


def _bbox_payload(bbox: Any) -> dict[str, int] | None:
    if bbox is None:
        return None
    return {
        "x1": int(getattr(bbox, "x1")),
        "y1": int(getattr(bbox, "y1")),
        "x2": int(getattr(bbox, "x2")),
        "y2": int(getattr(bbox, "y2")),
    }


def _write_manifest(
    *,
    images: tuple[ImageInput, ...],
    manifest_path: Path,
    detector_model: str,
    ocr_model: str,
) -> None:
    payload = {
        "type": "fast_alpr_image_manifest",
        "created_at": datetime.now().isoformat(),
        "detector_model": detector_model,
        "ocr_model": ocr_model,
        "images": tuple({"image_index": image.index, "source_image": str(image.path)} for image in images),
    }
    manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n", encoding="utf-8")


def _write_attempt_log(log_fp: Any, payload: dict[str, Any]) -> None:
    best_plate = payload.get("best_plate") or {}
    result = best_plate.get("plate_number") or "no_result"
    confidence = best_plate.get("confidence")
    confidence_text = f"{confidence:.4f}" if isinstance(confidence, float) else "-"
    log_fp.write(
        " ".join(
            (
                f"image[{payload['image_index']}]={payload['source_image']}",
                f"elapsed-ms={payload['elapsed_ms']:.2f}",
                f"status={payload['status']}",
                f"result={result}",
                f"confidence={confidence_text}",
                f"detections={len(payload.get('detections') or ())}",
            )
        )
        + "\n"
    )
    if payload.get("error"):
        log_fp.write(f"error={payload['error']}\n")
    if payload.get("traceback"):
        log_fp.write("traceback:\n")
        log_fp.write(str(payload["traceback"]))
        if not str(payload["traceback"]).endswith("\n"):
            log_fp.write("\n")
    log_fp.flush()


def _write_error(
    log_fp: Any,
    *,
    event_path: Path,
    message: str,
    traceback_text: str | None = None,
) -> None:
    log_fp.write(f"status=error reason={message}\n")
    if traceback_text:
        log_fp.write("traceback:\n")
        log_fp.write(traceback_text)
        if not traceback_text.endswith("\n"):
            log_fp.write("\n")
    log_fp.flush()
    with event_path.open("a", encoding="utf-8") as event_fp:
        event_fp.write(
            json.dumps(
                {
                    "type": "plate_ocr_run_error",
                    "source": "fast_alpr_image",
                    "error": message,
                    "timestamp": datetime.now(timezone.utc).isoformat(),
                },
                ensure_ascii=False,
                sort_keys=True,
            )
            + "\n"
        )


def _write_fast_alpr_status(
    status_path: Path | None,
    *,
    run_id: str,
    status: str,
    started_at: datetime,
    duration_seconds: float,
    image_count: int,
    recognized_images: int,
    error: str | None = None,
) -> None:
    if status_path is None:
        return
    write_run_status(
        status_path,
        run_id=run_id,
        task_id="front_camera_lpr",
        status=status,
        started_at=started_at.isoformat(),
        returncode=0 if status in {"completed", "no_result"} else None,
        duration_seconds=round(duration_seconds, 3),
        image_count=image_count,
        recognized_images=recognized_images,
        error=error,
    )


if __name__ == "__main__":
    raise SystemExit(main())
