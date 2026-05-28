from __future__ import annotations

import argparse
import contextlib
import json
import statistics
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


DETECTOR_MODEL = "yolo-v9-t-384-license-plate-end2end"
OCR_MODEL = "cct-xs-v2-global-model"
IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".bmp"}


@dataclass(frozen=True)
class ImageInput:
    index: int
    path: Path


def main() -> int:
    parser = argparse.ArgumentParser(description="Run FastALPR over a directory of plate test images.")
    parser.add_argument("--image-dir", default="tmp/car_number-test", help="Directory containing plate images.")
    parser.add_argument("--event-path", required=True, help="JSONL output path.")
    parser.add_argument("--log-path", required=True, help="Human-readable log path.")
    parser.add_argument("--manifest-path", required=True, help="Image manifest JSON path.")
    parser.add_argument("--append-log", action="store_true", help="Append to an existing launcher log instead of overwriting.")
    args = parser.parse_args()

    return run_fast_alpr_lpr(
        image_dir=Path(args.image_dir),
        event_path=Path(args.event_path),
        log_path=Path(args.log_path),
        manifest_path=Path(args.manifest_path),
        append_log=args.append_log,
    )


def run_fast_alpr_lpr(
    *,
    image_dir: Path,
    event_path: Path,
    log_path: Path,
    manifest_path: Path,
    append_log: bool = False,
) -> int:
    images = _discover_images(image_dir)
    event_path.parent.mkdir(parents=True, exist_ok=True)
    log_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    if event_path.exists():
        event_path.unlink()

    _write_manifest(images=images, manifest_path=manifest_path)
    log_context = contextlib.nullcontext(sys.stdout) if append_log else log_path.open("w", encoding="utf-8")
    with log_context as log_fp:
        log_fp.write("\nTowerSightAI FastALPR image LPR run\n")
        log_fp.write(f"detector-model={DETECTOR_MODEL}\n")
        log_fp.write(f"ocr-model={OCR_MODEL}\n")
        log_fp.write(f"image-dir={image_dir}\n")
        log_fp.write(f"image-count={len(images)}\n")
        log_fp.flush()

        if not images:
            log_fp.write("status=error reason=no_images\n")
            return 2

        try:
            from fast_alpr import ALPR

            init_started = time.perf_counter()
            alpr = ALPR(detector_model=DETECTOR_MODEL, ocr_model=OCR_MODEL, ocr_device="cpu")
            init_ms = (time.perf_counter() - init_started) * 1000.0
            log_fp.write(f"model-init-ms={init_ms:.2f}\n")
            log_fp.flush()
        except Exception as exc:
            _write_error(log_fp, event_path=event_path, message=f"fast-alpr initialization failed: {exc}")
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

        log_fp.write("\nTowerSightAI FastALPR image summary\n")
        for image in images:
            log_fp.write(f"image[{image.index}]={image.path}\n")
        log_fp.write(f"recognized-images={recognized_images}/{len(images)}\n")
        log_fp.write("status=recognized_all\n" if recognized_images == len(images) else "status=missing_results\n")
    return 0


def _discover_images(image_dir: Path) -> tuple[ImageInput, ...]:
    if not image_dir.exists():
        return ()
    paths = tuple(sorted(path for path in image_dir.iterdir() if path.is_file() and path.suffix.lower() in IMAGE_EXTENSIONS))
    return tuple(ImageInput(index=index, path=path) for index, path in enumerate(paths))


def _attempt_payload(*, image: ImageInput, results: list[Any], elapsed_ms: float) -> dict[str, Any]:
    detections = tuple(_result_payload(result) for result in results)
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


def _result_payload(result: Any) -> dict[str, Any]:
    detection = getattr(result, "detection", None)
    ocr = getattr(result, "ocr", None)
    confidence = _ocr_confidence(getattr(ocr, "confidence", None))
    return {
        "plate_number": _clean_plate_text(getattr(ocr, "text", "")) if ocr is not None else "",
        "confidence": confidence if confidence is not None else 0.0,
        "region": getattr(ocr, "region", None) if ocr is not None else None,
        "region_confidence": getattr(ocr, "region_confidence", None) if ocr is not None else None,
        "detection_label": getattr(detection, "label", None),
        "detection_confidence": getattr(detection, "confidence", None),
        "bbox": _bbox_payload(getattr(detection, "bounding_box", None)),
    }


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


def _write_manifest(*, images: tuple[ImageInput, ...], manifest_path: Path) -> None:
    payload = {
        "type": "fast_alpr_image_manifest",
        "created_at": datetime.now().isoformat(),
        "detector_model": DETECTOR_MODEL,
        "ocr_model": OCR_MODEL,
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
    log_fp.flush()


def _write_error(log_fp: Any, *, event_path: Path, message: str) -> None:
    log_fp.write(f"status=error reason={message}\n")
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


if __name__ == "__main__":
    raise SystemExit(main())
