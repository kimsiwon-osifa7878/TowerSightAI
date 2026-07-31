from __future__ import annotations

import re
from html.parser import HTMLParser
from pathlib import Path


PROTOTYPE_PATH = Path("docs/design/towersightai-ui-prototype.html")


class PrototypeParser(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.ids: set[str] = set()
        self.operator_actions: list[str] = []
        self.operator_labels: list[str] = []
        self._operator_action: str | None = None
        self._operator_label_parts: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        attributes = dict(attrs)
        element_id = attributes.get("id")
        if element_id:
            self.ids.add(element_id)
        if tag == "button" and attributes.get("data-operator-action"):
            self._operator_action = attributes["data-operator-action"]
            self._operator_label_parts = []
            self.operator_actions.append(self._operator_action)

    def handle_data(self, data: str) -> None:
        if self._operator_action is not None:
            self._operator_label_parts.append(data)

    def handle_endtag(self, tag: str) -> None:
        if tag == "button" and self._operator_action is not None:
            self.operator_labels.append("".join(self._operator_label_parts).strip())
            self._operator_action = None
            self._operator_label_parts = []


def _prototype() -> tuple[str, PrototypeParser]:
    html = PROTOTYPE_PATH.read_text(encoding="utf-8")
    parser = PrototypeParser()
    parser.feed(html)
    return html, parser


def test_html_prototype_contains_user_and_operator_mode_surfaces():
    html, parser = _prototype()

    assert {
        "user-screen",
        "operator-screen",
        "operator-entry-hotspot",
        "operator-shell",
        "operator-camera-grid",
        "operator-settings",
    } <= parser.ids
    assert 'id="operator-screen"' in html
    assert 'id="operator-screen" aria-label="TowerSightAI 운영모드 시안" hidden' in html
    assert 'class="operator-entry-hotspot"' in html
    assert ".operator-entry-hotspot {" in html
    assert "background: transparent;" in html
    assert "color: transparent;" in html


def test_hidden_operator_entry_requires_completed_two_second_hold():
    html, _parser = _prototype()

    assert ".mode-screen[hidden]" in html
    assert "display: none !important;" in html
    assert "const OPERATOR_HOLD_MS = 2000;" in html
    assert 'operatorEntryHotspot.addEventListener("pointerdown"' in html
    assert 'operatorEntryHotspot.addEventListener("pointermove"' in html
    assert '"pointerup", "pointercancel", "lostpointercapture"' in html
    assert 'setMode("operator")' in html
    assert "userScreen.hidden = operatorMode;" in html
    assert "operatorScreen.hidden = !operatorMode;" in html
    assert "if (!isInside) cancelOperatorHold();" in html


def test_user_mode_is_dark_camera_first_and_uses_large_action_overlays():
    html, _parser = _prototype()

    assert "#user-screen {" in html
    assert "background: #030609;" in html
    assert "#user-screen .camera-workspace" in html
    assert "position: absolute;" in html
    assert "inset: 0;" in html
    assert "#user-screen .instruction-band" in html
    assert "top: 22px;" in html
    assert "right: 24px;" in html
    assert "background: rgba(2, 8, 12, 0.5);" in html
    assert "font-size: clamp(52px, 4.7vw, 86px);" in html
    assert "inset: auto 0 46px 0;" in html
    assert "#user-screen .brand" in html
    assert "font-size: 15px;" in html
    assert "DESIGN PROTOTYPE · PLC BLOCKED" in html
    for action in (
        'title: "천천히 진입"',
        'title: "오른쪽 이동"',
        'title: "정지"',
        'title: "주차기 밖으로 이동"',
        'title: "즉시 밖으로 이동"',
    ):
        assert action in html


def test_operator_mode_matches_current_pyqt_menu_contract():
    _html, parser = _prototype()

    assert parser.operator_labels == [
        "사용자모드",
        "전체 카메라",
        "카메라 설정",
        "이전 AI Detection",
        "차량 전용 검출",
        "번호판 이미지 LPR",
        "정면카메라LPR",
        "사람 존재 감지",
        "차량 진입 시뮬레이션",
        "EMPTY",
        "EMPTY",
    ]
    assert parser.operator_actions.count("empty") == 2
    assert "user-mode" in parser.operator_actions


def test_operator_prototype_actions_remain_non_authoritative_and_block_ok():
    html, _parser = _prototype()

    assert "DESIGN PROTOTYPE · AI/PLC OUTPUT BLOCKED" in html
    assert "FINAL OK BLOCKED" in html
    assert "HTML 목업은 AI를 실행하지 않습니다 · PLC OK 차단" in html
    assert "시뮬레이션은 PLC OK를 절대 허용하지 않습니다" in html
    assert "상태 변경 없음 · 실제 동작 없음 · PLC OK 차단" in html
    assert "설정은 저장·활성화되지 않습니다 · PLC OK 차단" in html


def test_operator_prototype_contains_only_masked_rtsp_examples():
    html, _parser = _prototype()
    rtsp_urls = re.findall(r"rtsp://[^<\s]+", html)

    assert rtsp_urls
    assert all("rtsp://***:***@" in url for url in rtsp_urls)
    assert "/home/" not in html
    assert "BEGIN OPENSSH PRIVATE KEY" not in html
