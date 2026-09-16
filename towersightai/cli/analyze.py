"""``towersightai-analyze`` — local, read-only analysis dashboard over the NAS raw archive."""

from __future__ import annotations

import argparse
import json
import sys
import webbrowser
from pathlib import Path

from towersightai.analyze.config import DEFAULT_ANALYSIS_ROOT, AnalysisPaths, SiteConfig, SitesStore, site_from_env


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="TowerSightAI 데이터 분석 대시보드 (개발·검증 전용, 로컬 실행).")
    parser.add_argument("--data-dir", default=str(DEFAULT_ANALYSIS_ROOT), help="분석 캐시/라벨 저장 위치 (기본 data/analysis)")
    sub = parser.add_subparsers(dest="command", required=True)

    sites = sub.add_parser("sites", help="NAS 현장(site) 등록 관리")
    sites_sub = sites.add_subparsers(dest="sites_command", required=True)
    sites_sub.add_parser("list", help="등록된 현장 목록")
    add = sites_sub.add_parser("add", help="현장 추가/수정")
    add.add_argument("name")
    add.add_argument("--label", default="")
    add.add_argument("--host", required=True)
    add.add_argument("--port", type=int, default=22)
    add.add_argument("--user", required=True)
    add.add_argument("--password", default="", help="비우면 기존 값을 유지")
    add.add_argument("--folder", required=True)
    add.add_argument("--known-hosts", default="~/.ssh/known_hosts")
    add.add_argument("--timezone", default="Asia/Seoul")
    add.add_argument("--default-host", default="", help="호스트 폴더·manifest 없이 올라온 날짜의 소유 장비(현장기 호스트명)")
    imp = sites_sub.add_parser("import-env", help="배포 .env의 SYNOLOGY_NAS_* 값으로 현장 등록")
    imp.add_argument("name")
    imp.add_argument("--env", default=".env")
    imp.add_argument("--label", default="")
    imp.add_argument("--default-host", default="", help="호스트 폴더·manifest 없이 올라온 날짜의 소유 장비(현장기 호스트명)")
    rm = sites_sub.add_parser("remove", help="현장 삭제")
    rm.add_argument("name")

    days = sub.add_parser("days", help="캐시된 날짜(기본) 또는 NAS 날짜(--remote) 목록")
    days.add_argument("--site", default=None)
    days.add_argument("--remote", action="store_true")

    sync = sub.add_parser("sync", help="NAS 날짜 폴더를 로컬 캐시로 내려받기 (검증 포함)")
    sync.add_argument("--site", default=None)
    sync.add_argument("--from", dest="start", default=None, help="YYYY-MM-DD")
    sync.add_argument("--to", dest="end", default=None, help="YYYY-MM-DD")
    sync.add_argument("--host", default=None, help="source_host 필터")
    sync.add_argument("--media", action="store_true", help="스냅샷/클립도 미리 내려받기 (기본은 볼 때 개별 다운로드)")

    index = sub.add_parser("index", help="캐시된 날짜의 다이제스트 재계산")
    index.add_argument("--site", default=None)
    index.add_argument("--from", dest="start", default=None)
    index.add_argument("--to", dest="end", default=None)

    serve = sub.add_parser("serve", help="대시보드 서버 실행 (기본 http://127.0.0.1:8765)")
    serve.add_argument("--host", default="127.0.0.1")
    serve.add_argument("--port", type=int, default=8765)
    serve.add_argument("--open", action="store_true", help="브라우저 자동 열기")
    serve.add_argument("--auto-sync-minutes", type=float, default=10.0, help="NAS 자동 최신화 주기(분, 0=끔; 이벤트만, 미디어 제외)")

    args = parser.parse_args(argv)
    paths = AnalysisPaths(Path(args.data_dir))
    store = SitesStore(paths.sites_file)

    if args.command == "sites":
        return _sites(args, store)
    if args.command == "days":
        return _days(args, paths, store)
    if args.command == "sync":
        return _sync(args, paths, store)
    if args.command == "index":
        return _index(args, paths, store)
    if args.command == "serve":
        return _serve(args, paths)
    return 2


