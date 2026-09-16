"""Dashboard server routes and NAS reader through fakes — no sockets, no SFTP."""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import stat
from datetime import datetime, timezone
from pathlib import Path

import pytest

from towersightai.analyze.config import AnalysisPaths, SiteConfig, SitesStore
from towersightai.analyze.nas_reader import SYNC_MARKER, NasReader, local_days
from towersightai.analyze.server import AnalysisApp
from tests.test_analyze_core import _synthetic_day, _write_day


class _Attr:
    def __init__(self, name: str, is_dir: bool, size: int = 0) -> None:
        self.filename = name
        self.st_mode = stat.S_IFDIR if is_dir else stat.S_IFREG
        self.st_size = size


class FakeSftp:
    """Minimal SFTP double over an in-memory tree {path: bytes}."""

    def __init__(self, files: dict[str, bytes]) -> None:
        self.files = files
        self.closed = False
        self.opened: list[str] = []

    def listdir_attr(self, path: str):
        prefix = path.rstrip("/") + "/"
        names: dict[str, _Attr] = {}
        for full, data in self.files.items():
            if not full.startswith(prefix):
                continue
            rest = full[len(prefix) :]
            head, _, tail = rest.partition("/")
            if head not in names:
                names[head] = _Attr(head, bool(tail), 0 if tail else len(data))
        if not names:
            raise OSError("no such directory")
        return list(names.values())

    def open(self, path: str, mode: str = "rb"):
        if path not in self.files:
            raise OSError("no such file")
        self.opened.append(path)
        return io.BytesIO(self.files[path])

    def close(self) -> None:
        self.closed = True


def _remote_tree(records: list[dict], *, with_manifest: bool = True, per_host: bool = False) -> dict[str, bytes]:
    shard = io.BytesIO()
    with gzip.GzipFile(fileobj=shard, mode="wb") as gz:
        for record in records:
            gz.write((json.dumps(record) + "\n").encode("utf-8"))
    payload = shard.getvalue()
    image = b"\xff\xd8fakejpeg"
    base = "/home/share/raw/pakrio/2026-09-03" if per_host else "/home/share/raw/2026-09-03"
    files = {f"{base}/events-20260903-0900.jsonl.gz": payload, f"{base}/media/images/000140-000000-person-front.jpg": image}
    if with_manifest:
        manifest = {
            "schema_version": 2,
            "day": "2026-09-03",
            "source_host": "pakrio",
            "files": [
                {"relative_path": "events-20260903-0900.jsonl.gz", "size_bytes": len(payload), "sha256": hashlib.sha256(payload).hexdigest(), "media_type": "application/x-ndjson+gzip"},
                {"relative_path": "media/images/000140-000000-person-front.jpg", "size_bytes": len(image), "sha256": hashlib.sha256(image).hexdigest(), "media_type": "image/jpeg"},
            ],
        }
        files[f"{base}/manifest.json"] = json.dumps(manifest).encode("utf-8")
    return files


def _site() -> SiteConfig:
    return SiteConfig(name="s", label="테스트", nas_host="nas.example.com", nas_username="u", nas_password="p", nas_folder="/home/share", timezone_name="UTC")


@pytest.fixture
def paths(tmp_path: Path) -> AnalysisPaths:
    return AnalysisPaths(tmp_path / "analysis")


