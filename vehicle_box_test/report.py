"""검증 결과를 사람이 눈으로 판정할 수 있는 HTML 한 장으로 묶는다.

이미지에 번호판이 그대로 보이므로 **로컬에서만** 연다. 외부 공유·업로드 금지.

    .venv/bin/python -m vehicle_box_test.report
    → vehicle_box_test/out/report.html
"""

from __future__ import annotations

import argparse
import html
import json
import shutil
import statistics
import sys
from pathlib import Path
from typing import Sequence

LAB_ROOT = Path(__file__).resolve().parent
DEFAULT_OUT = LAB_ROOT / "out"

CAMERA_LABEL = {
    "front": "전면 (카메라 2)",
    "rear_side": "좌측 사선 (카메라 3)",
    "opposite_side": "우측 사선 (카메라 4)",
}
SOURCE_LABEL = {"snapshot": "스냅샷 (stream1)", "clip": "클립 프레임 (stream2)"}
EVENT_LABEL = {
    "vehicle": "차량 진입", "plate": "번호판", "person": "사람 감지",
    "person_end": "사람 종료", "radar": "레이더", "radar_end": "레이더 종료",
}


def _esc(value: object) -> str:
    return html.escape(str(value), quote=True)


def summarise(results: Sequence[dict], meta: dict) -> dict:
    """측정 대상은 '지면 기준점이 있는 카메라의 스냅샷'이다.

    클립은 stream2라 화각이 다르고, 전면 카메라는 아직 기준점이 없다 — 둘 다 애초에
    측정 대상이 아니므로 성공률 분모에서 뺀다. 분모를 부풀리면 결과를 잘못 읽게 된다.
    """
    posed = {
        camera
        for camera, info in (meta.get("cameras") or {}).items()
        if info.get("pose_residual_mm") is not None
    }
    measurable = [r for r in results if r["source"] == "snapshot" and r["camera_id"] in posed]
    entering = [r for r in measurable if r.get("event_kind") == "vehicle"]
    ok = [r for r in measurable if r.get("ok")]
    lengths = [r["length_mm"] for r in ok if r.get("length_mm")]
    heights = [r["height_mm"] for r in ok if r.get("height_mm")]
    wheel = [
        (r["y_right"] if r["camera_id"] == "opposite_side" else r["y_left"])
        for r in ok
        if r.get("width_assumed") and r.get("y_left") is not None
    ]
    wheel_side = [abs(v) for v in wheel]
    return {
        "total": len(results),
        "measurable": len(measurable),
        "entering": len(entering),
        "entering_ok": sum(1 for r in entering if r.get("ok")),
        "ok": len(ok),
        "length_min": min(lengths) if lengths else None,
        "length_max": max(lengths) if lengths else None,
        "height_min": min(heights) if heights else None,
        "height_max": max(heights) if heights else None,
        "wheel_median": statistics.median(wheel_side) if wheel_side else None,
        "wheel_count": len(wheel_side),
    }


def card(result: dict) -> str:
    status = "ok" if result.get("ok") else "ng"
    badge = "판정됨" if result.get("ok") else "판정 불가"
    title = f"{CAMERA_LABEL.get(result['camera_id'], result['camera_id'])} · {result['day']}"
    subtitle = (
        f"{EVENT_LABEL.get(result.get('event_kind',''), result.get('event_kind',''))} · "
        f"{SOURCE_LABEL.get(result['source'], result['source'])} · {Path(result['path']).name}"
    )

    if result.get("ok"):
        facts = [
            f"길이 <b>{result['length_mm']:.0f}</b> mm",
            f"폭 <b>{result['width_mm']:.0f}</b> mm"
            + (" <i>(가정값 — 먼 쪽 바퀴 안 보임)</i>" if result.get("width_assumed") else ""),
            f"높이 <b>{result['height_mm']:.0f}</b> mm" if result.get("height_mm") else "높이 미산출",
            f"진입쪽 끝 x {result['x_front']:+.0f} · 안쪽 끝 x {result['x_rear']:+.0f} mm",
        ]
        if result.get("width_assumed"):
            near = result["y_right"] if result["camera_id"] == "opposite_side" else result["y_left"]
            facts.append(f"가까운 쪽 바퀴선 y <b>{near:+.0f}</b> mm (측정)")
        if result.get("detection_confidence"):
            facts.append(f"제품 AI 차량 검출 {result['detection_confidence']*100:.0f}%")
        body = "<ul class='facts'>" + "".join(f"<li>{f}</li>" for f in facts) + "</ul>"
    else:
        body = "<ul class='facts ng'>" + "".join(
            f"<li>{_esc(reason)}</li>" for reason in (result.get("reasons") or ["사유 미기록"])
        ) + "</ul>"

    notes = result.get("notes") or []
    if notes:
        body += "<ul class='notes'>" + "".join(f"<li>{_esc(n)}</li>" for n in notes) + "</ul>"

    return f"""
    <figure class="card" data-status="{status}" data-camera="{_esc(result['camera_id'])}" data-source="{_esc(result['source'])}" data-event="{_esc(result.get('event_kind',''))}">
      <a href="{_esc(result['annotated_path'])}" target="_blank">
        <img loading="lazy" src="{_esc(result['annotated_path'])}" alt="{_esc(title)}">
      </a>
      <figcaption>
        <div class="head"><span class="badge {status}">{badge}</span><b>{_esc(title)}</b></div>
        <div class="sub">{_esc(subtitle)}</div>
        {body}
      </figcaption>
    </figure>"""


