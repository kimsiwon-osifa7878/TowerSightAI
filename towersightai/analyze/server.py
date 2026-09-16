"""Local-only HTTP dashboard (stdlib server, JSON API + static single-page front end).

Binds to 127.0.0.1 by default. The request handler is a thin shell around
``AnalysisApp.dispatch`` so tests exercise routes without sockets.
"""

from __future__ import annotations

import csv
import io
import json
import logging
import mimetypes
import re
import threading
import urllib.parse
from dataclasses import dataclass, field
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Mapping
from zoneinfo import ZoneInfo

from towersightai.analyze.config import AnalysisPaths, SiteConfig, SitesStore, site_from_env
from towersightai.analyze.dictionary import dictionary_payload
from towersightai.analyze.digest import coverage_seconds
from towersightai.analyze.episodes import SOURCE_CAMERA, SOURCE_RADAR, Episode, EpisodeParams
from towersightai.analyze.labels import DEFAULT_TAGS, VERDICT_LABELS, VERDICTS, LabelStore, reviewer_agreement
from towersightai.analyze.loader import find_records
from towersightai.analyze.media import content_type_for, ensure_mp4, ffmpeg_path, resolve_media
from towersightai.analyze.metrics import coverage_for_sources, summarize, sweep_camera, sweep_radar
from towersightai.analyze.nas_reader import NasReader, RemoteDay, SyncProgress
from towersightai.analyze.store import AnalysisStore, parse_day

_LOGGER = logging.getLogger("towersightai.analyze.server")
STATIC_DIR = Path(__file__).resolve().parent / "static"
_STATIC_NAME = re.compile(r"^[A-Za-z0-9_.-]+$")
MAX_EPISODES_PAGE = 500


@dataclass
class Response:
    status: int = 200
    body: bytes = b""
    content_type: str = "application/json; charset=utf-8"
    headers: dict[str, str] = field(default_factory=dict)
    file_path: Path | None = None

    @classmethod
    def json(cls, payload: Any, status: int = 200) -> "Response":
        return cls(status=status, body=json.dumps(payload, ensure_ascii=False).encode("utf-8"))

    @classmethod
    def error(cls, message: str, status: int = 400) -> "Response":
        return cls.json({"error": message}, status=status)