def test_nas_reader_lists_both_layouts_and_syncs_with_verification(paths: AnalysisPaths):
    files = {**_remote_tree(_synthetic_day()), **_remote_tree(_synthetic_day(), per_host=True)}
    files["/home/share/raw/pakrio/2026-09-03/manifest.json"] = json.dumps(
        {**json.loads(files["/home/share/raw/2026-09-03/manifest.json"]), "source_host": "ignored-for-per-host"}
    ).encode("utf-8")
    fakes: list[FakeSftp] = []

    def factory(site: SiteConfig) -> FakeSftp:
        fake = FakeSftp(files)
        fakes.append(fake)
        return fake

    reader = NasReader(_site(), paths, sftp_factory=factory)
    days = reader.list_days()
    assert [(d.host, d.day, d.layout) for d in days] == [("pakrio", "2026-09-03", "legacy"), ("pakrio", "2026-09-03", "per_host")]
    assert days[0].has_manifest and days[0].event_files == 1 and days[0].media_files == 1 and not days[0].cached_events
    progress = reader.sync_days(days[:1])
    assert progress.finished == ["pakrio/2026-09-03"] and not progress.errors
    day_dir = paths.cache_dir("s", "pakrio", "2026-09-03")
    assert (day_dir / "events-20260903-0900.jsonl.gz").is_file()
    assert not (day_dir / "media" / "images").exists()  # media is lazy by default
    marker = json.loads((day_dir / SYNC_MARKER).read_text())
    assert marker["events_complete"] and not marker["media_complete"] and not marker["partial"]
    assert local_days(paths, "s")[0]["events_complete"]
    assert all(fake.closed for fake in fakes)
    # Second sync skips verified files; on-demand media fetch verifies against the manifest.
    before = len(fakes[-1].opened)
    reader.sync_days(days[:1])
    assert not any(name.endswith(".jsonl.gz") for name in fakes[-1].opened[before:])
    fetched = reader.ensure_media("pakrio", "2026-09-03", "media/images/000140-000000-person-front.jpg")
    assert fetched.read_bytes().startswith(b"\xff\xd8")


def test_nas_reader_rejects_corrupt_download_and_flags_missing_manifest(paths: AnalysisPaths):
    files = _remote_tree(_synthetic_day())
    files["/home/share/raw/2026-09-03/events-20260903-0900.jsonl.gz"] = b"corrupted"
    reader = NasReader(_site(), paths, sftp_factory=lambda site: FakeSftp(files))
    progress = reader.sync_days(reader.list_days())
    assert progress.errors and "SHA-256" in progress.errors[0]
    assert not (paths.cache_dir("s", "pakrio", "2026-09-03") / "events-20260903-0900.jsonl.gz").exists()

    partial = _remote_tree(_synthetic_day(), with_manifest=False)
    reader = NasReader(_site(), paths, sftp_factory=lambda site: FakeSftp(partial))
    days = reader.list_days()
    assert days[0].partial and days[0].host == "unknown-host"
    reader.sync_days(days)
    marker = json.loads((paths.cache_dir("s", "unknown-host", "2026-09-03") / SYNC_MARKER).read_text())
    assert marker["partial"] is True


def test_nas_reader_requires_configured_site(paths: AnalysisPaths):
    with pytest.raises(ValueError):
        NasReader(SiteConfig(name="empty"), paths)


@pytest.fixture
def app(paths: AnalysisPaths) -> AnalysisApp:
    SitesStore(paths.sites_file).save([_site()])
    _write_day(paths.cache_dir("s", "pakrio", "2026-09-03"), _synthetic_day())
    tree = _remote_tree(_synthetic_day())  # built once: gzip headers carry a timestamp, so rebuilding would change the manifest
    return AnalysisApp(paths, sftp_factory=lambda site: FakeSftp(tree), clock=lambda: datetime(2026, 9, 10, tzinfo=timezone.utc))


def _get(app: AnalysisApp, path: str) -> dict:
    response = app.dispatch("GET", path)
    assert response.status == 200, response.body
    return json.loads(response.body)


def test_static_pages_and_dictionary(app: AnalysisApp):
    index = app.dispatch("GET", "/")
    assert index.status == 200 and b"TowerSightAI" in index.body and "text/html" in index.content_type
    assert app.dispatch("GET", "/static/app.js").status == 200
    assert app.dispatch("GET", "/static/../config.py").status == 404
    assert app.dispatch("GET", "/nope").status == 404
    payload = _get(app, "/api/dictionary")
    assert any(item["name"] == "ld2410_sample" for item in payload["event_types"])


