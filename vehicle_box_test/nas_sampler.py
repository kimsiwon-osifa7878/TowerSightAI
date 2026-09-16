"""NAS에서 '차량이 찍힌' 스냅샷·클립만 골라 내려받는 샘플러 (검증 전용).

이 랩 폴더는 주차기 앱과 분리된 실험실이다. 여기 코드는 절대 운영 경로에서 import 되지
않으며, NAS는 **읽기 전용**으로만 접근한다(목록·다운로드만, 업로드·삭제 없음).

동작:

1. ``data/analysis/sites.json``에 등록된 현장 설정으로 SFTP 접속 (analyze 대시보드와 동일한
   strict host key 정책).
2. ``raw/<host>/<day>/`` (또는 레거시 ``raw/<day>/``)의 이벤트 샤드(JSONL.gz)만 먼저 받아
   읽는다. 샤드는 작아서 전량 받아도 부담이 없다.
3. 샤드에서 두 가지를 뽑는다.
   - ``media_artifact_created`` → 미디어 파일 목록(경로·SHA-256·크기·카메라·시각·종류)
   - ``detection_batch`` → 차량 라벨(car/truck/bus/motorcycle) 검출의 카메라별 시각 목록
4. 미디어마다 "그 시각 그 카메라에 차량이 몇 번 잡혔는가"를 세어 점수를 매긴다.
   ``event_kind``가 vehicle/plate인 미디어는 무조건 후보, person/radar 미디어도 차량 검출이
   겹치면 후보에 넣는다(사람 이벤트 영상에 차가 같이 찍힌 경우를 놓치지 않기 위함).
5. 날짜·카메라를 고르게 섞어 표본을 뽑고 내려받은 뒤 SHA-256으로 검증한다.
6. 결과를 ``sample-index.json``으로 남긴다(다음 단계인 프레임 추출·기하 검증이 이 파일을 읽는다).

사용:

    .venv/bin/python -m vehicle_box_test.nas_sampler --site shinantower \\
        --max-videos 12 --max-snapshots 40
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import json
import posixpath
import stat
import sys
from collections import defaultdict
from dataclasses import asdict, dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable, Iterator, Sequence

from towersightai.analyze.config import AnalysisPaths, SiteConfig, SitesStore
from towersightai.analyze.nas_reader import paramiko_sftp


LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = LAB_ROOT / "data"

VEHICLE_LABELS = frozenset({"car", "truck", "bus", "motorcycle"})
#: 차량이 확실히 들어있는 이벤트 종류. 나머지(person/radar)는 차량 검출이 겹칠 때만 채택.
VEHICLE_EVENT_KINDS = frozenset({"vehicle", "plate"})

SNAPSHOT_MATCH_SECONDS = 6.0
#: 클립 파트 1개 길이(RAW_MEDIA_CLIP_PART_SECONDS 기본 300초)를 상한으로 겹침을 본다.
VIDEO_PART_SECONDS = 300.0
VIDEO_PRE_SECONDS = 10.0
#: vehicle_session_ended 없이 끝난 세션(앱 비정상 종료 등)을 차량 구간으로 인정하는 길이.
OPEN_SESSION_SECONDS = 180.0


# ----------------------------------------------------------------------- 자료형


@dataclass
class MediaItem:
    day: str
    camera_id: str
    kind: str  # "snapshot" | "video"
    event_kind: str  # "vehicle" | "plate" | "person" | "radar" | ...
    captured_at: str
    relative_path: str
    sha256: str
    size_bytes: int
    part: int = 1
    vehicle_hits: int = 0
    vehicle_conf_max: float = 0.0
    #: 이 미디어가 걸쳐 있는 차량 입고 세션 id (vehicle_entered ~ vehicle_session_ended)
    session_id: str = ""
    local_path: str = ""
    remote_dir: str = ""

    @property
    def captured(self) -> datetime:
        return _parse_time(self.captured_at) or datetime.fromtimestamp(0, timezone.utc)

    @property
    def guaranteed(self) -> bool:
        """차량이 들어있다고 보장되는 미디어: 차량/번호판 이벤트이거나 입고 세션 구간."""
        return self.event_kind in VEHICLE_EVENT_KINDS or bool(self.session_id)

    def score(self) -> tuple:
        """정렬 키: 입고 세션 → 차량 이벤트 → 앞 파트(진입 순간) → 차량 검출 수 → 신뢰도."""
        return (
            bool(self.session_id),
            self.event_kind in VEHICLE_EVENT_KINDS,
            -min(self.part, 99),
            self.vehicle_hits,
            self.vehicle_conf_max,
        )


@dataclass
class DayScan:
    day: str
    host: str
    remote_dir: str
    shards: int = 0
    media: list[MediaItem] = field(default_factory=list)
    vehicle_detections: int = 0
    sessions: int = 0


# ----------------------------------------------------------------------- 유틸


def _parse_time(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _human(size: float) -> str:
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.1f}{unit}"
        size /= 1024
    return f"{size:.1f}GB"


def _is_dir(entry: Any) -> bool:
    return stat.S_ISDIR(entry.st_mode or 0)


# ----------------------------------------------------------------------- NAS 접근


def open_site(site_name: str) -> SiteConfig:
    store = SitesStore(AnalysisPaths().sites_file)
    site = store.get(site_name)
    if site is None:
        names = [s.name for s in store.load()]
        raise SystemExit(f"사이트 {site_name!r}를 찾을 수 없습니다. 등록된 사이트: {names}")
    if not site.configured:
        raise SystemExit(f"사이트 {site_name!r} NAS 설정 누락: {', '.join(site.missing)}")
    return site


def list_day_dirs(sftp: Any, site: SiteConfig, host: str | None) -> list[tuple[str, str, str]]:
    """(host, day, remote_dir) 목록을 오래된 순으로 반환. 호스트별/레거시 레이아웃 모두 지원."""
    root = site.remote_raw_dir
    legacy: dict[tuple[str, str], str] = {}
    per_host: dict[tuple[str, str], str] = {}
    for entry in sftp.listdir_attr(root):
        name = entry.filename
        if not _is_dir(entry):
            continue
        if _looks_like_day(name):  # 레거시 raw/<day>
            legacy[(site.default_host or "unknown-host", name)] = posixpath.join(root, name)
            continue
        if host and name != host:
            continue
        host_dir = posixpath.join(root, name)
        for sub in sftp.listdir_attr(host_dir):
            if _is_dir(sub) and _looks_like_day(sub.filename):
                per_host[(name, sub.filename)] = posixpath.join(host_dir, sub.filename)
    merged = {**legacy, **per_host}  # 같은 (호스트, 날짜)면 호스트별 레이아웃이 최신이다
    return [(key[0], key[1], value) for key, value in sorted(merged.items(), key=lambda kv: kv[0][::-1])]


def _looks_like_day(name: str) -> bool:
    return len(name) == 10 and name[4] == "-" and name[7] == "-" and name.replace("-", "").isdigit()


def fetch_event_shards(sftp: Any, remote_dir: str, dest: Path, *, reuse: Path | None) -> int:
    """이벤트 샤드만 내려받는다(미디어는 선별 후 별도 다운로드). 이미 있으면 건너뜀."""
    dest.mkdir(parents=True, exist_ok=True)
    count = 0
    for entry in sftp.listdir_attr(remote_dir):
        name = entry.filename
        if _is_dir(entry) or not name.startswith("events-"):
            continue
        target = dest / name
        if target.is_file() and target.stat().st_size == entry.st_size:
            count += 1
            continue
        if reuse is not None:
            cached = reuse / name
            if cached.is_file() and cached.stat().st_size == entry.st_size:
                target.write_bytes(cached.read_bytes())
                count += 1
                continue
        sftp.get(posixpath.join(remote_dir, name), str(target))
        count += 1
    return count


def download_media(sftp: Any, remote_dir: str, item: MediaItem, dest_root: Path) -> Path:
    """미디어 1개를 내려받고 SHA-256을 검증한다. 이미 검증된 파일은 재다운로드하지 않는다."""
    target = dest_root / item.day / Path(item.relative_path).name
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.is_file() and target.stat().st_size == item.size_bytes and _sha256(target) == item.sha256:
        return target
    tmp = target.with_suffix(target.suffix + ".part")
    sftp.get(posixpath.join(remote_dir, item.relative_path), str(tmp))
    actual = _sha256(tmp)
    if item.sha256 and actual != item.sha256:
        tmp.unlink(missing_ok=True)
        raise RuntimeError(f"SHA-256 불일치: {item.relative_path}")
    tmp.replace(target)
    return target


# ----------------------------------------------------------------------- 이벤트 해석


def _is_gzip(path: Path) -> bool:
    """확장자를 믿지 않는다 — 현장 샤드에 `.jsonl`인데 gzip 내용인 파일이 섞여 있다."""
    try:
        with path.open("rb") as handle:
            return handle.read(2) == b"\x1f\x8b"
    except OSError:
        return False


def iter_records(day_dir: Path) -> Iterator[dict]:
    """샤드를 한 줄씩 읽는다. 잘린 gzip(현장에서 실제로 나옴)은 읽은 데까지만 쓰고 넘어간다."""
    for shard in sorted(day_dir.glob("events-*.jsonl*")):
        opener = gzip.open if _is_gzip(shard) else open
        try:
            handle = opener(shard, "rt", encoding="utf-8", errors="replace")  # type: ignore[operator]
        except OSError:
            continue
        try:
            while True:
                try:
                    line = handle.readline()
                except (OSError, EOFError):
                    break  # 잘린 샤드 — 여기까지만 쓴다
                if not line:
                    break
                line = line.strip()
                if not line:
                    continue
                try:
                    yield json.loads(line)
                except json.JSONDecodeError:
                    continue
        finally:
            handle.close()


def scan_day(day: str, host: str, remote_dir: str, day_dir: Path) -> DayScan:
    """하루치 샤드를 읽어 미디어 목록과 카메라별 차량 검출 시각을 뽑는다."""
    scan = DayScan(day=day, host=host, remote_dir=remote_dir)
    media: list[MediaItem] = []
    vehicle_times: dict[str, list[tuple[datetime, float]]] = defaultdict(list)
    session_open: dict[str, datetime] = {}
    sessions: list[tuple[str, datetime, datetime]] = []

    for record in iter_records(day_dir):
        event_type = record.get("event_type")
        payload = record.get("payload") or {}
        if event_type in {"vehicle_entered", "vehicle_session_ended"}:
            session = str(record.get("vehicle_session_id") or "")
            when = _parse_time(record.get("recorded_at"))
            if not session or when is None:
                continue
            if event_type == "vehicle_entered":
                session_open[session] = when
            else:
                start = session_open.pop(session, when)
                sessions.append((session, start, when))
            continue
        if event_type == "media_artifact_created":
            metadata = payload.get("metadata") or {}
            captured = _parse_time(payload.get("captured_at")) or _parse_time(record.get("recorded_at"))
            media.append(
                MediaItem(
                    day=day,
                    camera_id=str(payload.get("camera_id") or "unknown"),
                    kind=str(payload.get("kind") or ""),
                    event_kind=str(metadata.get("event_kind") or ""),
                    captured_at=captured.isoformat() if captured else "",
                    relative_path=str(payload.get("relative_path") or ""),
                    sha256=str(payload.get("sha256") or ""),
                    size_bytes=int(payload.get("size_bytes") or 0),
                    part=int(metadata.get("part") or 1),
                    remote_dir=remote_dir,
                )
            )
        elif event_type == "detection_batch":
            for detection in payload.get("detections") or []:
                label = str(detection.get("label") or "").lower()
                if label not in VEHICLE_LABELS:
                    continue
                when = _parse_time(detection.get("timestamp")) or _parse_time(record.get("recorded_at"))
                if when is None:
                    continue
                camera = str(detection.get("camera_id") or payload.get("camera_id") or "unknown")
                vehicle_times[camera].append((when, float(detection.get("confidence") or 0.0)))

    for times in vehicle_times.values():
        times.sort(key=lambda row: row[0])
    scan.vehicle_detections = sum(len(v) for v in vehicle_times.values())
    # 닫히지 않은 세션(앱이 죽은 경우)도 진입 순간부터 일정 시간은 차량 구간으로 본다.
    for session, start in session_open.items():
        sessions.append((session, start, start + timedelta(seconds=OPEN_SESSION_SECONDS)))
    scan.sessions = len(sessions)

    for item in media:
        if not item.relative_path or not item.captured_at:
            continue
        start, end = _match_window(item)
        for session, s_start, s_end in sessions:
            if start <= s_end and end >= s_start:  # 시간 구간이 겹치면 그 세션의 자료
                item.session_id = session
                break
        hits = 0
        best = 0.0
        for when, confidence in vehicle_times.get(item.camera_id, ()):  # 같은 카메라만
            if when < start:
                continue
            if when > end:
                break
            hits += 1
            best = max(best, confidence)
        item.vehicle_hits = hits
        item.vehicle_conf_max = round(best, 4)
        scan.media.append(item)
    return scan


def _match_window(item: MediaItem) -> tuple[datetime, datetime]:
    """미디어가 실제로 담고 있는 시간 구간.

    영상은 파트마다 `captured_at`이 같으므로(녹화 시작 시각) 파트 번호로 offset을 준다.
    part001 = 진입 순간, partN = 시작 + (N-1)×파트길이.
    """
    captured = item.captured
    if item.kind == "video":
        start = captured + timedelta(seconds=VIDEO_PART_SECONDS * (item.part - 1) - VIDEO_PRE_SECONDS)
        end = captured + timedelta(seconds=VIDEO_PART_SECONDS * item.part)
    else:
        start = captured - timedelta(seconds=SNAPSHOT_MATCH_SECONDS)
        end = captured + timedelta(seconds=SNAPSHOT_MATCH_SECONDS)
    return start, end


# ----------------------------------------------------------------------- 표본 선정


def select_sample(
    scans: Sequence[DayScan],
    *,
    max_videos: int,
    max_snapshots: int,
    max_bytes: int,
    max_part: int,
    include_unconfirmed: bool,
) -> list[MediaItem]:
    """차량이 확실한 것부터 채우고, 모자라면 차량 검출이 겹친 것으로 채운다.

    두 단계로 나누는 이유: 한 번에 섞어 라운드로빈하면 차량 자료가 없는 (날짜, 카메라)
    버킷의 사람 이벤트가 먼저 들어와 정작 차량 표본이 밀린다.
    """
    pool = [item for scan in scans for item in scan.media if item.kind != "video" or item.part <= max_part]
    sure = [item for item in pool if item.guaranteed]
    maybe = [item for item in pool if not item.guaranteed and item.vehicle_hits > 0]
    if not include_unconfirmed:
        maybe = []

    chosen: list[MediaItem] = []
    for kind, limit in (("snapshot", max_snapshots), ("video", max_videos)):
        budget = None if kind == "snapshot" else max(max_bytes - sum(i.size_bytes for i in chosen), 0)
        picked = _round_robin([i for i in sure if i.kind == kind], limit, max_bytes=budget)
        if len(picked) < limit:
            spent = sum(i.size_bytes for i in picked)
            rest = None if budget is None else max(budget - spent, 0)
            picked += _round_robin([i for i in maybe if i.kind == kind], limit - len(picked), max_bytes=rest)
        chosen += picked
    return chosen


def _round_robin(items: Sequence[MediaItem], limit: int, *, max_bytes: int | None) -> list[MediaItem]:
    """(날짜, 카메라) 버킷을 번갈아 가며 뽑아 한 날짜·한 카메라에 쏠리지 않게 한다."""
    buckets: dict[tuple[str, str], list[MediaItem]] = defaultdict(list)
    for item in items:
        buckets[(item.day, item.camera_id)].append(item)
    for bucket in buckets.values():
        bucket.sort(key=MediaItem.score, reverse=True)

    order = sorted(buckets, key=lambda key: (-max(i.score() for i in buckets[key])[1], key))
    picked: list[MediaItem] = []
    used = 0
    while len(picked) < limit and any(buckets[key] for key in order):
        for key in order:
            if len(picked) >= limit:
                break
            bucket = buckets[key]
            if not bucket:
                continue
            item = bucket.pop(0)
            if max_bytes is not None and used + item.size_bytes > max_bytes:
                continue
            picked.append(item)
            used += item.size_bytes
    return picked


# ----------------------------------------------------------------------- 실행


def run(args: argparse.Namespace) -> int:
    site = open_site(args.site)
    out_root = Path(args.out).resolve()
    events_root = out_root / "events"
    media_root = out_root / "media"
    analysis_cache = AnalysisPaths().site_root(site.name) / "cache"

    print(f"[1/4] NAS 접속: {site.nas_host}:{site.nas_port} {site.remote_raw_dir} (읽기 전용)")
    sftp = paramiko_sftp(site)
    scans: list[DayScan] = []
    try:
        days = list_day_dirs(sftp, site, args.host)
        if args.host:
            days = [row for row in days if row[0] == args.host]
        if args.since:
            days = [row for row in days if row[1] >= args.since]
        days = days[-args.days :] if args.days else days
        print(f"[2/4] 대상 날짜 {len(days)}일: {', '.join(d for _, d, _ in days) or '(없음)'}")

        for host, day, remote_dir in days:
            day_dir = events_root / host / day
            reuse = analysis_cache / host / day
            shards = fetch_event_shards(sftp, remote_dir, day_dir, reuse=reuse if reuse.is_dir() else None)
            scan = scan_day(day, host, remote_dir, day_dir)
            scan.shards = shards
            scans.append(scan)
            guaranteed = sum(1 for m in scan.media if m.guaranteed)
            overlap = sum(1 for m in scan.media if not m.guaranteed and m.vehicle_hits > 0)
            print(
                f"      {day} 샤드 {shards:3d} · 미디어 {len(scan.media):5d} · 입고세션 {scan.sessions:2d} "
                f"· 차량이벤트 {guaranteed:3d} · 차량검출겹침 {overlap:3d} "
                f"· 차량검출 {scan.vehicle_detections}"
            )

        sample = select_sample(
            scans,
            max_videos=args.max_videos,
            max_snapshots=args.max_snapshots,
            max_bytes=args.max_mb * 1024 * 1024,
            max_part=args.max_part,
            include_unconfirmed=not args.only_vehicle_events,
        )
        missing: list[str] = []
        total = sum(item.size_bytes for item in sample)
        print(f"[3/4] 표본 {len(sample)}개 선정 (합계 {_human(total)}) — 다운로드 시작")

        for index, item in enumerate(sample, start=1):
            remote_dir = item.remote_dir
            if args.dry_run:
                print(f"      [{index:3d}/{len(sample)}] (dry-run) {item.day}/{item.relative_path}")
                continue
            try:
                local = download_media(sftp, remote_dir, item, media_root)
            except (FileNotFoundError, OSError, RuntimeError) as exc:
                # 샤드에는 기록됐지만 NAS에 없는 파일이 있다(업로드 실패/부분 동기화).
                missing.append(item.relative_path)
                print(f"      [{index:3d}/{len(sample)}] 건너뜀 {item.day}/{item.relative_path}: {exc}")
                continue
            item.local_path = str(local.relative_to(out_root))
            print(
                f"      [{index:3d}/{len(sample)}] {item.day} {item.camera_id:14s} "
                f"{item.event_kind:10s} hits={item.vehicle_hits:3d} {_human(item.size_bytes):>8s} "
                f"→ {item.local_path}"
            )
    finally:
        sftp.close()

    if missing:
        print(f"      NAS에 없어 건너뛴 파일 {len(missing)}개")

    index_path = out_root / "sample-index.json"
    index_path.parent.mkdir(parents=True, exist_ok=True)
    index_path.write_text(
        json.dumps(
            {
                "site": site.name,
                "generated_at": datetime.now(timezone.utc).isoformat(),
                "days": [
                    {
                        "day": scan.day,
                        "host": scan.host,
                        "media": len(scan.media),
                        "vehicle_detections": scan.vehicle_detections,
                    }
                    for scan in scans
                ],
                "missing": missing,
                "items": [asdict(item) for item in sample if item.local_path or args.dry_run],
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    print(f"[4/4] 색인 저장: {index_path}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="NAS에서 차량이 찍힌 미디어를 표본 추출해 내려받는다.")
    parser.add_argument("--site", default="shinantower", help="data/analysis/sites.json의 사이트 이름")
    parser.add_argument("--host", default="pakrio-shinantower", help="현장 호스트 (빈 값이면 전체)")
    parser.add_argument("--days", type=int, default=0, help="최근 N일만 (0=전체)")
    parser.add_argument("--since", default="", help="YYYY-MM-DD 이후만")
    parser.add_argument("--max-videos", type=int, default=12)
    parser.add_argument("--max-snapshots", type=int, default=40)
    parser.add_argument("--max-mb", type=int, default=800, help="영상 다운로드 용량 상한(MB)")
    parser.add_argument("--max-part", type=int, default=3, help="영상은 part001~N만 (진입 순간 위주)")
    parser.add_argument(
        "--only-vehicle-events",
        action="store_true",
        help="event_kind가 vehicle/plate인 미디어만 (사람 이벤트에 차가 같이 찍힌 건 제외)",
    )
    parser.add_argument("--dry-run", action="store_true", help="선정만 하고 내려받지 않는다")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    return run(build_parser().parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
