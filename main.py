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
from PySide6.QtCore import Qt, QUrl, QPointF, QRectF, QProcess, QTimer, QSizeF
from PySide6.QtGui import QFont, QTransform, QPolygonF
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
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


def compute_view_transform(
    view: QGraphicsView,
    scene_rect: QRectF,
    keystone: int,
    aspect: int = 0,
) -> QTransform:
    """
    aspect: -50 ~ +50, 0이면 1.0배. 음수는 좌우 압축, 양수는 좌우 늘림.
    """
    vw = view.viewport().width()
    vh = view.viewport().height()
    sw = scene_rect.width()
    sh = scene_rect.height()
    if sw == 0 or sh == 0:
        return QTransform()

    scale = min(vw / sw, vh / sh)
    # 가로 비율 보정: -50 ~ +50 → 0.7 ~ 1.3 배율
    h_scale = 1.0 + aspect / 100.0 * 0.6
    scaled_w = sw * scale * h_scale
    scaled_h = sh * scale
    ox = (vw - scaled_w) / 2
    oy = (vh - scaled_h) / 2

    fit = QTransform()
    fit.translate(ox, oy)
    fit.scale(scale * h_scale, scale)

    if keystone == 0:
        return fit

    # 키스톤은 늘려진 영역 기준
    offset = abs(keystone) / 100.0 * vh * 0.25
    x_left = ox
    x_right = ox + scaled_w
    y_top = oy
    y_bot = oy + scaled_h

    # 현재 fit 변환 후의 사각형 → 키스톤 적용된 사각형
    src = QPolygonF([
        QPointF(x_left, y_top), QPointF(x_right, y_top),
        QPointF(x_right, y_bot), QPointF(x_left, y_bot),
    ])
    if keystone > 0:
        dst = QPolygonF([
            QPointF(x_left, y_top), QPointF(x_right, y_top + offset),
            QPointF(x_right, y_bot), QPointF(x_left, y_bot - offset),
        ])
    else:
        dst = QPolygonF([
            QPointF(x_left, y_top + offset), QPointF(x_right, y_top),
            QPointF(x_right, y_bot - offset), QPointF(x_left, y_bot),
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
        self.proxy_widget.setVisible(False)

        # 영상 - 한 번만 생성, 재사용
        self.video_item = QGraphicsVideoItem()
        self.video_item.setSize(QSizeF(LOGICAL_W, LOGICAL_H))
        self.scene.addItem(self.video_item)
        self.video_item.setVisible(False)

        self.audio = QAudioOutput()
        self.player = QMediaPlayer()
        self.player.setAudioOutput(self.audio)
        self.player.setVideoOutput(self.video_item)

        self._keystone_value = 0
        self._aspect_value = 0
        self._content_mode = None  # "browser" / "video" / None

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

        toolbar.addSeparator()
        toolbar.addWidget(QLabel(" 🔊 "))
        self.volume_slider = QSlider(Qt.Horizontal)
        self.volume_slider.setRange(0, 100)
        self.volume_slider.setFixedWidth(120)
        self.volume_slider.valueChanged.connect(self._on_volume_changed)
        toolbar.addWidget(self.volume_slider)
        self.volume_label = QLabel("100%")
        self.volume_label.setFixedWidth(45)
        toolbar.addWidget(self.volume_label)

        self.addToolBar(toolbar)
        self._refresh_screens()

    on_volume_changed = None  # KeystonePlayer가 설정 저장용으로 사용

    def set_volume(self, value: int):
        """볼륨 설정 (0-100). 시그널 발생 안 시킴."""
        self.volume_slider.blockSignals(True)
        self.volume_slider.setValue(value)
        self.volume_slider.blockSignals(False)
        self._apply_volume(value)

    def _on_volume_changed(self, value: int):
        self._apply_volume(value)
        if self.on_volume_changed:
            self.on_volume_changed(value)

    def _apply_volume(self, value: int):
        self.audio.setVolume(value / 100.0)
        self.volume_label.setText(f"{value}%")

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

    # ---- 화면보호기 ----

    _inhibit_cookie: int | None = None

    def _inhibit_screensaver(self):
        if self._inhibit_cookie is not None:
            return
        try:
            import subprocess
            result = subprocess.run(
                ["qdbus", "org.freedesktop.ScreenSaver",
                 "/org/freedesktop/ScreenSaver",
                 "org.freedesktop.ScreenSaver.Inhibit",
                 "Keystone Player", "Playing content"],
                capture_output=True, text=True, timeout=2,
            )
            if result.returncode == 0:
                self._inhibit_cookie = int(result.stdout.strip())
        except (subprocess.TimeoutExpired, ValueError, FileNotFoundError):
            pass

    def _uninhibit_screensaver(self):
        if self._inhibit_cookie is None:
            return
        try:
            import subprocess
            subprocess.run(
                ["qdbus", "org.freedesktop.ScreenSaver",
                 "/org/freedesktop/ScreenSaver",
                 "org.freedesktop.ScreenSaver.UnInhibit",
                 str(self._inhibit_cookie)],
                capture_output=True, timeout=2,
            )
        except (subprocess.TimeoutExpired, FileNotFoundError):
            pass
        self._inhibit_cookie = None

    # ---- 콘텐츠 ----

    def show_browser(self, url: str):
        """브라우저 표시 (재생성 없이 URL만 변경)"""
        self._stop_video()
        self._content_mode = "browser"
        self.video_item.setVisible(False)
        self.proxy_widget.setVisible(True)
        self.web_view.load(QUrl(url))
        self._inhibit_screensaver()
        self._update_all_transforms()

    def show_video(self, file_path: str):
        """영상 재생 (재생성 없이 source만 변경)"""
        self._content_mode = "video"
        self.proxy_widget.setVisible(False)
        self.web_view.load(QUrl("about:blank"))
        self.video_item.setVisible(True)
        self.player.setSource(QUrl.fromLocalFile(file_path))
        self.player.play()
        self._inhibit_screensaver()
        self._update_all_transforms()

    def play_video(self):
        if self._content_mode == "video":
            self.player.play()

    def pause_video(self):
        if self._content_mode == "video":
            self.player.pause()

    def is_video_playing(self) -> bool:
        return (
            self._content_mode == "video"
            and self.player.playbackState() == QMediaPlayer.PlayingState
        )

    def is_video_paused(self) -> bool:
        return (
            self._content_mode == "video"
            and self.player.playbackState() == QMediaPlayer.PausedState
        )

    def _stop_video(self):
        if self.player.playbackState() != QMediaPlayer.StoppedState:
            self.player.stop()

    def hide_content(self):
        """콘텐츠 숨기기 (재생성 없이 숨김만)"""
        self._stop_video()
        self.video_item.setVisible(False)
        self.proxy_widget.setVisible(False)
        self.web_view.load(QUrl("about:blank"))
        self._uninhibit_screensaver()
        self._content_mode = None

    # ---- 키스톤 ----

    def set_keystone(self, value: int):
        self._keystone_value = value
        self._update_all_transforms()

    def set_aspect(self, value: int):
        self._aspect_value = value
        self._update_all_transforms()

    def _update_all_transforms(self):
        scene_rect = self.scene.sceneRect()
        k = self._keystone_value
        a = self._aspect_value
        self.view.setTransform(compute_view_transform(self.view, scene_rect, k, a))
        if self.output_view:
            self.output_view.setTransform(
                compute_view_transform(self.output_view, scene_rect, k, a)
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
        self.volume_value = self._settings.get("volume", 100)
        self.aspect_value = self._settings.get("aspect", 0)
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

        # --- 좌우 비율 ---
        aspect_group = QGroupBox("좌우 비율")
        aspect_layout = QHBoxLayout(aspect_group)
        aspect_layout.addWidget(QLabel("⇤"))
        self.aspect_slider = QSlider(Qt.Horizontal)
        self.aspect_slider.setRange(-50, 50)
        self.aspect_slider.setValue(self.aspect_value)
        self.aspect_slider.setTickPosition(QSlider.TicksBelow)
        self.aspect_slider.setTickInterval(10)
        self.aspect_slider.valueChanged.connect(self._on_aspect_changed)
        aspect_layout.addWidget(self.aspect_slider)
        aspect_layout.addWidget(QLabel("⇥"))
        self.aspect_value_label = QLabel(str(self.aspect_value))
        self.aspect_value_label.setFixedWidth(40)
        self.aspect_value_label.setAlignment(Qt.AlignCenter)
        aspect_layout.addWidget(self.aspect_value_label)
        btn_aspect_reset = QPushButton("초기화")
        btn_aspect_reset.setFixedWidth(80)
        btn_aspect_reset.clicked.connect(lambda: self.aspect_slider.setValue(0))
        aspect_layout.addWidget(btn_aspect_reset)
        layout.addWidget(aspect_group)

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
            self.projector_window.on_volume_changed = self._on_volume_changed
            self.projector_window.set_volume(self.volume_value)

        pw = self.projector_window
        pw.set_keystone(self.keystone_value)
        pw.set_aspect(self.aspect_value)
        if not pw.isVisible():
            pw.resize(960, 540)
            pw.show()
        return pw

    def _on_volume_changed(self, value: int):
        self.volume_value = value
        self._settings["volume"] = value
        save_settings(self._settings)

    def _on_aspect_changed(self, value: int):
        self.aspect_value = value
        self.aspect_value_label.setText(str(value))
        self._settings["aspect"] = value
        save_settings(self._settings)
        if self.projector_window:
            self.projector_window.set_aspect(value)

    # ---- 키스톤 ----

    def _calc_perspective_filter(self, k: int, w: int, h: int) -> str:
        if k == 0:
            return ""
        offset = int(abs(k) / 100.0 * h * 0.25)
        if k > 0:
            x0, y0 = 0, 0
            x1, y1 = w, offset
            x2, y2 = 0, h - offset
            x3, y3 = w, h
        else:
            x0, y0 = 0, offset
            x1, y1 = w, 0
            x2, y2 = 0, h
            x3, y3 = w, h - offset
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
        self.emulator_check.setChecked(True)
        pw = self._ensure_projector_window()
        pw.show_video(self.current_file)
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
        if self.playback_mode != "file" or not self.projector_window:
            return
        pw = self.projector_window
        if pw.is_video_playing():
            pw.pause_video()
            self.status_label.setText("일시정지")
        elif pw.is_video_paused():
            pw.play_video()
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