def test_sites_api_hides_password_and_imports_env(app: AnalysisApp, tmp_path: Path):
    payload = _get(app, "/api/sites")
    assert payload["sites"][0]["has_password"] and "nas_password" not in payload["sites"][0]
    env = tmp_path / ".env"
    env.write_text("SYNOLOGY_NAS_HOST=nas2.example.com\nSYNOLOGY_NAS_ID=u2\nSYNOLOGY_NAS_PW=pw2\nSYNOLOGY_NAS_FOLDER=/home/two\n", encoding="utf-8")
    response = app.dispatch("POST", "/api/sites/import-env", json.dumps({"env_path": str(env), "name": "two"}).encode())
    assert response.status == 200 and json.loads(response.body)["site"]["nas_host"] == "nas2.example.com"
    assert app.dispatch("POST", "/api/sites", b"{bad json").status == 400
    assert json.loads(app.dispatch("POST", "/api/sites/delete", json.dumps({"name": "two"}).encode()).body)["removed"]


def test_overview_day_episodes_review_and_label_roundtrip(app: AnalysisApp):
    overview = _get(app, "/api/overview?site=s")
    assert overview["days"][0]["camera"] == 2 and overview["days"][0]["radar"] == 2 and overview["days"][0]["both"] == 1
    assert overview["summary"]["per_source"]["camera"]["precision"] is None
    day = _get(app, "/api/day?site=s&host=pakrio&day=2026-09-03")
    assert len(day["episodes"]) == 4 and day["radar_source"] == "ld2410_sample" and day["radar_strip"]
    listing = _get(app, "/api/episodes?site=s&agreement=both&source=camera")
    assert listing["total"] == 1
    episode = listing["episodes"][0]
    assert episode["media_images"] == 1 and episode["media_videos"] == 1 and episode["verdict"] is None
    detail = _get(app, f"/api/episode?site=s&host=pakrio&day=2026-09-03&id={episode['id']}")
    assert detail["media"][0]["url"].startswith("/media?site=s&host=pakrio&day=2026-09-03&path=media/images")
    assert detail["paired"] and detail["paired"][0]["source"] == "radar"
    assert detail["raw_windows"][0]["id"] == "pw-A" and len(detail["radar_samples"]) > 0
    label = app.dispatch(
        "POST",
        "/api/label",
        json.dumps({"site": "s", "episode_id": episode["id"], "host": "pakrio", "day": "2026-09-03", "source": "camera", "start": episode["start"], "end": episode["end"], "verdict": "person", "reviewer": "kim", "tags": ["작업자"]}).encode(),
    )
    assert label.status == 200 and json.loads(label.body)["label"]["labeled_at"].startswith("2026-09-10")
    assert app.dispatch("POST", "/api/label", json.dumps({"site": "s", "episode_id": "x", "host": "h", "day": "d", "source": "camera", "verdict": "maybe"}).encode()).status == 400
    relisted = _get(app, "/api/episodes?site=s&label=person")
    assert relisted["total"] == 1 and relisted["episodes"][0]["reviewer"] == "kim"
    overview = _get(app, "/api/overview?site=s")
    assert overview["summary"]["per_source"]["camera"]["precision"] == 1.0
    assert _get(app, "/api/labels?site=s")["latest"][episode["id"]]["verdict"] == "person"
    # Parameters flow through the query string and change the episode set.
    strict = _get(app, "/api/episodes?site=s&min_confidence=0.9")
    assert strict["total"] == 2 and all(e["source"] == "radar" for e in strict["episodes"])
    assert app.dispatch("GET", f"/api/episode?site=s&host=pakrio&day=2026-09-03&id={episode['id']}&min_confidence=0.9").status == 404
    csv_response = app.dispatch("GET", "/api/export/episodes.csv?site=s")
    assert csv_response.status == 200 and csv_response.body.count(b"\n") == 5
    records = _get(app, "/api/records?site=s&host=pakrio&day=2026-09-03&ids=pw-A-start")
    assert records["records"][0]["event_type"] == "person_window_started"
    sweep = _get(app, "/api/sweep?site=s&kind=camera")
    assert sweep["labeled"] == 1 and sweep["rows"]