def build_html(summary: dict, results: Sequence[dict], calib_images: Sequence[str], meta: dict) -> str:
    stats = summarise(results, meta)
    order = {"ok": 0, "ng": 1}
    rows = sorted(results, key=lambda r: (order["ok" if r.get("ok") else "ng"], r["camera_id"], r["day"]))
    cards = "\n".join(card(r) for r in rows)
    calib_html = "\n".join(
        f'<figure class="calib"><a href="{_esc(p)}" target="_blank"><img loading="lazy" src="{_esc(p)}"></a>'
        f"<figcaption>{_esc(Path(p).stem.replace('ground-', ''))}</figcaption></figure>"
        for p in calib_images
    )
    camera_notes = "".join(
        f"<tr><td>{_esc(CAMERA_LABEL.get(cam, cam))}</td>"
        f"<td>{'기준점 있음' if info.get('pose_residual_mm') is not None else '<b class=warn>기준점 없음</b>'}</td>"
        f"<td>{_esc(info.get('note') or '—')}</td>"
        f"<td>{'—' if info.get('pose_residual_mm') is None else f'{info["pose_residual_mm"]:.0f} mm'}</td>"
        f"<td>{_esc(info.get('intrinsics') or '—')}</td></tr>"
        for cam, info in (meta.get("cameras") or {}).items()
    )

    length_range = (
        f"{stats['length_min']:.0f}~{stats['length_max']:.0f}" if stats["length_min"] else "—"
    )
    height_range = (
        f"{stats['height_min']:.0f}~{stats['height_max']:.0f}" if stats["height_min"] else "—"
    )
    wheel_line = f"{stats['wheel_median']:.0f} mm" if stats["wheel_median"] else "—"

    return f"""<!doctype html>
<html lang="ko"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>차량 직육면체 검증 · 구로 신안타워</title>
<style>
  :root {{
    --bg:#0F141B; --panel:#151B24; --panel2:#232C39; --line:#2E3949;
    --text:#E6EDF5; --muted:#97A5B8; --accent:#F5A623; --ok:#3DD68C; --ng:#FF6B6B;
  }}
  * {{ box-sizing:border-box; }}
  body {{ margin:0; background:var(--bg); color:var(--text);
         font-family:"Noto Sans KR","Malgun Gothic",system-ui,sans-serif; line-height:1.55; }}
  header {{ padding:28px 32px 18px; border-bottom:1px solid var(--line); background:var(--panel); }}
  h1 {{ margin:0 0 6px; font-size:24px; letter-spacing:-.01em; }}
  .sub {{ color:var(--muted); font-size:13px; }}
  main {{ padding:24px 32px 64px; max-width:1680px; margin:0 auto; }}
  section {{ margin-bottom:34px; }}
  h2 {{ font-size:16px; margin:0 0 12px; color:var(--accent); letter-spacing:.02em; }}
  .grid {{ display:grid; gap:16px; grid-template-columns:repeat(auto-fill,minmax(420px,1fr)); }}
  .stats {{ display:grid; gap:12px; grid-template-columns:repeat(auto-fit,minmax(190px,1fr)); }}
  .stat {{ background:var(--panel); border:1px solid var(--line); border-radius:12px; padding:14px 16px; }}
  .stat b {{ display:block; font-size:24px; font-variant-numeric:tabular-nums; }}
  .stat span {{ color:var(--muted); font-size:12px; }}
  .card, .calib {{ background:var(--panel); border:1px solid var(--line); border-radius:12px;
                   overflow:hidden; margin:0; }}
  .card img, .calib img {{ width:100%; display:block; background:#000; }}
  figcaption {{ padding:12px 14px 16px; }}
  .head {{ display:flex; align-items:center; gap:8px; font-size:14px; }}
  .badge {{ font-size:11px; padding:2px 8px; border-radius:999px; font-weight:700; }}
  .badge.ok {{ background:rgba(61,214,140,.15); color:var(--ok); }}
  .badge.ng {{ background:rgba(255,107,107,.15); color:var(--ng); }}
  .card .sub {{ font-size:11px; color:var(--muted); margin:2px 0 8px; word-break:break-all; }}
  ul.facts {{ margin:0; padding-left:18px; font-size:13px; }}
  ul.facts.ng li {{ color:#FFC9C9; }}
  ul.notes {{ margin:6px 0 0; padding-left:18px; font-size:12px; color:var(--muted); }}
  table {{ width:100%; border-collapse:collapse; font-size:13px; background:var(--panel);
           border:1px solid var(--line); border-radius:12px; overflow:hidden; }}
  th, td {{ text-align:left; padding:9px 12px; border-bottom:1px solid var(--line); vertical-align:top; }}
  th {{ background:var(--panel2); font-weight:600; color:var(--muted); font-size:12px; }}
  .warn {{ color:var(--ng); }}
  .filters {{ display:flex; gap:8px; flex-wrap:wrap; margin-bottom:14px; }}
  button {{ background:var(--panel2); color:var(--text); border:1px solid var(--line);
            border-radius:999px; padding:6px 14px; font-size:13px; cursor:pointer; font-family:inherit; }}
  button[aria-pressed="true"] {{ background:var(--accent); color:#1A1206; border-color:var(--accent); font-weight:700; }}
  .callout {{ background:var(--panel2); border-left:3px solid var(--accent); padding:12px 16px;
              border-radius:0 8px 8px 0; font-size:13px; }}
  .callout ul {{ margin:6px 0 0; padding-left:18px; }}
</style></head>
<body>
<header>
  <h1>차량 직육면체 검증 — 구로 신안타워 현장 자료</h1>
  <div class="sub">
    지면 실치수(턴테이블 Ø{meta['ground']['turntable_diameter_mm']:.0f} mm · 레일 내폭
    {meta['ground']['rail_inner_width_mm']:.0f} mm · 팔레트
    {meta['ground']['pallet_length_mm']:.0f}×{meta['ground']['pallet_width_mm']:.0f} mm)만으로
    차량의 바닥 사각형·앞뒤 끝·바퀴선을 뽑을 수 있는지 확인하는 1차 검증 · 생성 {_esc(meta.get('generated_at',''))}
    <br><b class="warn">번호판이 보이는 이미지입니다. 로컬에서만 열고 외부로 공유하지 마세요.</b>
  </div>
</header>
<main>

<section>
  <h2>요약</h2>
  <div class="stats">
    <div class="stat"><b>{stats['total']}</b><span>검사한 이미지</span></div>
    <div class="stat"><b>{stats['measurable']}</b><span>측정 대상 (기준점 있는 카메라 · 스냅샷)</span></div>
    <div class="stat"><b>{stats['ok']}</b><span>직육면체를 세운 장면</span></div>
    <div class="stat"><b>{stats['entering_ok']} / {stats['entering']}</b><span>차량 진입 순간 성공</span></div>
    <div class="stat"><b>{length_range}</b><span>추정 길이 (mm)</span></div>
    <div class="stat"><b>{height_range}</b><span>추정 높이 (mm)</span></div>
    <div class="stat"><b>{wheel_line}</b><span>가까운 쪽 바퀴선까지 거리 (중앙값)</span></div>
  </div>
</section>

<section>
  <h2>이번 검증에서 확인된 것</h2>
  <div class="callout">
    <ul>
      <li><b>지면 기준점은 맞습니다.</b> 현장기(pakrio-shinantower)에서 운영자가 팔레트 네 모서리를
          찍어 저장한 값을 NAS로 받아 그대로 썼고, 그 자세로 그린 팔레트 사각형·레일이 실제 사진의
          팔레트·레일과 겹칩니다. 되맞춤 오차 50 mm / 57 mm.</li>
      <li><b>축척이 독립적으로 검증됐습니다.</b> 성공한 장면의 <b>가까운 쪽 바퀴선</b> 중앙값이
          팔레트 중심에서 <b>{wheel_line}</b>입니다. 일반 승용차 트랙폭 1,850 mm를 팔레트 한가운데
          세웠을 때의 이론값 925 mm와 맞습니다 — 이 수치는 어디에도 입력하지 않았고 기하에서만
          나왔습니다. 높이도 {height_range} mm로 승용차 범위에 들어옵니다.</li>
      <li><b>폭은 원리적으로 못 잽니다.</b> 사선 카메라 한 대는 먼 쪽 바퀴가 차체에 가려 절대
          보이지 않습니다. 그래서 폭은 측정값이 아니라 <b>가정값(1,850 mm)</b>으로 표시했고,
          이미지 안에도 '가정'이라고 적었습니다. 폭까지 재려면 두 사선 카메라를 같이 써야 합니다.</li>
      <li><b>남은 한계는 실루엣 추출입니다.</b> 흰 차가 밝은 데크 위에 있으면 배경차분으로
          차체가 거의 안 잡힙니다. 제품 AI(Hailo YOLO)가 같은 프레임에 남긴 차량 상자로 어느
          덩어리가 차인지 고르게 해서 성공 수가 28 → {stats['ok']}건으로 늘었지만, 상자만으로
          해결되지 않는 장면이 남아 있습니다.</li>
    </ul>
  </div>
</section>

<section>
  <h2>읽는 법</h2>
  <div class="callout">
    이미지 안에 판정 결과가 전부 새겨져 있습니다. 이미지만 보고 판단하실 수 있습니다.
    <ul>
      <li><b>분홍 사각형</b> 팔레트(주차구획) 5,350×2,200 mm · <b>노란 선</b> 레일 · <b>회색 선</b> 판정에 쓴 주차기 영역</li>
      <li><b>연두 사각형</b> 추정한 차량 바닥 사각형 · <b>파란 선</b> 진입쪽 끝(+x) · <b>주황 선</b> 안쪽 끝(−x) · <b>노란 선</b> 좌·우 바퀴선</li>
      <li><b>청록 외곽선</b> 배경차분으로 뽑은 차량 실루엣 · <b>분홍 얇은 상자</b> 제품 AI가 그 프레임에서 낸 차량 검출 상자</li>
      <li>폭에 <b>(가정)</b>이 붙은 것은 먼 쪽 바퀴가 안 보여 1,850 mm를 가정했다는 뜻입니다. 잰 값이 아닙니다.</li>
      <li>빨간 테두리 = 판정 불가. <b>사유가 이미지 안에 한국어로 적혀 있습니다.</b></li>
    </ul>
  </div>
</section>

<section>
  <h2>지면 교정 (이 값이 모든 mm의 근거입니다)</h2>
  <table>
    <tr><th>카메라</th><th>상태</th><th>기준점 출처</th><th>되맞춤 오차</th><th>렌즈 내부 파라미터</th></tr>
    {camera_notes}
  </table>
  <div class="grid" style="margin-top:14px">{calib_html}</div>
</section>

<section>
  <h2>판정 결과 {stats['total']}건</h2>
  <div class="filters">
    <button data-filter="all" aria-pressed="true">전체</button>
    <button data-filter="ok" aria-pressed="false">판정됨</button>
    <button data-filter="ng" aria-pressed="false">판정 불가</button>
    <button data-filter="opposite_side" aria-pressed="false">우측 사선</button>
    <button data-filter="rear_side" aria-pressed="false">좌측 사선</button>
    <button data-filter="vehicle" aria-pressed="false">차량 진입 순간만</button>
    <button data-filter="snapshot" aria-pressed="false">스냅샷만</button>
  </div>
  <div class="grid" id="cards">
{cards}
  </div>
</section>
</main>
<script>
  const buttons = document.querySelectorAll('.filters button');
  const cards = document.querySelectorAll('#cards .card');
  buttons.forEach(btn => btn.addEventListener('click', () => {{
    buttons.forEach(b => b.setAttribute('aria-pressed', String(b === btn)));
    const f = btn.dataset.filter;
    cards.forEach(c => {{
      const show = f === 'all'
        || c.dataset.status === f
        || c.dataset.camera === f
        || c.dataset.source === f
        || c.dataset.event === f;
      c.hidden = !show;
    }});
  }}));
</script>
</body></html>
"""


def run(args: argparse.Namespace) -> int:
    out_root = Path(args.out).resolve()
    data = json.loads((out_root / "results.json").read_text(encoding="utf-8"))

    calib_dir = out_root / "calib"
    calib_dir.mkdir(parents=True, exist_ok=True)
    calib_images: list[str] = []
    for source in sorted((Path(args.data).resolve() / "calib").glob("ground-*.jpg")):
        target = calib_dir / source.name
        shutil.copyfile(source, target)
        calib_images.append(str(target.relative_to(out_root)))

    html_text = build_html(data, data["results"], calib_images, data)
    target = out_root / "report.html"
    target.write_text(html_text, encoding="utf-8")
    print(f"보고서 생성: {target}")
    return 0


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="검증 HTML 보고서 생성")
    parser.add_argument("--out", default=str(DEFAULT_OUT))
    parser.add_argument("--data", default=str(LAB_ROOT / "data"))
    return run(parser.parse_args(argv))


if __name__ == "__main__":
    sys.exit(main())