def _resolve_site(store: SitesStore, name: str | None) -> SiteConfig:
    sites = store.load()
    if not sites:
        raise SystemExit("등록된 현장이 없습니다. `towersightai-analyze sites import-env <name> --env .env` 로 등록하세요.")
    if name is None:
        return sites[0]
    site = next((item for item in sites if item.name == name), None)
    if site is None:
        raise SystemExit(f"알 수 없는 현장: {name}")
    return site


def _sites(args: argparse.Namespace, store: SitesStore) -> int:
    if args.sites_command == "list":
        print(json.dumps([site.to_public_dict() for site in store.load()], ensure_ascii=False, indent=2))
        return 0
    if args.sites_command == "add":
        site = SiteConfig(
            name=args.name,
            label=args.label,
            nas_host=args.host,
            nas_port=args.port,
            nas_username=args.user,
            nas_password=args.password,
            nas_folder=args.folder,
            known_hosts_path=Path(args.known_hosts),
            timezone_name=args.timezone,
            default_host=args.default_host,
        )
        saved = store.upsert(site)
        print(json.dumps(saved.to_public_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.sites_command == "import-env":
        from dataclasses import replace

        site = site_from_env(Path(args.env), name=args.name, label=args.label)
        if args.default_host:
            site = replace(site, default_host=args.default_host)
        saved = store.upsert(site)
        print(json.dumps(saved.to_public_dict(), ensure_ascii=False, indent=2))
        return 0
    if args.sites_command == "remove":
        print(json.dumps({"removed": store.remove(args.name)}))
        return 0
    return 2


def _days(args: argparse.Namespace, paths: AnalysisPaths, store: SitesStore) -> int:
    site = _resolve_site(store, args.site)
    if args.remote:
        from towersightai.analyze.nas_reader import NasReader

        rows = [item.to_dict() for item in NasReader(site, paths).list_days()]
    else:
        from towersightai.analyze.nas_reader import local_days

        rows = local_days(paths, site.name)
    print(json.dumps(rows, ensure_ascii=False, indent=2))
    return 0


def _sync(args: argparse.Namespace, paths: AnalysisPaths, store: SitesStore) -> int:
    from towersightai.analyze.nas_reader import NasReader

    site = _resolve_site(store, args.site)
    reader = NasReader(site, paths)
    targets = [
        item
        for item in reader.list_days()
        if (not args.start or item.day >= args.start) and (not args.end or item.day <= args.end) and (not args.host or item.host == args.host)
    ]
    if not targets:
        print(json.dumps({"ok": False, "reason": "no matching remote days"}))
        return 1
    progress = reader.sync_days(targets, media=args.media)
    print(json.dumps(progress.to_dict(), ensure_ascii=False, indent=2))
    return 0 if not progress.errors else 1


def _index(args: argparse.Namespace, paths: AnalysisPaths, store: SitesStore) -> int:
    from towersightai.analyze.store import AnalysisStore

    site = _resolve_site(store, args.site)
    analysis = AnalysisStore(site, paths)
    rebuilt = []
    for item in analysis.days_in_range(args.start, args.end):
        digest = analysis.digest(item["host"], item["day"], rebuild=True)
        if digest is not None:
            rebuilt.append({"host": item["host"], "day": item["day"], "records": sum(digest["counts"].values()), "radar_source": digest["radar_source"]})
    print(json.dumps(rebuilt, ensure_ascii=False, indent=2))
    return 0


def _serve(args: argparse.Namespace, paths: AnalysisPaths) -> int:
    from towersightai.analyze.server import AnalysisApp, make_server

    app = AnalysisApp(paths)
    server = make_server(app, host=args.host, port=args.port)
    url = f"http://{args.host}:{server.server_address[1]}/"
    auto = f"NAS 자동 최신화 {args.auto_sync_minutes:g}분" if args.auto_sync_minutes > 0 else "NAS 자동 최신화 꺼짐"
    print(f"TowerSightAI 분석 대시보드: {url}  ({auto}, Ctrl+C 로 종료)", file=sys.stderr)
    app.warm_up()
    app.start_auto_update(args.auto_sync_minutes)
    if args.open:
        webbrowser.open(url)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        app.stop_auto_update()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