def test_media_route_fetches_on_demand_and_blocks_traversal(app: AnalysisApp, paths: AnalysisPaths):
    (paths.cache_dir("s", "pakrio", "2026-09-03") / SYNC_MARKER).write_text(json.dumps({"remote_dir": "/home/share/raw/2026-09-03"}))
    response = app.dispatch("GET", "/media?site=s&host=pakrio&day=2026-09-03&path=media%2Fimages%2F000140-000000-person-front.jpg")
    assert response.status == 200 and response.file_path is not None and response.file_path.read_bytes().startswith(b"\xff\xd8")
    assert app.dispatch("GET", "/media?site=s&host=pakrio&day=2026-09-03&path=..%2F..%2Fsites.json").status == 400
    missing = app.dispatch("GET", "/media?site=s&host=pakrio&day=2026-09-03&path=media%2Fimages%2Fnope.jpg")
    assert missing.status in {404, 500}


def test_nas_days_and_sync_through_fake_sftp(app: AnalysisApp):
    remote = _get(app, "/api/nas/days?site=s")
    assert remote["days"][0]["host"] == "pakrio" and remote["days"][0]["cached_events"] is False
    started = app.dispatch("POST", "/api/sync", json.dumps({"site": "s", "days": remote["days"]}).encode())
    assert started.status == 200
    import time

    for _ in range(100):
        status = _get(app, "/api/sync/status")
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["finished"] == ["pakrio/2026-09-03"] and not status["errors"]
    assert app.dispatch("POST", "/api/sync", json.dumps({"site": "s", "days": []}).encode()).status == 400


def test_missing_site_and_unknown_site_errors(paths: AnalysisPaths):
    app = AnalysisApp(paths)
    assert app.dispatch("GET", "/api/overview").status == 404
    SitesStore(paths.sites_file).save([_site()])
    assert app.dispatch("GET", "/api/overview?site=ghost").status == 404
    assert app.dispatch("GET", "/api/day?site=s").status == 400


def test_plan_update_picks_new_changed_and_growing_days(paths: AnalysisPaths):
    files = _remote_tree(_synthetic_day())
    reader = NasReader(_site(), paths, sftp_factory=lambda site: FakeSftp(files))
    assert [f"{d.host}/{d.day}" for d in reader.plan_update()] == ["pakrio/2026-09-03"]
    reader.sync_days(reader.plan_update())
    assert reader.plan_update() == []  # unchanged manifest → nothing to do
    # A new shard appended on the NAS changes the manifest → the day is planned again.
    manifest = json.loads(files["/home/share/raw/2026-09-03/manifest.json"])
    extra = b"{}\n"
    files["/home/share/raw/2026-09-03/events-20260903-1000.jsonl"] = extra
    manifest["files"].append({"relative_path": "events-20260903-1000.jsonl", "size_bytes": len(extra), "sha256": hashlib.sha256(extra).hexdigest(), "media_type": "application/x-ndjson"})
    files["/home/share/raw/2026-09-03/manifest.json"] = json.dumps(manifest).encode("utf-8")
    planned = reader.plan_update()
    assert [d.changed for d in planned] == [True]
    reader.sync_days(planned)
    assert (paths.cache_dir("s", "pakrio", "2026-09-03") / "events-20260903-1000.jsonl").is_file()
    assert reader.plan_update() == []
    # Partial (no manifest) day: re-planned only when its shard bytes grow, and unchanged shards are not re-downloaded.
    partial = _remote_tree(_synthetic_day(), with_manifest=False)
    fakes: list[FakeSftp] = []

    def factory(site: SiteConfig) -> FakeSftp:
        fake = FakeSftp(partial)
        fakes.append(fake)
        return fake

    reader = NasReader(_site(), paths, sftp_factory=factory)
    reader.sync_days(reader.plan_update())
    assert reader.plan_update() == []
    partial["/home/share/raw/2026-09-03/events-20260903-1100.jsonl"] = b'{"event_type":"x","recorded_at":"2026-09-03T11:00:00+00:00"}\n'
    planned = reader.plan_update()
    assert len(planned) == 1 and planned[0].changed
    before = len(fakes[-1].opened)
    reader.sync_days(planned)
    downloaded = [name for name in fakes[-1].opened[before:] if name.endswith(".jsonl") or name.endswith(".gz")]
    assert downloaded == ["/home/share/raw/2026-09-03/events-20260903-1100.jsonl"]


