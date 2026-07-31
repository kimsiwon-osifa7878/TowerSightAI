from __future__ import annotations

from collections.abc import Mapping

from PyQt6.QtCore import QEvent, QTimer, Qt, pyqtSignal
from PyQt6.QtGui import QFont, QMouseEvent, QResizeEvent
from PyQt6.QtWidgets import (
    QFrame,
    QGridLayout,
    QHBoxLayout,
    QLabel,
    QSizePolicy,
    QVBoxLayout,
    QWidget,
)

from towersightai.config.settings import CameraRole
from towersightai.ui.model import DriverDisplayModel, DriverLayout, DriverTone


OPERATOR_HOLD_MS = 2000
OPERATOR_HOTSPOT_SIZE = 72
DRIVER_BOTTOM_STRIP_HEIGHT = 42
DRIVER_REFERENCE_WIDTH = 1920
DRIVER_REFERENCE_HEIGHT = 1024
DRIVER_REFERENCE_ASPECT = DRIVER_REFERENCE_WIDTH / DRIVER_REFERENCE_HEIGHT


class OperatorEntryHotspot(QWidget):
    activated = pyqtSignal()

    def __init__(self, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("operatorEntryHotspot")
        self.setFixedSize(OPERATOR_HOTSPOT_SIZE, OPERATOR_HOTSPOT_SIZE)
        self.setAttribute(Qt.WidgetAttribute.WA_AcceptTouchEvents, True)
        self.setMouseTracking(True)
        self._holding = False
        self._completed = False
        self._timer = QTimer(self)
        self._timer.setSingleShot(True)
        self._timer.setInterval(OPERATOR_HOLD_MS)
        self._timer.timeout.connect(self._complete_hold)

    @property
    def holding(self) -> bool:
        return self._holding

    def begin_hold(self) -> None:
        self.cancel_hold()
        self._holding = True
        self._completed = False
        self._timer.start()

    def cancel_hold(self) -> None:
        self._timer.stop()
        self._holding = False
        self._completed = False

    def _complete_hold(self) -> None:
        if not self._holding:
            return
        self._holding = False
        self._completed = True
        self.activated.emit()

    def mousePressEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self.begin_hold()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event: QMouseEvent) -> None:
        if self._holding and not self.rect().contains(event.position().toPoint()):
            self.cancel_hold()
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event: QMouseEvent) -> None:
        if event.button() is Qt.MouseButton.LeftButton:
            self.cancel_hold()
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def leaveEvent(self, event: QEvent) -> None:
        if self._holding:
            self.cancel_hold()
        super().leaveEvent(event)

    def event(self, event: QEvent) -> bool:
        if event.type() is QEvent.Type.TouchBegin:
            points = event.points()
            if len(points) == 1 and self.rect().contains(points[0].position().toPoint()):
                self.begin_hold()
            else:
                self.cancel_hold()
            event.accept()
            return True
        if event.type() is QEvent.Type.TouchUpdate:
            points = event.points()
            if len(points) != 1 or not self.rect().contains(points[0].position().toPoint()):
                self.cancel_hold()
            event.accept()
            return True
        if event.type() in {QEvent.Type.TouchEnd, QEvent.Type.TouchCancel}:
            self.cancel_hold()
            event.accept()
            return True
        return super().event(event)


