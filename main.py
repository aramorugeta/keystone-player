#!/usr/bin/env python3
"""
Keystone Player - 프로젝터용 좌우 키스톤 보정 영상 플레이어
에뮬레이터 창에서 키스톤 보정 미리보기 + 프로젝터 출력
파일(mpv) 재생 및 브라우저(QWebEngineView) 재생 지원
"""

import sys
import os
import json
import socket
import tempfile
from pathlib import Path

SETTINGS_DIR = os.path.join(Path.home(), ".local", "share", "keystone-player")
SETTINGS_FILE = os.path.join(SETTINGS_DIR, "settings.json")


def load_settings() -> dict:
    try:
        with open(SETTINGS_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_settings(data: dict):
    os.makedirs(SETTINGS_DIR, exist_ok=True)
    with open(SETTINGS_FILE, "w") as f:
        json.dump(data, f)

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QSlider, QLabel, QFileDialog, QComboBox,
    QGroupBox, QSizePolicy, QLineEdit,
    QCheckBox, QGraphicsView, QGraphicsScene, QToolBar,
)
from PySide6.QtCore import Qt, QUrl, QPointF, QRectF, QProcess, QTimer
from PySide6.QtGui import QFont, QTransform, QPolygonF
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineSettings, QWebEngineProfile, QWebEnginePage,
)

LOGICAL_W = 1920
LOGICAL_H = 1080


def make_graphics_view(scene: QGraphicsScene) -> QGraphicsView:
    view = QGraphicsView(scene)
    view.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.setVerticalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
    view.setFrameShape(QGraphicsView.NoFrame)
    view.setBackgroundBrush(Qt.black)
    from PySide6.QtGui import QPainter
    view.setRenderHints(
        QPainter.Antialiasing | QPainter.SmoothPixmapTransform
    )
    return view


def compute_view_transform(view: QGraphicsView, scene_rect: QRectF, keystone: int) -> QTransform:
    vw = view.viewport().width()
    vh = view.viewport().height()
    sw = scene_rect.width()
    sh = scene_rect.height()
    if sw == 0 or sh == 0:
        return QTransform()

    scale = min(vw / sw, vh / sh)
    ox = (vw - sw * scale) / 2
    oy = (vh - sh * scale) / 2

    fit = QTransform()
    fit.translate(ox, oy)
    fit.scale(scale, scale)

    if keystone == 0:
        return fit

    offset = abs(keystone) / 100.0 * vh * 0.25
    src = QPolygonF([
        QPointF(0, 0), QPointF(vw, 0),
        QPointF(vw, vh), QPointF(0, vh),
    ])
    if keystone > 0:
        dst = QPolygonF([
            QPointF(0, 0), QPointF(vw, offset),
            QPointF(vw, vh - offset), QPointF(0, vh),
        ])
    else:
        dst = QPolygonF([
            QPointF(0, offset), QPointF(vw, 0),
            QPointF(vw, vh), QPointF(0, vh - offset),
        ])
    ks = QTransform()
    QTransform.quadToQuad(src, dst, ks)
    return ks * fit