def test_update_endpoint_syncs_delta_and_reports_last_update(app: AnalysisApp):
    import time

    started = app.dispatch("POST", "/api/sync/update", json.dumps({"site": "s"}).encode())
    assert started.status == 200
    for _ in range(200):
        status = _get(app, "/api/sync/status")
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["last_update"]["synced"] == ["pakrio/2026-09-03"] and not status["last_update"]["errors"]
    assert status["last_update"]["finished_at"].startswith("2026-09-10")
    assert status["auto_minutes"] == 0
    # Second run finds nothing new.
    app.dispatch("POST", "/api/sync/update", json.dumps({"site": "s"}).encode())
    for _ in range(200):
        status = _get(app, "/api/sync/status")
        if not status["running"]:
            break
        time.sleep(0.02)
    assert status["last_update"]["planned"] == [] and status["last_update"]["synced"] == []


def test_host_roles_scope_queries_and_cache_delete(app: AnalysisApp, paths: AnalysisPaths, monkeypatch):
    import socket

    monkeypatch.setattr(socket, "gethostname", lambda: "dev-box")
    _write_day(paths.cache_dir("s", "dev-box", "2026-09-03"), _synthetic_day())
    _write_day(paths.cache_dir("s", "unknown-host", "2026-09-03"), _synthetic_day())
    hosts = {row["name"]: row for row in _get(app, "/api/days?site=s")["hosts"]}
    assert hosts["pakrio"]["role"] == "field" and hosts["dev-box"]["role"] == "dev" and hosts["unknown-host"]["role"] == "unknown"
    assert not hosts["pakrio"]["explicit"]
    # Default scope = field hosts only; explicit selectors widen or narrow it.
    assert [d["host"] for d in _get(app, "/api/overview?site=s")["days"]] == ["pakrio"]
    assert sorted(d["host"] for d in _get(app, "/api/overview?site=s&host=all")["days"]) == ["dev-box", "pakrio", "unknown-host"]
    assert [d["host"] for d in _get(app, "/api/overview?site=s&host=dev")["days"]] == ["dev-box"]
    assert [d["host"] for d in _get(app, "/api/overview?site=s&host=unknown-host")["days"]] == ["unknown-host"]
    assert _get(app, "/api/episodes?site=s")["total"] == 4
    assert _get(app, "/api/episodes?site=s&host=all")["total"] == 12
    # Reclassify: the unknown day is really the dev box → hidden from the default view; an override persists.
    response = app.dispatch("POST", "/api/hosts/role", json.dumps({"site": "s", "host": "unknown-host", "role": "dev"}).encode())
    assert response.status == 200 and SitesStore(paths.sites_file).get("s").host_roles == {"unknown-host": "dev"}
    assert [d["host"] for d in _get(app, "/api/overview?site=s&host=dev")["days"]] == ["dev-box", "unknown-host"]
    app.dispatch("POST", "/api/hosts/role", json.dumps({"site": "s", "host": "unknown-host", "role": "auto"}).encode())
    assert SitesStore(paths.sites_file).get("s").host_roles == {}
    assert app.dispatch("POST", "/api/hosts/role", json.dumps({"site": "s", "host": "x", "role": "bogus"}).encode()).status == 400
    # Local cache deletion removes only that host's copies.
    deleted = app.dispatch("POST", "/api/cache/delete", json.dumps({"site": "s", "host": "dev-box"}).encode())
    assert deleted.status == 200 and not paths.cache_dir("s", "dev-box", "2026-09-03").exists()
    assert paths.cache_dir("s", "pakrio", "2026-09-03").is_dir()
    assert sorted(row["name"] for row in _get(app, "/api/days?site=s")["hosts"]) == ["pakrio", "unknown-host"]