class DriverView(QWidget):
    operator_requested = pyqtSignal()

    def __init__(
        self,
        camera_widgets: Mapping[CameraRole, QWidget],
        parent: QWidget | None = None,
        *,
        preview_mode: bool = False,
    ) -> None:
        super().__init__(parent)
        self.setObjectName("driverView")
        self.camera_widgets = dict(camera_widgets)
        self.preview_mode = preview_mode
        self.display: DriverDisplayModel | None = None
        self.layout_revision = 0
        self._layout_key: tuple[DriverLayout, tuple[CameraRole, ...], CameraRole | None] | None = None

        self.camera_area = QWidget(self)
        self.camera_area.setObjectName("driverCameraArea")
        self.camera_grid = QGridLayout(self.camera_area)
        self.camera_grid.setContentsMargins(0, 0, 0, 0)
        self.camera_grid.setSpacing(4)

        self.instruction_panel = QFrame(self)
        self.instruction_panel.setObjectName("driverInstructionPanel")
        self.instruction_panel.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        instruction_layout = QHBoxLayout(self.instruction_panel)
        instruction_layout.setContentsMargins(20, 14, 20, 14)
        instruction_layout.setSpacing(18)

        self.symbol_label = QLabel("P")
        self.symbol_label.setObjectName("driverSymbolLabel")
        self.symbol_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
        self.symbol_label.setSizePolicy(QSizePolicy.Policy.Fixed, QSizePolicy.Policy.Fixed)
        instruction_layout.addWidget(self.symbol_label)

        copy_layout = QVBoxLayout()
        copy_layout.setContentsMargins(0, 0, 0, 0)
        copy_layout.setSpacing(4)
        self.headline_label = QLabel("진입 준비")
        self.headline_label.setObjectName("driverHeadlineLabel")
        self.headline_label.setWordWrap(False)
        self.detail_label = QLabel("안내가 표시되면 천천히 진입하세요.")
        self.detail_label.setObjectName("driverDetailLabel")
        self.detail_label.setWordWrap(False)
        copy_layout.addStretch(1)
        copy_layout.addWidget(self.headline_label)
        copy_layout.addWidget(self.detail_label)
        copy_layout.addStretch(1)
        instruction_layout.addLayout(copy_layout, 1)

        self.blocking_label = QLabel("최종 OK 차단")
        self.blocking_label.setObjectName("driverBlockingLabel")
        self.blocking_label.setWordWrap(True)
        self.blocking_label.setAlignment(Qt.AlignmentFlag.AlignVCenter | Qt.AlignmentFlag.AlignLeft)
        self.blocking_label.setMinimumWidth(220)
        self.blocking_label.setMaximumWidth(290)
        instruction_layout.addWidget(self.blocking_label)

        self.bottom_strip = QFrame(self)
        self.bottom_strip.setObjectName("driverBottomStrip")
        self.bottom_strip.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        bottom_layout = QHBoxLayout(self.bottom_strip)
        bottom_layout.setContentsMargins(22, 4, 22, 4)
        bottom_layout.setSpacing(14)
        self.brand_label = QLabel("TowerSightAI")
        self.brand_label.setObjectName("driverBrandLabel")
        self.stage_label = QLabel("입차 대기")
        self.stage_label.setObjectName("driverStageLabel")
        self.status_label = QLabel("최종 OK 차단")
        self.status_label.setObjectName("driverStatusLabel")
        self.status_label.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        bottom_layout.addWidget(self.brand_label)
        bottom_layout.addWidget(self.stage_label)
        bottom_layout.addStretch(1)
        bottom_layout.addWidget(self.status_label)

        self.operator_hotspot = OperatorEntryHotspot(self)
        self.operator_hotspot.activated.connect(self.operator_requested)
        if preview_mode:
            self.operator_hotspot.setEnabled(False)
            self.operator_hotspot.hide()
        self.operator_hotspot.raise_()
        self._apply_scale(DRIVER_REFERENCE_WIDTH)

    def apply_display(
        self,
        display: DriverDisplayModel,
        *,
        apply_layout: bool = True,
        force_layout: bool = False,
    ) -> None:
        previous = self.display
        self.display = display
        if previous != display:
            self.symbol_label.setText(display.symbol)
            self.headline_label.setText(display.headline)
            detail = display.detail
            if display.masked_plate_text:
                detail = f"{detail} · 번호판 {display.masked_plate_text}"
            self.detail_label.setText(detail)
            self.blocking_label.setText(display.blocking_reason)
            self.stage_label.setText(display.stage_label)
            prefix = "TEST · " if display.simulated else ""
            self.status_label.setText(f"{prefix}{display.tone.value} · 최종 OK {'허용' if display.can_show_final_ok else '차단'}")
        tone = display.tone.value.lower()
        if self.instruction_panel.property("tone") != tone:
            self.instruction_panel.setProperty("tone", tone)
            self.instruction_panel.style().unpolish(self.instruction_panel)
            self.instruction_panel.style().polish(self.instruction_panel)
        layout_key = (display.layout, display.visible_roles, display.primary_role)
        if apply_layout and (force_layout or self._layout_key != layout_key):
            self._apply_camera_layout(display)

    def detach_cameras(self) -> None:
        while self.camera_grid.count():
            item = self.camera_grid.takeAt(0)
            widget = item.widget()
            if widget is not None:
                widget.hide()
        self._layout_key = None

    def _apply_camera_layout(self, display: DriverDisplayModel) -> None:
        self.detach_cameras()
        for index in range(4):
            self.camera_grid.setColumnStretch(index, 0)
            self.camera_grid.setRowStretch(index, 0)
        for widget in self.camera_widgets.values():
            if hasattr(widget, "set_display_mode"):
                widget.set_display_mode("cover")
            if self.preview_mode:
                widget.setMinimumSize(80, 48)
            else:
                widget.setMinimumSize(260, 160)
            widget.setMaximumSize(16777215, 16777215)

        if display.layout is DriverLayout.FRONT:
            self._add_camera(CameraRole.front, 0, 0)
            self.camera_grid.setColumnStretch(0, 1)
            self.camera_grid.setRowStretch(0, 1)
            self._finish_layout(display)
            return
        if display.layout is DriverLayout.ENTRY:
            self._add_camera(CameraRole.front, 0, 0)
            self._add_camera(CameraRole.ceiling, 0, 1)
            self.camera_grid.setColumnStretch(0, 68)
            self.camera_grid.setColumnStretch(1, 32)
            self.camera_grid.setRowStretch(0, 1)
            self._finish_layout(display)
            return
        if display.layout is DriverLayout.ALIGNMENT:
            self._add_camera(CameraRole.ceiling, 0, 0)
            self._add_camera(CameraRole.front, 0, 1)
            self.camera_grid.setColumnStretch(0, 64)
            self.camera_grid.setColumnStretch(1, 36)
            self.camera_grid.setRowStretch(0, 1)
            self._finish_layout(display)
            return
        if display.layout is DriverLayout.HUMAN and display.primary_role is not None:
            secondary = [role for role in display.visible_roles if role is not display.primary_role]
            self._add_camera(display.primary_role, 0, 0, len(secondary), 1)
            for row, role in enumerate(secondary):
                self._add_camera(role, row, 1)
                self.camera_grid.setRowStretch(row, 1)
            self.camera_grid.setColumnStretch(0, 72)
            self.camera_grid.setColumnStretch(1, 28)
            self._finish_layout(display)
            return

        positions = (
            (CameraRole.ceiling, 0, 0),
            (CameraRole.front, 0, 1),
            (CameraRole.rear_side, 1, 0),
            (CameraRole.opposite_side, 1, 1),
        )
        for role, row, column in positions:
            self._add_camera(role, row, column)
        self.camera_grid.setColumnStretch(0, 1)
        self.camera_grid.setColumnStretch(1, 1)
        self.camera_grid.setRowStretch(0, 1)
        self.camera_grid.setRowStretch(1, 1)
        self._finish_layout(display)

    def _add_camera(self, role: CameraRole, row: int, column: int, row_span: int = 1, column_span: int = 1) -> None:
        widget = self.camera_widgets[role]
        self.camera_grid.addWidget(widget, row, column, row_span, column_span)
        widget.show()

    def _finish_layout(self, display: DriverDisplayModel) -> None:
        self._layout_key = (display.layout, display.visible_roles, display.primary_role)
        self.layout_revision += 1
        self.camera_grid.invalidate()
        self.camera_grid.activate()

    def restore_presentation(self) -> None:
        """Restore geometry and overlay stacking after returning from operator mode."""
        self._sync_presentation_geometry()
        self.camera_area.show()
        self.camera_grid.invalidate()
        self.camera_grid.activate()
        self.instruction_panel.show()
        self.bottom_strip.show()
        if not self.preview_mode:
            self.operator_hotspot.show()
        self.instruction_panel.raise_()
        self.bottom_strip.raise_()
        if not self.preview_mode:
            self.operator_hotspot.raise_()
        self.update()

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        self._sync_presentation_geometry()

    def _sync_presentation_geometry(self) -> None:
        width = self.width()
        height = self.height()
        self._apply_scale(width)
        scale = max(0.4, min(1.0, width / DRIVER_REFERENCE_WIDTH))
        self.camera_area.setGeometry(self.rect())
        horizontal_margin = max(10, round(24 * scale))
        top_margin = max(8, round(22 * scale))
        vertical_padding = max(20, round(28 * scale))
        copy_height = (
            self.headline_label.fontMetrics().height()
            + self.detail_label.fontMetrics().height()
            + max(3, round(4 * scale))
        )
        panel_height = max(
            round(124 * scale),
            self.symbol_label.height() + vertical_padding,
            copy_height + vertical_padding,
        )
        panel_height = min(panel_height, max(1, height - DRIVER_BOTTOM_STRIP_HEIGHT - top_margin))
        self.instruction_panel.setGeometry(
            horizontal_margin,
            top_margin,
            max(1, width - horizontal_margin * 2),
            panel_height,
        )
        bottom_height = max(24, round(DRIVER_BOTTOM_STRIP_HEIGHT * scale))
        self.bottom_strip.setGeometry(
            0,
            max(0, height - bottom_height),
            width,
            bottom_height,
        )
        self.operator_hotspot.move(max(0, width - OPERATOR_HOTSPOT_SIZE), 0)
        self.instruction_panel.raise_()
        self.bottom_strip.raise_()
        if not self.preview_mode:
            self.operator_hotspot.raise_()

    def _apply_scale(self, width: int) -> None:
        minimum_headline = 24 if self.preview_mode else 38
        minimum_symbol = 40 if self.preview_mode else 58
        headline_px = max(minimum_headline, min(86, round(width * 0.047)))
        symbol_size = max(minimum_symbol, min(88, round(width * 0.046)))
        self.symbol_label.setFixedSize(symbol_size, symbol_size)
        self._set_pixel_size(self.headline_label, headline_px, bold=True)
        symbol_minimum = 22 if self.preview_mode else 38
        self._set_pixel_size(
            self.symbol_label,
            max(symbol_minimum, round(symbol_size * 0.64)),
            bold=True,
        )
        detail_minimum = 10 if self.preview_mode else 14
        blocking_minimum = 9 if self.preview_mode else 11
        self._set_pixel_size(
            self.detail_label,
            max(detail_minimum, min(23, round(width * 0.012))),
            bold=True,
        )
        self._set_pixel_size(
            self.blocking_label,
            max(blocking_minimum, min(16, round(width * 0.0083))),
            bold=True,
        )
        self._set_pixel_size(self.brand_label, max(9, round(15 * width / DRIVER_REFERENCE_WIDTH)), bold=True)
        self._set_pixel_size(self.stage_label, max(8, round(12 * width / DRIVER_REFERENCE_WIDTH)), bold=True)
        self._set_pixel_size(self.status_label, max(8, round(11 * width / DRIVER_REFERENCE_WIDTH)), bold=True)

    @staticmethod
    def _set_pixel_size(label: QLabel, size: int, *, bold: bool) -> None:
        font = QFont(label.font())
        font.setPixelSize(size)
        font.setBold(bold)
        label.setFont(font)


