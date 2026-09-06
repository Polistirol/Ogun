#!/usr/bin/env python3
"""
Native desktop frontend for master.py.

Qt widgets (PySide6), no browser / Qt WebEngine.
Start from the repo root:

    python -m frontend
    python -m frontend --provider deepseek
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import List, Optional, Tuple

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from PySide6.QtCore import (
    QFileSystemWatcher,
    Qt,
    QThread,
    QTimer,
    Signal,
)
from PySide6.QtGui import QColor, QFont, QPalette, QTextCursor
from PySide6.QtWidgets import (
    QApplication,
    QComboBox,
    QFrame,
    QHBoxLayout,
    QLabel,
    QMainWindow,
    QPlainTextEdit,
    QPushButton,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QStatusBar,
    QTabWidget,
    QTextBrowser,
    QVBoxLayout,
    QWidget,
)

from llm_provider import SUPPORTED_PROVIDERS
from master import (
    AVAILABLE_TOOLS,
    MEMORY_FILES,
    TurnResult,
    load_memory,
    memory_dir,
    process_turn,
)

MEMORY_TABS = (
    ("profile.md", "Profile"),
    ("search-criteria.md", "Criteria"),
    ("market-insights.md", "Market"),
    ("decisions-log.md", "Decisions"),
)

TAB_INDEX = {name: i for i, (name, _) in enumerate(MEMORY_TABS)}


STYLESHEET = """
QMainWindow, QWidget#shell {
    background: #141210;
    color: #efe8df;
}
QStatusBar {
    background: #1c1916;
    color: #a89888;
    border-top: 1px solid #2c2824;
    font-size: 11px;
}
QLabel#brand {
    font-size: 20px;
    font-weight: 700;
    letter-spacing: 3px;
    color: #f3ece3;
}
QLabel#subtitle, QLabel#muted, QLabel#chip {
    color: #a89888;
    font-size: 12px;
}
QLabel#chip {
    padding: 2px 0;
}
QFrame#header {
    background: #1c1916;
    border-bottom: 1px solid #2c2824;
}
QFrame#memoryPane, QFrame#chatPane, QFrame#toolsBox {
    background: #1a1714;
    border: 1px solid #2c2824;
    border-radius: 10px;
}
QLabel#paneTitle {
    color: #c4b8a8;
    font-size: 11px;
    font-weight: 600;
    letter-spacing: 1.4px;
}
QComboBox {
    background: #241f1b;
    color: #efe8df;
    border: 1px solid #3a342e;
    border-radius: 6px;
    padding: 6px 10px;
    min-width: 140px;
}
QComboBox:hover, QComboBox:focus {
    border-color: #c46a38;
}
QComboBox QAbstractItemView {
    background: #241f1b;
    color: #efe8df;
    selection-background-color: #3a2a20;
    border: 1px solid #3a342e;
}
QPushButton#send {
    background: #c46a38;
    color: #1a120c;
    font-weight: 700;
    border: none;
    border-radius: 8px;
    padding: 10px 22px;
    min-width: 96px;
}
QPushButton#send:hover {
    background: #d57a46;
}
QPushButton#send:disabled {
    background: #3a342e;
    color: #7a7066;
}
QPushButton#ghost {
    background: transparent;
    color: #c4b8a8;
    border: 1px solid #3a342e;
    border-radius: 6px;
    padding: 6px 12px;
}
QPushButton#ghost:hover {
    border-color: #c46a38;
    color: #efe8df;
}
QPlainTextEdit#input {
    background: #241f1b;
    color: #efe8df;
    border: 1px solid #3a342e;
    border-radius: 8px;
    padding: 8px;
    font-size: 13px;
    selection-background-color: #5a3a28;
}
QPlainTextEdit#input:focus {
    border-color: #c46a38;
}
QScrollArea#chatScroll {
    background: transparent;
    border: none;
}
QWidget#chatInner {
    background: transparent;
}
QTabWidget::pane {
    border: none;
    background: #141210;
}
QTabBar::tab {
    background: transparent;
    color: #8a7c6e;
    padding: 8px 14px;
    margin-right: 2px;
    border-bottom: 2px solid transparent;
}
QTabBar::tab:selected {
    color: #efe8df;
    border-bottom: 2px solid #c46a38;
}
QTabBar::tab:hover {
    color: #d4c8ba;
}
QTextBrowser {
    background: #141210;
    color: #d8cfc4;
    border: none;
    font-size: 13px;
    padding: 8px;
    selection-background-color: #5a3a28;
}
QFrame#bubbleUser {
    background: #2a241e;
    border: 1px solid #3a342e;
    border-radius: 12px;
}
QFrame#bubbleAssistant {
    background: #1e1c18;
    border: 1px solid #3a342e;
    border-left: 3px solid #c46a38;
    border-radius: 12px;
}
QFrame#bubbleSystem {
    background: #1a2218;
    border: 1px solid #2e3a2a;
    border-radius: 10px;
}
QFrame#bubbleError {
    background: #2a1814;
    border: 1px solid #5a3028;
    border-radius: 10px;
}
QFrame#bubbleStatus {
    background: transparent;
    border: 1px dashed #3a342e;
    border-radius: 10px;
}
QLabel#role {
    font-size: 10px;
    font-weight: 700;
    letter-spacing: 1px;
    color: #8a7c6e;
}
QLabel#roleUser { color: #d4c8ba; }
QLabel#roleAssistant { color: #c46a38; }
QLabel#roleSystem { color: #7d9a6a; }
QLabel#roleError { color: #d07060; }
QSplitter::handle {
    background: #141210;
    width: 8px;
}
"""


def _apply_dark_palette(app: QApplication) -> None:
    palette = QPalette()
    bg = QColor("#141210")
    text = QColor("#efe8df")
    palette.setColor(QPalette.ColorRole.Window, bg)
    palette.setColor(QPalette.ColorRole.WindowText, text)
    palette.setColor(QPalette.ColorRole.Base, QColor("#1a1714"))
    palette.setColor(QPalette.ColorRole.AlternateBase, QColor("#1c1916"))
    palette.setColor(QPalette.ColorRole.Text, text)
    palette.setColor(QPalette.ColorRole.Button, QColor("#241f1b"))
    palette.setColor(QPalette.ColorRole.ButtonText, text)
    palette.setColor(QPalette.ColorRole.Highlight, QColor("#c46a38"))
    palette.setColor(QPalette.ColorRole.HighlightedText, QColor("#1a120c"))
    palette.setColor(QPalette.ColorRole.PlaceholderText, QColor("#7a7066"))
    app.setPalette(palette)


def _default_provider() -> str:
    value = (os.environ.get("LLM_PROVIDER") or "anthropic").strip().lower()
    if value in SUPPORTED_PROVIDERS:
        return value
    return "anthropic"


class TurnWorker(QThread):
    completed = Signal(object)

    def __init__(
        self,
        text: str,
        history: List[Tuple[str, str]],
        provider: Optional[str],
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        self._text = text
        self._history = list(history)
        self._provider = provider

    def run(self) -> None:
        try:
            result = process_turn(
                self._text, self._history, provider=self._provider
            )
        except Exception as exc:
            result = TurnResult(
                user_text=self._text,
                reply="",
                memory_notes=[],
                ok=False,
                error=str(exc),
            )
        self.completed.emit(result)


class ChatInput(QPlainTextEdit):
    send_requested = Signal()

    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("input")
        self.setPlaceholderText("Write to the master…  Enter sends, Shift+Enter newline")
        self.setFixedHeight(88)
        self.setTabChangesFocus(True)

    def keyPressEvent(self, event) -> None:  # type: ignore[override]
        if event.key() in (Qt.Key.Key_Return, Qt.Key.Key_Enter):
            if event.modifiers() & Qt.KeyboardModifier.ShiftModifier:
                super().keyPressEvent(event)
                return
            self.send_requested.emit()
            return
        super().keyPressEvent(event)


class MessageBubble(QFrame):
    def __init__(
        self,
        role: str,
        text: str,
        notes: Optional[List[str]] = None,
        parent: Optional[QWidget] = None,
    ) -> None:
        super().__init__(parent)
        kind = {
            "user": "bubbleUser",
            "assistant": "bubbleAssistant",
            "system": "bubbleSystem",
            "error": "bubbleError",
            "status": "bubbleStatus",
        }.get(role, "bubbleSystem")
        self.setObjectName(kind)
        self.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Maximum)

        labels = {
            "user": "YOU",
            "assistant": "OGUN",
            "system": "SYSTEM",
            "error": "ERROR",
            "status": "…",
        }
        role_ids = {
            "user": "roleUser",
            "assistant": "roleAssistant",
            "system": "roleSystem",
            "error": "roleError",
            "status": "role",
        }

        layout = QVBoxLayout(self)
        layout.setContentsMargins(14, 10, 14, 12)
        layout.setSpacing(6)

        header = QLabel(labels.get(role, role.upper()))
        header.setObjectName(role_ids.get(role, "role"))
        layout.addWidget(header)

        body = QTextBrowser()
        body.setOpenExternalLinks(True)
        body.setFrameShape(QFrame.Shape.NoFrame)
        body.setVerticalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)
        body.setMarkdown(text or "")
        body.document().setDocumentMargin(0)
        body.setSizePolicy(QSizePolicy.Policy.Expanding, QSizePolicy.Policy.Minimum)
        body.textChanged.connect(lambda b=body: _fit_browser_height(b))
        layout.addWidget(body)
        self._body = body
        QTimer.singleShot(0, lambda b=body: _fit_browser_height(b))

        if notes:
            meta = QLabel("Memory: " + "; ".join(notes))
            meta.setObjectName("muted")
            meta.setWordWrap(True)
            layout.addWidget(meta)

    def resizeEvent(self, event) -> None:  # type: ignore[override]
        super().resizeEvent(event)
        if event.oldSize().width() != event.size().width():
            _fit_browser_height(self._body)


def _fit_browser_height(browser: QTextBrowser) -> None:
    doc = browser.document()
    doc.setTextWidth(max(browser.viewport().width(), 100))
    height = max(int(doc.size().height()) + 8, 24)
    if browser.height() != height:
        browser.setFixedHeight(height)


class ChatView(QScrollArea):
    def __init__(self, parent: Optional[QWidget] = None) -> None:
        super().__init__(parent)
        self.setObjectName("chatScroll")
        self.setWidgetResizable(True)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarPolicy.ScrollBarAlwaysOff)

        self._inner = QWidget()
        self._inner.setObjectName("chatInner")
        self._layout = QVBoxLayout(self._inner)
        self._layout.setContentsMargins(4, 8, 12, 8)
        self._layout.setSpacing(10)
        self._layout.addStretch(1)
        self.setWidget(self._inner)

    def add_bubble(
        self,
        role: str,
        text: str,
        notes: Optional[List[str]] = None,
        align_right: bool = False,
    ) -> MessageBubble:
        row = QHBoxLayout()
        row.setContentsMargins(0, 0, 0, 0)
        bubble = MessageBubble(role, text, notes)
        bubble.setMaximumWidth(720)
        if align_right:
            row.addStretch(1)
            row.addWidget(bubble, 6)
        else:
            row.addWidget(bubble, 7)
            row.addStretch(1)
        wrap = QWidget()
        wrap.setLayout(row)
        self._layout.insertWidget(self._layout.count() - 1, wrap)
        QTimer.singleShot(40, self.scroll_to_bottom)
        return bubble

    def remove_widget(self, bubble: MessageBubble) -> None:
        parent = bubble.parentWidget()
        if parent is None:
            return
        self._layout.removeWidget(parent)
        parent.deleteLater()

    def scroll_to_bottom(self) -> None:
        bar = self.verticalScrollBar()
        bar.setValue(bar.maximum())


class MainWindow(QMainWindow):
    def __init__(self, provider: Optional[str] = None) -> None:
        super().__init__()
        self.setWindowTitle("OgunJob - master")
        self.resize(1220, 780)
        self.setMinimumSize(920, 600)

        self._provider = provider or _default_provider()
        self._history: List[Tuple[str, str]] = []
        self._worker: Optional[TurnWorker] = None
        self._pending: Optional[MessageBubble] = None
        self._busy = False

        shell = QWidget()
        shell.setObjectName("shell")
        root = QVBoxLayout(shell)
        root.setContentsMargins(0, 0, 0, 0)
        root.setSpacing(0)
        root.addWidget(self._build_header())

        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(16, 16, 16, 12)
        body_layout.setSpacing(12)

        splitter = QSplitter(Qt.Orientation.Horizontal)
        splitter.addWidget(self._build_memory_pane())
        splitter.addWidget(self._build_chat_pane())
        splitter.setStretchFactor(0, 4)
        splitter.setStretchFactor(1, 6)
        splitter.setSizes([460, 720])
        body_layout.addWidget(splitter, 1)
        root.addWidget(body, 1)

        self.setCentralWidget(shell)
        status = QStatusBar()
        self.setStatusBar(status)
        self._status = status

        self._watcher = QFileSystemWatcher(self)
        mem = memory_dir()
        if mem.is_dir():
            self._watcher.addPath(str(mem))
            for name in MEMORY_FILES:
                path = mem / name
                if path.is_file():
                    self._watcher.addPath(str(path))
        self._watcher.fileChanged.connect(lambda _: self.refresh_memory())
        self._watcher.directoryChanged.connect(lambda _: self.refresh_memory())

        self.refresh_memory()
        self._update_status("Ready. The LLM key is used on the first message.")
        self._input.setFocus()

        self._chat.add_bubble(
            "system",
            "Local master agent. Criteria and decisions said in chat "
            "are written to `memory/`. No automatic submissions: CV and "
            "profile always go through human review.",
        )

    def _build_header(self) -> QWidget:
        header = QFrame()
        header.setObjectName("header")
        layout = QHBoxLayout(header)
        layout.setContentsMargins(20, 14, 20, 14)

        titles = QVBoxLayout()
        titles.setSpacing(2)
        brand = QLabel("OGUNJOB")
        brand.setObjectName("brand")
        sub = QLabel("Master agent  ·  local markdown memory")
        sub.setObjectName("subtitle")
        titles.addWidget(brand)
        titles.addWidget(sub)
        layout.addLayout(titles)
        layout.addStretch(1)

        provider_cap = QLabel("PROVIDER")
        provider_cap.setObjectName("chip")
        layout.addWidget(provider_cap)

        self._provider_combo = QComboBox()
        for name in SUPPORTED_PROVIDERS:
            self._provider_combo.addItem(name)
        idx = self._provider_combo.findText(self._provider)
        self._provider_combo.setCurrentIndex(max(idx, 0))
        self._provider_combo.currentTextChanged.connect(self._on_provider_changed)
        layout.addWidget(self._provider_combo)

        reload_btn = QPushButton("Reload memory")
        reload_btn.setObjectName("ghost")
        reload_btn.clicked.connect(self.refresh_memory)
        layout.addWidget(reload_btn)
        return header

    def _build_memory_pane(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("memoryPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("MEMORY")
        title.setObjectName("paneTitle")
        layout.addWidget(title)

        self._mem_hint = QLabel("")
        self._mem_hint.setObjectName("muted")
        self._mem_hint.setWordWrap(True)
        layout.addWidget(self._mem_hint)

        self._tabs = QTabWidget()
        self._browsers: dict[str, QTextBrowser] = {}
        for filename, label in MEMORY_TABS:
            browser = QTextBrowser()
            browser.setOpenExternalLinks(True)
            self._tabs.addTab(browser, label)
            self._browsers[filename] = browser
        layout.addWidget(self._tabs, 1)

        tools = QFrame()
        tools.setObjectName("toolsBox")
        tools_layout = QVBoxLayout(tools)
        tools_layout.setContentsMargins(10, 8, 10, 10)
        tools_cap = QLabel("TOOLS (master registry)")
        tools_cap.setObjectName("paneTitle")
        tools_layout.addWidget(tools_cap)
        for name, spec in AVAILABLE_TOOLS.items():
            linked = "wired" if spec.get("handler") else "not wired"
            line = QLabel(f"{name}  —  {linked}")
            line.setObjectName("muted")
            line.setWordWrap(True)
            tools_layout.addWidget(line)
        layout.addWidget(tools)
        return pane

    def _build_chat_pane(self) -> QWidget:
        pane = QFrame()
        pane.setObjectName("chatPane")
        layout = QVBoxLayout(pane)
        layout.setContentsMargins(14, 14, 14, 14)
        layout.setSpacing(10)

        title = QLabel("CONVERSATION")
        title.setObjectName("paneTitle")
        layout.addWidget(title)

        self._chat = ChatView()
        layout.addWidget(self._chat, 1)

        row = QHBoxLayout()
        row.setSpacing(10)
        self._input = ChatInput()
        self._input.send_requested.connect(self._send)
        row.addWidget(self._input, 1)
        self._send_btn = QPushButton("Send")
        self._send_btn.setObjectName("send")
        self._send_btn.clicked.connect(self._send)
        row.addWidget(self._send_btn, 0, Qt.AlignmentFlag.AlignBottom)
        layout.addLayout(row)
        return pane

    def _on_provider_changed(self, name: str) -> None:
        self._provider = name
        self._update_status(f"Provider: {name}. Applies from the next message.")

    def _update_status(self, extra: str = "") -> None:
        mem = memory_dir()
        present = sum(1 for name in MEMORY_FILES if (mem / name).is_file())
        bits = [
            f"memory {present}/{len(MEMORY_FILES)}",
            f"provider {self._provider}",
        ]
        if extra:
            bits.append(extra)
        self._status.showMessage("  ·  ".join(bits))

    def refresh_memory(self) -> None:
        loaded = load_memory()
        mem = memory_dir()
        present = [n for n in MEMORY_FILES if (mem / n).is_file()]
        self._mem_hint.setText(
            f"{mem}  —  {', '.join(present) if present else 'no files'}"
        )
        for filename, browser in self._browsers.items():
            body = (loaded.get(filename) or "").strip() or "_Empty or missing file._"
            browser.setMarkdown(body)
            browser.moveCursor(QTextCursor.MoveOperation.Start)
        self._update_status()

    def _set_busy(self, busy: bool) -> None:
        self._busy = busy
        self._send_btn.setEnabled(not busy)
        self._input.setEnabled(not busy)
        self._provider_combo.setEnabled(not busy)

    def _send(self) -> None:
        if self._busy:
            return
        text = self._input.toPlainText().strip()
        if not text:
            return
        self._input.clear()
        self._chat.add_bubble("user", text, align_right=True)
        self._pending = self._chat.add_bubble("status", "Ogun is thinking…")
        self._set_busy(True)
        self._update_status("LLM call in progress…")

        self._worker = TurnWorker(text, self._history, self._provider, self)
        self._worker.completed.connect(self._on_turn_done)
        self._worker.finished.connect(self._worker.deleteLater)
        self._worker.start()

    def _on_turn_done(self, result: object) -> None:
        if self._pending is not None:
            self._chat.remove_widget(self._pending)
            self._pending = None
        self._set_busy(False)
        self.refresh_memory()

        if not isinstance(result, TurnResult):
            self._chat.add_bubble("error", "Unexpected response from the worker.")
            return

        if result.memory_notes:
            for note in result.memory_notes:
                for filename, idx in TAB_INDEX.items():
                    if filename in note:
                        self._tabs.setCurrentIndex(idx)
                        break

        if not result.llm_available:
            self._chat.add_bubble("error", result.reply, result.memory_notes)
            self._update_status(result.error or "LLM unavailable")
            return

        if not result.ok:
            msg = f"LLM call failed: {result.error}"
            if result.memory_notes:
                msg += "\n\nMemory was still updated."
            self._chat.add_bubble("error", msg, result.memory_notes)
            self._update_status(result.error or "LLM error")
            return

        self._history.append(("user", result.user_text))
        self._history.append(("assistant", result.reply))
        self._chat.add_bubble("assistant", result.reply, result.memory_notes)
        extra = (
            "Memory updated" if result.memory_notes else "Reply received"
        )
        self._update_status(extra)
        self._input.setFocus()


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(
        description="PySide6 desktop frontend for the OgunJob master agent"
    )
    parser.add_argument(
        "--provider",
        choices=list(SUPPORTED_PROVIDERS),
        default=None,
        help="LLM provider (overrides LLM_PROVIDER in .env)",
    )
    args, qt_args = parser.parse_known_args(argv)

    qt_argv = [sys.argv[0], *qt_args]
    app = QApplication(qt_argv)
    app.setApplicationName("OgunJob")
    app.setStyle("Fusion")
    _apply_dark_palette(app)
    app.setStyleSheet(STYLESHEET)
    app.setFont(QFont("Segoe UI", 10))

    window = MainWindow(provider=args.provider)
    window.show()
    return app.exec()


if __name__ == "__main__":
    raise SystemExit(main())