def test_remote_raw_day_dir_is_keyed_by_source_host():
    from towersightai.storage.archive import remote_raw_day_dir

    assert remote_raw_day_dir("/home/share/", "2026-09-10", "pakrio-shinantower") == "/home/share/raw/pakrio-shinantower/2026-09-10"
    assert remote_raw_day_dir("/home/share", "2026-09-10", "bad host/name") == "/home/share/raw/bad-host-name/2026-09-10"
    assert remote_raw_day_dir("/home/share", "2026-09-10", "") == "/home/share/raw/unknown-host/2026-09-10"


def test_legacy_day_without_manifest_belongs_to_the_site_default_host(paths: AnalysisPaths):
    partial = _remote_tree(_synthetic_day(), with_manifest=False)
    site = SiteConfig(name="s", nas_host="nas.example.com", nas_username="u", nas_password="p", nas_folder="/home/share", default_host="pakrio-shinantower")
    reader = NasReader(site, paths, sftp_factory=lambda s: FakeSftp(partial))
    days = reader.list_days()
    assert days[0].host == "pakrio-shinantower" and days[0].partial
    assert site.host_role("pakrio-shinantower") == "field"
    # A manifest still wins over the default; a host folder wins over both.
    with_manifest = _remote_tree(_synthetic_day())
    assert NasReader(site, paths, sftp_factory=lambda s: FakeSftp(with_manifest)).list_days()[0].host == "pakrio"
    per_host = _remote_tree(_synthetic_day(), per_host=True)
    assert NasReader(site, paths, sftp_factory=lambda s: FakeSftp(per_host)).list_days()[0].host == "pakrio"
    stored = SitesStore(paths.sites_file)
    stored.save([site])
    assert stored.get("s").default_host == "pakrio-shinantower"
    assert SiteConfig(name="x", default_host="bad host/name").default_host == "bad-host-name"


def test_handler_swallows_client_disconnects():
    from towersightai.analyze.server import Response, _Handler

    class Stub(_Handler):
        def __init__(self) -> None:  # noqa: D401 - bypass socket setup
            self.path = "/api/x"
            self.sent: list = []

        def send_response(self, code, message=None):
            self.sent.append(code)

        def send_header(self, key, value):
            pass

        def end_headers(self):
            pass

    class BrokenFile:
        def write(self, data):
            raise BrokenPipeError

    stub = Stub()
    stub.wfile = BrokenFile()
    stub._serve(Response.json({"ok": True}))  # must not raise
    assert stub.sent == [200]


def test_sites_save_from_form_keeps_host_roles(app: AnalysisApp, paths: AnalysisPaths):
    app.dispatch("POST", "/api/hosts/role", json.dumps({"site": "s", "host": "some-box", "role": "dev"}).encode())
    form = {"name": "s", "nas_host": "nas.example.com", "nas_port": 22, "nas_username": "u", "nas_folder": "/home/share", "default_host": "pakrio-shinantower"}
    assert app.dispatch("POST", "/api/sites", json.dumps(form).encode()).status == 200
    saved = SitesStore(paths.sites_file).get("s")
    assert saved.host_roles == {"some-box": "dev"} and saved.default_host == "pakrio-shinantower" and saved.nas_password == "p"


def test_progress_endpoint_reports_store_work_and_warm_up(app: AnalysisApp, paths: AnalysisPaths):
    assert _get(app, "/api/progress")["stage"] == "idle"
    seen: list[tuple] = []
    original = app.report_progress

    def spy(stage, detail, done=None, total=None):
        seen.append((stage, detail, done, total))
        original(stage, detail, done, total)

    app.report_progress = spy
    app.warm_up(background=False)
    assert any(stage == "warm" and total == 1 for stage, _d, _n, total in seen)
    assert _get(app, "/api/progress")["stage"] == "idle"
    seen.clear()
    _get(app, "/api/overview?site=s")
    stages = [item[0] for item in seen]
    assert "overview" in stages and stages[-1] == "idle"
    assert any(total == 1 and done == 1 for stage, _d, done, total in seen if stage == "overview")
    assert paths.digest_path("s", "pakrio", "2026-09-03").is_file()