class DriverPreviewHost(QWidget):
    """Keep an embedded driver preview at the production 1920x1024 ratio."""

    def __init__(self, view: DriverView, parent: QWidget | None = None) -> None:
        super().__init__(parent)
        self.setObjectName("driverPreviewHost")
        self.view = view
        self.view.setParent(self)

    def resizeEvent(self, event: QResizeEvent) -> None:
        super().resizeEvent(event)
        available_width = max(1, self.width() - 16)
        available_height = max(1, self.height() - 16)
        width = min(available_width, round(available_height * DRIVER_REFERENCE_ASPECT))
        height = min(available_height, round(width / DRIVER_REFERENCE_ASPECT))
        width = min(width, round(height * DRIVER_REFERENCE_ASPECT))
        x = (self.width() - width) // 2
        y = (self.height() - height) // 2
        self.view.setGeometry(x, y, max(1, width), max(1, height))


DRIVER_STYLESHEET = """
    #driverPreviewHost {
        background: #010305;
    }
    #driverView, #driverCameraArea {
        background: #030609;
    }
    #driverInstructionPanel {
        background: rgba(2, 8, 12, 128);
        border: 1px solid rgba(103, 232, 249, 184);
        border-radius: 10px;
    }
    #driverInstructionPanel[tone="danger"] {
        background: rgba(95, 7, 19, 128);
        border: 6px solid #ef233c;
    }
    #driverInstructionPanel[tone="ready"] {
        background: rgba(3, 55, 28, 128);
        border: 2px solid #22c55e;
    }
    #driverInstructionPanel[tone="test"] {
        background: rgba(68, 43, 4, 128);
        border: 2px solid #f59e0b;
    }
    #driverSymbolLabel {
        background: rgba(4, 25, 35, 128);
        border: 5px solid #22d3ee;
        border-radius: 44px;
        color: #f8fafc;
    }
    #driverInstructionPanel[tone="danger"] #driverSymbolLabel {
        background: #ef233c;
        border-color: #ffffff;
    }
    #driverHeadlineLabel {
        background: transparent;
        border: 0;
        color: #ffffff;
    }
    #driverDetailLabel {
        background: transparent;
        border: 0;
        color: #cbd5e1;
    }
    #driverBlockingLabel {
        color: #f8fafc;
        background: rgba(24, 15, 2, 128);
        border: 1px solid rgba(245, 158, 11, 184);
        border-radius: 5px;
        padding: 10px 13px;
    }
    #driverBottomStrip {
        background: rgba(2, 8, 12, 128);
        border-top: 1px solid rgba(148, 163, 184, 46);
    }
    #driverBrandLabel {
        background: transparent;
        border: 0;
        color: #f8fafc;
    }
    #driverStageLabel {
        background: rgba(3, 18, 27, 128);
        color: #a5f3fc;
        border: 1px solid rgba(103, 232, 249, 115);
        border-radius: 10px;
        padding: 3px 8px;
    }
    #driverStatusLabel {
        background: transparent;
        border: 0;
        color: #cbd5e1;
    }
    #operatorEntryHotspot {
        background: transparent;
        border: 0;
    }
"""