class ProjectorWindow(QMainWindow):
    """에뮬레이터 미리보기 창 (브라우저 모드) + 프로젝터 출력 관리"""

    on_closed = None

    def __init__(self):
        super().__init__()
        self.setWindowTitle("Projector Emulator")
        self.setAttribute(Qt.WA_DeleteOnClose, False)

        # QGraphicsScene + QWebEngineView (영구 유지, 재생성 안 함)
        self.scene = QGraphicsScene()
        self.scene.setSceneRect(0, 0, LOGICAL_W, LOGICAL_H)

        self.view = make_graphics_view(self.scene)
        self.setCentralWidget(self.view)

        # 브라우저 - 한 번만 생성
        storage_path = os.path.join(Path.home(), ".local", "share", "keystone-player")
        self._profile = QWebEngineProfile("keystone")
        self._profile.setPersistentStoragePath(storage_path)
        self._profile.setHttpCacheType(QWebEngineProfile.DiskHttpCache)
        self._profile.setPersistentCookiesPolicy(QWebEngineProfile.ForcePersistentCookies)
        self._profile.setHttpUserAgent(
            "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
            "(KHTML, like Gecko) Chrome/125.0.0.0 Safari/537.36"
        )

        self._page = QWebEnginePage(self._profile, self)
        self.web_view = QWebEngineView()
        self.web_view.setPage(self._page)

        settings = self.web_view.settings()
        settings.setAttribute(QWebEngineSettings.PlaybackRequiresUserGesture, False)
        settings.setAttribute(QWebEngineSettings.FullScreenSupportEnabled, True)
        settings.setAttribute(QWebEngineSettings.JavascriptEnabled, True)

        self.web_view.setFixedSize(LOGICAL_W, LOGICAL_H)
        self.proxy_widget = self.scene.addWidget(self.web_view)
        # 처음엔 숨김
        self.proxy_widget.setVisible(False)

        self._keystone_value = 0
        self._content_mode = None  # "browser" or None (mpv는 별도 창)

        # 프로젝터 출력 (복제 창)
        self.output_window: QMainWindow | None = None
        self.output_view: QGraphicsView | None = None

        self._build_toolbar()

    def _build_toolbar(self):
        toolbar = QToolBar()
        toolbar.setMovable(False)
        toolbar.setFloatable(False)

        toolbar.addWidget(QLabel(" 출력: "))
        self.screen_combo = QComboBox()
        self.screen_combo.setMinimumWidth(200)
        toolbar.addWidget(self.screen_combo)

        btn_refresh = QPushButton("새로고침")
        btn_refresh.clicked.connect(self._refresh_screens)
        toolbar.addWidget(btn_refresh)

        self.output_btn = QPushButton("프로젝터 출력")
        self.output_btn.setCheckable(True)
        self.output_btn.toggled.connect(self._toggle_output)
        toolbar.addWidget(self.output_btn)

        self.addToolBar(toolbar)
        self._refresh_screens()

    def _refresh_screens(self):
        self.screen_combo.clear()
        app = QApplication.instance()
        primary = app.primaryScreen()
        for i, screen in enumerate(app.screens()):
            if screen == primary:
                continue
            geo = screen.geometry()
            name = screen.name()
            self.screen_combo.addItem(
                f"{name} ({geo.width()}x{geo.height()} @ {geo.x()},{geo.y()})", i
            )

    def _toggle_output(self, checked: bool):
        if checked:
            self._start_output()
        else:
            self._stop_output()

    def _start_output(self):
        idx = self.screen_combo.currentData()
        app = QApplication.instance()
        screens = app.screens()
        if idx is None or idx >= len(screens):
            self.output_btn.setChecked(False)
            return

        screen = screens[idx]
        if screen == app.primaryScreen():
            self.output_btn.setChecked(False)
            return

        self.output_window = QMainWindow()
        self.output_window.setWindowTitle("Projector Output")
        self.output_view = make_graphics_view(self.scene)
        self.output_window.setCentralWidget(self.output_view)

        geo = screen.geometry()
        self.output_window.setGeometry(geo)
        self.output_window.showFullScreen()

        self._update_all_transforms()
        self.output_btn.setText(f"출력 중: {screen.name()}")
        self.screen_combo.setEnabled(False)

    def _stop_output(self):
        if self.output_window:
            self.output_window.close()
            self.output_window = None
            self.output_view = None
        self.output_btn.setText("프로젝터 출력")
        self.screen_combo.setEnabled(True)

    # ---- 콘텐츠 ----

    def show_browser(self, url: str):
        """브라우저 표시 (재생성 없이 URL만 변경)"""
        self._content_mode = "browser"
        self.proxy_widget.setVisible(True)
        self.web_view.load(QUrl(url))
        self._update_all_transforms()

    def hide_content(self):
        """콘텐츠 숨기기 (브라우저는 파괴하지 않고 숨김)"""
        self.proxy_widget.setVisible(False)
        self.web_view.load(QUrl("about:blank"))
        self._content_mode = None

    # ---- 키스톤 ----

    def set_keystone(self, value: int):
        self._keystone_value = value
        self._update_all_transforms()

    def _update_all_transforms(self):
        scene_rect = self.scene.sceneRect()
        k = self._keystone_value
        self.view.setTransform(compute_view_transform(self.view, scene_rect, k))
        if self.output_view:
            self.output_view.setTransform(
                compute_view_transform(self.output_view, scene_rect, k)
            )

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._update_all_transforms()

    def closeEvent(self, event):
        self._stop_output()
        self.hide_content()
        if self.on_closed:
            self.on_closed()
        super().closeEvent(event)


class KeystonePlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Keystone Player")
        self.setMinimumSize(560, 480)

        self.current_file = ""
        self._settings = load_settings()
        self.keystone_value = self._settings.get("keystone", 0)
        self.projector_window: ProjectorWindow | None = None
        self.playback_mode = "file"  # "file" or "browser"

        # mpv (파일 재생용)
        self.mpv_process: QProcess | None = None
        self.ipc_path = os.path.join(tempfile.gettempdir(), f"keystone-mpv-{os.getpid()}")
        self.is_paused = False

        self._build_ui()

        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._poll_mpv_status)
        self.status_timer.start(500)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(10)

        # --- 모드 선택 ---
        mode_group = QGroupBox("재생 모드")
        mode_layout = QVBoxLayout(mode_group)

        mode_row = QHBoxLayout()
        self.mode_combo = QComboBox()
        self.mode_combo.addItem("파일 (mpv)", "file")
        self.mode_combo.addItem("브라우저 (Web)", "browser")
        self.mode_combo.currentIndexChanged.connect(self._on_mode_changed)
        mode_row.addWidget(QLabel("모드:"))
        mode_row.addWidget(self.mode_combo)
        mode_layout.addLayout(mode_row)

        # 파일 선택
        self.file_widget = QWidget()
        file_layout = QHBoxLayout(self.file_widget)
        file_layout.setContentsMargins(0, 0, 0, 0)
        self.file_label = QLabel("선택된 파일 없음")
        self.file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.file_label.setWordWrap(True)
        btn_open = QPushButton("열기")
        btn_open.setFixedWidth(80)
        btn_open.clicked.connect(self._open_file)
        file_layout.addWidget(self.file_label)
        file_layout.addWidget(btn_open)
        mode_layout.addWidget(self.file_widget)

        # URL 입력
        self.browser_widget = QWidget()
        browser_layout = QHBoxLayout(self.browser_widget)
        browser_layout.setContentsMargins(0, 0, 0, 0)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self.url_input.returnPressed.connect(self._play_browser)
        btn_go = QPushButton("이동")
        btn_go.setFixedWidth(80)
        btn_go.clicked.connect(self._play_browser)
        browser_layout.addWidget(self.url_input)
        browser_layout.addWidget(btn_go)
        mode_layout.addWidget(self.browser_widget)
        self.browser_widget.hide()

        layout.addWidget(mode_group)

        # --- 에뮬레이터 ---
        emu_row = QHBoxLayout()
        self.emulator_check = QCheckBox("에뮬레이터 (프로젝터 미리보기 창)")
        self.emulator_check.setChecked(False)
        self.emulator_check.toggled.connect(self._on_emulator_toggled)
        emu_row.addWidget(self.emulator_check)
        layout.addLayout(emu_row)

        # --- 키스톤 보정 ---
        ks_group = QGroupBox("좌우 키스톤 보정")
        ks_layout = QVBoxLayout(ks_group)

        slider_row = QHBoxLayout()
        slider_row.addWidget(QLabel("◁ 좌"))
        self.ks_slider = QSlider(Qt.Horizontal)
        self.ks_slider.setRange(-100, 100)
        self.ks_slider.setValue(self.keystone_value)
        self.ks_slider.setTickPosition(QSlider.TicksBelow)
        self.ks_slider.setTickInterval(10)
        self.ks_slider.valueChanged.connect(self._on_keystone_changed)
        slider_row.addWidget(self.ks_slider)
        slider_row.addWidget(QLabel("우 ▷"))
        ks_layout.addLayout(slider_row)

        value_row = QHBoxLayout()
        self.ks_value_label = QLabel("0")
        self.ks_value_label.setAlignment(Qt.AlignCenter)
        self.ks_value_label.setFont(QFont("monospace", 16, QFont.Bold))
        btn_reset = QPushButton("초기화")
        btn_reset.setFixedWidth(80)
        btn_reset.clicked.connect(lambda: self.ks_slider.setValue(0))
        value_row.addStretch()
        value_row.addWidget(self.ks_value_label)
        value_row.addStretch()
        value_row.addWidget(btn_reset)
        ks_layout.addLayout(value_row)

        fine_row = QHBoxLayout()
        for label, delta in [("-5", -5), ("-1", -1), ("+1", 1), ("+5", 5)]:
            btn = QPushButton(label)
            btn.setFixedWidth(60)
            btn.clicked.connect(lambda checked, d=delta: self.ks_slider.setValue(
                max(-100, min(100, self.ks_slider.value() + d))
            ))
            fine_row.addWidget(btn)
        ks_layout.addLayout(fine_row)

        layout.addWidget(ks_group)

        # --- 재생 컨트롤 ---
        ctrl_group = QGroupBox("재생")
        ctrl_layout = QHBoxLayout(ctrl_group)
        self.btn_play = QPushButton("▶ 재생")
        self.btn_play.clicked.connect(self._play)
        self.btn_pause = QPushButton("⏸ 일시정지")
        self.btn_pause.clicked.connect(self._toggle_pause)
        self.btn_stop = QPushButton("⏹ 정지")
        self.btn_stop.clicked.connect(self._stop)
        for btn in [self.btn_play, self.btn_pause, self.btn_stop]:
            ctrl_layout.addWidget(btn)
        layout.addWidget(ctrl_group)

        # --- 상태 ---
        self.status_label = QLabel("준비")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

    # ---- UI 이벤트 ----

    def _on_mode_changed(self, index: int):
        mode = self.mode_combo.currentData()
        self._stop_mpv()
        if self.projector_window:
            self.projector_window.hide_content()
        self.playback_mode = mode
        self.file_widget.setVisible(mode == "file")
        self.browser_widget.setVisible(mode == "browser")
        self._update_playback_buttons()
        self.status_label.setText("준비")

    def _update_playback_buttons(self):
        is_file = self.playback_mode == "file"
        self.btn_play.setEnabled(is_file)
        self.btn_pause.setEnabled(is_file)
        self.btn_stop.setEnabled(is_file)

    def _on_emulator_toggled(self, checked: bool):
        if checked:
            self._ensure_projector_window()
        else:
            self._stop_mpv()
            if self.projector_window:
                self.projector_window.hide_content()
                self.projector_window._stop_output()
                self.projector_window.hide()
            self.status_label.setText("준비")

    def _on_emulator_window_closed(self):
        self._stop_mpv()
        self.emulator_check.blockSignals(True)
        self.emulator_check.setChecked(False)
        self.emulator_check.blockSignals(False)
        self.status_label.setText("준비")

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "영상 파일 선택", str(Path.home()),
            "영상 파일 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.iso);;모든 파일 (*)",
        )
        if path:
            self.current_file = path
            self.file_label.setText(os.path.basename(path))

    def _ensure_projector_window(self) -> ProjectorWindow:
        if self.projector_window is None:
            self.projector_window = ProjectorWindow()
            self.projector_window.on_closed = self._on_emulator_window_closed

        pw = self.projector_window
        pw.set_keystone(self.keystone_value)
        if not pw.isVisible():
            pw.resize(960, 540)
            pw.show()
        return pw

    # ---- 키스톤 ----

    def _calc_perspective_filter(self, k: int, w: int, h: int) -> str:
        if k == 0:
            return ""
        offset = int(abs(k) / 100.0 * h * 0.25)
        if k > 0:
            x0, y0 = 0, 0
            x1, y1 = w, offset
            x2, y2 = 0, h
            x3, y3 = w, h - offset
        else:
            x0, y0 = 0, offset
            x1, y1 = w, 0
            x2, y2 = 0, h - offset
            x3, y3 = w, h
        return f"perspective={x0}:{y0}:{x1}:{y1}:{x2}:{y2}:{x3}:{y3}:cubic"

    def _on_keystone_changed(self, value: int):
        self.keystone_value = value
        self.ks_value_label.setText(str(value))

        # 설정 저장
        self._settings["keystone"] = value
        save_settings(self._settings)

        # mpv 파일 모드: FFmpeg perspective 필터
        if self.mpv_process and self.mpv_process.state() == QProcess.Running:
            vf = self._calc_perspective_filter(value, 1920, 1080)
            if vf:
                self._send_mpv_command(["set_property", "vf", f"lavfi=[{vf}]"])
            else:
                self._send_mpv_command(["set_property", "vf", ""])

        # 브라우저 모드: QTransform
        if self.projector_window:
            self.projector_window.set_keystone(value)

    # ---- 재생 ----

    def _play(self):
        if self.playback_mode == "file":
            self._play_file()
        else:
            self._play_browser()

    def _play_file(self):
        if not self.current_file:
            self.status_label.setText("파일을 먼저 선택하세요")
            return

        self._stop_mpv()

        args = [
            "mpv",
            "--input-ipc-server=" + self.ipc_path,
            "--geometry=960x540",
            "--keep-open=yes",
            "--osd-level=0",
            f"--title=Keystone Player - {os.path.basename(self.current_file)}",
        ]

        vf = self._calc_perspective_filter(self.keystone_value, 1920, 1080)
        if vf:
            args.append(f"--vf=lavfi=[{vf}]")

        args.append(self.current_file)

        self.mpv_process = QProcess(self)
        self.mpv_process.finished.connect(self._on_mpv_finished)
        self.mpv_process.start(args[0], args[1:])
        self.is_paused = False
        self.status_label.setText(f"재생 중: {os.path.basename(self.current_file)}")

    def _play_browser(self):
        url = self.url_input.text().strip()
        if not url:
            self.status_label.setText("URL을 입력하세요")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

        self._stop_mpv()
        self.emulator_check.setChecked(True)
        pw = self._ensure_projector_window()
        pw.show_browser(url)
        self.status_label.setText(f"브라우저: {url}")

    def _toggle_pause(self):
        if self.playback_mode == "file":
            if self.mpv_process and self.mpv_process.state() == QProcess.Running:
                self._send_mpv_command(["cycle", "pause"])
                self.is_paused = not self.is_paused
                if self.is_paused:
                    self.status_label.setText("일시정지")
                else:
                    self.status_label.setText(f"재생 중: {os.path.basename(self.current_file)}")

    def _stop(self):
        self._stop_mpv()
        if self.projector_window:
            self.projector_window.hide_content()
        self.is_paused = False
        self.status_label.setText("정지됨")

    # ---- mpv ----

    def _stop_mpv(self):
        if self.mpv_process and self.mpv_process.state() != QProcess.NotRunning:
            self._send_mpv_command(["quit"])
            self.mpv_process.waitForFinished(2000)
            if self.mpv_process.state() != QProcess.NotRunning:
                self.mpv_process.kill()
        self.mpv_process = None
        self.is_paused = False
        try:
            os.unlink(self.ipc_path)
        except FileNotFoundError:
            pass

    def _send_mpv_command(self, cmd: list):
        try:
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(1.0)
            sock.connect(self.ipc_path)
            payload = json.dumps({"command": cmd}) + "\n"
            sock.sendall(payload.encode())
            sock.close()
        except (ConnectionRefusedError, FileNotFoundError, OSError):
            pass

    def _poll_mpv_status(self):
        if self.mpv_process and self.mpv_process.state() == QProcess.NotRunning:
            self.mpv_process = None
            self.status_label.setText("재생 완료")

    def _on_mpv_finished(self):
        self.status_label.setText("재생 완료")
        self.is_paused = False

    def closeEvent(self, event):
        self._stop_mpv()
        if self.projector_window:
            self.projector_window.close()
            self.projector_window = None
        super().closeEvent(event)


def _setup_widevine():
    # Widevine CDM 디렉토리를 앱 데이터 경로에 심볼릭 링크로 연결
    storage = os.path.join(Path.home(), ".local", "share", "keystone-player")
    cdm_link = os.path.join(storage, "WidevineCdm")
    cdm_source = os.path.join(Path.home(), ".config", "chromium", "WidevineCdm")

    if os.path.isdir(cdm_source) and not os.path.exists(cdm_link):
        os.makedirs(storage, exist_ok=True)
        os.symlink(cdm_source, cdm_link)

    # CDM 디렉토리 경로를 Chromium에 전달
    if os.path.isdir(cdm_link):
        flags = os.environ.get("QTWEBENGINE_CHROMIUM_FLAGS", "")
        os.environ["QTWEBENGINE_CHROMIUM_FLAGS"] = (
            f"{flags} --component-updater=fast-update"
            f" --widevine-cdn-path={cdm_source}"
        ).strip()


def main():
    _setup_widevine()
    app = QApplication(sys.argv)
    app.setApplicationName("Keystone Player")
    window = KeystonePlayer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