def test_day_owner_override_reattributes_legacy_days(app: AnalysisApp, paths: AnalysisPaths):
    # Legacy manifest-less day lands on the site's default host, then gets moved to the dev box.
    partial = _remote_tree(_synthetic_day(), with_manifest=False)
    site = SiteConfig(name="s", nas_host="nas.example.com", nas_username="u", nas_password="p", nas_folder="/home/share", default_host="pakrio")
    SitesStore(paths.sites_file).save([site])
    with app._lock:
        app._stores.clear()
    app._sftp_factory = lambda s: FakeSftp(partial)
    reader = app.reader(SitesStore(paths.sites_file).get("s"))
    reader.sync_days(reader.list_days())
    assert [d["host"] for d in _get(app, "/api/overview?site=s")["days"]] == ["pakrio"]
    moved = app.dispatch("POST", "/api/days/owner", json.dumps({"site": "s", "day": "2026-09-03", "from_host": "pakrio", "host": "dev-box"}).encode())
    assert moved.status == 200 and json.loads(moved.body)["host"] == "dev-box"
    assert SitesStore(paths.sites_file).get("s").day_owners == {"2026-09-03": "dev-box"}
    assert paths.cache_dir("s", "dev-box", "2026-09-03").is_dir() and not paths.cache_dir("s", "pakrio", "2026-09-03").exists()
    assert [d["host"] for d in _get(app, "/api/overview?site=s&host=all")["days"]] == ["dev-box"]
    app.dispatch("POST", "/api/hosts/role", json.dumps({"site": "s", "host": "dev-box", "role": "dev"}).encode())
    assert _get(app, "/api/overview?site=s")["days"] == []  # dev-box is now a dev host → hidden by default
    # The reader honours the override on the next listing/sync, so an update does not bring it back.
    reader = app.reader(SitesStore(paths.sites_file).get("s"))
    assert reader.list_days()[0].host == "dev-box" and reader.plan_update() == []
    # Clearing the override moves it back to the default host.
    cleared = app.dispatch("POST", "/api/days/owner", json.dumps({"site": "s", "day": "2026-09-03", "from_host": "dev-box", "host": ""}).encode())
    assert cleared.status == 200 and json.loads(cleared.body)["host"] == "pakrio"
    assert paths.cache_dir("s", "pakrio", "2026-09-03").is_dir()
    assert app.dispatch("POST", "/api/days/owner", json.dumps({"site": "s", "day": "2026-09-03", "from_host": "pakrio", "host": "bad host"}).encode()).status == 400


def test_plate_reaches_the_episode_list_filter_detail_and_csv(paths: AnalysisPaths):
    from tests.test_analyze_core import _plate_day

    SitesStore(paths.sites_file).save([_site()])
    _write_day(paths.cache_dir("s", "pakrio", "2026-09-03"), _plate_day())
    app = AnalysisApp(paths)
    listing = _get(app, "/api/episodes?site=s&source=camera")
    plates = {e["plate"]: e for e in listing["episodes"]}
    assert set(plates) == {"12가3456", "미인식"}
    assert plates["12가3456"]["plate_recognized"] is True and plates["12가3456"]["plate_reads"] == 2
    assert plates["12가3456"]["plate_attempts"] == 4
    assert plates["미인식"]["plate_recognized"] is False
    assert [e["plate"] for e in _get(app, "/api/episodes?site=s&plate=has")["episodes"]] == ["12가3456"]
    assert [e["plate"] for e in _get(app, "/api/episodes?site=s&plate=unrecognized")["episodes"]] == ["미인식"]
    assert _get(app, "/api/episodes?site=s&plate=none")["total"] == 0
    detail = _get(app, f"/api/episode?site=s&host=pakrio&day=2026-09-03&id={plates['12가3456']['id']}")
    assert detail["plates"][0]["plate"] == "12가3456"
    assert len(detail["plate_attempts"]) == 4
    assert any(m["kind"] == "plate_crop" for m in detail["media"])
    csv_text = app.dispatch("GET", "/api/export/episodes.csv?site=s").body.decode("utf-8")
    header, *rows = [line for line in csv_text.splitlines() if line.strip()]
    assert "plate" in header and "plate_recognized" in header
    assert any("12가3456,y," in row for row in rows) and any("미인식,n," in row for row in rows)