class AnalysisApp:
    def __init__(
        self,
        paths: AnalysisPaths,
        *,
        sftp_factory: Callable[[SiteConfig], Any] | None = None,
        ffmpeg: str | None = None,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.paths = paths
        self.sites = SitesStore(paths.sites_file)
        self._sftp_factory = sftp_factory
        self._ffmpeg = ffmpeg
        self._clock = clock
        self._stores: dict[str, AnalysisStore] = {}
        self._labels: dict[str, LabelStore] = {}
        self._sync_progress = SyncProgress()
        self._sync_stop = threading.Event()
        self._sync_thread: threading.Thread | None = None
        self._auto_thread: threading.Thread | None = None
        self._auto_stop = threading.Event()
        self._auto_minutes = 0.0
        self._last_update: dict[str, Any] = {}
        self._progress: dict[str, Any] = {"stage": "idle", "detail": "", "done": None, "total": None, "at": 0.0}
        self._lock = threading.RLock()  # store()/labels() re-enter from api_sync

    # ------------------------------------------------------------------ helpers

    def _site(self, name: str | None) -> SiteConfig:
        sites = self.sites.load()
        if not sites:
            raise LookupError("등록된 현장(site)이 없습니다. 데이터 페이지에서 NAS 주소를 등록하세요.")
        if not name:
            return sites[0]
        site = next((item for item in sites if item.name == name), None)
        if site is None:
            raise LookupError(f"unknown site: {name}")
        return site

    def store(self, site: SiteConfig) -> AnalysisStore:
        with self._lock:
            store = self._stores.get(site.name)
            if store is None or store.site != site:
                store = AnalysisStore(site, self.paths)
                store.on_progress = self.report_progress
                self._stores[site.name] = store
            return store

    def report_progress(self, stage: str, detail: str, done: int | None = None, total: int | None = None) -> None:
        import time

        self._progress = {"stage": stage, "detail": detail, "done": done, "total": total, "at": time.time()}

    def api_progress(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        """What the server is busy with right now (digest build, episode replay, warm-up, sync)."""
        progress = dict(self._progress)
        if self._sync_progress.running:
            progress = {"stage": "sync", "detail": f"NAS 동기화 {self._sync_progress.current}", "done": self._sync_progress.done, "total": self._sync_progress.total, "at": progress["at"]}
        return Response.json(progress)

    def warm_up(self, *, background: bool = True) -> None:
        """Pre-build digests for every cached day of every site so the first page is quick."""

        def run() -> None:
            for site in self.sites.load():
                try:
                    self.store(site).warm()
                except Exception:  # noqa: BLE001 - warm-up is best effort.
                    _LOGGER.exception("warm-up failed site=%s", site.name)
            self.report_progress("idle", "")

        if background:
            threading.Thread(target=run, name="analysis-warm-up", daemon=True).start()
        else:
            run()

    def labels(self, site: SiteConfig) -> LabelStore:
        with self._lock:
            store = self._labels.get(site.name)
            if store is None:
                store = LabelStore(self.paths.labels_path(site.name))
                self._labels[site.name] = store
            return store

    def reader(self, site: SiteConfig) -> NasReader:
        return NasReader(site, self.paths, sftp_factory=self._sftp_factory)

    # ------------------------------------------------------------------ dispatch

    def dispatch(self, method: str, raw_path: str, body: bytes = b"") -> Response:
        parsed = urllib.parse.urlsplit(raw_path)
        path = parsed.path
        query = {key: values[-1] for key, values in urllib.parse.parse_qs(parsed.query, keep_blank_values=True).items()}
        try:
            return self._route(method.upper(), path, query, body)
        except LookupError as exc:
            return Response.error(str(exc), 404)
        except ValueError as exc:
            return Response.error(str(exc), 400)
        except Exception as exc:  # noqa: BLE001 - surface as JSON, never crash the server thread.
            _LOGGER.exception("dashboard request failed path=%s", path)
            return Response.error(f"{type(exc).__name__}: {exc}", 500)

    def _route(self, method: str, path: str, query: dict[str, str], body: bytes) -> Response:
        if method == "GET" and path in {"/", "/index.html"}:
            return self._static("index.html")
        if method == "GET" and path.startswith("/static/"):
            return self._static(path[len("/static/") :])
        if method == "GET" and path == "/media":
            return self._media(query)
        if not path.startswith("/api/"):
            return Response.error("not found", 404)
        payload = _json_body(body) if method == "POST" else {}
        name = path[len("/api/") :]
        handler = self._API.get((method, name))
        if handler is None:
            return Response.error("not found", 404)
        return handler(self, query, payload)

    # ------------------------------------------------------------------ static / media

    def _static(self, name: str) -> Response:
        if not _STATIC_NAME.match(name):
            return Response.error("not found", 404)
        path = STATIC_DIR / name
        if not path.is_file():
            return Response.error("not found", 404)
        content_type = mimetypes.guess_type(name)[0] or "application/octet-stream"
        if content_type.startswith("text/") or content_type in {"application/javascript", "application/json"}:
            content_type += "; charset=utf-8"
        return Response(body=path.read_bytes(), content_type=content_type, headers={"Cache-Control": "no-cache"})

    def _media(self, query: dict[str, str]) -> Response:
        site = self._site(query.get("site"))
        host, day, relative = query.get("host") or "", query.get("day") or "", query.get("path") or ""
        if not (host and day and relative):
            return Response.error("host, day, path are required")
        path = resolve_media(self.paths, site.name, host, day, relative)
        if not path.is_file():
            try:
                path = self.reader(site).ensure_media(host, day, relative)
            except FileNotFoundError as exc:
                return Response.error(str(exc), 404)
        if query.get("mp4") and path.suffix.lower() == ".mkv":
            try:
                path = ensure_mp4(self.paths, site.name, host, day, path, ffmpeg=self._ffmpeg)
            except RuntimeError:
                return Response.error("ffmpeg가 없어 MP4 변환을 할 수 없습니다. 클립을 내려받아 재생하세요.", 501)
        return Response(content_type=content_type_for(path), file_path=path, headers={"Cache-Control": "private, max-age=3600"})

    # ------------------------------------------------------------------ sites

    def api_sites(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        return Response.json({"sites": [site.to_public_dict() for site in self.sites.load()], "ffmpeg": bool(self._ffmpeg or ffmpeg_path())})

    def api_sites_save(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = SiteConfig.from_mapping(payload)
        existing = self.sites.get(site.name)
        if existing is not None and "host_roles" not in payload:
            site = site.with_host_role("", None)  # no-op copy keeps dataclass semantics
            from dataclasses import replace

            site = replace(site, host_roles=existing.host_roles)
        saved = self.sites.upsert(site)
        with self._lock:
            self._stores.pop(saved.name, None)
        return Response.json({"site": saved.to_public_dict()})

    def api_sites_delete(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        name = str(payload.get("name") or "")
        removed = self.sites.remove(name)
        with self._lock:
            self._stores.pop(name, None)
        return Response.json({"removed": removed})

    def api_host_role(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(str(payload.get("site") or ""))
        host = str(payload.get("host") or "")
        role = payload.get("role")
        if not host:
            return Response.error("host is required")
        if role not in (None, "", "auto", "field", "dev"):
            return Response.error("role must be field, dev or auto")
        saved = self.sites.upsert(site.with_host_role(host, role if role in ("field", "dev") else None))
        with self._lock:
            self._stores.pop(saved.name, None)
        return Response.json({"site": saved.to_public_dict(), "hosts": self._host_rows(saved, self.store(saved))})

    def api_day_owner(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        """Re-attribute a legacy (no host folder) day to another machine; persists so re-syncs agree."""
        site = self._site(str(payload.get("site") or ""))
        day = parse_day(str(payload.get("day") or ""))
        from_host = str(payload.get("from_host") or "")
        to_host = str(payload.get("host") or "").strip()
        if not (day and from_host):
            return Response.error("day, from_host are required")
        if to_host and not re.match(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,127}$", to_host):
            return Response.error("invalid host name")
        with self._lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return Response.error("동기화 중에는 바꿀 수 없습니다", 409)
        store = self.store(site)
        saved = self.sites.upsert(site.with_day_owner(day, to_host or None))
        # Resolve where the day now belongs so the local cache follows the rule immediately.
        resolved = to_host or saved.default_host or "unknown-host"
        if resolved != from_host:
            store.move_day(day, from_host, resolved)
        with self._lock:
            self._stores.pop(saved.name, None)
        return Response.json({"site": saved.to_public_dict(), "day": day, "host": resolved})

    def api_cache_delete(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        """Delete one host's local cache (never touches the NAS; labels are kept)."""
        site = self._site(str(payload.get("site") or ""))
        host = str(payload.get("host") or "")
        if not host:
            return Response.error("host is required")
        with self._lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return Response.error("동기화 중에는 삭제할 수 없습니다", 409)
        removed = self.store(site).delete_host_cache(host)
        return Response.json({"removed": removed, "host": host})

    def api_sites_import_env(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        env_path = Path(str(payload.get("env_path") or ".env")).expanduser()
        name = str(payload.get("name") or "default")
        site = site_from_env(env_path, name=name, label=str(payload.get("label") or ""))
        saved = self.sites.upsert(site)
        return Response.json({"site": saved.to_public_dict()})

    # ------------------------------------------------------------------ days / sync

    def api_days(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        days = store.days()
        for item in days:
            digest = store.digest(item["host"], item["day"]) if query.get("detail") else None
            if digest is not None:
                cov = digest["coverage"]
                item.update(
                    {
                        "records": sum(digest["counts"].values()),
                        "radar_source": digest["radar_source"],
                        "raw_windows": len(digest["raw_windows"]),
                        "media": len(digest["media"]),
                        "monitoring_seconds": round(coverage_seconds(cov["monitoring"]), 1),
                        "radar_seconds": round(coverage_seconds(cov["radar"]), 1),
                    }
                )
        return Response.json({"site": site.name, "days": days, "hosts": self._host_rows(site, store)})

    def api_nas_days(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        try:
            days = self.reader(site).list_days()
        except Exception as exc:  # noqa: BLE001 - report NAS failures to the page.
            return Response.error(f"NAS 접속 실패: {type(exc).__name__}: {exc}", 502)
        return Response.json({"site": site.name, "days": [item.to_dict() for item in days]})

    def api_sync(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(str(payload.get("site") or ""))
        selection = payload.get("days")
        if not isinstance(selection, list) or not selection:
            return Response.error("days 목록이 비어 있습니다")
        with self._lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return Response.error("동기화가 이미 실행 중입니다", 409)
            reader = self.reader(site)
            targets = [
                RemoteDay(host=str(item["host"]), day=str(item["day"]), remote_dir=str(item["remote_dir"]), layout=str(item.get("layout") or "legacy"), has_manifest=bool(item.get("has_manifest")))
                for item in selection
                if isinstance(item, dict) and item.get("host") and item.get("day") and item.get("remote_dir")
            ]
            self._sync_progress = SyncProgress()
            self._sync_stop.clear()
            media = bool(payload.get("media"))
            store = self.store(site)

            def run() -> None:
                try:
                    reader.sync_days(targets, media=media, progress=self._sync_progress, stop=self._sync_stop)
                except Exception as exc:  # noqa: BLE001
                    self._sync_progress.errors.append(f"{type(exc).__name__}: {exc}"[:200])
                    self._sync_progress.running = False
                finally:
                    for target in targets:
                        store.invalidate(target.host, target.day)

            self._sync_thread = threading.Thread(target=run, name="analysis-nas-sync", daemon=True)
            self._sync_thread.start()
        return Response.json({"started": True, "days": len(targets)})

    def api_sync_status(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        return Response.json({**self._sync_progress.to_dict(), "last_update": self._last_update, "auto_minutes": self._auto_minutes})

    def api_sync_update(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        """Bring the local cache up to date: new days, changed manifests, growing partial days (events only)."""
        site = self._site(str(payload.get("site") or query.get("site") or ""))
        started = self.start_update(site)
        if not started:
            return Response.error("동기화가 이미 실행 중입니다", 409)
        return Response.json({"started": True})

    def start_update(self, site: SiteConfig) -> bool:
        with self._lock:
            if self._sync_thread is not None and self._sync_thread.is_alive():
                return False
            self._sync_progress = SyncProgress()
            self._sync_progress.running = True
            self._sync_progress.site = site.name
            self._sync_progress.current = "NAS 목록 확인"
            self._sync_stop.clear()
            progress = self._sync_progress
            self._sync_thread = threading.Thread(target=self._run_update, args=(site, progress), name="analysis-nas-update", daemon=True)
            self._sync_thread.start()
            return True

    def _run_update(self, site: SiteConfig, progress: SyncProgress) -> None:
        started_at = (self._clock() if self._clock else datetime.now(timezone.utc)).isoformat()
        summary: dict[str, Any] = {"site": site.name, "started_at": started_at, "synced": [], "errors": []}
        store = self.store(site)
        try:
            reader = self.reader(site)
            targets = reader.plan_update()
            summary["planned"] = [f"{t.host}/{t.day}" for t in targets]
            if targets:
                reader.sync_days(targets, media=False, progress=progress, stop=self._sync_stop)
                summary["synced"] = list(progress.finished)
                summary["errors"] = list(progress.errors)
                for target in targets:
                    store.invalidate(target.host, target.day)
        except Exception as exc:  # noqa: BLE001 - background refresh must report, never crash.
            _LOGGER.exception("NAS update failed site=%s", site.name)
            summary["errors"].append(f"{type(exc).__name__}: {exc}"[:200])
            progress.errors.append(f"{type(exc).__name__}: {exc}"[:200])
        finally:
            progress.running = False
            progress.current = ""
            summary["finished_at"] = (self._clock() if self._clock else datetime.now(timezone.utc)).isoformat()
            self._last_update = summary

    def start_auto_update(self, minutes: float) -> None:
        """Periodic background refresh of every configured site (events only)."""
        self._auto_minutes = float(minutes)
        if minutes <= 0:
            return

        def loop() -> None:
            while not self._auto_stop.is_set():
                for site in self.sites.load():
                    if self._auto_stop.is_set():
                        return
                    if not site.configured:
                        continue
                    if self.start_update(site):
                        thread = self._sync_thread
                        if thread is not None:
                            thread.join()
                if self._auto_stop.wait(max(60.0, minutes * 60.0)):
                    return

        self._auto_thread = threading.Thread(target=loop, name="analysis-auto-update", daemon=True)
        self._auto_thread.start()

    def stop_auto_update(self) -> None:
        self._auto_stop.set()

    def api_sync_cancel(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        self._sync_stop.set()
        return Response.json({"cancelling": True})

    def api_index_rebuild(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(str(payload.get("site") or ""))
        store = self.store(site)
        host, day = payload.get("host"), payload.get("day")
        rebuilt = 0
        for item in store.days():
            if host and item["host"] != host:
                continue
            if day and item["day"] != day:
                continue
            if store.digest(item["host"], item["day"], rebuild=True) is not None:
                rebuilt += 1
        return Response.json({"rebuilt": rebuilt})

    # ------------------------------------------------------------------ analysis

    def _range(self, query: dict[str, str]) -> tuple[str | None, str | None, str | None]:
        return parse_day(query.get("from")), parse_day(query.get("to")), (query.get("host") or None)

    def _host_scope(self, site: SiteConfig, store: AnalysisStore, query: dict[str, str]) -> tuple[str | None, set[str] | None]:
        """``host`` query: a host name, ``all``, ``dev``, ``unknown``, or ``field``/empty (default).
        Returns (single host, allowed set)."""
        selector = (query.get("host") or "field").strip()
        if selector == "all":
            return None, None
        if selector in {"field", "dev", "unknown"}:
            return None, {host for host in store.hosts() if site.host_role(host) == selector}
        return selector, None

    def _host_rows(self, site: SiteConfig, store: AnalysisStore) -> list[dict[str, Any]]:
        rows = []
        for host in store.hosts():
            rows.append(
                {
                    "name": host,
                    "role": site.host_role(host),
                    "explicit": host in site.host_roles,
                    "days": sum(1 for item in store.days() if item["host"] == host),
                }
            )
        return rows

    def api_overview(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        params = EpisodeParams.from_query(query)
        start, end, _host = self._range(query)
        host, hosts = self._host_scope(site, store, query)
        days = store.days_in_range(start, end, host=host, hosts=hosts)
        labels = self.labels(site).latest()
        rows = []
        all_eps: list[Episode] = []
        digests = []
        tz = ZoneInfo(site.timezone_name)
        for index, item in enumerate(days, start=1):
            self.report_progress("overview", f"{item['host']}/{item['day']} 집계", index, len(days))
            digest = store.digest(item["host"], item["day"])
            if digest is None:
                continue
            digests.append(digest)
            eps = store.episodes(item["host"], item["day"], params)
            all_eps.extend(eps)
            cov = digest["coverage"]
            rows.append(
                {
                    "day": item["day"],
                    "host": item["host"],
                    "camera": sum(1 for e in eps if e.source == SOURCE_CAMERA),
                    "radar": sum(1 for e in eps if e.source == SOURCE_RADAR),
                    "both": sum(1 for e in eps if e.source == SOURCE_CAMERA and e.agreement == "both"),
                    "camera_only": sum(1 for e in eps if e.agreement == "camera_only"),
                    "radar_only": sum(1 for e in eps if e.agreement == "radar_only"),
                    "radar_both": sum(1 for e in eps if e.source == SOURCE_RADAR and e.agreement == "both"),
                    "labeled": sum(1 for e in eps if e.id in labels),
                    "monitoring_seconds": round(coverage_seconds(cov["monitoring"]), 1),
                    "radar_seconds": round(coverage_seconds(cov["radar"]), 1),
                    "app_seconds": round(coverage_seconds(cov["app"]), 1),
                    "radar_source": digest["radar_source"],
                    "partial": bool(item.get("partial")),
                    "records": sum(digest["counts"].values()),
                    "media": len(digest["media"]),
                }
            )
        self.report_progress("overview", "지표 계산", len(days), len(days))
        summary = summarize(all_eps, labels, coverage=coverage_for_sources(digests), timezone_name=site.timezone_name)
        self.report_progress("idle", "")
        return Response.json(
            {
                "site": site.to_public_dict(),
                "params": params.to_dict(),
                "range": {"from": start, "to": end, "host": query.get("host") or "field"},
                "hosts": self._host_rows(site, store),
                "days": rows,
                "summary": summary,
                "cameras": sorted({c for d in digests for c in (d.get("person_frames") or {})}),
                "tz_offset_minutes": int(datetime.now(tz).utcoffset().total_seconds() // 60),
            }
        )

    def api_day(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        host, day = query.get("host") or "", parse_day(query.get("day"))
        if not host or not day:
            return Response.error("host, day are required")
        digest = store.digest(host, day)
        if digest is None:
            return Response.error("cached day not found", 404)
        params = EpisodeParams.from_query(query)
        eps = store.episodes(host, day, params)
        labels = self.labels(site).latest()
        return Response.json(
            {
                "site": site.name,
                "host": host,
                "day": day,
                "timezone": site.timezone_name,
                "params": params.to_dict(),
                "episodes": [_episode_light(e, labels) for e in eps],
                "raw_windows": [
                    {"id": w["id"], "start": w["start"], "end": w["end"], "cameras": list(w["cameras"]), "samples": len(w["samples"])}
                    for w in digest["raw_windows"]
                ],
                "radar_windows": [{"id": w["id"], "start": w.get("start"), "end": w.get("end"), "reason": w.get("reason")} for w in digest["radar_windows"]],
                "coverage": digest["coverage"],
                "vehicle_sessions": digest["vehicle_sessions"],
                "radar_strip": _radar_strip(digest["radar_samples"], params),
                "radar_source": digest["radar_source"],
                "counts": digest["counts"],
                "first_t": digest["first_t"],
                "last_t": digest["last_t"],
                "media_count": len(digest["media"]),
            }
        )

    def api_episodes(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        params = EpisodeParams.from_query(query)
        start, end, _host = self._range(query)
        host, hosts = self._host_scope(site, store, query)
        labels = self.labels(site).latest()
        eps = store.episodes_in_range(start, end, params, host=host, hosts=hosts)
        eps = _filter_episodes(eps, query, labels)
        total = len(eps)
        offset = max(0, int(query.get("offset") or 0))
        limit = min(MAX_EPISODES_PAGE, max(1, int(query.get("limit") or 200)))
        page = eps[offset : offset + limit]
        return Response.json(
            {
                "site": site.name,
                "total": total,
                "offset": offset,
                "limit": limit,
                "ids": [e.id for e in eps],
                "episodes": [_episode_light(e, labels) for e in page],
                "params": params.to_dict(),
            }
        )

    def api_episode(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        params = EpisodeParams.from_query(query)
        episode_id = query.get("id") or ""
        host, day = query.get("host") or None, parse_day(query.get("day"))
        ep = store.find_episode(episode_id, params, host=host, day=day)
        if ep is None:
            return Response.error("episode not found for the current parameters", 404)
        digest = store.digest(ep.host, ep.day) or {}
        label_store = self.labels(site)
        latest = label_store.latest().get(ep.id)
        paired = [_episode_light(other, label_store.latest()) for other in store.episodes(ep.host, ep.day, params) if other.id in ep.paired_ids]
        lo, hi = ep.start - 15.0, ep.end + 15.0
        radar_rows = [row for row in digest.get("radar_samples", []) if lo <= row[0] <= hi]
        raw_windows = [w for w in digest.get("raw_windows", []) if w["id"] in ep.raw_window_ids]
        radar_windows = [w for w in digest.get("radar_windows", []) if w["id"] in ep.radar_window_ids]
        vehicles = [v for v in digest.get("vehicle_sessions", []) if v["id"] in ep.vehicle_session_ids]
        media = []
        for item in ep.media:
            entry = dict(item)
            base = f"/media?site={urllib.parse.quote(site.name)}&host={urllib.parse.quote(ep.host)}&day={ep.day}&path={urllib.parse.quote(item['path'])}"
            entry["url"] = base
            if item["kind"] == "video":
                entry["mp4_url"] = base + "&mp4=1"
            media.append(entry)
        return Response.json(
            {
                "site": site.name,
                "timezone": site.timezone_name,
                "episode": ep.to_dict(),
                "paired": paired,
                "label": latest,
                "history": label_store.history(ep.id),
                "media": media,
                "radar_samples": radar_rows,
                "radar_source": digest.get("radar_source"),
                "raw_windows": raw_windows,
                "radar_windows": radar_windows,
                "vehicle_sessions": vehicles,
                "plates": ep.plates,
                "plate_attempts": ep.plate_attempts,
                "verdicts": [{"value": v, "label": VERDICT_LABELS[v]} for v in VERDICTS],
                "tags": list(DEFAULT_TAGS),
            }
        )

    def api_label(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(str(payload.get("site") or ""))
        required = ("episode_id", "host", "day", "source", "verdict")
        missing = [key for key in required if not payload.get(key)]
        if missing:
            return Response.error(f"missing: {', '.join(missing)}")
        record = self.labels(site).append(
            episode_id=str(payload["episode_id"]),
            site=site.name,
            host=str(payload["host"]),
            day=str(payload["day"]),
            source=str(payload["source"]),
            start=float(payload.get("start") or 0.0),
            end=float(payload.get("end") or 0.0),
            verdict=str(payload["verdict"]),
            reviewer=str(payload.get("reviewer") or ""),
            tags=payload.get("tags") or (),
            note=str(payload.get("note") or ""),
            labeled_at=self._clock() if self._clock else None,
        )
        return Response.json({"label": record})

    def api_labels(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.labels(site)
        return Response.json({"site": site.name, "latest": store.latest(), "agreement": reviewer_agreement(store.latest_by_reviewer())})

    def api_records(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        host, day = query.get("host") or "", parse_day(query.get("day"))
        ids = {part for part in (query.get("ids") or "").split(",") if part}
        if not (host and day and ids):
            return Response.error("host, day, ids are required")
        day_dir = self.paths.cache_dir(site.name, host, day)
        return Response.json({"records": find_records(day_dir, event_ids=ids, limit=50)})

    def api_sweep(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        params = EpisodeParams.from_query(query)
        start, end, _host = self._range(query)
        host, hosts = self._host_scope(site, store, query)
        labels = self.labels(site).latest()
        digests = store.digests_in_range(start, end, host=host, hosts=hosts)
        reference = store.episodes_in_range(start, end, params, host=host, hosts=hosts)
        kind = query.get("kind") or "camera"
        if kind == "radar":
            rows = sweep_radar(digests, params, reference, labels)
        else:
            rows = sweep_camera(digests, params, reference, labels)
        return Response.json({"kind": kind, "rows": rows, "params": params.to_dict(), "labeled": sum(1 for e in reference if e.id in labels)})

    def api_dictionary(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        return Response.json(dictionary_payload())

    def api_export_csv(self, query: dict[str, str], payload: dict[str, Any]) -> Response:
        site = self._site(query.get("site"))
        store = self.store(site)
        params = EpisodeParams.from_query(query)
        start, end, _host = self._range(query)
        host, hosts = self._host_scope(site, store, query)
        labels = self.labels(site).latest()
        eps = _filter_episodes(store.episodes_in_range(start, end, params, host=host, hosts=hosts), query, labels)
        tz = ZoneInfo(site.timezone_name)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["episode_id", "day", "host", "source", "start_local", "end_local", "duration_s", "agreement", "cameras", "max_confidence", "radar_median_cm", "radar_moving_fraction", "plate", "plate_recognized", "plate_confidence", "plate_reads", "verdict", "reviewer", "tags", "note"])
        for ep in eps:
            label = labels.get(ep.id, {})
            writer.writerow(
                [
                    ep.id,
                    ep.day,
                    ep.host,
                    ep.source,
                    datetime.fromtimestamp(ep.start, tz).isoformat(),
                    datetime.fromtimestamp(ep.end, tz).isoformat(),
                    round(ep.duration, 2),
                    ep.agreement,
                    " ".join(ep.cameras),
                    ep.max_confidence if ep.max_confidence is not None else "",
                    ep.radar.get("median_distance_cm", "") if ep.radar else "",
                    ep.radar.get("moving_fraction", "") if ep.radar else "",
                    ep.plate,
                    "" if not ep.plate else ("y" if ep.plate_recognized else "n"),
                    ep.plate_confidence if ep.plate_confidence is not None else "",
                    ep.plate_reads if ep.plate_reads is not None else "",
                    label.get("verdict", ""),
                    label.get("reviewer", ""),
                    " ".join(label.get("tags", [])),
                    label.get("note", ""),
                ]
            )
        return Response(
            body=("﻿" + buffer.getvalue()).encode("utf-8"),
            content_type="text/csv; charset=utf-8",
            headers={"Content-Disposition": f'attachment; filename="episodes-{start or "all"}-{end or "all"}.csv"'},
        )

    _API: dict[tuple[str, str], Callable[["AnalysisApp", dict[str, str], dict[str, Any]], Response]] = {
        ("GET", "sites"): api_sites,
        ("POST", "sites"): api_sites_save,
        ("POST", "sites/delete"): api_sites_delete,
        ("POST", "sites/import-env"): api_sites_import_env,
        ("POST", "hosts/role"): api_host_role,
        ("POST", "cache/delete"): api_cache_delete,
        ("POST", "days/owner"): api_day_owner,
        ("GET", "days"): api_days,
        ("GET", "nas/days"): api_nas_days,
        ("POST", "sync"): api_sync,
        ("GET", "sync/status"): api_sync_status,
        ("GET", "progress"): api_progress,
        ("POST", "sync/update"): api_sync_update,
        ("POST", "sync/cancel"): api_sync_cancel,
        ("POST", "index/rebuild"): api_index_rebuild,
        ("GET", "overview"): api_overview,
        ("GET", "day"): api_day,
        ("GET", "episodes"): api_episodes,
        ("GET", "episode"): api_episode,
        ("POST", "label"): api_label,
        ("GET", "labels"): api_labels,
        ("GET", "records"): api_records,
        ("GET", "sweep"): api_sweep,
        ("GET", "dictionary"): api_dictionary,
        ("GET", "export/episodes.csv"): api_export_csv,
    }


def _json_body(body: bytes) -> dict[str, Any]:
    if not body:
        return {}
    try:
        payload = json.loads(body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("invalid JSON body") from exc
    return payload if isinstance(payload, dict) else {}


def _episode_light(ep: Episode, labels: Mapping[str, Mapping[str, Any]]) -> dict[str, Any]:
    label = labels.get(ep.id)
    return {
        "id": ep.id,
        "host": ep.host,
        "day": ep.day,
        "source": ep.source,
        "start": ep.start,
        "end": ep.end,
        "duration": round(ep.duration, 2),
        "cameras": ep.cameras,
        "max_confidence": ep.max_confidence,
        "frame_count": ep.frame_count,
        "agreement": ep.agreement,
        "paired_ids": ep.paired_ids,
        "group_id": ep.group_id,
        "radar": ep.radar,
        "partial_radar": ep.partial_radar,
        "media_images": sum(1 for m in ep.media if m["kind"] != "video"),
        "media_videos": sum(1 for m in ep.media if m["kind"] == "video"),
        "thumbnail": next((m["path"] for m in ep.media if m["kind"] == "snapshot"), None),
        "verdict": label.get("verdict") if label else None,
        "reviewer": label.get("reviewer") if label else None,
        "tags": label.get("tags") if label else [],
        "vehicle": bool(ep.vehicle_session_ids),
        "plate": ep.plate,
        "plate_confidence": ep.plate_confidence,
        "plate_recognized": ep.plate_recognized,
        "plate_reads": ep.plate_reads,
        "plate_source": ep.plate_source,
        "plate_attempts": len(ep.plate_attempts),
        "camera_check": ep.camera_check,
    }


def _filter_episodes(eps: list[Episode], query: Mapping[str, str], labels: Mapping[str, Mapping[str, Any]]) -> list[Episode]:
    source = query.get("source") or ""
    agreement = query.get("agreement") or ""
    label = query.get("label") or ""
    camera = query.get("camera") or ""
    min_duration = float(query.get("min_duration") or 0)
    max_duration = float(query.get("max_duration") or 0)
    with_media = query.get("with_media") or ""
    plate_filter = query.get("plate") or ""
    result = []
    for ep in eps:
        if plate_filter == "has" and not (ep.plate and ep.plate_recognized):
            continue
        if plate_filter == "unrecognized" and not (ep.plate and not ep.plate_recognized):
            continue
        if plate_filter == "none" and ep.plate:
            continue
        if source and ep.source != source:
            continue
        if agreement and ep.agreement != agreement:
            continue
        if camera and camera not in ep.cameras:
            continue
        if min_duration and ep.duration < min_duration:
            continue
        if max_duration and ep.duration > max_duration:
            continue
        if with_media == "1" and not ep.media:
            continue
        verdict = labels.get(ep.id, {}).get("verdict")
        if label == "labeled" and not verdict:
            continue
        if label == "unlabeled" and verdict:
            continue
        if label in VERDICTS and verdict != label:
            continue
        result.append(ep)
    order = query.get("order") or "start"
    if order == "duration":
        result.sort(key=lambda e: -e.duration)
    elif order == "confidence":
        result.sort(key=lambda e: -(e.max_confidence or 0))
    return result


def _radar_strip(rows: list[list[Any]], params: EpisodeParams, *, bin_seconds: float = 30.0) -> list[list[Any]]:
    """Downsampled radar status per bin: [t, present_fraction, unknown_fraction, samples]."""
    from towersightai.analyze.episodes import radar_present

    bins: dict[int, list[int]] = {}
    for row in rows:
        key = int(row[0] // bin_seconds)
        bucket = bins.setdefault(key, [0, 0, 0])
        presence = radar_present(row, params)
        bucket[2] += 1
        if presence == "present":
            bucket[0] += 1
        elif presence == "unknown":
            bucket[1] += 1
    return [[key * bin_seconds, round(v[0] / v[2], 3), round(v[1] / v[2], 3), v[2]] for key, v in sorted(bins.items())]


class _Handler(BaseHTTPRequestHandler):
    app: AnalysisApp

    def log_message(self, format: str, *args: Any) -> None:  # noqa: A002 - BaseHTTPRequestHandler signature.
        _LOGGER.debug("%s - %s", self.address_string(), format % args)

    def do_GET(self) -> None:  # noqa: N802
        self._serve(self.app.dispatch("GET", self.path))

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else b""
        self._serve(self.app.dispatch("POST", self.path, body))

    def handle_error(self, request: Any, client_address: Any) -> None:  # pragma: no cover - socketserver hook
        _LOGGER.debug("request handler error from %s", client_address, exc_info=True)

    def _serve(self, response: Response) -> None:
        try:
            if response.file_path is not None:
                self._serve_file(response)
                return
            self.send_response(response.status)
            self.send_header("Content-Type", response.content_type)
            self.send_header("Content-Length", str(len(response.body)))
            for key, value in response.headers.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(response.body)
        except (BrokenPipeError, ConnectionResetError, ConnectionAbortedError):
            # The browser gave up on the response (aborted video preload, navigation away).
            # Nothing to recover; a traceback on the console would only be noise.
            _LOGGER.debug("client closed the connection early path=%s", self.path)

    def _serve_file(self, response: Response) -> None:
        path = response.file_path
        assert path is not None
        size = path.stat().st_size
        start, end = 0, size - 1
        range_header = self.headers.get("Range")
        partial = False
        if range_header and range_header.startswith("bytes="):
            spec = range_header[len("bytes=") :].split(",")[0].strip()
            first, _, last = spec.partition("-")
            try:
                if first:
                    start = int(first)
                    end = int(last) if last else size - 1
                elif last:
                    start = max(0, size - int(last))
            except ValueError:
                start, end = 0, size - 1
            end = min(end, size - 1)
            partial = start <= end and (start > 0 or end < size - 1)
            if start > end:
                self.send_response(416)
                self.send_header("Content-Range", f"bytes */{size}")
                self.end_headers()
                return
        length = end - start + 1
        self.send_response(206 if partial else 200)
        self.send_header("Content-Type", response.content_type)
        self.send_header("Accept-Ranges", "bytes")
        self.send_header("Content-Length", str(length))
        if partial:
            self.send_header("Content-Range", f"bytes {start}-{end}/{size}")
        for key, value in response.headers.items():
            self.send_header(key, value)
        self.end_headers()
        with path.open("rb") as fp:
            fp.seek(start)
            remaining = length
            while remaining > 0:
                chunk = fp.read(min(1024 * 256, remaining))
                if not chunk:
                    break
                try:
                    self.wfile.write(chunk)
                except (BrokenPipeError, ConnectionResetError):
                    return
                remaining -= len(chunk)


def make_server(app: AnalysisApp, *, host: str = "127.0.0.1", port: int = 8765) -> ThreadingHTTPServer:
    handler = type("AnalysisHandler", (_Handler,), {"app": app})
    server = ThreadingHTTPServer((host, port), handler)
    server.daemon_threads = True
    return server


__all__ = ["AnalysisApp", "Response", "make_server"]
