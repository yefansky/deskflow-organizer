"""Qt desktop presentation layer for DESKFLOW.

The legacy Tk implementation applies ``-alpha`` to the whole top-level window.
Windows then fades every final pixel, including file icons and text.  This
module instead uses a per-pixel translucent Qt surface: ``paintEvent`` draws
only the glass plate with alpha while child labels and icons remain fully
opaque.  No colour-key transparency is used, so there is no key-colour fringe
or dark rectangle behind native icons.
"""
from __future__ import annotations

from copy import deepcopy
import os
import queue
import re
import subprocess
from pathlib import Path
from typing import Any, Callable

from PySide6.QtCore import QFileInfo, QPoint, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QCursor, QFont, QIcon, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import (
    QApplication,
    QCheckBox,
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileIconProvider,
    QFormLayout,
    QFrame,
    QGraphicsDropShadowEffect,
    QGridLayout,
    QHBoxLayout,
    QInputDialog,
    QLabel,
    QMenu,
    QMessageBox,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSlider,
    QSpinBox,
    QStyle,
    QToolButton,
    QVBoxLayout,
    QWidget,
)
from shiboken6 import isValid
from win32com.shell import shell, shellcon

from layout_engine import Rect, overlaps, pack_rectangles


PANEL_MODES = ("list", "compact", "cards")
PANEL_MODE_LABELS = {
    "list": "单列清单",
    "compact": "紧凑双列",
    "cards": "纯图标网格",
}
PANEL_MODE_GLYPHS = {"list": "☷", "compact": "≡", "cards": "▦"}


def _with_shadow(widget: QWidget, strength: int = 220) -> None:
    """Give opaque content a small dark halo over bright/busy wallpapers."""
    effect = QGraphicsDropShadowEffect(widget)
    effect.setBlurRadius(4.0)
    effect.setOffset(0.0, 1.0)
    effect.setColor(QColor(0, 0, 0, strength))
    widget.setGraphicsEffect(effect)


def _set_transparent(widget: QWidget) -> None:
    widget.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
    widget.setStyleSheet("background: transparent;")


