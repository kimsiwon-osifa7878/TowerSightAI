/* TowerSightAI 분석 대시보드 — 의존성 없는 단일 페이지 앱 (읽기 전용 분석). */
(function () {
  'use strict';

  const DEFAULT_PARAMS = {
    min_confidence: 0.2, consecutive_frames: 2, stale_seconds: 3.0, merge_gap_seconds: 5.0, cameras: '',
    radar_confirm_seconds: 3.0, radar_clear_seconds: 5.0, radar_moving_only: false, radar_max_distance_cm: 0,
    radar_min_energy: 0, pair_tolerance_seconds: 3.0,
  };
  const SOURCE_LABEL = { camera: '카메라', radar: '레이더' };
  const AGREEMENT_LABEL = { both: '둘 다', camera_only: '카메라만', radar_only: '레이더만' };
  const ROLE_LABEL = { field: '현장', dev: '개발', unknown: '미분류' };
  const VERDICT_LABEL = { person: '사람 있음', no_person: '사람 없음', unsure: '판단 불가', unlabeled: '미라벨' };
  const RADAR_STATUS = { 0: '없음', 1: '이동', 2: '정지', 3: '이동+정지' };
  const COLORS = { camera: '#3BC9DB', radar: '#E64980', both: '#F1F3F5', person: '#51CF66', no_person: '#FF6B6B', unsure: '#FCC419', cover: '#2B3A4E', raw: '#5C7CFA', vehicle: '#FFA94D', amber: '#F5A623', muted: '#93A0B3' };

  const state = load();
  state.tz = state.tz || 'Asia/Seoul';
  if (state.host === undefined || state.host === null || state.host === '') state.host = 'field';
  state.sites = [];
  state.hosts = [];
  state.listIds = state.listIds || [];
  let tip = null;

  function load() {
    try { return Object.assign({ params: Object.assign({}, DEFAULT_PARAMS) }, JSON.parse(localStorage.getItem('tsai-analyze') || '{}')); }
    catch (e) { return { params: Object.assign({}, DEFAULT_PARAMS) }; }
  }
  function save() {
    try { localStorage.setItem('tsai-analyze', JSON.stringify({ site: state.site, host: state.host, from: state.from, to: state.to, params: state.params, reviewer: state.reviewer, listIds: (state.listIds || []).slice(0, 2000), tz: state.tz })); } catch (e) { /* ignore */ }
  }

  // ------------------------------------------------------------------ helpers
  const $ = (sel, root) => (root || document).querySelector(sel);
  const $$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  function h(tag, attrs, ...children) {
    const el = document.createElement(tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) {
      if (k === 'class') el.className = v;
      else if (k === 'html') el.innerHTML = v;
      else if (k.startsWith('on')) el.addEventListener(k.slice(2), v);
      else if (v !== null && v !== undefined) el.setAttribute(k, v);
    }
    for (const c of children.flat()) if (c !== null && c !== undefined) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    return el;
  }
  function esc(s) { return String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c])); }
  function toast(msg, err) {
    const el = h('div', { class: 't' + (err ? ' err' : '') }, msg);
    $('#toast').appendChild(el);
    setTimeout(() => el.remove(), err ? 6000 : 3000);
  }
  function fmtTime(t, opts) {
    if (t === null || t === undefined) return '—';
    const d = new Date(t * 1000);
    return new Intl.DateTimeFormat('ko-KR', Object.assign({ timeZone: state.tz, hour12: false, hour: '2-digit', minute: '2-digit', second: '2-digit' }, opts || {})).format(d);
  }
  function fmtDateTime(t) { return fmtTime(t, { month: '2-digit', day: '2-digit', hour: '2-digit', minute: '2-digit', second: '2-digit' }); }
  function fmtDur(s) { if (s === null || s === undefined) return '—'; if (s < 60) return s.toFixed(1) + '초'; if (s < 3600) return (s / 60).toFixed(1) + '분'; return (s / 3600).toFixed(2) + '시간'; }
  function pct(v) { return v === null || v === undefined ? '—' : (v * 100).toFixed(1) + '%'; }
  function num(v, d) { return v === null || v === undefined ? '—' : Number(v).toFixed(d === undefined ? 0 : d); }
  function dayStartEpoch(day) {
    // Local-midnight of `day` in the site timezone (offset from the API when available).
    const off = state.tzOffsetMinutes === undefined ? 540 : state.tzOffsetMinutes;
    return Date.UTC(+day.slice(0, 4), +day.slice(5, 7) - 1, +day.slice(8, 10)) / 1000 - off * 60;
  }
  function paramsQuery() {
    const q = {};
    for (const [k, v] of Object.entries(state.params)) {
      if (v === '' || v === null || v === undefined) continue;
      if (typeof v === 'boolean') { if (v) q[k] = 'true'; continue; }
      q[k] = v;
    }
    return q;
  }
  function baseQuery() { return Object.assign({ site: state.site || '', from: state.from || '', to: state.to || '', host: state.host || '' }, paramsQuery()); }
  function qs(obj) { return Object.entries(obj).filter(([, v]) => v !== '' && v !== null && v !== undefined).map(([k, v]) => encodeURIComponent(k) + '=' + encodeURIComponent(v)).join('&'); }
  async function api(path, query, body) {
    const url = '/api/' + path + (query ? '?' + qs(query) : '');
    const res = await fetch(url, body ? { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) } : undefined);
    let data = null;
    try { data = await res.json(); } catch (e) { data = { error: 'invalid response' }; }
    if (!res.ok) throw new Error(data.error || ('HTTP ' + res.status));
    return data;
  }
  function badge(kind, text) { return h('span', { class: 'badge ' + kind }, text); }
  function cameraCheckCell(e) {
    const c = e.camera_check;
    if (!c || !c.samples) return h('span', { class: 'note' }, '—');
    const cls = c.agreement >= 0.5 ? 'both' : c.agreement > 0 ? 'unsure' : 'radar';
    const cams = Object.entries(c.by_camera || {}).map(([k, v]) => `${k} ${v}회`).join(', ') || '없음';
    const title = `레이더 감지 중 카메라 상태 ${c.samples}초 기록 · 사람 본 초 ${c.camera_present_samples} · 카메라별 ${cams}` + (c.max_confidence !== null && c.max_confidence !== undefined ? ` · 최대 신뢰도 ${c.max_confidence}` : '');
    return h('span', { title }, badge(cls, `${c.camera_present_samples}/${c.samples}`));
  }
  function plateCell(e) {
    if (!e.plate) return h('span', { class: 'note' }, '—');
    const cls = e.plate_recognized ? 'plate' : 'unsure';
    const title = [
      e.plate_recognized ? '인식 성공' : '진입은 있었으나 번호판 판독 실패(미인식)',
      e.plate_confidence !== null && e.plate_confidence !== undefined ? `신뢰도 ${e.plate_confidence.toFixed(2)}` : null,
      e.plate_reads !== null && e.plate_reads !== undefined ? `투표 판독 ${e.plate_reads}회` : null,
      e.plate_attempts ? `1초 주기 시도 ${e.plate_attempts}건` : null,
      e.plate_source === 'vehicle_session' ? '같은 차량 세션' : e.plate_source === 'nearby_read' ? '±60초 내 판독' : null,
    ].filter(Boolean).join(' · ');
    return h('span', { title }, badge(cls, e.plate));
  }
  function why(text) { return h('span', { class: 'why', title: text }, 'ⓘ'); }
  function showTip(evt, html) {
    if (!tip) { tip = h('div', { class: 'tl-tip' }); document.body.appendChild(tip); }
    tip.innerHTML = html; tip.style.display = 'block';
    tip.style.left = Math.min(window.innerWidth - 340, evt.clientX + 12) + 'px';
    tip.style.top = (evt.clientY + 12) + 'px';
  }
  function hideTip() { if (tip) tip.style.display = 'none'; }

  // ------------------------------------------------------------------ SVG charts
  const NS = 'http://www.w3.org/2000/svg';
  function svg(tag, attrs, ...children) {
    const el = document.createElementNS(NS, tag);
    if (attrs) for (const [k, v] of Object.entries(attrs)) { if (v === null || v === undefined) continue; if (k.startsWith('on')) el.addEventListener(k.slice(2), v); else el.setAttribute(k, v); }
    for (const c of children.flat()) if (c !== null && c !== undefined) el.appendChild(typeof c === 'string' ? document.createTextNode(c) : c);
    return el;
  }
  function barChart(rows, series, opts) {
    // rows: [{label, values:{key:n}}], series: [{key, label, color}]
    opts = opts || {};
    const W = Math.max(560, rows.length * 42 + 80), H = opts.height || 220, padL = 44, padB = 44, padT = 12;
    const max = Math.max(1, ...rows.map(r => opts.stacked ? series.reduce((a, s) => a + (r.values[s.key] || 0), 0) : Math.max(...series.map(s => r.values[s.key] || 0))));
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    const innerH = H - padB - padT, innerW = W - padL - 10, bw = innerW / rows.length;
    for (let i = 0; i <= 4; i++) {
      const y = padT + innerH - innerH * i / 4;
      root.appendChild(svg('line', { x1: padL, x2: W - 10, y1: y, y2: y, stroke: '#2E3947' }));
      root.appendChild(svg('text', { x: padL - 6, y: y + 4, 'text-anchor': 'end', fill: COLORS.muted, 'font-size': 10 }, String(Math.round(max * i / 4))));
    }
    rows.forEach((r, i) => {
      let acc = 0;
      const gw = bw * 0.7 / (opts.stacked ? 1 : series.length);
      series.forEach((s, j) => {
        const v = r.values[s.key] || 0;
        const hgt = innerH * v / max;
        const x = padL + bw * i + bw * 0.15 + (opts.stacked ? 0 : gw * j);
        const y = padT + innerH - hgt - (opts.stacked ? innerH * acc / max : 0);
        const rect = svg('rect', { x, y, width: gw, height: hgt, fill: s.color, rx: 2, onmousemove: e => showTip(e, `<b>${esc(r.label)}</b><br>${esc(s.label)}: ${v}`), onmouseleave: hideTip });
        if (r.href) { rect.style.cursor = 'pointer'; rect.addEventListener('click', () => { location.hash = r.href; }); }
        root.appendChild(rect);
        if (opts.stacked) acc += v;
      });
      root.appendChild(svg('text', { x: padL + bw * i + bw / 2, y: H - padB + 14, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 10, transform: rows.length > 16 ? `rotate(-45 ${padL + bw * i + bw / 2} ${H - padB + 14})` : null }, r.label));
    });
    return root;
  }
  function legend(items) { return h('div', { class: 'legend' }, items.map(i => h('span', null, h('i', { style: 'background:' + i.color }), i.label))); }
  function histogram(values, opts) {
    opts = opts || {};
    const W = 560, H = 180, padL = 36, padB = 34, padT = 8;
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    if (!values.length) { root.appendChild(svg('text', { x: W / 2, y: H / 2, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 12 }, '데이터 없음')); return root; }
    const bins = opts.bins || 20;
    const lo = opts.min !== undefined ? opts.min : Math.min(...values), hi = opts.max !== undefined ? opts.max : Math.max(...values);
    const width = (hi - lo) || 1;
    const counts = new Array(bins).fill(0);
    values.forEach(v => { let k = Math.floor((v - lo) / width * bins); if (k >= bins) k = bins - 1; if (k < 0) k = 0; counts[k]++; });
    const max = Math.max(1, ...counts), innerH = H - padB - padT, innerW = W - padL - 10, bw = innerW / bins;
    counts.forEach((c, i) => {
      const hgt = innerH * c / max;
      const a = lo + width * i / bins, b = lo + width * (i + 1) / bins;
      root.appendChild(svg('rect', { x: padL + bw * i + 1, y: padT + innerH - hgt, width: bw - 2, height: hgt, fill: opts.color || COLORS.amber, onmousemove: e => showTip(e, `${a.toFixed(1)} ~ ${b.toFixed(1)} ${esc(opts.unit || '')}<br>${c}건`), onmouseleave: hideTip }));
    });
    for (let i = 0; i <= 4; i++) {
      const x = padL + innerW * i / 4;
      root.appendChild(svg('text', { x, y: H - padB + 14, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 10 }, (lo + width * i / 4).toFixed(opts.digits === undefined ? 1 : opts.digits)));
    }
    root.appendChild(svg('text', { x: padL - 4, y: padT + 10, 'text-anchor': 'end', fill: COLORS.muted, 'font-size': 10 }, String(max)));
    if (opts.zero && lo < 0 && hi > 0) { const x = padL + innerW * (0 - lo) / width; root.appendChild(svg('line', { x1: x, x2: x, y1: padT, y2: H - padB, stroke: COLORS.both, 'stroke-dasharray': '3 3' })); }
    return root;
  }
  function heatmap(rows, cols, matrix, opts) {
    opts = opts || {};
    const cw = 22, ch = 20, padL = 90, padT = 20;
    const W = padL + cols.length * cw + 10, H = padT + rows.length * ch + 6;
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, style: 'max-width:' + W + 'px' });
    const max = Math.max(1, ...matrix.flat());
    cols.forEach((c, j) => root.appendChild(svg('text', { x: padL + cw * j + cw / 2, y: padT - 6, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 9 }, String(c))));
    rows.forEach((r, i) => {
      root.appendChild(svg('text', { x: padL - 6, y: padT + ch * i + ch / 2 + 3, 'text-anchor': 'end', fill: COLORS.muted, 'font-size': 10 }, String(r)));
      cols.forEach((c, j) => {
        const v = matrix[i][j] || 0;
        root.appendChild(svg('rect', { x: padL + cw * j + 1, y: padT + ch * i + 1, width: cw - 2, height: ch - 2, rx: 2, fill: opts.color || COLORS.amber, 'fill-opacity': v ? 0.15 + 0.85 * v / max : 0.04, onmousemove: e => showTip(e, `${esc(r)} · ${esc(c)}시<br>${v}건`), onmouseleave: hideTip }));
      });
    });
    return root;
  }
  function scatter(points, opts) {
    // points: [{x, y, label, current}] in [0,1]
    const W = 420, H = 320, pad = 40;
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}`, style: 'max-width:460px' });
    for (let i = 0; i <= 4; i++) {
      const x = pad + (W - pad - 10) * i / 4, y = H - pad - (H - pad - 10) * i / 4;
      root.appendChild(svg('line', { x1: pad, x2: W - 10, y1: y, y2: y, stroke: '#2E3947' }));
      root.appendChild(svg('line', { y1: 10, y2: H - pad, x1: x, x2: x, stroke: '#2E3947' }));
      root.appendChild(svg('text', { x: pad - 6, y: y + 4, 'text-anchor': 'end', fill: COLORS.muted, 'font-size': 10 }, (i * 25) + '%'));
      root.appendChild(svg('text', { x, y: H - pad + 14, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 10 }, (i * 25) + '%'));
    }
    root.appendChild(svg('text', { x: W / 2, y: H - 6, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 11 }, opts.xLabel || '재현율'));
    root.appendChild(svg('text', { x: 12, y: H / 2, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 11, transform: `rotate(-90 12 ${H / 2})` }, opts.yLabel || '정밀도'));
    points.forEach(p => {
      if (p.x === null || p.y === null) return;
      const cx = pad + (W - pad - 10) * p.x, cy = H - pad - (H - pad - 10) * p.y;
      root.appendChild(svg('circle', { cx, cy, r: p.current ? 7 : 4.5, fill: p.current ? COLORS.amber : (opts.color || COLORS.camera), 'fill-opacity': p.current ? 1 : 0.7, stroke: p.current ? '#fff' : 'none', onmousemove: e => showTip(e, p.label), onmouseleave: hideTip }));
    });
    return root;
  }

  // ------------------------------------------------------------------ timeline (day)
  function timeline(container, data, domain) {
    const day0 = dayStartEpoch(data.day);
    const t0 = domain ? domain[0] : day0, t1 = domain ? domain[1] : day0 + 86400;
    const W = 1100, rowH = 22, padL = 120, padT = 24;
    const bands = [
      { key: 'monitoring', label: '카메라 감시 커버리지', items: (data.coverage.monitoring || []).map(iv => ({ s: iv[0], e: iv[1], color: COLORS.cover, tip: '감시 추론 실행 중' })) },
      { key: 'radarcov', label: '레이더 커버리지', items: (data.coverage.radar || []).map(iv => ({ s: iv[0], e: iv[1], color: '#4A2B3A', tip: 'ESP32 연결/fresh 샘플' })) },
      { key: 'camera', label: '카메라 에피소드', items: data.episodes.filter(e => e.source === 'camera').map(e => ({ s: e.start, e: e.end, color: e.agreement === 'both' ? COLORS.both : COLORS.camera, ep: e })) },
      { key: 'radar', label: '레이더 에피소드' + (data.radar_source !== 'ld2410_sample' ? ' (부분)' : ''), items: data.episodes.filter(e => e.source === 'radar').map(e => ({ s: e.start, e: e.end, color: e.agreement === 'both' ? COLORS.both : COLORS.radar, ep: e })) },
      { key: 'radarstrip', label: '레이더 상태 (30초)', strip: data.radar_strip || [] },
      { key: 'raw', label: 'raw 사람 창 (0.2)', items: data.raw_windows.map(w => ({ s: w.start, e: w.end || w.start, color: COLORS.raw, tip: `raw 창 ${w.id.slice(0, 8)}<br>${(w.cameras || []).join(', ')} · 샘플 ${w.samples}` })) },
      { key: 'rwin', label: '레이더 창 (기록)', items: data.radar_windows.filter(w => w.start).map(w => ({ s: w.start, e: w.end || w.start, color: '#B0397A', tip: `레이더 창 ${w.id.slice(0, 8)}<br>${w.reason || ''}` })) },
      { key: 'vehicle', label: '차량 세션', items: data.vehicle_sessions.map(v => ({ s: v.start, e: v.end || v.start, color: COLORS.vehicle, tip: `차량 세션 ${v.plate || ''} ${v.simulated ? '(시뮬레이션)' : ''}<br>${v.reason || ''}` })) },
    ];
    const H = padT + bands.length * rowH + 30;
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    const x = t => padL + (W - padL - 10) * (t - t0) / (t1 - t0);
    const ticks = domain ? 8 : 24;
    for (let i = 0; i <= ticks; i++) {
      const t = t0 + (t1 - t0) * i / ticks;
      root.appendChild(svg('line', { x1: x(t), x2: x(t), y1: padT - 4, y2: H - 26, stroke: '#232C39' }));
      root.appendChild(svg('text', { x: x(t), y: padT - 8, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 10 }, domain ? fmtTime(t) : fmtTime(t, { hour: '2-digit', minute: '2-digit', second: undefined })));
    }
    bands.forEach((b, i) => {
      const y = padT + rowH * i;
      root.appendChild(svg('text', { x: padL - 8, y: y + rowH / 2 + 4, 'text-anchor': 'end', fill: COLORS.muted, 'font-size': 11 }, b.label));
      root.appendChild(svg('rect', { x: padL, y: y + 2, width: W - padL - 10, height: rowH - 4, fill: '#151B24' }));
      if (b.strip) {
        b.strip.forEach(([t, present, unknown]) => {
          if (t + 30 < t0 || t > t1) return;
          const x0 = x(Math.max(t, t0)), x1 = x(Math.min(t + 30, t1));
          root.appendChild(svg('rect', { x: x0, y: y + 3, width: Math.max(1, x1 - x0), height: rowH - 6, fill: present > 0 ? COLORS.radar : '#3A4556', 'fill-opacity': present > 0 ? 0.25 + 0.75 * present : 0.35 + 0.4 * (1 - unknown), onmousemove: e => showTip(e, `${fmtTime(t)}<br>감지 비율 ${(present * 100).toFixed(0)}% · 알 수 없음 ${(unknown * 100).toFixed(0)}%`), onmouseleave: hideTip }));
        });
        return;
      }
      b.items.forEach(it => {
        if (it.e < t0 || it.s > t1) return;
        const x0 = x(Math.max(it.s, t0)), x1 = x(Math.min(it.e, t1));
        const r = svg('rect', { x: x0, y: y + 3, width: Math.max(2, x1 - x0), height: rowH - 6, rx: 2, fill: it.color, 'fill-opacity': it.ep ? 0.95 : 0.8 });
        const ep = it.ep;
        const tipHtml = ep ? `<b>${SOURCE_LABEL[ep.source]}</b> ${AGREEMENT_LABEL[ep.agreement]}<br>${fmtTime(ep.start)} ~ ${fmtTime(ep.end)} (${fmtDur(ep.duration)})<br>${ep.cameras.join(', ')} ${ep.max_confidence ? 'conf ' + ep.max_confidence.toFixed(2) : ''}${ep.radar && ep.radar.median_distance_cm ? '거리 ' + ep.radar.median_distance_cm + 'cm' : ''}${ep.plate ? '<br>번호판: ' + esc(ep.plate) + (ep.plate_recognized ? '' : ' (판독 실패)') : ''}<br>${ep.verdict ? '라벨: ' + VERDICT_LABEL[ep.verdict] : '미라벨'}` : it.tip;
        r.addEventListener('mousemove', e => showTip(e, tipHtml));
        r.addEventListener('mouseleave', hideTip);
        if (ep) { r.style.cursor = 'pointer'; r.addEventListener('click', () => openReview(ep)); }
        root.appendChild(r);
      });
    });
    // brush zoom
    let drag = null;
    const overlay = svg('rect', { x: padL, y: padT, width: W - padL - 10, height: bands.length * rowH, fill: 'transparent' });
    const sel = svg('rect', { x: 0, y: padT, width: 0, height: bands.length * rowH, fill: COLORS.amber, 'fill-opacity': 0.15, style: 'pointer-events:none' });
    root.appendChild(sel);
    root.appendChild(overlay);
    const toT = evt => { const pt = root.createSVGPoint(); pt.x = evt.clientX; pt.y = evt.clientY; const p = pt.matrixTransform(root.getScreenCTM().inverse()); return t0 + (t1 - t0) * (p.x - padL) / (W - padL - 10); };
    overlay.addEventListener('mousedown', e => { drag = [toT(e), toT(e)]; });
    overlay.addEventListener('mousemove', e => { if (!drag) return; drag[1] = toT(e); const a = Math.min(drag[0], drag[1]), b = Math.max(drag[0], drag[1]); sel.setAttribute('x', x(a)); sel.setAttribute('width', x(b) - x(a)); });
    window.addEventListener('mouseup', () => { if (!drag) return; const a = Math.min(drag[0], drag[1]), b = Math.max(drag[0], drag[1]); drag = null; sel.setAttribute('width', 0); if (b - a > 20) timeline(container, data, [a, b]); });
    overlay.addEventListener('dblclick', () => timeline(container, data, null));
    container.innerHTML = '';
    container.appendChild(root);
    container.appendChild(h('div', { class: 'note' }, '드래그로 확대, 더블클릭으로 하루 전체. 막대를 클릭하면 에피소드 검토로 이동. ', domain ? `표시 구간 ${fmtTime(t0)} ~ ${fmtTime(t1)}` : ''));
  }

  // ------------------------------------------------------------------ episode timeline (review)
  function episodeChart(ep, radarRows, rawWindows) {
    const W = 760, H = 220, padL = 40, padR = 46, padT = 14, padB = 28;
    const lo = ep.start - 10, hi = ep.end + 10;
    const root = svg('svg', { class: 'chart', viewBox: `0 0 ${W} ${H}` });
    const x = t => padL + (W - padL - padR) * (t - lo) / (hi - lo);
    const yConf = v => padT + (H - padT - padB) * (1 - v);
    for (let i = 0; i <= 4; i++) { const y = yConf(i / 4); root.appendChild(svg('line', { x1: padL, x2: W - padR, y1: y, y2: y, stroke: '#2E3947' })); root.appendChild(svg('text', { x: padL - 4, y: y + 3, 'text-anchor': 'end', fill: COLORS.camera, 'font-size': 9 }, (i / 4).toFixed(2))); }
    for (let i = 0; i <= 6; i++) { const t = lo + (hi - lo) * i / 6; root.appendChild(svg('text', { x: x(t), y: H - padB + 14, 'text-anchor': 'middle', fill: COLORS.muted, 'font-size': 10 }, fmtTime(t))); }
    root.appendChild(svg('rect', { x: x(ep.start), y: padT, width: Math.max(2, x(ep.end) - x(ep.start)), height: H - padT - padB, fill: ep.source === 'camera' ? COLORS.camera : COLORS.radar, 'fill-opacity': 0.12 }));
    (rawWindows || []).forEach(w => { if (!w.start) return; root.appendChild(svg('rect', { x: x(Math.max(w.start, lo)), y: H - padB - 4, width: Math.max(2, x(Math.min(w.end || w.start, hi)) - x(Math.max(w.start, lo))), height: 4, fill: COLORS.raw })); });
    // camera confidence points (camera episode timeline: [t, camera, conf])
    if (ep.source === 'camera') {
      const cams = Array.from(new Set(ep.timeline.map(p => p[1])));
      const palette = [COLORS.camera, '#63E6BE', '#A5D8FF', '#FFD8A8'];
      cams.forEach((c, i) => {
        const pts = ep.timeline.filter(p => p[1] === c);
        root.appendChild(svg('polyline', { points: pts.map(p => `${x(p[0])},${yConf(p[2])}`).join(' '), fill: 'none', stroke: palette[i % palette.length], 'stroke-width': 1.5 }));
        pts.forEach(p => root.appendChild(svg('circle', { cx: x(p[0]), cy: yConf(p[2]), r: 2.2, fill: palette[i % palette.length], onmousemove: e => showTip(e, `${esc(c)} ${fmtTime(p[0])}<br>신뢰도 ${p[2].toFixed(3)}`), onmouseleave: hideTip })));
        root.appendChild(svg('text', { x: padL + 4 + i * 110, y: padT + 10, fill: palette[i % palette.length], 'font-size': 10 }, c));
      });
    }
    // radar rows: [t, status, target, dist, me, se, md, sd, source]
    const rows = (radarRows || []).filter(r => r[0] >= lo && r[0] <= hi);
    if (rows.length) {
      const yDist = d => padT + (H - padT - padB) * (1 - Math.min(d, 600) / 600);
      for (let i = 0; i <= 3; i++) { const d = 600 * i / 3; root.appendChild(svg('text', { x: W - padR + 4, y: yDist(d) + 3, fill: COLORS.radar, 'font-size': 9 }, d + 'cm')); }
      rows.forEach(r => {
        const present = r[1] === 0 && r[2] !== null && r[2] !== 0;
        const unknown = r[1] !== 0;
        root.appendChild(svg('rect', { x: x(r[0]) - 1.5, y: H - padB - 10, width: 3, height: 6, fill: unknown ? '#3A4556' : present ? COLORS.radar : '#556270', onmousemove: e => showTip(e, `${fmtTime(r[0])} 레이더 ${unknown ? '알 수 없음(' + (r[1] === 1 ? 'stale' : 'unavailable') + ')' : RADAR_STATUS[r[2]] || r[2]}<br>거리 ${r[3] ?? '—'}cm · 이동E ${r[4] ?? '—'} · 정지E ${r[5] ?? '—'}`), onmouseleave: hideTip }));
        if (present && r[3] !== null) root.appendChild(svg('circle', { cx: x(r[0]), cy: yDist(r[3]), r: 2.5, fill: COLORS.radar, 'fill-opacity': 0.9 }));
      });
    }
    return root;
  }

  // ------------------------------------------------------------------ views
  const views = {};

  views.overview = async function (main) {
    const data = await api('overview', baseQuery());
    state.tzOffsetMinutes = data.tz_offset_minutes;
    applyHosts(data.hosts);
    const s = data.summary, cam = s.per_source.camera, rad = s.per_source.radar;
    main.append(
      h('h1', null, '개요'),
      h('p', { class: 'lead' }, `${data.site.label} · ${data.range.from || '처음'} ~ ${data.range.to || '마지막'} · 호스트 ${({ field: '현장만', all: '전체', dev: '개발기만', unknown: '미분류' })[data.range.host] || data.range.host} · 날짜 ${data.days.length}일 · 에피소드 ${s.episodes_total} (라벨 ${s.labeled_total})`),
    );
    if (!data.days.length) { main.append(h('div', { class: 'empty' }, '이 기간·호스트 범위에 캐시된 날짜가 없습니다. 사이드바의 호스트를 "전체"로 바꾸거나 데이터 · NAS 설정에서 날짜를 내려받으세요.')); return; }
    const kpis = h('div', { class: 'grid cols-4' });
    for (const [key, ps] of [['camera', cam], ['radar', rad]]) {
      kpis.append(
        h('div', { class: 'kpi ' + key }, why('정밀도 = 사람 있음 / (사람 있음 + 사람 없음), 라벨된 에피소드만'), h('div', { class: 'v' }, pct(ps.precision)), h('div', { class: 'l' }, `${SOURCE_LABEL[key]} 정밀도`), h('div', { class: 'd' }, `있음 ${ps.verdicts.person} · 없음 ${ps.verdicts.no_person} · 불가 ${ps.verdicts.unsure} / 전체 ${ps.episodes}`)),
        h('div', { class: 'kpi ' + key }, why('상호 재현율 = 사람이 있었다고 확인된 존재 그룹 중 이 소스가 잡은 비율. 둘 다 놓친 사람은 알 수 없음'), h('div', { class: 'v' }, pct(ps.mutual_recall)), h('div', { class: 'l' }, `${SOURCE_LABEL[key]} 상호 재현율`), h('div', { class: 'd' }, `${ps.caught_groups} / ${ps.true_groups} 그룹`)),
      );
    }
    kpis.append(
      h('div', { class: 'kpi neutral' }, why('사람 없음 에피소드 수 ÷ 커버리지 시간'), h('div', { class: 'v' }, `${num(cam.false_per_hour, 2)} / ${num(rad.false_per_hour, 2)}`), h('div', { class: 'l' }, '시간당 오탐 (카메라 / 레이더)'), h('div', { class: 'd' }, `커버리지 ${fmtDur(cam.coverage_seconds)} / ${fmtDur(rad.coverage_seconds)}`)),
      h('div', { class: 'kpi neutral' }, why('둘 다 감지한 쌍에서 레이더 시작 − 카메라 시작. 음수면 레이더가 먼저'), h('div', { class: 'v' }, s.latency_seconds.length ? num(s.latency_seconds[Math.floor(s.latency_seconds.length / 2)], 1) + '초' : '—'), h('div', { class: 'l' }, '감지 지연 중앙값 (레이더 − 카메라)'), h('div', { class: 'd' }, `${s.latency_seconds.length}쌍`)),
      h('div', { class: 'kpi neutral' }, h('div', { class: 'v' }, `${cam.agreement.both || 0} / ${cam.agreement.camera_only || 0} / ${rad.agreement.radar_only || 0}`), h('div', { class: 'l' }, '둘 다 / 카메라만 / 레이더만'), h('div', { class: 'd' }, '카메라 기준 쌍 / 단독')),
      h('div', { class: 'kpi neutral' }, h('div', { class: 'v' }, `${s.labeled_total} / ${s.episodes_total}`), h('div', { class: 'l' }, '라벨 진행'), h('div', { class: 'd' }, h('a', { href: '#/episodes?label=unlabeled' }, '미라벨 에피소드 검토 →'))),
    );
    main.append(kpis, h('p', { class: 'note' }, s.note));
    if (rad.partial_radar) main.append(h('p', { class: 'warn' }, '이 기간의 레이더 값은 카메라 사람 창 안(person_sample)에만 기록된 부분 데이터입니다. 레이더 단독 감지는 raw 보강(ld2410_sample) 이후 날짜부터 계산됩니다.'));

    main.append(h('h2', null, '일별 에피소드'));
    const rows = data.days.map(d => ({ label: d.day.slice(5), href: `#/day/${encodeURIComponent(d.host)}/${d.day}`, values: { camera_only: d.camera_only, both: d.both, radar_only: d.radar_only } }));
    main.append(h('div', { class: 'panel' }, barChart(rows, [{ key: 'both', label: '둘 다', color: COLORS.both }, { key: 'camera_only', label: '카메라만', color: COLORS.camera }, { key: 'radar_only', label: '레이더만', color: COLORS.radar }], { stacked: true }), legend([{ color: COLORS.both, label: '둘 다' }, { color: COLORS.camera, label: '카메라만' }, { color: COLORS.radar, label: '레이더만' }])));
    main.append(h('h2', null, '커버리지 (시간)'));
    main.append(h('div', { class: 'panel' }, barChart(data.days.map(d => ({ label: d.day.slice(5), values: { m: +(d.monitoring_seconds / 3600).toFixed(2), r: +(d.radar_seconds / 3600).toFixed(2) } })), [{ key: 'm', label: '카메라 감시', color: COLORS.camera }, { key: 'r', label: '레이더 연결', color: COLORS.radar }]), h('p', { class: 'note' }, '커버리지 밖에서는 "감지 없음"이 "사람 없음"을 뜻하지 않습니다. 09-04처럼 추론이 반복 재시작한 날은 카메라 커버리지가 거의 0입니다.')));

    const table = h('table', null, h('thead', null, h('tr', null, ['날짜', '호스트', '카메라', '레이더', '둘 다 (카메라/레이더)', '라벨', '감시 커버리지', '레이더 커버리지', '레코드', '미디어', '레이더 소스', ''].map(t => h('th', null, t)))));
    const tb = h('tbody');
    data.days.forEach(d => tb.append(h('tr', { class: 'click', onclick: () => { location.hash = `#/day/${encodeURIComponent(d.host)}/${d.day}`; } },
      h('td', null, d.day), h('td', null, d.host), h('td', { class: 'num' }, String(d.camera)), h('td', { class: 'num' }, String(d.radar)), h('td', { class: 'num' }, `${d.both} / ${d.radar_both}`), h('td', { class: 'num' }, String(d.labeled)),
      h('td', { class: 'num' }, fmtDur(d.monitoring_seconds)), h('td', { class: 'num' }, fmtDur(d.radar_seconds)), h('td', { class: 'num' }, String(d.records)), h('td', { class: 'num' }, String(d.media)),
      h('td', null, d.radar_source === 'ld2410_sample' ? badge('radar', '1 Hz 상시') : d.radar_source === 'person_sample' ? badge('partial', '부분') : badge('muted', '없음')),
      h('td', null, d.partial ? badge('partial', 'manifest 없음') : ''))));
    table.append(tb);
    main.append(h('div', { class: 'panel table-wrap' }, table));
  };

  views.day = async function (main, args) {
    let host = args[0] ? decodeURIComponent(args[0]) : '', day = args[1] || '';
    if (!host || !day) {
      const d = await api('days', { site: state.site });
      const roleOf = name => (d.hosts.find(x => x.name === name) || {}).role;
      const days = d.days.filter(x => state.host === 'all' ? true : ['field', 'dev', 'unknown'].includes(state.host) ? roleOf(x.host) === state.host : x.host === state.host);
      if (!days.length) { main.append(h('div', { class: 'empty' }, '캐시된 날짜가 없습니다.')); return; }
      const last = days[days.length - 1];
      location.hash = `#/day/${encodeURIComponent(last.host)}/${last.day}`;
      return;
    }
    const data = await api('day', Object.assign({ site: state.site, host, day }, paramsQuery()));
    const days = (await api('days', { site: state.site })).days.filter(x => x.host === host).map(x => x.day);
    const idx = days.indexOf(day);
    const nav = h('div', { class: 'side-actions' },
      h('button', { class: 'small', disabled: idx <= 0 ? 'disabled' : null, onclick: () => { location.hash = `#/day/${encodeURIComponent(host)}/${days[idx - 1]}`; } }, '◀ 이전 날'),
      h('button', { class: 'small', disabled: idx >= days.length - 1 ? 'disabled' : null, onclick: () => { location.hash = `#/day/${encodeURIComponent(host)}/${days[idx + 1]}`; } }, '다음 날 ▶'),
      h('a', { href: `#/episodes?host=${encodeURIComponent(host)}&day=${day}`, class: 'badge muted' }, '이 날 에피소드 목록'));
    main.append(h('h1', null, `타임라인 · ${day}`), h('p', { class: 'lead' }, `${host} · 레코드 ${Object.values(data.counts).reduce((a, b) => a + b, 0)} · 카메라 에피소드 ${data.episodes.filter(e => e.source === 'camera').length} · 레이더 에피소드 ${data.episodes.filter(e => e.source === 'radar').length} · raw 창 ${data.raw_windows.length} · 미디어 ${data.media_count}`), nav);
    const tl = h('div', { class: 'panel chart-wrap' });
    main.append(tl);
    timeline(tl, data, null);
    main.append(legend([{ color: COLORS.both, label: '둘 다 감지' }, { color: COLORS.camera, label: '카메라만' }, { color: COLORS.radar, label: '레이더만' }, { color: COLORS.cover, label: '커버리지' }, { color: COLORS.raw, label: 'raw 사람 창(신뢰도 0.2, 전 카메라)' }, { color: COLORS.vehicle, label: '차량 세션' }]));
    const counts = h('div', { class: 'panel' }, h('h3', null, '이 날의 레코드 종류'), h('div', { class: 'dict-field' }, Object.entries(data.counts).sort((a, b) => b[1] - a[1]).flatMap(([k, v]) => [h('code', null, k), h('span', null, String(v))])));
    main.append(counts);
  };

  views.episodes = async function (main, args, query) {
    const f = Object.assign({ source: '', agreement: '', label: '', camera: '', min_duration: '', order: 'start', with_media: '', plate: '' }, query || {});
    if (query && query.day) { f.from = query.day; f.to = query.day; }
    const q = Object.assign(baseQuery(), f, { limit: 200, offset: query.offset || 0 });
    if (query && query.host) q.host = query.host;
    const data = await api('episodes', q);
    state.listIds = data.ids; save();
    main.append(h('h1', null, '에피소드'), h('p', { class: 'lead' }, `${data.total}건 · ${q.from || '처음'} ~ ${q.to || '마지막'} · 클릭하면 검토 화면으로`));
    const filters = h('div', { class: 'filters' });
    const sel = (name, label, options) => { const s = h('select', { name }); options.forEach(([v, t]) => s.append(h('option', { value: v, selected: f[name] === v ? 'selected' : null }, t))); return h('label', null, label, s); };
    filters.append(
      sel('source', '소스', [['', '전체'], ['camera', '카메라'], ['radar', '레이더']]),
      sel('agreement', '일치', [['', '전체'], ['both', '둘 다'], ['camera_only', '카메라만'], ['radar_only', '레이더만']]),
      sel('label', '라벨', [['', '전체'], ['unlabeled', '미라벨'], ['labeled', '라벨됨'], ['person', '사람 있음'], ['no_person', '사람 없음'], ['unsure', '판단 불가']]),
      h('label', null, '카메라', h('input', { name: 'camera', value: f.camera, placeholder: 'opposite_side' })),
      h('label', null, '최소 길이(초)', h('input', { name: 'min_duration', type: 'number', step: '0.5', value: f.min_duration })),
      sel('order', '정렬', [['start', '시간'], ['duration', '길이'], ['confidence', '신뢰도']]),
      sel('with_media', '미디어', [['', '전체'], ['1', '있음만']]),
      sel('plate', '번호판', [['', '전체'], ['has', '인식됨'], ['unrecognized', '미인식'], ['none', '없음']]),
      h('button', { class: 'primary small', onclick: () => { const nq = {}; $$('select,input', filters).forEach(el => { if (el.value) nq[el.name] = el.value; }); if (query && query.host) nq.host = query.host; if (query && query.day) nq.day = query.day; location.hash = '#/episodes?' + qs(nq); } }, '필터 적용'),
      h('a', { class: 'badge muted', href: '/api/export/episodes.csv?' + qs(q), download: 'episodes.csv' }, 'CSV 내려받기'),
    );
    main.append(filters);
    if (!data.episodes.length) { main.append(h('div', { class: 'empty' }, '조건에 맞는 에피소드가 없습니다.')); return; }
    const table = h('table', null, h('thead', null, h('tr', null, ['', '시작 (KST)', '길이', '소스', '일치', '카메라', '최대 신뢰도', '레이더 거리', '이동 비율', '카메라 대조', '번호판', '미디어', '라벨', '검토자', '태그'].map(t => h('th', null, t)))));
    const tb = h('tbody');
    data.episodes.forEach(e => {
      const thumb = e.thumbnail ? h('img', { class: 'thumb', loading: 'lazy', src: `/media?${qs({ site: state.site, host: e.host, day: e.day, path: e.thumbnail })}` }) : h('div', { class: 'thumb' });
      tb.append(h('tr', { class: 'click', onclick: () => openReview(e) },
        h('td', null, thumb), h('td', null, `${e.day} ${fmtTime(e.start)}`), h('td', { class: 'num' }, fmtDur(e.duration)),
        h('td', null, badge(e.source, SOURCE_LABEL[e.source]), e.partial_radar ? ' ' : '', e.partial_radar ? badge('partial', '부분') : ''),
        h('td', null, badge(e.agreement, AGREEMENT_LABEL[e.agreement])), h('td', null, e.cameras.join(', ')), h('td', { class: 'num' }, e.max_confidence !== null && e.max_confidence !== undefined ? e.max_confidence.toFixed(2) : '—'),
        h('td', { class: 'num' }, e.radar && e.radar.median_distance_cm ? e.radar.median_distance_cm + 'cm' : '—'), h('td', { class: 'num' }, e.radar && e.radar.moving_fraction !== undefined ? pct(e.radar.moving_fraction) : '—'),
        h('td', null, cameraCheckCell(e)),
        h('td', null, plateCell(e)),
        h('td', { class: 'num' }, `${e.media_images}장 / ${e.media_videos}클립`),
        h('td', null, e.verdict ? badge(e.verdict, VERDICT_LABEL[e.verdict]) : badge('muted', '미라벨')), h('td', null, e.reviewer || ''), h('td', null, (e.tags || []).join(', '))));
    });
    table.append(tb);
    main.append(h('div', { class: 'panel table-wrap' }, table));
    if (data.total > data.offset + data.limit || data.offset > 0) {
      main.append(h('div', { class: 'side-actions' },
        h('button', { class: 'small', disabled: data.offset <= 0 ? 'disabled' : null, onclick: () => { location.hash = '#/episodes?' + qs(Object.assign({}, query, { offset: Math.max(0, data.offset - data.limit) })); } }, '◀ 이전'),
        h('span', { class: 'note' }, `${data.offset + 1} ~ ${Math.min(data.total, data.offset + data.limit)} / ${data.total}`),
        h('button', { class: 'small', disabled: data.offset + data.limit >= data.total ? 'disabled' : null, onclick: () => { location.hash = '#/episodes?' + qs(Object.assign({}, query, { offset: data.offset + data.limit })); } }, '다음 ▶')));
    }
  };

  function openReview(e) { location.hash = `#/review/${encodeURIComponent(e.host)}/${e.day}/${e.id}`; }

  views.review = async function (main, args) {
    const host = decodeURIComponent(args[0] || ''), day = args[1] || '', id = args[2] || '';
    const data = await api('episode', Object.assign({ site: state.site, host, day, id }, paramsQuery()));
    state.tz = data.timezone || state.tz;
    const ep = data.episode;
    const ids = state.listIds || [];
    const pos = ids.indexOf(ep.id);
    const go = delta => { const nid = ids[pos + delta]; if (nid) location.hash = `#/review/${encodeURIComponent(host)}/${day}/${nid}`; };
    const head = h('div', { class: 'panel review-head' },
      h('span', { class: 'big' }, `${ep.day} ${fmtTime(ep.start)} ~ ${fmtTime(ep.end)}`), h('span', null, fmtDur(ep.duration)),
      badge(ep.source, SOURCE_LABEL[ep.source]), badge(ep.agreement, AGREEMENT_LABEL[ep.agreement]), ep.partial_radar ? badge('partial', '부분 레이더') : null,
      ep.cameras.length ? h('span', null, '카메라: ' + ep.cameras.join(', ')) : null,
      ep.max_confidence !== null && ep.max_confidence !== undefined ? h('span', null, `최대 신뢰도 ${ep.max_confidence.toFixed(2)} · ${ep.frame_count}프레임`) : null,
      ep.radar && ep.radar.present_samples ? h('span', null, `레이더 감지 샘플 ${ep.radar.present_samples} · 거리 ${ep.radar.min_distance_cm}~${ep.radar.max_distance_cm}cm (중앙 ${ep.radar.median_distance_cm}) · 이동 비율 ${pct(ep.radar.moving_fraction)} · 최대 에너지 이동 ${ep.radar.max_moving_energy ?? '—'}/정지 ${ep.radar.max_motionless_energy ?? '—'}`) : null,
      data.vehicle_sessions.length ? badge('muted', '차량 세션 중') : null,
      ep.camera_check && ep.camera_check.samples ? h('span', null, '카메라 대조 ', cameraCheckCell(ep)) : null,
      ep.plate ? h('span', null, '번호판 ', plateCell(ep)) : null,
      h('span', { style: 'margin-left:auto' }, h('button', { class: 'small', disabled: pos <= 0 ? 'disabled' : null, onclick: () => go(-1) }, '◀ 이전'), ' ', h('span', { class: 'note' }, pos >= 0 ? `${pos + 1}/${ids.length}` : '목록 밖'), ' ', h('button', { class: 'small', disabled: pos < 0 || pos >= ids.length - 1 ? 'disabled' : null, onclick: () => go(1) }, '다음 ▶'), ' ', h('a', { class: 'badge muted', href: `#/day/${encodeURIComponent(host)}/${day}` }, '타임라인')),
    );
    main.append(h('h1', null, '에피소드 검토'), head);
    if (data.paired.length) main.append(h('p', { class: 'note' }, '같은 사건으로 묶인 상대 에피소드: ', data.paired.map(p => h('a', { href: `#/review/${encodeURIComponent(p.host)}/${p.day}/${p.id}`, style: 'margin-right:8px' }, `${SOURCE_LABEL[p.source]} ${fmtTime(p.start)} (${fmtDur(p.duration)})${p.verdict ? ' · ' + VERDICT_LABEL[p.verdict] : ''}`))));

    const split = h('div', { class: 'split' });
    // media
    const mediaPanel = h('div', { class: 'panel' }, h('h3', null, `증거 미디어 (${data.media.length})`));
    const grid = h('div', { class: 'media-grid' });
    const images = data.media.filter(m => m.kind !== 'video'), videos = data.media.filter(m => m.kind === 'video');
    images.forEach(m => grid.append(h('div', { class: 'media-card' }, h('a', { href: m.url, target: '_blank' }, h('img', { src: m.url, loading: 'lazy', alt: m.path })), h('div', { class: 'cap' }, h('span', null, `${m.camera} · ${m.kind === 'plate_image' ? '번호판 원본' : m.kind === 'plate_crop' ? '번호판 잘라내기' : m.event_kind === 'person' ? '창 시작' : m.event_kind === 'person_end' ? '창 종료' : m.event_kind === 'radar' ? '레이더 시작' : m.event_kind === 'radar_end' ? '레이더 종료' : m.event_kind}`), h('span', null, fmtTime(m.t))))));
    videos.forEach(m => grid.append(h('div', { class: 'media-card' }, h('video', { controls: '', preload: 'metadata', src: m.mp4_url }), h('div', { class: 'cap' }, h('span', null, `${m.camera} · 클립 (5초 프리롤)`), h('a', { href: m.url, download: '' }, 'MKV 저장')))));
    if (!data.media.length) grid.append(h('div', { class: 'note' }, '이 에피소드에 연결된 스냅샷/클립이 없습니다.', ep.media_failures && ep.media_failures.length ? ' 사유: ' + ep.media_failures.map(f => `${f.camera}:${f.reason}`).join(', ') : ''));
    if (ep.media_failures && ep.media_failures.length && data.media.length) grid.append(h('div', { class: 'note' }, '캡처 실패/생략: ' + ep.media_failures.map(f => `${f.kind} ${f.camera}: ${f.reason}`).join(', ')));
    mediaPanel.append(grid, h('p', { class: 'note' }, '미디어는 NAS에서 처음 볼 때 내려받아 캐시합니다. 클립은 ffmpeg로 MP4 변환(재인코딩 없음).'));
    // chart + samples
    const right = h('div');
    right.append(h('div', { class: 'panel' }, h('h3', null, '시간축 (카메라 신뢰도 · 레이더 거리/상태)'), episodeChart(ep, data.radar_samples, data.raw_windows), legend([{ color: COLORS.camera, label: '카메라 person 신뢰도(좌축)' }, { color: COLORS.radar, label: '레이더 감지 거리(우축)·상태 틱' }, { color: '#556270', label: '레이더 없음' }, { color: '#3A4556', label: '레이더 알 수 없음' }, { color: COLORS.raw, label: 'raw 사람 창' }])));
    // label panel
    const labelPanel = h('div', { class: 'panel' }, h('h3', null, '판정 ', h('span', { class: 'note' }, '단축키 ', h('span', { class: 'kbd' }, '1'), ' 있음 ', h('span', { class: 'kbd' }, '2'), ' 없음 ', h('span', { class: 'kbd' }, '3'), ' 불가 ', h('span', { class: 'kbd' }, '←'), h('span', { class: 'kbd' }, '→'), ' 이동')));
    let verdict = data.label ? data.label.verdict : null;
    const tagsSel = new Set(data.label ? data.label.tags || [] : []);
    const vrow = h('div', { class: 'verdict-row' });
    const vbtns = {};
    data.verdicts.forEach(v => { vbtns[v.value] = h('button', { class: v.value + (verdict === v.value ? ' active' : ''), onclick: () => setVerdict(v.value) }, v.label); vrow.append(vbtns[v.value]); });
    const tags = h('div', { class: 'tags' });
    data.tags.forEach(t => { const l = h('label', { class: tagsSel.has(t) ? 'on' : '', onclick: () => { if (tagsSel.has(t)) tagsSel.delete(t); else tagsSel.add(t); l.className = tagsSel.has(t) ? 'on' : ''; } }, t); tags.append(l); });
    const note = h('textarea', { placeholder: '메모 (선택)' }); note.value = data.label ? data.label.note || '' : '';
    const status = h('div', { class: 'note' }, data.label ? `현재 라벨: ${VERDICT_LABEL[data.label.verdict]} · ${data.label.reviewer || '(검토자 없음)'} · ${data.label.labeled_at}` : '아직 라벨이 없습니다.');
    async function setVerdict(v) {
      verdict = v;
      Object.entries(vbtns).forEach(([k, b]) => b.classList.toggle('active', k === v));
      try {
        const res = await api('label', null, { site: state.site, episode_id: ep.id, host: ep.host, day: ep.day, source: ep.source, start: ep.start, end: ep.end, verdict: v, reviewer: state.reviewer || '', tags: Array.from(tagsSel), note: note.value });
        status.textContent = `저장됨: ${VERDICT_LABEL[res.label.verdict]} · ${res.label.reviewer || '(검토자 없음)'} · ${res.label.labeled_at}`;
        toast(`라벨 저장: ${VERDICT_LABEL[v]}`);
      } catch (e) { toast('라벨 저장 실패: ' + e.message, true); }
    }
    labelPanel.append(vrow, tags, note, h('div', { class: 'side-actions' }, h('button', { class: 'small', onclick: () => { if (verdict) setVerdict(verdict); else toast('먼저 판정을 고르세요', true); } }, '태그/메모 저장'), status));
    if (data.history.length > 1) labelPanel.append(h('details', null, h('summary', null, `이력 ${data.history.length}건`), h('table', null, h('tbody', null, data.history.map(r => h('tr', null, h('td', null, r.labeled_at), h('td', null, badge(r.verdict, VERDICT_LABEL[r.verdict])), h('td', null, r.reviewer || ''), h('td', null, (r.tags || []).join(', ')), h('td', null, r.note || '')))))));
    right.append(labelPanel);
    split.append(mediaPanel, right);
    main.append(split);

    // camera-vs-radar comparison during the radar window
    const rwin = (data.radar_windows || []).filter(w => (w.samples || []).length);
    if (rwin.length) {
      const panel = h('div', { class: 'panel' }, h('h3', null, '레이더 감지 중 카메라 상태'));
      rwin.forEach(w => {
        const cams = Array.from(new Set(w.samples.flatMap(s => Object.keys(s.cams || {}))));
        const t = h('table', null, h('thead', null, h('tr', null, ['시각', ...cams.map(c => c + ' 사람'), '레이더', '거리', '이동E', '정지E'].map(x => h('th', null, x)))));
        const tb = h('tbody');
        w.samples.forEach(s => tb.append(h('tr', null, h('td', null, fmtTime(s.t)),
          ...cams.map(c => h('td', null, s.cams && s.cams[c] ? badge('camera', (s.conf && s.conf[c] !== undefined ? s.conf[c].toFixed(2) : '●')) : h('span', { class: 'note' }, '·'))),
          h('td', null, s.ld ? (s.ld.s !== 0 ? badge('muted', s.ld.s === 1 ? 'stale' : '없음') : badge(s.ld.ts ? 'radar' : 'muted', RADAR_STATUS[s.ld.ts] || String(s.ld.ts))) : badge('muted', '—')),
          h('td', { class: 'num' }, s.ld && s.ld.d !== null && s.ld.d !== undefined ? s.ld.d + 'cm' : '—'),
          h('td', { class: 'num' }, s.ld ? String(s.ld.me ?? '—') : '—'), h('td', { class: 'num' }, s.ld ? String(s.ld.se ?? '—') : '—'))));
        t.append(tb);
        panel.append(h('div', { style: 'margin:6px 0' }, badge(w.camera_agreement >= 0.5 ? 'both' : w.camera_agreement > 0 ? 'unsure' : 'radar', `카메라 동의 ${w.camera_present_samples}/${w.camera_samples}`), ' ',
          h('span', { class: 'note' }, `${fmtTime(w.start)} ~ ${w.end ? fmtTime(w.end) : '—'}${w.reason ? ' · ' + w.reason : ''}`)),
          h('div', { class: 'table-wrap' }, t));
      });
      panel.append(h('p', { class: 'note' }, '레이더가 사람을 몇 초 이상 계속 감지하면 창이 열리고, 그동안 1초마다 카메라 상태를 함께 기록합니다. 동의 0회면 레이더 단독 감지이며 위의 스냅샷·클립으로 실제 사람 여부를 판정하세요.'));
      main.append(panel);
    }
    // plate panel
    if ((data.plates && data.plates.length) || (data.plate_attempts && data.plate_attempts.length)) {
      const panel = h('div', { class: 'panel' }, h('h3', null, '번호판 인식'));
      (data.plates || []).forEach(p => {
        panel.append(h('div', { style: 'margin:4px 0' },
          badge(p.recognized ? 'plate' : 'unsure', p.recognized ? p.plate : '미인식'), ' ',
          h('span', { class: 'note' }, `${fmtTime(p.t)} · ${p.camera || 'front'}${p.confidence !== null && p.confidence !== undefined ? ' · 신뢰도 ' + p.confidence.toFixed(2) : ''}${p.reads !== null && p.reads !== undefined ? ' · 투표 판독 ' + p.reads + '회' : ''}${p.reason ? ' · ' + p.reason : ''}${p.simulated ? ' · 시뮬레이션' : ''}`)));
      });
      const attempts = data.plate_attempts || [];
      if (attempts.length) {
        const accepted = attempts.filter(a => a.accepted).length;
        const t = h('table', null, h('thead', null, h('tr', null, ['시각', '판독', '신뢰도', '반영', '사유'].map(x => h('th', null, x)))));
        const tb = h('tbody');
        attempts.forEach(a => tb.append(h('tr', null, h('td', null, fmtTime(a.t)), h('td', null, a.plate || h('span', { class: 'note' }, '—')),
          h('td', { class: 'num' }, a.confidence !== null && a.confidence !== undefined ? a.confidence.toFixed(2) : '—'),
          h('td', null, a.accepted ? badge('plate', '반영') : badge('muted', '제외')), h('td', null, h('span', { class: 'note' }, a.reason || '')))));
        t.append(tb);
        panel.append(h('details', null, h('summary', null, `1초 주기 판독 ${attempts.length}건 · 투표 반영 ${accepted}건 (${pct(accepted / attempts.length)})`), h('div', { class: 'table-wrap' }, t)));
      }
      panel.append(h('p', { class: 'note' }, '번호판은 전면 카메라 FastALPR(CPU)이 진입 중 1초 주기로 읽고 차량진입선 아래 판독만 다수결에 반영합니다. 미인식은 진입은 있었으나 판독에 실패했다는 뜻입니다.'));
      main.append(panel);
    }
    // sample table (raw windows) + raw records
    if (data.raw_windows.length) {
      const w = data.raw_windows[0];
      const cams = Array.from(new Set(w.samples.flatMap(s => Object.keys(s.cams || {}))));
      const t = h('table', null, h('thead', null, h('tr', null, ['시각', ...cams.map(c => c + ' 사람'), '레이더', '거리', '이동E', '정지E', 'age'].map(x => h('th', null, x)))));
      const tb = h('tbody');
      w.samples.forEach(s => tb.append(h('tr', null, h('td', null, fmtTime(s.t)), ...cams.map(c => h('td', null, s.cams && s.cams[c] ? badge('camera', (s.conf && s.conf[c] !== undefined ? s.conf[c].toFixed(2) : '●')) : h('span', { class: 'note' }, '·'))),
        h('td', null, s.ld ? (s.ld.s !== 0 ? badge('muted', s.ld.s === 1 ? 'stale' : '없음') : badge(s.ld.ts ? 'radar' : 'muted', RADAR_STATUS[s.ld.ts] || String(s.ld.ts))) : badge('muted', '—')),
        h('td', { class: 'num' }, s.ld && s.ld.d !== null && s.ld.d !== undefined ? s.ld.d + 'cm' : '—'), h('td', { class: 'num' }, s.ld ? String(s.ld.me ?? '—') : '—'), h('td', { class: 'num' }, s.ld ? String(s.ld.se ?? '—') : '—'), h('td', { class: 'num' }, s.ld && s.ld.age !== null && s.ld.age !== undefined ? s.ld.age + 'ms' : '—'))));
      t.append(tb);
      main.append(h('div', { class: 'panel' }, h('details', null, h('summary', null, `raw 사람 창 0.5초 샘플 ${w.samples.length}개 (창 ${w.id.slice(0, 8)} · ${data.raw_windows.length > 1 ? '첫 창만 표시' : ''})`), h('div', { class: 'table-wrap' }, t))));
    }
    const rawIds = [...data.raw_windows.flatMap(w => [w.start_event_id, w.close_event_id]), ...data.radar_windows.flatMap(w => [w.start_event_id, w.close_event_id]), ...data.media.map(m => m.event_id)].filter(Boolean);
    if (rawIds.length) {
      const box = h('pre', { class: 'json' }, '버튼을 누르면 원본 JSONL 레코드를 불러옵니다 (event_id 기준).');
      main.append(h('div', { class: 'panel' }, h('h3', null, '원본 레코드'), h('div', { class: 'side-actions' }, h('button', { class: 'small', onclick: async () => { box.textContent = '불러오는 중…'; try { const r = await api('records', { site: state.site, host, day, ids: rawIds.slice(0, 40).join(',') }); box.textContent = JSON.stringify(r.records, null, 2); } catch (e) { box.textContent = '실패: ' + e.message; } } }, '원본 보기'), h('a', { href: '#/dictionary', class: 'badge muted' }, '필드 뜻은 데이터 사전')), box));
    }

    const keyHandler = e => {
      if (['INPUT', 'TEXTAREA', 'SELECT'].includes(document.activeElement.tagName)) return;
      if (e.key === '1') setVerdict('person'); else if (e.key === '2') setVerdict('no_person'); else if (e.key === '3') setVerdict('unsure');
      else if (e.key === 'ArrowLeft') go(-1); else if (e.key === 'ArrowRight') go(1);
    };
    document.addEventListener('keydown', keyHandler);
    main._cleanup = () => document.removeEventListener('keydown', keyHandler);
  };

  views.compare = async function (main) {
    const data = await api('overview', baseQuery());
    const labels = await api('labels', { site: state.site });
    const s = data.summary, cam = s.per_source.camera, rad = s.per_source.radar;
    main.append(h('h1', null, '비교'), h('p', { class: 'lead' }, `${data.range.from || '처음'} ~ ${data.range.to || '마지막'} · 라벨 ${s.labeled_total}/${s.episodes_total}`));
    // matrix
    const verdicts = ['person', 'no_person', 'unsure', 'unlabeled'];
    const mt = h('table', null, h('thead', null, h('tr', null, h('th', null, '일치 분류 \\ 판정'), ...verdicts.map(v => h('th', { class: 'num' }, VERDICT_LABEL[v])), h('th', { class: 'num' }, '정밀도'))));
    const mb = h('tbody');
    ['both', 'camera_only', 'radar_only'].forEach(a => { const row = s.agreement_matrix[a] || {}; const tp = row.person || 0, fp = row.no_person || 0; mb.append(h('tr', null, h('td', null, badge(a, AGREEMENT_LABEL[a])), ...verdicts.map(v => h('td', { class: 'num' }, String(row[v] || 0))), h('td', { class: 'num' }, tp + fp ? pct(tp / (tp + fp)) : '—'))); });
    mt.append(mb);
    main.append(h('div', { class: 'grid cols-2' },
      h('div', { class: 'panel' }, h('h3', null, '일치 매트릭스 (에피소드 수)'), mt, h('p', { class: 'note' }, '"둘 다"는 카메라·레이더 각각의 에피소드가 모두 세어집니다(한 사건에 2건).')),
      h('div', { class: 'panel' }, h('h3', null, '소스별 요약'), h('table', null, h('tbody', null,
        h('tr', null, h('td', null, '지표'), h('td', null, badge('camera', '카메라')), h('td', null, badge('radar', '레이더'))),
        h('tr', null, h('td', null, '에피소드'), h('td', { class: 'num' }, String(cam.episodes)), h('td', { class: 'num' }, String(rad.episodes))),
        h('tr', null, h('td', null, '라벨됨'), h('td', { class: 'num' }, String(cam.labeled)), h('td', { class: 'num' }, String(rad.labeled))),
        h('tr', null, h('td', null, '정밀도'), h('td', { class: 'num' }, pct(cam.precision)), h('td', { class: 'num' }, pct(rad.precision))),
        h('tr', null, h('td', null, '상호 재현율'), h('td', { class: 'num' }, pct(cam.mutual_recall)), h('td', { class: 'num' }, pct(rad.mutual_recall))),
        h('tr', null, h('td', null, '시간당 오탐'), h('td', { class: 'num' }, num(cam.false_per_hour, 2)), h('td', { class: 'num' }, num(rad.false_per_hour, 2))),
        h('tr', null, h('td', null, '커버리지'), h('td', { class: 'num' }, fmtDur(cam.coverage_seconds)), h('td', { class: 'num' }, fmtDur(rad.coverage_seconds))),
        h('tr', null, h('td', null, '길이 중앙값 / p90'), h('td', { class: 'num' }, `${num(cam.duration.p50, 1)} / ${num(cam.duration.p90, 1)}초`), h('td', { class: 'num' }, `${num(rad.duration.p50, 1)} / ${num(rad.duration.p90, 1)}초`)),
      )), h('p', { class: 'note' }, s.note))));
    main.append(h('div', { class: 'grid cols-2' },
      h('div', { class: 'panel' }, h('h3', null, '감지 지연 (레이더 시작 − 카메라 시작, 초)'), histogram(s.latency_seconds, { bins: 20, zero: true, unit: '초', color: COLORS.both }), h('p', { class: 'note' }, `${s.latency_seconds.length}쌍 · 음수 = 레이더가 먼저`)),
      h('div', { class: 'panel' }, h('h3', null, '에피소드 길이 분포 (초, 120초 상한)'), legend([{ color: COLORS.camera, label: '카메라' }, { color: COLORS.radar, label: '레이더' }]), durationHists(data))));
    // hour heatmap
    const hours = Array.from({ length: 24 }, (_, i) => i);
    main.append(h('div', { class: 'grid cols-2' },
      h('div', { class: 'panel' }, h('h3', null, '시간대별 에피소드'), heatmap(['카메라', '레이더'], hours, [cam.by_hour, rad.by_hour])),
      h('div', { class: 'panel' }, h('h3', null, '카메라별 에피소드'), barChart(Object.entries(cam.by_camera).map(([k, v]) => ({ label: k, values: { n: v } })), [{ key: 'n', label: '에피소드', color: COLORS.camera }], { height: 180 }), h('p', { class: 'note' }, 'opposite_side는 문이 열리면 바깥 통행이 보입니다. 엔진 IDLE 감시는 이 카메라를 제외합니다(파라미터의 카메라 항목으로 재현).'))));
    // radar by verdict
    const rb = s.radar_by_verdict;
    const rt = h('table', null, h('thead', null, h('tr', null, ['판정', '건수', '거리 중앙값(cm)', '거리 범위', '최대 에너지 중앙값', '이동 비율 평균'].map(t => h('th', null, t)))));
    const rtb = h('tbody');
    Object.entries(rb).forEach(([v, b]) => { const d = b.distance.slice().sort((a, c) => a - c), e = b.energy.slice().sort((a, c) => a - c); rtb.append(h('tr', null, h('td', null, badge(v === 'unlabeled' ? 'muted' : v, VERDICT_LABEL[v])), h('td', { class: 'num' }, String(b.energy.length)), h('td', { class: 'num' }, d.length ? String(d[Math.floor(d.length / 2)]) : '—'), h('td', { class: 'num' }, d.length ? `${d[0]}~${d[d.length - 1]}` : '—'), h('td', { class: 'num' }, e.length ? String(e[Math.floor(e.length / 2)]) : '—'), h('td', { class: 'num' }, b.moving_fraction.length ? pct(b.moving_fraction.reduce((a, c) => a + c, 0) / b.moving_fraction.length) : '—'))); });
    rt.append(rtb);
    main.append(h('div', { class: 'panel' }, h('h3', null, '레이더 값 분포 (판정별)'), rt, h('p', { class: 'note' }, '사람 있음/없음 사이에 거리·에너지·이동 비율 차이가 뚜렷하면 레이더 게이트(임계값 스윕)를 걸 근거가 됩니다.')));
    // reviewer agreement
    const ag = labels.agreement;
    if (ag.reviewers.length > 1) {
      const at = h('table', null, h('thead', null, h('tr', null, ['검토자 A', '검토자 B', '공통 라벨', '일치율', "Cohen's κ", '불일치'].map(t => h('th', null, t)))));
      const atb = h('tbody');
      ag.pairs.forEach(p => atb.append(h('tr', null, h('td', null, p.a), h('td', null, p.b), h('td', { class: 'num' }, String(p.shared)), h('td', { class: 'num' }, pct(p.agreement)), h('td', { class: 'num' }, num(p.kappa, 2)), h('td', null, p.conflicts.slice(0, 5).map(id => h('a', { href: '#/episodes?label=labeled', style: 'margin-right:6px' }, id.slice(0, 8)))))));
      at.append(atb);
      main.append(h('div', { class: 'panel' }, h('h3', null, '검토자 간 일치'), at));
    }
  };
  function durationHists(data) {
    const wrap = h('div');
    // Approximate from by-source quantiles isn't enough; fetch light episodes for the range instead.
    api('episodes', Object.assign(baseQuery(), { limit: 500 })).then(r => {
      const cam = r.episodes.filter(e => e.source === 'camera').map(e => Math.min(120, e.duration));
      const rad = r.episodes.filter(e => e.source === 'radar').map(e => Math.min(120, e.duration));
      wrap.append(histogram(cam, { bins: 24, min: 0, max: 120, color: COLORS.camera, unit: '초', digits: 0 }), histogram(rad, { bins: 24, min: 0, max: 120, color: COLORS.radar, unit: '초', digits: 0 }), h('p', { class: 'note' }, `처음 ${r.episodes.length}건 기준`));
    }).catch(e => wrap.append(h('p', { class: 'err' }, e.message)));
    return wrap;
  }

  views.tuning = async function (main) {
    main.append(h('h1', null, '임계값 스윕'), h('p', { class: 'lead' }, '라벨된 에피소드를 기준으로 파라미터를 바꿔 가며 정밀도·재현율을 다시 계산합니다. 결과는 참고용이며 적용은 운영 콘솔 감시 설정에서 사람이 합니다.'));
    const wrap = h('div', { class: 'grid cols-2' });
    main.append(wrap);
    for (const kind of ['camera', 'radar']) {
      const panel = h('div', { class: 'panel' }, h('h3', null, kind === 'camera' ? '카메라: 신뢰도 × 연속 프레임' : '레이더: 확정 초 × 이동만 × 거리 게이트'), h('p', { class: 'note' }, '계산 중…'));
      wrap.append(panel);
      api('sweep', Object.assign(baseQuery(), { kind })).then(r => {
        panel.innerHTML = '';
        panel.append(h('h3', null, kind === 'camera' ? '카메라: 신뢰도 × 연속 프레임' : '레이더: 확정 초 × 이동만 × 거리 게이트'));
        if (!r.labeled) { panel.append(h('p', { class: 'warn' }, '라벨이 없어 스윕을 평가할 수 없습니다. 에피소드를 먼저 검토하세요.')); return; }
        const cur = r.params;
        const isCur = row => kind === 'camera' ? (row.min_confidence === cur.min_confidence && row.consecutive_frames === cur.consecutive_frames) : (row.radar_confirm_seconds === cur.radar_confirm_seconds && row.radar_moving_only === cur.radar_moving_only && row.radar_max_distance_cm === cur.radar_max_distance_cm);
        const label = row => kind === 'camera' ? `conf ≥ ${row.min_confidence}, ${row.consecutive_frames}프레임` : `${row.radar_confirm_seconds}초, ${row.radar_moving_only ? '이동만' : '전체'}, ${row.radar_max_distance_cm ? '≤' + row.radar_max_distance_cm + 'cm' : '거리 무제한'}`;
        panel.append(scatter(r.rows.map(row => ({ x: row.recall, y: row.precision, current: isCur(row), label: `<b>${label(row)}</b><br>정밀도 ${pct(row.precision)} · 재현율 ${pct(row.recall)}<br>에피소드 ${row.candidates} · TP ${row.tp} · FP ${row.fp}` })), { color: kind === 'camera' ? COLORS.camera : COLORS.radar }));
        const t = h('table', null, h('thead', null, h('tr', null, ['설정', '에피소드', 'TP', 'FP', '정밀도', '재현율'].map(x => h('th', null, x)))));
        const tb = h('tbody');
        r.rows.forEach(row => tb.append(h('tr', { style: isCur(row) ? 'color:var(--amber);font-weight:600' : '' }, h('td', null, label(row) + (isCur(row) ? ' (현재)' : '')), h('td', { class: 'num' }, String(row.candidates)), h('td', { class: 'num' }, String(row.tp)), h('td', { class: 'num' }, String(row.fp)), h('td', { class: 'num' }, pct(row.precision)), h('td', { class: 'num' }, pct(row.recall)))));
        t.append(tb);
        panel.append(h('div', { class: 'table-wrap' }, t), h('p', { class: 'note' }, `라벨된 기준 에피소드와 겹치는 후보만 평가 (${r.labeled}건 라벨). 재현율은 라벨 '사람 있음' 기준 에피소드 중 후보가 하나라도 겹친 비율.`));
      }).catch(e => { panel.append(h('p', { class: 'err' }, e.message)); });
    }
  };

  views.dictionary = async function (main) {
    const d = await api('dictionary');
    main.append(h('h1', null, '데이터 사전'), h('p', { class: 'lead' }, 'raw JSONL 이벤트와 대시보드가 만드는 파생 값의 뜻. 모두 분석 전용(raw_only)이며 안전 게이트에 영향이 없습니다.'));
    main.append(h('h2', null, '파생 값 (대시보드 계산)'));
    main.append(h('div', { class: 'panel' }, h('div', { class: 'dict-field' }, d.derived.flatMap(x => [h('b', null, x.name), h('span', null, x.desc)]))));
    main.append(h('h2', null, 'raw 이벤트 (schema v2)'));
    d.event_types.forEach(e => main.append(h('div', { class: 'panel' }, h('h3', null, h('code', null, e.name), ' ', badge('muted', e.safety)), h('p', null, e.desc), h('p', { class: 'note' }, '출처: ' + e.module), h('div', { class: 'dict-field' }, Object.entries(e.fields).flatMap(([k, v]) => [h('code', null, k), h('span', null, v)])))));
  };

  views.data = async function (main) {
    const sites = await api('sites');
    main.append(h('h1', null, '데이터 · NAS 설정'), h('p', { class: 'lead' }, '현장별 NAS 주소를 등록하고 날짜 폴더를 로컬 캐시로 내려받습니다. 읽기 전용 접근이며 NAS의 파일은 바꾸지 않습니다.'));
    // sites
    const sp = h('div', { class: 'panel' }, h('h3', null, '등록된 현장'));
    const st = h('table', null, h('thead', null, h('tr', null, ['이름', '표시명', 'NAS 호스트', '포트', '계정', '폴더', '시간대', '상태', ''].map(t => h('th', null, t)))));
    const stb = h('tbody');
    const form = siteForm(null);
    sites.sites.forEach(s => stb.append(h('tr', null, h('td', null, s.name), h('td', null, s.label), h('td', null, s.nas_host), h('td', { class: 'num' }, String(s.nas_port)), h('td', null, s.nas_username), h('td', null, s.nas_folder), h('td', null, s.timezone_name), h('td', null, s.configured && s.has_password ? badge('person', '설정됨') : badge('unsure', '누락: ' + s.missing.join(','))),
      h('td', null, h('button', { class: 'small', onclick: () => { form.fill(s); } }, '편집'), ' ', h('button', { class: 'small', onclick: async () => { if (!confirm(`현장 ${s.name} 등록을 삭제할까요? (캐시·라벨 파일은 남습니다)`)) return; await api('sites/delete', null, { name: s.name }); route(); } }, '삭제')))));
    st.append(stb);
    sp.append(h('div', { class: 'table-wrap' }, st), form.el);
    main.append(sp);
    if (!sites.ffmpeg) main.append(h('p', { class: 'warn' }, 'ffmpeg를 찾지 못했습니다. 클립은 브라우저에서 재생되지 않고 MKV 저장만 가능합니다.'));
    if (!state.site) return;
    const local = await api('days', { site: state.site, detail: 1 });
    // host roles
    const hp = h('div', { class: 'panel' }, h('h3', null, '호스트 구분 (현장 / 개발)'), h('p', { class: 'note' }, '귀속 규칙: NAS의 호스트 폴더(raw/<host>/날짜) > 아래 로컬 캐시 표의 날짜별 "소유 장비" 지정 > manifest의 source_host > 현장 설정의 기본 호스트(폴더·manifest 없이 올라온 날짜) 순서입니다. 이 PC의 호스트명은 자동으로 "개발", 나머지는 "현장"으로 보며 여기서 바꿀 수 있습니다. 기본 화면은 현장 데이터만 보여줍니다. 로컬 캐시 삭제는 이 PC의 복사본만 지우고 NAS는 건드리지 않습니다.'));
    const ht = h('table', null, h('thead', null, h('tr', null, ['호스트', '캐시 날짜', '구분', '', ''].map(t => h('th', null, t)))));
    const htb = h('tbody');
    (local.hosts || []).forEach(x => {
      const sel = h('select', null, [['auto', '자동'], ['field', '현장'], ['dev', '개발']].map(([v, t]) => h('option', { value: v, selected: (x.explicit ? x.role : 'auto') === v ? 'selected' : null }, t)));
      sel.addEventListener('change', async () => { try { await api('hosts/role', null, { site: state.site, host: x.name, role: sel.value }); toast(`${x.name}: ${sel.options[sel.selectedIndex].text}`); const d = await api('days', { site: state.site }); applyHosts(d.hosts); route(); } catch (e) { toast(e.message, true); } });
      htb.append(h('tr', null, h('td', null, x.name), h('td', { class: 'num' }, String(x.days)), h('td', null, badge(x.role === 'field' ? 'camera' : x.role === 'dev' ? 'muted' : 'partial', ROLE_LABEL[x.role] || x.role), x.explicit ? '' : h('span', { class: 'note' }, ' (자동)')), h('td', null, sel),
        h('td', null, h('button', { class: 'small', onclick: async () => { if (!confirm(`${x.name}의 로컬 캐시(${x.days}일)를 지울까요? NAS 원본과 라벨은 남습니다.`)) return; try { await api('cache/delete', null, { site: state.site, host: x.name }); toast('로컬 캐시 삭제'); route(); } catch (e) { toast(e.message, true); } } }, '로컬 캐시 삭제'))));
    });
    ht.append(htb);
    hp.append(h('div', { class: 'table-wrap' }, ht));
    main.append(hp);
    // local days
    const lp = h('div', { class: 'panel' }, h('h3', null, '로컬 캐시 날짜'));
    if (!local.days.length) lp.append(h('p', { class: 'note' }, '아직 내려받은 날짜가 없습니다.'));
    else {
      const lt = h('table', null, h('thead', null, h('tr', null, ['호스트', '날짜', '샤드', '레코드', 'raw 창', '미디어(기록)', '감시 커버리지', '레이더 커버리지', '레이더 소스', '상태', '소유 장비', ''].map(t => h('th', null, t)))));
      const ltb = h('tbody');
      const site = sites.sites.find(x => x.name === state.site) || {};
      const ownerCell = d => {
        if (d.layout === 'per_host') return h('span', { class: 'note' }, '폴더로 확정');
        const owners = Array.from(new Set([...(local.hosts || []).map(x => x.name), site.default_host].filter(Boolean)));
        const current = (site.day_owners || {})[d.day] || '';
        const sel = h('select', null, h('option', { value: '', selected: current ? null : 'selected' }, d.has_manifest ? 'manifest 기준' : `기본 (${site.default_host || 'unknown-host'})`), owners.map(o => h('option', { value: o, selected: current === o ? 'selected' : null }, o)));
        sel.addEventListener('change', async () => { try { const r = await api('days/owner', null, { site: state.site, day: d.day, from_host: d.host, host: sel.value }); toast(`${d.day} → ${r.host}`); const dd = await api('days', { site: state.site }); applyHosts(dd.hosts); route(); } catch (e) { toast(e.message, true); } });
        return sel;
      };
      local.days.forEach(d => ltb.append(h('tr', null, h('td', null, d.host), h('td', null, h('a', { href: `#/day/${encodeURIComponent(d.host)}/${d.day}` }, d.day)), h('td', { class: 'num' }, String(d.event_files)), h('td', { class: 'num' }, String(d.records ?? '—')), h('td', { class: 'num' }, String(d.raw_windows ?? '—')), h('td', { class: 'num' }, String(d.media ?? '—')), h('td', { class: 'num' }, fmtDur(d.monitoring_seconds)), h('td', { class: 'num' }, fmtDur(d.radar_seconds)), h('td', null, d.radar_source || '—'),
        h('td', null, d.partial ? badge('partial', 'manifest 없음') : d.events_complete ? badge('person', '완료') : badge('unsure', '부분'), d.media_complete ? ' ' : '', d.media_complete ? badge('muted', '미디어 전체') : ''),
        h('td', null, ownerCell(d)),
        h('td', null, h('button', { class: 'small', onclick: async () => { await api('index/rebuild', null, { site: state.site, host: d.host, day: d.day }); toast('다이제스트 재계산 완료'); route(); } }, '재계산')))));
      lt.append(ltb);
      lp.append(h('div', { class: 'table-wrap' }, lt));
    }
    main.append(lp);
    // remote days
    const rp = h('div', { class: 'panel' }, h('h3', null, 'NAS 날짜 목록'));
    const listBox = h('div');
    const prog = h('div');
    const btn = h('button', { class: 'primary', onclick: async () => { btn.disabled = true; listBox.innerHTML = '<p class="note">NAS에 접속하는 중…</p>'; try { const r = await api('nas/days', { site: state.site }); renderRemote(r.days); } catch (e) { listBox.innerHTML = `<p class="err">${esc(e.message)}</p>`; } btn.disabled = false; } }, 'NAS 목록 불러오기');
    rp.append(h('div', { class: 'side-actions' }, btn, h('span', { class: 'note' }, '레거시 raw/YYYY-MM-DD 와 호스트별 raw/<host>/YYYY-MM-DD 두 구조를 모두 읽습니다.')), listBox, prog);
    main.append(rp);
    function renderRemote(days) {
      listBox.innerHTML = '';
      if (!days.length) { listBox.append(h('p', { class: 'note' }, 'NAS raw 폴더에 날짜가 없습니다.')); return; }
      const t = h('table', null, h('thead', null, h('tr', null, [h('input', { type: 'checkbox', onchange: e => $$('input[type=checkbox][data-day]', t).forEach(c => { c.checked = e.target.checked; }) }), '호스트', '날짜', '구조', '파일', '이벤트 샤드', '미디어', '크기', 'manifest', '로컬'].map(x => h('th', null, x)))));
      const tb = h('tbody');
      days.forEach(d => tb.append(h('tr', null, h('td', null, h('input', { type: 'checkbox', 'data-day': JSON.stringify(d) })), h('td', null, d.host), h('td', null, d.day), h('td', null, d.layout === 'per_host' ? '호스트별' : '레거시'), h('td', { class: 'num' }, String(d.files)), h('td', { class: 'num' }, String(d.event_files)), h('td', { class: 'num' }, String(d.media_files)), h('td', { class: 'num' }, (d.bytes / 1e6).toFixed(1) + ' MB'), h('td', null, d.has_manifest ? badge('person', '있음') : badge('unsure', '없음(부분 업로드)')), h('td', null, d.cached_events ? badge('muted', d.cached_media ? '이벤트+미디어' : '이벤트') : ''))));
      t.append(tb);
      const media = h('input', { type: 'checkbox' });
      listBox.append(h('div', { class: 'table-wrap' }, t), h('div', { class: 'side-actions' }, h('button', { class: 'primary', onclick: async () => { const sel = $$('input[type=checkbox][data-day]:checked', t).map(c => JSON.parse(c.getAttribute('data-day'))); if (!sel.length) { toast('날짜를 선택하세요', true); return; } try { await api('sync', null, { site: state.site, days: sel, media: media.checked }); pollSync(); } catch (e) { toast(e.message, true); } } }, '선택 날짜 내려받기'), h('label', null, media, ' 스냅샷/클립도 미리 내려받기 (기본은 볼 때 개별 다운로드)')));
    }
    async function pollSync() {
      const bar = h('div', { class: 'progress' }, h('div'));
      const txt = h('div', { class: 'note' });
      prog.innerHTML = ''; prog.append(bar, txt, h('button', { class: 'small', onclick: () => api('sync/cancel', null, {}) }, '중지'));
      const timer = setInterval(async () => {
        try {
          const s = await api('sync/status');
          bar.firstChild.style.width = (s.total ? (s.done / s.total * 100) : 0) + '%';
          txt.textContent = `${s.done}/${s.total} · ${s.current || ''} · ${(s.bytes_done / 1e6).toFixed(1)} MB` + (s.errors.length ? ' · 오류 ' + s.errors.join('; ') : '');
          if (!s.running) { clearInterval(timer); toast(s.errors.length ? '동기화 완료 (오류 있음)' : '동기화 완료'); route(); }
        } catch (e) { clearInterval(timer); txt.textContent = e.message; }
      }, 1000);
    }
  };

  function siteForm(site) {
    const fields = [['name', '이름(영문 슬러그)', 'text'], ['label', '표시명', 'text'], ['nas_host', 'NAS 호스트 (bare hostname)', 'text'], ['nas_port', '포트', 'number'], ['nas_username', 'SFTP 계정', 'text'], ['nas_password', '비밀번호 (비우면 기존 값 유지)', 'password'], ['nas_folder', '공유 폴더 경로', 'text'], ['known_hosts_path', 'known_hosts 경로', 'text'], ['timezone_name', '시간대', 'text'], ['raw_subdir', 'raw 하위 폴더', 'text'], ['default_host', '기본 호스트 (호스트 폴더·manifest 없이 올라온 날짜의 소유 장비 = 현장기 호스트명)', 'text']];
    const inputs = {};
    const grid = h('div', { class: 'form-grid' }, fields.map(([k, l, t]) => { inputs[k] = h('input', { type: t, name: k }); return h('label', null, l, inputs[k]); }));
    const envRow = h('div', { class: 'side-actions' }, h('input', { type: 'text', id: 'env-path', value: '.env', style: 'width:220px' }), h('input', { type: 'text', id: 'env-name', placeholder: '현장 이름', style: 'width:160px' }), h('button', { class: 'small', onclick: async () => { try { await api('sites/import-env', null, { env_path: $('#env-path').value, name: $('#env-name').value || 'default' }); toast('.env에서 가져왔습니다'); route(); } catch (e) { toast(e.message, true); } } }, '.env의 SYNOLOGY_NAS_* 가져오기'));
    const el = h('details', { open: site ? 'open' : null }, h('summary', null, '현장 추가 / 편집'), grid, h('div', { class: 'side-actions' }, h('button', { class: 'primary small', onclick: async () => { const body = {}; Object.entries(inputs).forEach(([k, i]) => { body[k] = i.value; }); try { await api('sites', null, body); toast('저장했습니다'); route(); } catch (e) { toast(e.message, true); } } }, '저장')), h('h3', null, '또는 배포 .env에서 가져오기'), envRow);
    const fill = s => { Object.entries(inputs).forEach(([k, i]) => { i.value = s && s[k] !== undefined ? s[k] : (k === 'nas_port' ? 22 : k === 'known_hosts_path' ? '~/.ssh/known_hosts' : k === 'timezone_name' ? 'Asia/Seoul' : k === 'raw_subdir' ? 'raw' : ''); }); inputs.nas_password.value = ''; el.open = true; el.scrollIntoView({ behavior: 'smooth' }); };
    fill(site);
    return { el, fill };
  }

  // ------------------------------------------------------------------ shell
  function applyHosts(hosts) {
    state.hosts = (hosts || []).map(x => typeof x === 'string' ? { name: x, role: 'field' } : x);
    const sel = $('#host-select');
    const cur = state.host || 'field';
    sel.innerHTML = '';
    const nField = state.hosts.filter(x => x.role === 'field').length, nDev = state.hosts.filter(x => x.role === 'dev').length, nUnk = state.hosts.filter(x => x.role === 'unknown').length;
    sel.append(h('option', { value: 'field', selected: cur === 'field' ? 'selected' : null }, `현장만 (${nField})`));
    sel.append(h('option', { value: 'all', selected: cur === 'all' ? 'selected' : null }, '전체'));
    if (nDev) sel.append(h('option', { value: 'dev', selected: cur === 'dev' ? 'selected' : null }, `개발기만 (${nDev})`));
    if (nUnk) sel.append(h('option', { value: 'unknown', selected: cur === 'unknown' ? 'selected' : null }, `미분류 (${nUnk})`));
    state.hosts.forEach(x => sel.append(h('option', { value: x.name, selected: x.name === cur ? 'selected' : null }, `${x.name} · ${ROLE_LABEL[x.role] || x.role}`)));
  }
  function paramsToForm() {
    $$('[data-param]').forEach(el => { const k = el.getAttribute('data-param'); if (el.type === 'checkbox') el.checked = !!state.params[k]; else el.value = state.params[k] ?? ''; });
  }
  function formToParams() {
    const p = Object.assign({}, state.params);
    $$('[data-param]').forEach(el => { const k = el.getAttribute('data-param'); if (el.type === 'checkbox') p[k] = el.checked; else if (el.type === 'number') p[k] = el.value === '' ? DEFAULT_PARAMS[k] : Number(el.value); else p[k] = el.value.trim(); });
    state.params = p;
  }
  async function loadSites() {
    const data = await api('sites');
    state.sites = data.sites;
    const sel = $('#site-select');
    sel.innerHTML = '';
    if (!state.sites.length) { sel.append(h('option', { value: '' }, '(현장 없음)')); state.site = null; return; }
    if (!state.site || !state.sites.some(s => s.name === state.site)) state.site = state.sites[0].name;
    state.sites.forEach(s => sel.append(h('option', { value: s.name, selected: s.name === state.site ? 'selected' : null }, s.label || s.name)));
    const site = state.sites.find(s => s.name === state.site);
    if (site) state.tz = site.timezone_name || state.tz;
  }
  function parseHash() {
    const raw = location.hash.replace(/^#\/?/, '') || 'overview';
    const [pathPart, queryPart] = raw.split('?');
    const parts = pathPart.split('/');
    const query = {};
    if (queryPart) queryPart.split('&').forEach(kv => { const [k, v] = kv.split('='); if (k) query[decodeURIComponent(k)] = decodeURIComponent(v || ''); });
    return { view: parts[0], args: parts.slice(1), query };
  }
  const STAGE_LABEL = { idle: '준비 중', warm: '다이제스트 준비', digest: '원본 JSONL 읽기·다이제스트 계산', episodes: '에피소드 재구성', overview: '개요 집계', sync: 'NAS 동기화' };
  let routeSeq = 0;
  function loadingPanel(title) {
    const bar = h('div', { class: 'progress' }, h('div'));
    const text = h('div', { class: 'note', style: 'margin-top:8px' }, '서버에 요청 중…');
    const el = h('div', { class: 'panel loading' }, h('div', { style: 'font-weight:600;margin-bottom:8px' }, title), bar, text,
      h('p', { class: 'note', style: 'margin-top:10px' }, '처음 열 때는 날짜별 원본 JSONL을 읽어 다이제스트를 만듭니다(하루 3만 레코드 ≈ 1~2초). 한 번 만든 다이제스트는 디스크에 캐시되어 다음부터는 바로 열립니다.'));
    el.update = p => {
      const label = STAGE_LABEL[p.stage] || p.stage;
      const pct = p.total ? Math.round(100 * (p.done || 0) / p.total) : null;
      bar.firstChild.style.width = (pct === null ? 30 : pct) + '%';
      bar.firstChild.classList.toggle('indeterminate', pct === null);
      text.textContent = `${label}${p.detail ? ' · ' + p.detail : ''}${pct !== null ? ` · ${p.done}/${p.total} (${pct}%)` : ''}`;
    };
    return el;
  }
  async function withProgress(main, title, task) {
    const panel = loadingPanel(title);
    main.append(panel);
    let stop = false;
    (async () => {
      await new Promise(r => setTimeout(r, 250));
      while (!stop) {
        try { const p = await api('progress'); if (!stop) panel.update(p); } catch (e) { /* ignore */ }
        await new Promise(r => setTimeout(r, 400));
      }
    })();
    try { return await task(); }
    finally { stop = true; panel.remove(); }
  }
  async function route() {
    const { view, args, query } = parseHash();
    const main = $('#main');
    if (main._cleanup) { main._cleanup(); main._cleanup = null; }
    main.innerHTML = '';
    $$('#nav a').forEach(a => a.classList.toggle('active', a.getAttribute('data-route') === view));
    const fn = views[view] || views.overview;
    if (!state.site && view !== 'data' && view !== 'dictionary') {
      main.append(h('div', { class: 'empty' }, '등록된 현장(NAS)이 없습니다. ', h('a', { href: '#/data' }, '데이터 · NAS 설정'), ' 페이지에서 .env를 가져오거나 주소를 입력하세요.'));
      return;
    }
    const seq = ++routeSeq;
    const staging = h('div');
    const titles = { overview: '개요를 불러오는 중', day: '타임라인을 불러오는 중', episodes: '에피소드 목록을 불러오는 중', review: '에피소드를 불러오는 중', compare: '비교 지표를 계산하는 중', tuning: '임계값 스윕을 준비하는 중', dictionary: '데이터 사전', data: '데이터 · NAS 설정' };
    try {
      await withProgress(main, titles[view] || '불러오는 중', () => fn(staging, args, query));
      if (seq !== routeSeq) return;  // user navigated away meanwhile
      main.append(...Array.from(staging.childNodes));
    } catch (e) { if (seq === routeSeq) main.append(h('div', { class: 'panel err' }, '오류: ' + e.message)); }
  }
  function bindShell() {
    $('#site-select').addEventListener('change', e => { state.site = e.target.value; state.host = 'field'; const s = state.sites.find(x => x.name === state.site); if (s) state.tz = s.timezone_name; save(); route(); });
    $('#host-select').addEventListener('change', e => { state.host = e.target.value; save(); route(); });
    $('#range-from').value = state.from || ''; $('#range-to').value = state.to || '';
    $('#range-from').addEventListener('change', e => { state.from = e.target.value; save(); route(); });
    $('#range-to').addEventListener('change', e => { state.to = e.target.value; save(); route(); });
    $('#btn-range-week').addEventListener('click', () => { const d = new Date(); const to = d.toISOString().slice(0, 10); d.setDate(d.getDate() - 7); state.from = d.toISOString().slice(0, 10); state.to = to; $('#range-from').value = state.from; $('#range-to').value = state.to; save(); route(); });
    $('#btn-range-all').addEventListener('click', () => { state.from = ''; state.to = ''; $('#range-from').value = ''; $('#range-to').value = ''; save(); route(); });
    $('#btn-params-apply').addEventListener('click', () => { formToParams(); save(); route(); });
    $('#btn-params-reset').addEventListener('click', () => { state.params = Object.assign({}, DEFAULT_PARAMS); paramsToForm(); save(); route(); });
    $('#reviewer-name').value = state.reviewer || '';
    $('#reviewer-name').addEventListener('change', e => { state.reviewer = e.target.value.trim(); save(); });
    window.addEventListener('hashchange', route);
    $('#btn-nas-update').addEventListener('click', async () => {
      if (!state.site) { toast('현장을 먼저 등록하세요', true); return; }
      try { await api('sync/update', null, { site: state.site }); toast('NAS 최신화 시작'); watchUpdate(true); }
      catch (e) { toast(e.message, true); }
    });
    watchUpdate(false);
  }
  let updateTimer = null, lastSeenUpdate = null;
  function fmtAgo(iso) {
    if (!iso) return '';
    const s = Math.max(0, (Date.now() - new Date(iso).getTime()) / 1000);
    if (s < 60) return '방금'; if (s < 3600) return Math.floor(s / 60) + '분 전'; return Math.floor(s / 3600) + '시간 전';
  }
  function watchUpdate(fast) {
    // Poll sync status: every second while a sync runs, otherwise every 30 s; re-render when new data landed.
    if (updateTimer) clearInterval(updateTimer);
    const tick = async () => {
      let s;
      try { s = await api('sync/status'); } catch (e) { return; }
      const el = $('#nas-update-status');
      const btn = $('#btn-nas-update');
      btn.disabled = !!s.running;
      if (s.running) {
        el.textContent = `동기화 중 ${s.done}/${s.total} ${s.current || ''}`;
        return;
      }
      const lu = s.last_update || {};
      const autoTxt = s.auto_minutes > 0 ? ` · 자동 ${s.auto_minutes}분` : '';
      if (lu.finished_at) {
        const n = (lu.synced || []).length;
        el.textContent = `최신화 ${fmtAgo(lu.finished_at)} · 새 데이터 ${n}일${lu.errors && lu.errors.length ? ' · 오류' : ''}${autoTxt}`;
        el.title = (lu.planned || []).join(', ') + (lu.errors && lu.errors.length ? '\n' + lu.errors.join('\n') : '');
        if (lastSeenUpdate && lastSeenUpdate !== lu.finished_at && n > 0) {
          toast(`NAS에서 ${n}일치 새 데이터를 반영했습니다`);
          if (parseHash().view !== 'review') route();
        }
        lastSeenUpdate = lu.finished_at;
      } else {
        el.textContent = (s.auto_minutes > 0 ? `자동 ${s.auto_minutes}분` : '수동');
      }
      if (fast) { watchUpdate(false); }
    };
    tick();
    updateTimer = setInterval(tick, fast ? 1000 : 30000);
  }
  (async function boot() {
    paramsToForm();
    bindShell();
    $('#main').innerHTML = '';
    $('#main').append(loadingPanel('현장 목록을 불러오는 중'));
    try { await loadSites(); } catch (e) { toast('현장 목록을 불러오지 못했습니다: ' + e.message, true); }
    if (state.site) { try { const d = await api('days', { site: state.site }); applyHosts(d.hosts); } catch (e) { /* ignore */ } }
    route();
  })();
})();