class HeaderBar(QWidget):
    """Draggable, non-activating panel header."""

    context_requested = Signal(object)

    def __init__(self, panel: "PanelWindow") -> None:
        super().__init__(panel)
        self.panel = panel
        self._start_global: QPoint | None = None
        self._start_panel: QPoint | None = None
        _set_transparent(self)
        self.setFixedHeight(31)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._start_global = event.globalPosition().toPoint()
            self._start_panel = self.panel.pos()
            self.panel.controller.shell.keep_panels_above_desktop_backdrop()
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.context_requested.emit(event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._start_global is not None and self._start_panel is not None:
            delta = event.globalPosition().toPoint() - self._start_global
            wanted = self._start_panel + delta
            x, y = self.panel.controller.bound_position(
                wanted.x(), wanted.y(), self.panel.width(), self.panel.height()
            )
            self.panel.move(x, y)
            self.panel.controller.shell.keep_panels_above_desktop_backdrop()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._start_global is not None:
            self._start_global = self._start_panel = None
            self.panel.controller.finish_panel_geometry(self.panel.category.id)
            event.accept()
            return
        super().mouseReleaseEvent(event)


class TipBubble(QFrame):
    """In-panel summary bubble that survives Explorer desktop reparenting."""

    def __init__(self, panel: "PanelWindow") -> None:
        super().__init__(panel)
        self.panel = panel
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        self.setAttribute(Qt.WidgetAttribute.WA_StyledBackground, True)
        self.setStyleSheet(
            "QFrame { background: rgba(8, 16, 29, 242); border: 1px solid rgba(205,232,255,82);"
            " border-radius: 7px; }"
            "QLabel { color: #f7fbff; background: transparent; border: none; }"
        )
        layout = QVBoxLayout(self)
        layout.setContentsMargins(9, 7, 9, 7)
        self.label = QLabel(self)
        self.label.setWordWrap(True)
        self.label.setTextInteractionFlags(Qt.TextInteractionFlag.NoTextInteraction)
        self.label.setFont(QFont("Microsoft YaHei UI", max(9, panel.controller.font_size())))
        layout.addWidget(self.label)
        self.hide()

    def show_for(self, anchor: QWidget, text: str) -> None:
        if not text.strip() or not anchor.isVisible():
            self.hide()
            return
        width = max(150, min(360, self.panel.width() - 18))
        self.label.setFixedWidth(max(120, width - 18))
        self.label.setText(text)
        self.label.adjustSize()
        self.adjustSize()
        anchor_pos = anchor.mapTo(self.panel, QPoint(0, 0))
        x = max(8, min(anchor_pos.x() + 6, self.panel.width() - self.width() - 8))
        below = anchor_pos.y() + anchor.height() + 4
        y = below if below + self.height() <= self.panel.height() - 8 else anchor_pos.y() - self.height() - 4
        y = max(8, min(y, self.panel.height() - self.height() - 8))
        self.move(x, y)
        self.raise_()
        self.show()


class ItemWidget(QWidget):
    """One opaque icon/text item on a transparent content surface."""

    def __init__(self, panel: "PanelWindow", item: dict[str, Any], mode: str) -> None:
        super().__init__(panel.content)
        self.panel, self.item, self.mode = panel, item, mode
        self.hovered = False
        self.selected = False
        self._press_global: QPoint | None = None
        self._moved = False
        _set_transparent(self)
        self.setCursor(Qt.CursorShape.PointingHandCursor)
        self.setMouseTracking(True)

        compact_height = panel.controller.row_height()
        if mode == "cards":
            self.setFixedHeight(panel.controller.icon_grid_cell_height())
            layout = QVBoxLayout(self)
            layout.setContentsMargins(4, 4, 4, 4)
            layout.setSpacing(0)
            # The icon-grid mode deliberately has no visible filename.  Keep
            # recognition and density through a generous icon, while the same
            # rounded translucent hover used by list modes preserves the new
            # light glass visual language instead of restoring old square tiles.
            icon_size = panel.controller.icon_grid_icon_size()
            icon_label = QLabel(self)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_label.setPixmap(panel.controller.icon_for(item["path"], icon_size).pixmap(icon_size, icon_size))
            icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            layout.addWidget(icon_label, 1)
        else:
            self.setFixedHeight(compact_height)
            layout = QHBoxLayout(self)
            layout.setContentsMargins(4, 1, 5, 1)
            layout.setSpacing(6)
            icon_size = max(20, min(30, round(compact_height * .72)))
            icon_label = QLabel(self)
            icon_label.setFixedSize(icon_size + 2, icon_size + 2)
            icon_label.setAlignment(Qt.AlignmentFlag.AlignCenter)
            icon_label.setPixmap(panel.controller.icon_for(item["path"], icon_size).pixmap(icon_size, icon_size))
            icon_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            layout.addWidget(icon_label)
            name = Path(item["name"]).stem if item["kind"] != "folder" else item["name"]
            text = QLabel(name, self)
            text.setStyleSheet("color: #ffffff; background: transparent;")
            text.setFont(QFont("Microsoft YaHei UI", panel.controller.font_size()))
            text.setSizePolicy(QSizePolicy.Policy.Ignored, QSizePolicy.Policy.Preferred)
            text.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
            _with_shadow(text)
            layout.addWidget(text, 1)

        tip = panel.controller.item_tip(item)
        if mode == "cards":
            display_name = Path(item["name"]).stem if item["kind"] != "folder" else item["name"]
            tip = display_name if tip == item["name"] else f"{display_name}\n{tip}"
        # Qt's native tooltip is a separate top-level window.  DESKFLOW panels
        # are non-activating Explorer desktop children, so Windows can place
        # that tooltip behind the desktop composition surface.  Keep the Tip
        # in the panel's own paint tree instead.
        self.tip_text = tip

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        if self.selected or self.hovered:
            accent = QColor(self.panel.category.color)
            accent.setAlpha(86 if self.selected else 44)
            painter.setPen(Qt.PenStyle.NoPen)
            painter.setBrush(accent)
            painter.drawRoundedRect(self.rect().adjusted(1, 1, -1, -1), 5, 5)
        super().paintEvent(event)

    def enterEvent(self, event) -> None:  # type: ignore[override]
        self.panel.unsetCursor()
        self.hovered = True
        self.update()
        self.panel.schedule_item_tip(self)
        super().enterEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.hovered = False
        self.update()
        self.panel.hide_item_tip(self)
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self._press_global = event.globalPosition().toPoint()
            self._moved = False
            event.accept()
            return
        if event.button() == Qt.MouseButton.RightButton:
            self.panel.controller.select_item(self)
            self.panel.controller.show_item_menu(self, event.globalPosition().toPoint())
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        if self._press_global is not None:
            point = event.globalPosition().toPoint()
            if (point - self._press_global).manhattanLength() > 8:
                self._moved = True
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        if self._press_global is not None:
            destination = event.globalPosition().toPoint()
            moved = self._moved
            self._press_global = None
            self._moved = False
            if moved:
                self.panel.controller.drop_item(self, destination)
            else:
                self.panel.controller.select_item(self)
            event.accept()
            return
        super().mouseReleaseEvent(event)

    def mouseDoubleClickEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            self.panel.controller.open_item(self.item["path"])
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class PanelWindow(QWidget):
    """Per-pixel glass panel.  Only the painted plate is translucent."""

    EDGE = 6

    def __init__(
        self,
        controller: "QtOrganizerUI",
        category: Any,
        items: list[dict[str, Any]],
        mode: str,
        panel_background: str,
        opacity: float,
    ) -> None:
        flags = (
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowDoesNotAcceptFocus
        )
        super().__init__(None, flags)
        self.controller, self.category, self.items, self.mode = controller, category, items, mode
        self.setObjectName(f"deskflow-panel-{category.id}")
        self.setWindowTitle(f"DESKFLOW:{category.id}")
        self.panel_background, self.plate_opacity = panel_background, opacity
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        self.setMouseTracking(True)

        outer = QVBoxLayout(self)
        outer.setContentsMargins(8, 4, 7, 7)
        outer.setSpacing(1)
        self.header = HeaderBar(self)
        self.header.context_requested.connect(
            lambda point: self.controller.show_panel_menu(self.category.id, point)
        )
        header_layout = QHBoxLayout(self.header)
        header_layout.setContentsMargins(2, 1, 0, 1)
        header_layout.setSpacing(4)

        accent = QFrame(self.header)
        accent.setFixedSize(3, 15)
        accent.setStyleSheet(f"background: {category.color}; border-radius: 1px;")
        accent.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(accent)
        title = controller.model.store.data["ui"].get("category_names", {}).get(category.id, category.title)
        title_label = QLabel(title, self.header)
        title_label.setStyleSheet("color: #ffffff; background: transparent;")
        title_label.setFont(QFont("Microsoft YaHei UI", min(14, controller.font_size() + 1), QFont.Weight.DemiBold))
        title_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        _with_shadow(title_label)
        header_layout.addWidget(title_label, 1)

        self.count_label = QLabel(str(len(items)), self.header)
        self.count_label.setStyleSheet(
            "color: #e9f2ff; background: rgba(4, 10, 20, 88);"
            "border: 1px solid rgba(255,255,255,28); border-radius: 8px; padding: 0 6px;"
        )
        self.count_label.setFont(QFont("Segoe UI", 8, QFont.Weight.DemiBold))
        self.count_label.setFixedHeight(18)
        self.count_label.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents, True)
        header_layout.addWidget(self.count_label)

        next_mode = PANEL_MODES[(PANEL_MODES.index(mode) + 1) % len(PANEL_MODES)]
        actions = (
            ("⇅", "排序", lambda: controller.show_sort_menu(category.id, QCursor.pos())),
            (
                PANEL_MODE_GLYPHS[mode],
                f"当前：{PANEL_MODE_LABELS[mode]}；点击切换到：{PANEL_MODE_LABELS[next_mode]}",
                lambda: controller.cycle_mode(category.id),
            ),
            ("↗", "按内容弹性排版", controller.arrange_top_right),
            ("⚙", "外观与屏幕设置", controller.settings),
            ("×", "关闭并恢复原生桌面", controller.close),
        )
        for glyph, tooltip, callback in actions:
            button = QToolButton(self.header)
            button.setText(glyph)
            button.setToolTip(tooltip)
            button.setFixedSize(22, 22)
            color = "#ff9caf" if glyph == "×" else "#f4f8ff"
            button.setStyleSheet(
                f"QToolButton {{ color: {color}; background: transparent; border: none; font-size: 13px; }}"
                "QToolButton:hover { background: rgba(255,255,255,28); border-radius: 4px; }"
            )
            button.clicked.connect(callback)
            header_layout.addWidget(button)
        outer.addWidget(self.header)

        self.scroll = QScrollArea(self)
        self.scroll.setWidgetResizable(True)
        self.scroll.setFrameShape(QFrame.Shape.NoFrame)
        self.scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        self.scroll.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAsNeeded)
        self.scroll.setStyleSheet(
            "QScrollArea, QScrollArea > QWidget > QWidget { background: transparent; border: none; }"
            "QScrollBar:vertical { background: transparent; width: 6px; margin: 2px 0; }"
            "QScrollBar::handle:vertical { background: rgba(235,245,255,75); min-height: 24px; border-radius: 3px; }"
            "QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }"
            "QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }"
        )
        self.scroll.viewport().setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground, True)
        self.content = QWidget(self.scroll)
        _set_transparent(self.content)
        self.grid = QGridLayout(self.content)
        self.grid.setContentsMargins(1, 0, 1, 1)
        self.grid.setHorizontalSpacing(3)
        self.grid.setVerticalSpacing(0)
        self.item_widgets: list[ItemWidget] = []
        self.columns = 4 if mode == "cards" else 2 if mode == "compact" else 1
        self.empty_label: QLabel | None = None
        self._stretch_row = -1
        for column in range(self.columns):
            self.grid.setColumnStretch(column, 1)
        self.item_widgets = [ItemWidget(self, item, mode) for item in items]
        self._reflow_items()
        self.scroll.setWidget(self.content)
        outer.addWidget(self.scroll, 1)
        self.tip_bubble = TipBubble(self)
        self._tip_generation = 0

    def _reflow_items(self) -> None:
        """Repack existing item widgets without rebuilding the panel window."""
        if self._stretch_row >= 0:
            self.grid.setRowStretch(self._stretch_row, 0)
        for widget in self.item_widgets:
            self.grid.removeWidget(widget)

        if self.item_widgets:
            if self.empty_label is not None:
                self.grid.removeWidget(self.empty_label)
                self.empty_label.hide()
            for index, widget in enumerate(self.item_widgets):
                self.grid.addWidget(widget, index // self.columns, index % self.columns)
                widget.show()
        else:
            if self.empty_label is None:
                self.empty_label = QLabel("保持清爽", self.content)
                self.empty_label.setStyleSheet(
                    "color: rgba(225,237,250,180); background: transparent;"
                )
                self.empty_label.setFont(
                    QFont("Microsoft YaHei UI", self.controller.font_size())
                )
                _with_shadow(self.empty_label, 190)
            self.grid.addWidget(self.empty_label, 0, 0)
            self.empty_label.show()

        self._stretch_row = max(
            1, (len(self.item_widgets) + self.columns - 1) // self.columns
        )
        self.grid.setRowStretch(self._stretch_row, 1)
        self.count_label.setText(str(len(self.item_widgets)))
        self.content.updateGeometry()

    def remove_item_widget(self, widget: ItemWidget) -> bool:
        """Remove one item in place and preserve this panel and its scroll state."""
        if widget not in self.item_widgets:
            return False
        scroll_bar = self.scroll.verticalScrollBar()
        previous_scroll = scroll_bar.value()
        target_path = os.path.normcase(os.path.abspath(str(widget.item["path"])))
        self.hide_item_tip(widget)
        self.grid.removeWidget(widget)
        self.item_widgets.remove(widget)
        self.items[:] = [
            item
            for item in self.items
            if os.path.normcase(os.path.abspath(str(item["path"]))) != target_path
        ]
        widget.hide()
        widget.deleteLater()
        self._reflow_items()

        def restore_scroll() -> None:
            scroll_bar.setValue(min(previous_scroll, scroll_bar.maximum()))

        restore_scroll()
        QTimer.singleShot(0, restore_scroll)
        return True

    def schedule_item_tip(self, item: ItemWidget) -> None:
        self._tip_generation += 1
        generation = self._tip_generation

        def show() -> None:
            if generation == self._tip_generation and item.hovered and item.isVisible():
                self.tip_bubble.show_for(item, item.tip_text)

        QTimer.singleShot(360, show)

    def hide_item_tip(self, _item: ItemWidget | None = None) -> None:
        self._tip_generation += 1
        self.tip_bubble.hide()

    def paintEvent(self, event) -> None:  # type: ignore[override]
        painter = QPainter(self)
        painter.setRenderHint(QPainter.RenderHint.Antialiasing)
        rect = QRectF(self.rect()).adjusted(0.7, 0.7, -0.7, -0.7)
        path = QPainterPath()
        path.addRoundedRect(rect, 7.5, 7.5)
        plate = QColor(self.panel_background)
        plate.setAlphaF(max(.04, min(.85, self.plate_opacity)))
        painter.fillPath(path, plate)
        border = QColor(226, 242, 255, 58)
        painter.setPen(QPen(border, 1.0))
        painter.drawPath(path)
        super().paintEvent(event)

    def set_plate_opacity(self, value: float) -> None:
        """Preview plate alpha without touching persisted settings or content."""
        self.plate_opacity = max(.08, min(.70, float(value)))
        self.update()

    def _resize_edges(self, point: QPoint) -> Qt.Edges:
        edges = Qt.Edge(0)
        if point.x() <= self.EDGE:
            edges |= Qt.Edge.LeftEdge
        elif point.x() >= self.width() - self.EDGE:
            edges |= Qt.Edge.RightEdge
        if point.y() <= self.EDGE:
            edges |= Qt.Edge.TopEdge
        elif point.y() >= self.height() - self.EDGE:
            edges |= Qt.Edge.BottomEdge
        return edges

    def mouseMoveEvent(self, event) -> None:  # type: ignore[override]
        edges = self._resize_edges(event.position().toPoint())
        cursors = {
            Qt.Edge.LeftEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.RightEdge: Qt.CursorShape.SizeHorCursor,
            Qt.Edge.TopEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.BottomEdge: Qt.CursorShape.SizeVerCursor,
            Qt.Edge.LeftEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.RightEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeFDiagCursor,
            Qt.Edge.RightEdge | Qt.Edge.TopEdge: Qt.CursorShape.SizeBDiagCursor,
            Qt.Edge.LeftEdge | Qt.Edge.BottomEdge: Qt.CursorShape.SizeBDiagCursor,
        }
        self.setCursor(cursors.get(edges, Qt.CursorShape.ArrowCursor))
        super().mouseMoveEvent(event)

    def leaveEvent(self, event) -> None:  # type: ignore[override]
        self.unsetCursor()
        super().leaveEvent(event)

    def mousePressEvent(self, event) -> None:  # type: ignore[override]
        if event.button() == Qt.MouseButton.LeftButton:
            edges = self._resize_edges(event.position().toPoint())
            handle = self.windowHandle()
            if edges and handle and handle.startSystemResize(edges):
                event.accept()
                return
        super().mousePressEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # type: ignore[override]
        self.controller.finish_panel_geometry(self.category.id)
        super().mouseReleaseEvent(event)


class QtOrganizerUI:
    """Qt owner for desktop panels while reusing the existing model/API/shell."""

    def __init__(
        self,
        model: Any,
        events: "queue.Queue[dict[str, Any]]",
        shell: Any,
        categories_provider: Callable[[Any], list[Any]],
    ) -> None:
        self.model, self.events, self.shell = model, events, shell
        self.categories_provider = categories_provider
        self.app = QApplication.instance() or QApplication([])
        self.app.setQuitOnLastWindowClosed(False)
        self.panels: dict[str, PanelWindow] = {}
        self.selected_item: ItemWidget | None = None
        self.group_snapshot: dict[str, tuple[tuple[str, str, bool], ...]] = {}
        self.last_desktop_signature: tuple[tuple[str, int, int], ...] = ()
        self.last_work_bounds: tuple[int, int, int, int] | None = None
        self.native_mouse_down = False
        self.native_drag: dict[str, Any] | None = None
        self.icon_provider = QFileIconProvider()
        self.icon_cache: dict[tuple[str, int], QIcon] = {}
        self._settings_dialog: QDialog | None = None
        # While the settings dialog is open, configuration may be changed in
        # memory to render a live preview.  It must never be written until
        # the user explicitly saves.
        self._settings_preview = False
        if self.shell.reconcile_target_monitor(self.model.store.data["ui"]):
            self.model.store.save()
        self.render()
        self.last_desktop_signature = self.model.desktop_signature()
        self.tick_timer = QTimer()
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(1200)
        self.native_timer = QTimer()
        self.native_timer.timeout.connect(self.watch_native_drag)
        self.native_timer.start(75)

    def categories(self) -> list[Any]:
        return self.categories_provider(self.model.store)

    def cell_size(self) -> int:
        return max(40, min(72, int(self.model.store.data["ui"].get("cell_size", 48))))

    def row_height(self) -> int:
        return max(27, min(43, round(self.cell_size() * .64)))

    def icon_grid_icon_size(self) -> int:
        return max(38, min(56, round(self.cell_size() * .92)))

    def icon_grid_cell_height(self) -> int:
        return max(66, self.icon_grid_icon_size() + 16)

    def font_size(self) -> int:
        return max(8, min(15, int(self.model.store.data["ui"].get("font_size", 9))))

    def layout_footprint(self) -> float:
        return max(.45, min(.95, float(self.model.store.data["ui"].get("layout_footprint", .82))))

    @staticmethod
    def tint(base: str, accent: str, amount: float) -> str:
        source = tuple(int(base[index:index + 2], 16) for index in (1, 3, 5))
        target = tuple(int(accent[index:index + 2], 16) for index in (1, 3, 5))
        return "#" + "".join(f"{round(a + (b - a) * amount):02x}" for a, b in zip(source, target))

    def icon_for(self, path: str, size: int) -> QIcon:
        key = (path, size)
        if key not in self.icon_cache:
            icon = self.shell_icon(path)
            if icon.isNull():
                icon = self.app.style().standardIcon(QStyle.StandardPixmap.SP_FileIcon)
            self.icon_cache[key] = icon
        return self.icon_cache[key]

    def shell_icon(self, path: str) -> QIcon:
        """Resolve the visible icon for any desktop entry.

        .url Internet shortcuts carry their icon in the file itself
        (IconFile/IconIndex) and Qt's QFileIconProvider does not read them,
        which used to render every game URL as a blank white document icon.
        """
        suffix = Path(path).suffix.lower()
        if suffix == ".url":
            source, index = self.url_icon_source(path)
            if source:
                icon = self.icon_from_source(source, index)
                if not icon.isNull():
                    return icon
        return self.icon_provider.icon(QFileInfo(path))

    def icon_from_source(self, source: str, index: int) -> QIcon:
        source = os.path.expandvars(source.strip().strip('"'))
        direct = Path(source)
        if direct.suffix.lower() in {".ico", ".png", ".jpg", ".jpeg", ".bmp"}:
            icon = QIcon(str(direct))
            if not icon.isNull():
                return icon
        # Executables/DLLs: let the shell hand out the icon at the stored
        # index; QFileInfo-based lookup is index-agnostic but still better
        # than the blank document glyph.
        return self.icon_provider.icon(QFileInfo(source))

    @staticmethod
    def url_icon_source(path: str) -> tuple[str | None, int]:
        try:
            lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            return None, 0
        icon_file: str | None = None
        icon_index = 0
        for line in lines:
            key, sep, value = line.partition("=")
            if not sep:
                continue
            key = key.strip().lower()
            if key == "iconfile" and not icon_file:
                icon_file = value.strip().strip('"') or None
            elif key == "iconindex":
                try:
                    icon_index = int(value.strip())
                except ValueError:
                    pass
        return icon_file, icon_index

    def item_tip(self, item: dict[str, Any]) -> str:
        key = item["name"].lower()
        cached = self.model.store.data.get("tip_summaries", {}).get(key, {})
        details = [cached.get("app", ""), cached.get("summary", "")]
        details = [value for value in details if value]
        tags = cached.get("tags", [])
        if tags:
            details.append("标签：" + " · ".join(tags))
        if item.get("manual"):
            details.append("已固定归类，自动整理不会移动")
        if details:
            return "\n".join(details)
        if item["kind"] == "folder":
            return f"文件夹：{item['name']}"
        return item["name"]

    @staticmethod
    def snapshot_groups(groups: dict[str, list[dict[str, Any]]]) -> dict[str, tuple[tuple[str, str, bool], ...]]:
        return {
            category: tuple((item["name"], item["modified"], bool(item.get("manual"))) for item in items)
            for category, items in groups.items()
        }

    @staticmethod
    def organized_paths(groups: dict[str, list[dict[str, Any]]]) -> list[str]:
        return [
            item["path"]
            for category, items in groups.items()
            if category != "inbox"
            for item in items
        ]

    @staticmethod
    def _position_from_saved(value: str, fallback: tuple[int, int]) -> tuple[int, int]:
        match = re.search(r"([+-]-?\d+)([+-]-?\d+)$", value or "")
        if not match:
            return fallback
        values = []
        for raw in match.groups():
            values.append(int(raw.replace("+-", "-").replace("--", "+")))
        return values[0], values[1]

    def render(self, only: str | None = None) -> None:
        # Rebuilding a panel invalidates all of its ItemWidget wrappers.  Never
        # retain a selection reference across that boundary: a later right
        # click would otherwise call update() on an already deleted C++ object.
        self.clear_selection()
        if only is None:
            for panel in self.panels.values():
                self.shell.panel_windows.pop(int(panel.winId()), None)
                panel.close()
                panel.deleteLater()
            self.panels = {}
        elif only in self.panels:
            previous = self.panels.pop(only)
            self.shell.panel_windows.pop(int(previous.winId()), None)
            previous.close()
            previous.deleteLater()

        self.shell.host = self.shell._find_overlay_host()
        if not self.shell.host:
            self._destroy_all()
            self.shell.restore_icons()
            return
        ui = self.model.store.data["ui"]
        monitor = self.shell.target_monitor(ui.get("target_monitor", "primary"))
        self.last_work_bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
        groups = self.model.scan()
        self.group_snapshot = self.snapshot_groups(groups)
        automatic = self.auto_layout(groups, monitor)
        profile = self.layout_profile(monitor)
        use_automatic_layout = profile.get("auto_layout", True)
        attached: list[bool] = []
        for category in self.categories():
            if category.id == "inbox" or (only is not None and category.id != only):
                continue
            items = self.model.sorted_items(category.id, groups.get(category.id, []))
            mode = self.model.store.data.get("category_modes", {}).get(category.id, category.mode)
            if mode not in PANEL_MODES:
                mode = "list"
            style = ui.setdefault("panel_styles", {}).get(category.id, {})
            base = style.get("background", ui.get("background", "#101827"))
            panel_background = self.tint(base, category.color, .045)
            opacity = float(style.get("alpha", ui.get("alpha", .22)))
            panel = PanelWindow(self, category, items, mode, panel_background, opacity)
            x, y, width, height = automatic[category.id]
            if not use_automatic_layout:
                saved_size = profile.get("panel_sizes", {}).get(category.id, {})
                width = int(saved_size.get("width", width))
                height = int(saved_size.get("height", height))
                x, y = self._position_from_saved(
                    profile.get("panel_positions", {}).get(category.id, ""), (x, y)
                )
            panel.setGeometry(x, y, width, height)
            panel.show()
            self.app.processEvents()
            attached.append(self.shell.attach(int(panel.winId())))
            self.panels[category.id] = panel

        if not all(attached) or not all(
            self.shell.panel_is_desktop_child(int(panel.winId())) for panel in self.panels.values()
        ):
            self._destroy_all()
            self.shell.restore_icons()
            return
        self.shell.keep_panels_above_desktop_backdrop()
        self.app.processEvents()
        if only is None:
            bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
            requested = [
                Rect(category, panel.x(), panel.y(), panel.width(), panel.height())
                for category, panel in self.panels.items()
            ]
            packed = (
                {item.key: item for item in requested}
                if use_automatic_layout
                else pack_rectangles(requested, bounds, margin=12, gap=10)
            )
            for category, panel in self.panels.items():
                rect = packed[category]
                panel.setGeometry(rect.x, rect.y, rect.width, rect.height)
        if self.panels and all(
            self.shell.panel_is_visible(int(panel.winId())) for panel in self.panels.values()
        ):
            self.shell.hide_icons(self.organized_paths(groups))
        else:
            self.shell.restore_icons()

    def _destroy_all(self) -> None:
        for panel in self.panels.values():
            self.shell.panel_windows.pop(int(panel.winId()), None)
            panel.close()
            panel.deleteLater()
        self.panels = {}

    def tick(self) -> None:
        self.shell.keep_panels_above_desktop_backdrop()
        rerender = False
        arrange = False
        recovery = False
        changed: set[str] = set()
        while True:
            try:
                event = self.events.get_nowait()
            except queue.Empty:
                break
            kind = event.get("type")
            if kind == "close":
                self.close()
                return
            if kind == "arrange":
                arrange = True
            elif kind in {"panel-style", "panel-mode"}:
                self.render(event["category"])
            elif kind == "move":
                self.move_panel(event["category"], event["x"], event["y"])
            elif kind == "settings":
                self.settings()
            elif kind == "assign":
                changed.update(event.get("categories", []))
            elif kind == "render":
                rerender = True

        ui = self.model.store.data["ui"]
        # Settings preview owns panel geometry temporarily.  Do not let the
        # 1.2s maintenance tick race it by rebuilding Explorer-owned windows:
        # that race is what creates stale/overlapping HWNDs and resets alpha.
        settings_preview_open = bool(
            self._settings_dialog
            and isValid(self._settings_dialog)
            and self._settings_dialog.isVisible()
        )
        if settings_preview_open:
            return
        monitor_changed = self.shell.reconcile_target_monitor(ui)
        if monitor_changed:
            self.model.store.save()
            arrange = recovery = True
        monitor = self.shell.target_monitor(ui.get("target_monitor", "primary"))
        bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
        if self.last_work_bounds is not None and bounds != self.last_work_bounds:
            arrange = recovery = True
        signature = self.model.desktop_signature()
        if signature != self.last_desktop_signature:
            groups = self.model.scan()
            snapshot = self.snapshot_groups(groups)
            changed.update(
                category
                for category in set(snapshot) | set(self.group_snapshot)
                if snapshot.get(category) != self.group_snapshot.get(category)
            )
            self.last_desktop_signature = signature
            self.group_snapshot = snapshot
            self.shell.hide_icons(self.organized_paths(groups))
        if arrange:
            self.arrange_top_right(rebuild=rerender or recovery)
        elif rerender:
            self.render()
        else:
            for category in changed:
                if category != "inbox":
                    self.render(category)

    def auto_layout(self, groups: dict[str, list[dict[str, Any]]], monitor: dict[str, Any]) -> dict[str, tuple[int, int, int, int]]:
        categories = [category for category in self.categories() if category.id != "inbox"]
        gap, margin = 7, 10
        screen_width = monitor["right"] - monitor["left"]
        screen_height = monitor["bottom"] - monitor["top"]
        usable_height = max(1, screen_height - 2 * margin)
        row_height = self.row_height()

        def preferred_height(category: Any) -> int:
            count = len(groups.get(category.id, []))
            mode = self.model.store.data.get("category_modes", {}).get(category.id, category.mode)
            if mode == "cards":
                rows = max(1, (count + 3) // 4)
                content = rows * self.icon_grid_cell_height()
            elif mode == "compact":
                rows = max(1, (count + 1) // 2)
                content = rows * row_height
            else:
                rows = max(1, count)
                content = rows * row_height
            return min(usable_height, max(86, 41 + content + 8))

        desired = [(category, preferred_height(category)) for category in categories]
        footprint_width = max(270, int(screen_width * self.layout_footprint()))
        panel_width = min(304, max(244, screen_width - 2 * margin))
        max_columns = max(1, (footprint_width + gap) // (panel_width + gap))
        columns = max(
            1,
            min(
                max_columns,
                (sum(height for _, height in desired) + gap * len(desired)) // max(1, usable_height) + 1,
            ),
        )

        def pack(column_count: int) -> list[list[tuple[Any, int]]] | None:
            packed: list[list[tuple[Any, int]]] = [[] for _ in range(column_count)]
            used = [0] * column_count
            column = 0
            for category, panel_height in desired:
                needed = panel_height + (gap if packed[column] else 0)
                if used[column] + needed > usable_height and packed[column]:
                    column += 1
                    if column == column_count:
                        return None
                    needed = panel_height
                packed[column].append((category, panel_height))
                used[column] += needed
            return packed

        packed = pack(columns)
        while packed is None and columns < max_columns:
            columns += 1
            packed = pack(columns)
        if packed is None:
            rows_per_column = max(1, (len(categories) + max_columns - 1) // max_columns)
            compact_height = max(82, (usable_height - gap * max(0, rows_per_column - 1)) // rows_per_column)
            desired = [(category, min(height, compact_height)) for category, height in desired]
            columns = max_columns
            packed = pack(columns)
            if packed is None:
                packed = [[] for _ in range(columns)]
                for index, item in enumerate(desired):
                    packed[min(columns - 1, index // rows_per_column)].append(item)

        result: dict[str, tuple[int, int, int, int]] = {}
        for column, items in enumerate(packed):
            y = monitor["top"] + margin
            x = monitor["right"] - margin - (column + 1) * panel_width - column * gap
            for category, panel_height in items:
                result[category.id] = (x, y, panel_width, panel_height)
                y += panel_height + gap
        return result

    def layout_profile(self, monitor: dict[str, Any]) -> dict[str, Any]:
        ui = self.model.store.data["ui"]
        key = (
            f"{monitor.get('screen_right', monitor['right']) - monitor.get('screen_left', monitor['left'])}x"
            f"{monitor.get('screen_bottom', monitor['bottom']) - monitor.get('screen_top', monitor['top'])}@"
            f"{monitor.get('screen_left', monitor['left'])},{monitor.get('screen_top', monitor['top'])}"
        )
        profiles = ui.setdefault("layout_profiles", {})
        if key not in profiles:
            profiles[key] = {"auto_layout": True, "panel_positions": {}, "panel_sizes": {}}
            if not self._settings_preview:
                self.model.store.save()
        self.active_profile = profiles[key]
        return self.active_profile

    def arrange_top_right(self, rebuild: bool = False) -> None:
        ui = self.model.store.data["ui"]
        monitor = self.shell.target_monitor(ui.get("target_monitor", "primary"))
        profile = self.layout_profile(monitor)
        self.last_work_bounds = (monitor["left"], monitor["top"], monitor["right"], monitor["bottom"])
        if rebuild:
            profile["panel_positions"] = {}
            profile["panel_sizes"] = {}
            profile["auto_layout"] = True
            self.render()
            if not self.panels:
                return
        automatic = self.auto_layout(self.model.scan(), monitor)
        for category, panel in self.panels.items():
            x, y, width, height = automatic[category]
            panel.setGeometry(x, y, width, height)
            profile.setdefault("panel_positions", {})[category] = f"+{x}+{y}"
            profile.setdefault("panel_sizes", {})[category] = {"width": width, "height": height}
        profile["auto_layout"] = False
        self.model.store.save()
        self.shell.keep_panels_above_desktop_backdrop()

    def bound_position(self, x: int, y: int, width: int, height: int) -> tuple[int, int]:
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
        margin = 8
        return (
            max(monitor["left"] + margin, min(x, monitor["right"] - width - margin)),
            max(monitor["top"] + margin, min(y, monitor["bottom"] - height - margin)),
        )

    def safe_position(self, category: str, wanted_x: int, wanted_y: int, width: int, height: int) -> tuple[int, int]:
        base_x, base_y = self.bound_position(wanted_x, wanted_y, width, height)
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
        occupied = [
            (panel.x(), panel.y(), panel.width(), panel.height())
            for name, panel in self.panels.items()
            if name != category
        ]

        def free(x: int, y: int) -> bool:
            return all(
                x + width + 7 <= ox
                or ox + ow + 7 <= x
                or y + height + 7 <= oy
                or oy + oh + 7 <= y
                for ox, oy, ow, oh in occupied
            )

        if free(base_x, base_y):
            return base_x, base_y
        candidates = [
            (x, y)
            for y in range(monitor["top"] + 8, max(monitor["top"] + 9, monitor["bottom"] - height - 7), 24)
            for x in range(monitor["left"] + 8, max(monitor["left"] + 9, monitor["right"] - width - 7), 24)
        ]
        candidates.sort(key=lambda point: (point[0] - base_x) ** 2 + (point[1] - base_y) ** 2)
        return next(((x, y) for x, y in candidates if free(x, y)), (base_x, base_y))

    def finish_panel_geometry(self, category: str) -> None:
        panel = self.panels.get(category)
        if not panel:
            return
        profile = getattr(self, "active_profile", None)
        if profile is None:
            monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
            profile = self.layout_profile(monitor)
        profile["auto_layout"] = False
        profile.setdefault("panel_positions", {})[category] = f"+{panel.x()}+{panel.y()}"
        profile.setdefault("panel_sizes", {})[category] = {
            "width": panel.width(),
            "height": panel.height(),
        }
        self.resolve_geometry_conflicts(category)
        self.model.store.save()
        self.shell.keep_panels_above_desktop_backdrop()

    def resolve_geometry_conflicts(self, anchor: str) -> None:
        for _ in range(max(1, len(self.panels) * 2)):
            rects = {
                name: Rect(name, panel.x(), panel.y(), panel.width(), panel.height())
                for name, panel in self.panels.items()
            }
            conflict: tuple[str, str] | None = None
            names = list(rects)
            for index, first in enumerate(names):
                for second in names[index + 1 :]:
                    if overlaps(rects[first], rects[second], gap=0):
                        conflict = first, second
                        break
                if conflict:
                    break
            if not conflict:
                return
            first, second = conflict
            victim = second if first == anchor else first if second == anchor else second
            panel = self.panels[victim]
            x, y = self.safe_position(victim, panel.x(), panel.y(), panel.width(), panel.height())
            if (x, y) == (panel.x(), panel.y()):
                return
            panel.move(x, y)
            self.active_profile.setdefault("panel_positions", {})[victim] = f"+{x}+{y}"

    def move_panel(self, category: str, x: int, y: int) -> None:
        panel = self.panels.get(category)
        if not panel:
            return
        x, y = self.safe_position(category, x, y, panel.width(), panel.height())
        panel.move(x, y)
        self.finish_panel_geometry(category)

    def panel_at(self, point: QPoint) -> str | None:
        for category, panel in self.panels.items():
            if panel.geometry().contains(point):
                return category
        return None

    def select_item(self, item: ItemWidget) -> None:
        if not isValid(item):
            return
        if self.selected_item and self.selected_item is not item:
            previous = self.selected_item
            self.selected_item = None
            if isValid(previous):
                previous.selected = False
                previous.update()
        self.selected_item = item
        item.selected = True
        item.update()

    def clear_selection(self) -> None:
        previous = self.selected_item
        self.selected_item = None
        if previous is not None and isValid(previous):
            previous.selected = False
            previous.update()

    def move_items_to_category(self, entries: list[tuple[str, str]], target: str) -> None:
        affected = {target}
        for name, source in entries:
            affected.add(source)
            self.model.set_category(name, target)
        groups = self.model.scan()
        self.shell.hide_icons(self.organized_paths(groups))
        for category in affected:
            if category != "inbox":
                self.render(category)

    def drop_item(self, widget: ItemWidget, point: QPoint) -> None:
        source = widget.panel.category.id
        destination = self.panel_at(point)
        if destination is None:
            self.move_items_to_category([(widget.item["name"], source)], "inbox")
            return
        if destination != source:
            self.move_items_to_category([(widget.item["name"], source)], destination)
            return
        candidates = widget.panel.item_widgets
        if not candidates:
            return
        target = min(
            candidates,
            key=lambda item: (item.mapToGlobal(item.rect().center()) - point).manhattanLength(),
        )
        before = target.item["name"] if target is not widget else None
        self.model.reorder_item(source, widget.item["name"], before)
        self.render(source)

    @staticmethod
    def explorer_display_name(item: dict[str, Any]) -> str:
        try:
            return shell.SHGetFileInfo(item["path"], 0, shellcon.SHGFI_DISPLAYNAME)[1][3]
        except Exception:
            return item["name"] if item["kind"] == "folder" else Path(item["name"]).stem

    def watch_native_drag(self) -> None:
        down = self.shell.left_button_down()
        if down and not self.native_mouse_down:
            self.native_drag = None
            if self.shell.pointer_over_listview():
                x, y = self.shell.cursor_position()
                self.native_drag = {"x": x, "y": y, "labels": []}
        elif down and self.native_drag is not None and not self.native_drag["labels"]:
            self.native_drag["labels"] = self.shell.selected_native_labels()
        elif not down and self.native_mouse_down and self.native_drag is not None:
            x, y = self.shell.cursor_position()
            state = self.native_drag
            self.native_drag = None
            target = self.panel_at(QPoint(x, y))
            if abs(x - state["x"]) + abs(y - state["y"]) > 8 and target and state["labels"]:
                groups = self.model.scan()
                wanted = {label.casefold() for label in state["labels"]}
                entries = [
                    (item["name"], "inbox")
                    for item in groups.get("inbox", [])
                    if self.explorer_display_name(item).casefold() in wanted
                ]
                if entries:
                    self.move_items_to_category(entries, target)
        self.native_mouse_down = down

    def open_item(self, path: str) -> None:
        try:
            os.startfile(path)  # type: ignore[attr-defined]
        except OSError as exc:
            QMessageBox.critical(None, "无法打开", str(exc))

    def open_location(self, path: str) -> None:
        try:
            subprocess.Popen(["explorer.exe", "/select,", str(Path(path))])
        except OSError as exc:
            QMessageBox.critical(None, "无法定位", str(exc))

    def show_item_menu(self, widget: ItemWidget, point: QPoint) -> None:
        item, source = widget.item, widget.panel.category.id
        from native_context_menu import show_file_menu
        actions = [
            ("打开所在位置", lambda: self.open_location(item["path"])),
            ("放回原生桌面，等待整理", lambda: self.move_items_to_category([(item["name"], source)], "inbox")),
        ]
        for category in self.categories():
            if category.id not in ("inbox", source):
                actions.append((f"归到 {category.title}", lambda target=category.id: self.move_items_to_category([(item["name"], source)], target)))
        actions.extend([
            ("取消手动归类，让 AI 按规则判断", lambda: self.reset_item(item["name"], source)),
            ("重命名（保留整理记录）", lambda: self.rename_item(widget)),
            ("移到回收站", lambda: self.recycle_item(widget)),
        ])
        try:
            callback = show_file_menu(item["path"], point.x(), point.y(), actions, lambda: self.rename_item(widget))
        except Exception as exc:
            QMessageBox.warning(None, "系统右键菜单无法加载", f"{exc}\n将显示备用菜单；可通过‘打开所在位置’使用资源管理器菜单。")
        else:
            if callback:
                QTimer.singleShot(0, callback)
            return
        menu = QMenu()
        menu.addAction("打开（也可双击）", lambda: self.open_item(item["path"]))
        menu.addAction("打开所在位置", lambda: self.open_location(item["path"]))
        rename_action = menu.addAction("重命名文件夹" if item["kind"] == "folder" else "重命名文件")
        menu.addSeparator()
        menu.addAction("放回原生桌面，等待整理", lambda: self.move_items_to_category([(item["name"], source)], "inbox"))
        assign = menu.addMenu("归到其他分类")
        for category in self.categories():
            if category.id != "inbox" and category.id != source:
                assign.addAction(
                    category.title,
                    lambda checked=False, target=category.id: self.move_items_to_category(
                        [(item["name"], source)], target
                    ),
                )
        menu.addSeparator()
        menu.addAction("取消手动归类，让 AI 按规则判断", lambda: self.reset_item(item["name"], source))
        menu.addSeparator()
        recycle_action = menu.addAction("移到回收站")
        chosen = menu.exec(point)
        # Run mutations after QMenu.exec() has unwound so the item is never
        # destroyed inside its mousePressEvent or the menu's nested event loop.
        if chosen is rename_action:
            self.rename_item(widget)
        elif chosen is recycle_action:
            self.recycle_item(widget)

    def rename_item(self, widget: ItemWidget) -> None:
        item = widget.item
        path = Path(item["path"])
        current = path.name if item["kind"] == "folder" else path.stem
        suffix_note = "" if item["kind"] == "folder" or not path.suffix else f"（扩展名 {path.suffix} 会保留）"
        value, accepted = QInputDialog.getText(None, "重命名", f"请输入新名称{suffix_note}：", text=current)
        if not accepted:
            return
        try:
            self.model.rename_item(item["path"], value)
        except ValueError as exc:
            QMessageBox.critical(None, "无法重命名", str(exc))
            return
        self.render()

    def reset_item(self, name: str, source: str) -> None:
        self.model.set_category(name, "archive", False)
        after = next(
            (
                category
                for category, items in self.model.scan().items()
                if any(item["name"].casefold() == name.casefold() for item in items)
            ),
            "archive",
        )
        for category in {source, after}:
            if category != "inbox":
                self.render(category)

    def recycle_item(self, widget: ItemWidget) -> None:
        item, source = widget.item, widget.panel.category.id
        path = Path(item["path"])
        if not path.exists():
            QMessageBox.critical(None, "无法删除", "文件已不存在或无法访问。")
            return
        kind = "文件夹" if path.is_dir() else "文件"
        answer = QMessageBox.question(
            None,
            "移到回收站",
            f"确定将{kind}“{path.name}”移到回收站吗？\n\n可在 Windows 回收站中恢复。",
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
            QMessageBox.StandardButton.No,
        )
        if answer != QMessageBox.StandardButton.Yes:
            return
        try:
            result, aborted = shell.SHFileOperation(
                (
                    0,
                    shellcon.FO_DELETE,
                    str(path),
                    None,
                    shellcon.FOF_ALLOWUNDO | shellcon.FOF_NOCONFIRMATION | shellcon.FOF_SILENT,
                    None,
                    None,
                )
            )
            if result or aborted:
                raise OSError(result or "操作被取消")
        except Exception as exc:
            QMessageBox.critical(None, "无法删除", f"无法将该项移到回收站：{exc}")
            return
        key = item["name"].casefold()
        data = self.model.store.data
        for field in ("overrides", "pending_inbox", "seen", "visual_assets", "tip_summaries"):
            data.get(field, {}).pop(key, None)
        for names in data.get("item_orders", {}).values():
            if isinstance(names, list):
                names[:] = [name for name in names if name.casefold() != key]
        self.model.store.save()
        self.clear_selection()
        panel = self.panels.get(source)
        removed_in_place = bool(
            panel is widget.panel
            and isValid(widget)
            and panel.remove_item_widget(widget)
        )

        # Keep the periodic filesystem watcher aligned with this local update;
        # otherwise its next tick would rebuild the source panel after deletion.
        groups = self.model.scan()
        self.group_snapshot = self.snapshot_groups(groups)
        self.last_desktop_signature = self.model.desktop_signature()
        self.shell.hide_icons(self.organized_paths(groups))
        if not removed_in_place:
            self.render(source)

    def cycle_mode(self, category: str) -> None:
        modes = self.model.store.data.setdefault("category_modes", {})
        current = modes.get(category, "list")
        if current not in PANEL_MODES:
            current = "list"
        next_mode = PANEL_MODES[(PANEL_MODES.index(current) + 1) % len(PANEL_MODES)]
        self.set_panel_mode(category, next_mode)

    def set_panel_mode(self, category: str, mode: str) -> None:
        if mode not in PANEL_MODES:
            return
        self.model.store.data.setdefault("category_modes", {})[category] = mode
        self.model.store.save()
        self.render(category)

    def set_sorting(self, category: str, mode: str) -> None:
        self.model.store.data.setdefault("category_sorting", {})[category] = mode
        self.model.store.save()
        self.render(category)

    def show_sort_menu(self, category: str, point: QPoint) -> None:
        menu = QMenu()
        labels = (
            ("smart", "智能分组（默认）"),
            ("name", "按名称"),
            ("modified", "按最近修改"),
            ("custom", "手动顺序"),
        )
        current = self.model.store.data.setdefault("category_sorting", {}).get(category, "smart")
        for value, label in labels:
            action = menu.addAction(label, lambda checked=False, choice=value: self.set_sorting(category, choice))
            action.setCheckable(True)
            action.setChecked(value == current)
        menu.exec(point)

    def show_panel_menu(self, category: str, point: QPoint) -> None:
        menu = QMenu()
        menu.addAction("新建块", self.create_block)
        menu.addAction("重命名此块", lambda: self.rename_category(category))
        mode_menu = menu.addMenu("排版模式")
        current_mode = self.model.store.data.setdefault("category_modes", {}).get(category, "list")
        for mode in PANEL_MODES:
            action = mode_menu.addAction(
                PANEL_MODE_LABELS[mode],
                lambda checked=False, value=mode: self.set_panel_mode(category, value),
            )
            action.setCheckable(True)
            action.setChecked(mode == current_mode)
        menu.addSeparator()
        menu.addAction("在当前屏幕弹性排版", self.arrange_top_right)
        move = menu.addMenu("移动全部到指定屏幕并弹性排版")
        move.addAction("主屏幕", lambda: self.choose_monitor_and_arrange("primary"))
        for index, monitor in enumerate(self.shell.monitors(), start=1):
            if monitor.get("primary"):
                continue
            width, height = monitor["right"] - monitor["left"], monitor["bottom"] - monitor["top"]
            target = f"monitor:{int(monitor.get('handle', index))}"
            move.addAction(
                f"屏幕 {index}（{width}×{height}）",
                lambda checked=False, preference=target: self.choose_monitor_and_arrange(preference),
            )
        custom_ids = {block["id"] for block in self.model.store.data.get("custom_blocks", [])}
        if category in custom_ids and not self.model.scan().get(category, []):
            menu.addSeparator()
            menu.addAction("删除空块", lambda: self.delete_empty_block(category))
        menu.exec(point)

    def rename_category(self, category: str) -> None:
        current = next(
            (
                self.model.store.data["ui"].get("category_names", {}).get(item.id, item.title)
                for item in self.categories()
                if item.id == category
            ),
            category,
        )
        value, ok = QInputDialog.getText(None, "修改分类名称", "名称：", text=current)
        if ok and value.strip():
            self.model.store.data["ui"].setdefault("category_names", {})[category] = value.strip()
            self.model.store.save()
            self.render(category)

    def create_block(self) -> None:
        import hashlib
        import time

        identifier = "custom-" + hashlib.sha1(f"未命名-{time.time_ns()}".encode()).hexdigest()[:8]
        palette = ["#8bd3ff", "#e3a7ff", "#8ee6bc", "#ffd37b"]
        blocks = self.model.store.data.setdefault("custom_blocks", [])
        blocks.append({"id": identifier, "title": "未命名", "color": palette[len(blocks) % len(palette)]})
        self.model.store.data.setdefault("category_modes", {})[identifier] = "list"
        self.model.store.save()
        self.arrange_top_right(rebuild=True)

    def delete_empty_block(self, category: str) -> None:
        if self.model.scan().get(category):
            return
        self.model.store.data["custom_blocks"] = [
            block for block in self.model.store.data.get("custom_blocks", []) if block["id"] != category
        ]
        self.model.store.data.get("category_modes", {}).pop(category, None)
        ui = self.model.store.data["ui"]
        ui.get("category_names", {}).pop(category, None)
        ui.get("panel_styles", {}).pop(category, None)
        self.model.store.save()
        self.render()

    def choose_monitor_and_arrange(self, preference: str) -> None:
        self.shell.remember_target_monitor(self.model.store.data["ui"], preference)
        self.model.store.save()
        self.arrange_top_right(rebuild=True)

    def preview_global_plate_opacity(self, value: float) -> None:
        """Preview the global plate alpha without mutating the Store."""
        panel_styles = self.model.store.data["ui"].get("panel_styles", {})
        for category, panel in self.panels.items():
            if "alpha" not in panel_styles.get(category, {}):
                panel.set_plate_opacity(value)

    def restore_plate_opacities(self, snapshot: dict[str, float]) -> None:
        for category, value in snapshot.items():
            panel = self.panels.get(category)
            if panel:
                panel.set_plate_opacity(value)

    def preview_settings_layout(self, values: dict[str, Any]) -> None:
        """Render an in-memory settings preview without persisting it.

        Density, font size, horizontal footprint, plate colour and target
        monitor all influence the panel tree or its geometry.  Updating only a
        widget would give a misleading partial preview, so rebuild the desktop
        layer from the temporary values.  ``layout_profile`` is aware of this
        mode and will not save a newly encountered monitor profile.
        """
        self.model.store.data["ui"].update(values)
        self._settings_preview = True
        try:
            self.render()
            # A preview rebuild creates new panel objects.  Reapply the
            # in-dialog alpha to those fresh windows explicitly so a footprint
            # relayout can never visually reset the opacity preview.
            self.preview_global_plate_opacity(float(values.get("alpha", .22)))
        finally:
            self._settings_preview = False

    def preview_footprint_layout(self, value: float) -> None:
        """Preview horizontal desktop usage by moving existing panels only.

        Recreating Explorer-owned transparent windows while a slider is being
        dragged leaves stale HWNDs behind on some Windows/DWM combinations.
        The result is duplicate panels, flicker and apparently lost alpha.
        Geometry changes are sufficient for footprint preview, so deliberately
        keep every existing PanelWindow and only apply the automatic layout's
        new rectangles.
        """
        ui = self.model.store.data["ui"]
        ui["layout_footprint"] = max(.45, min(.95, float(value)))
        monitor = self.shell.target_monitor(ui.get("target_monitor", "primary"))
        automatic = self.auto_layout(self.model.scan(), monitor)
        for category, panel in self.panels.items():
            rect = automatic.get(category)
            if rect is not None:
                x, y, width, height = rect
                panel.setGeometry(x, y, width, height)
        self.shell.keep_panels_above_desktop_backdrop()

    def _release_settings_topmost(self, dialog: QDialog) -> None:
        """Return the settings dialog to ordinary application Z-order."""
        if self._settings_dialog is not dialog or not dialog.isVisible():
            return
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, False)
        dialog.show()

    def present_settings_dialog(self, dialog: QDialog) -> None:
        """Present settings above the non-activating Explorer desktop band.

        DESKFLOW panels are deliberately Explorer-owned and do not accept focus.
        A normal Qt dialog opened from one of those panels can therefore be
        created behind the desktop or another application.  Present it as an
        independent application window, briefly raise it above the current
        desktop surface, then immediately restore normal Z-order.
        """
        monitor = self.shell.target_monitor(self.model.store.data["ui"].get("target_monitor", "primary"))
        dialog.adjustSize()
        x = monitor["left"] + max(0, (monitor["right"] - monitor["left"] - dialog.width()) // 2)
        y = monitor["top"] + max(0, (monitor["bottom"] - monitor["top"] - dialog.height()) // 2)
        dialog.move(x, y)
        dialog.setWindowFlag(Qt.WindowType.WindowStaysOnTopHint, True)
        dialog.show()
        dialog.raise_()
        dialog.activateWindow()
        QTimer.singleShot(220, lambda current=dialog: self._release_settings_topmost(current))

    def settings(self) -> None:
        if self._settings_dialog and self._settings_dialog.isVisible():
            self.present_settings_dialog(self._settings_dialog)
            return
        ui = self.model.store.data["ui"]
        original_ui = deepcopy(ui)
        original_opacity = {
            category: panel.plate_opacity for category, panel in self.panels.items()
        }
        saved = {"value": False}
        dialog = QDialog()
        self._settings_dialog = dialog
        dialog.setWindowTitle("DESKFLOW 设置")
        dialog.setMinimumWidth(390)
        dialog.setStyleSheet(
            "QDialog { background: #111b2b; color: #f4f8ff; }"
            "QLabel, QCheckBox { color: #f4f8ff; }"
            "QPushButton, QComboBox, QSpinBox { background: #20324b; color: #f4f8ff; padding: 5px; }"
        )
        form = QFormLayout(dialog)
        density = QSpinBox(dialog)
        density.setRange(40, 72)
        density.setValue(self.cell_size())
        form.addRow("项目密度", density)
        font = QSpinBox(dialog)
        font.setRange(8, 15)
        font.setValue(self.font_size())
        form.addRow("文字大小", font)
        opacity = QSlider(Qt.Orientation.Horizontal, dialog)
        opacity.setRange(8, 70)
        opacity.setValue(round(float(ui.get("alpha", .22)) * 100))
        opacity.setToolTip("只改变玻璃底板；图标与文字始终保持完全不透明")
        opacity_row = QWidget(dialog)
        opacity_layout = QHBoxLayout(opacity_row)
        opacity_layout.setContentsMargins(0, 0, 0, 0)
        opacity_layout.setSpacing(8)
        opacity_layout.addWidget(opacity, 1)
        opacity_value = QLabel(f"{opacity.value()}%", opacity_row)
        opacity_value.setMinimumWidth(34)
        opacity_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        opacity_layout.addWidget(opacity_value)
        form.addRow("底板不透明度", opacity_row)
        footprint = QSlider(Qt.Orientation.Horizontal, dialog)
        footprint.setRange(45, 95)
        footprint.setValue(round(self.layout_footprint() * 100))
        footprint.setToolTip("实时预览桌面分组从右侧横向占用的宽度；取消将恢复原布局")
        footprint_row = QWidget(dialog)
        footprint_layout = QHBoxLayout(footprint_row)
        footprint_layout.setContentsMargins(0, 0, 0, 0)
        footprint_layout.setSpacing(8)
        footprint_layout.addWidget(footprint, 1)
        footprint_value = QLabel(f"{footprint.value()}%", footprint_row)
        footprint_value.setMinimumWidth(34)
        footprint_value.setAlignment(Qt.AlignmentFlag.AlignRight | Qt.AlignmentFlag.AlignVCenter)
        footprint_layout.addWidget(footprint_value)
        form.addRow("桌面横向占用", footprint_row)
        shadow = QCheckBox("为文字增加深色轮廓，适配明亮壁纸", dialog)
        shadow.setChecked(True)
        shadow.setEnabled(False)
        form.addRow("内容可读性", shadow)
        explanation = QLabel("底板采用逐像素透明；文字与图标不会随底板一起变淡。", dialog)
        explanation.setWordWrap(True)
        explanation.setStyleSheet("color: #8fdff3;")
        form.addRow("", explanation)
        color_button = QPushButton("选择底板颜色", dialog)
        chosen = {"color": ui.get("background", "#101827")}

        def choose_color() -> None:
            color = QColorDialog.getColor(QColor(chosen["color"]), dialog, "选择底板颜色")
            if color.isValid():
                chosen["color"] = color.name()
                schedule_preview()

        color_button.clicked.connect(choose_color)
        form.addRow("底板颜色", color_button)
        monitor_combo = QComboBox(dialog)
        monitor_combo.addItem("主屏幕", "primary")
        for index, monitor in enumerate(self.shell.monitors(), start=1):
            if monitor.get("primary"):
                continue
            monitor_combo.addItem(
                f"屏幕 {index} · {monitor['right'] - monitor['left']}×{monitor['bottom'] - monitor['top']}",
                f"monitor:{int(monitor.get('handle', index))}",
            )
        current = ui.get("target_monitor", "primary")
        current_index = monitor_combo.findData(current)
        monitor_combo.setCurrentIndex(max(0, current_index))
        form.addRow("所有分组显示在", monitor_combo)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Save | QDialogButtonBox.StandardButton.Cancel,
            parent=dialog,
        )
        form.addRow(buttons)

        def preview_opacity(value: int) -> None:
            opacity_value.setText(f"{value}%")
            ui["alpha"] = value / 100
            self.preview_global_plate_opacity(value / 100)

        def preview_layout() -> None:
            footprint_value.setText(f"{footprint.value()}%")
            self.preview_settings_layout(
                {
                    "cell_size": density.value(),
                    "icon_size": max(20, min(34, round(density.value() * .48))),
                    "font_size": font.value(),
                    "alpha": opacity.value() / 100,
                    "layout_footprint": footprint.value() / 100,
                    "background": chosen["color"],
                    "target_monitor": monitor_combo.currentData(),
                }
            )

        def schedule_preview(*_args: Any) -> None:
            # Density/font/colour/monitor preview is a deliberate complete
            # redraw, but each of these controls changes discretely.
            preview_layout()

        def update_footprint_value(*_args: Any) -> None:
            footprint_value.setText(f"{footprint.value()}%")

        def preview_footprint_on_release() -> None:
            # A geometry preview happens once after the user settles the
            # slider, never repeatedly during a drag.  That keeps the
            # Explorer desktop surface stable while still allowing preview
            # before Save.
            self.preview_footprint_layout(footprint.value() / 100)

        opacity.valueChanged.connect(preview_opacity)
        density.valueChanged.connect(schedule_preview)
        font.valueChanged.connect(schedule_preview)
        footprint.valueChanged.connect(update_footprint_value)
        footprint.sliderReleased.connect(preview_footprint_on_release)
        monitor_combo.currentIndexChanged.connect(schedule_preview)

        def save() -> None:
            ui.update(
                {
                    "cell_size": density.value(),
                    "icon_size": max(20, min(34, round(density.value() * .48))),
                    "font_size": font.value(),
                    "alpha": opacity.value() / 100,
                    "layout_footprint": footprint.value() / 100,
                    "background": chosen["color"],
                }
            )
            self.shell.remember_target_monitor(ui, monitor_combo.currentData())
            self.model.store.save()
            saved["value"] = True
            dialog.accept()
            self.arrange_top_right(rebuild=True)

        def finish(_result: int) -> None:
            if not saved["value"]:
                # Every control is an in-memory preview.  Cancel and the
                # title-bar close button restore both the original values and
                # the complete original panel geometry/appearance.
                ui.clear()
                ui.update(deepcopy(original_ui))
                self._settings_preview = True
                try:
                    self.render()
                finally:
                    self._settings_preview = False
                self.restore_plate_opacities(original_opacity)
            self._settings_dialog = None

        buttons.accepted.connect(save)
        buttons.rejected.connect(dialog.reject)
        dialog.finished.connect(finish)
        self.present_settings_dialog(dialog)

    def close(self) -> None:
        self.model.store.save()
        self.shell.restore_icons()
        self._destroy_all()
        self.app.quit()

    def run(self) -> None:
        self.app.exec()
