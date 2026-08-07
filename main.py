#!/usr/bin/env python3
"""
Keystone Player - 프로젝터용 좌우 키스톤 보정 영상 플레이어
에뮬레이터 창에서 키스톤 보정 미리보기 + 프로젝터 출력
파일(QMediaPlayer) 재생 및 브라우저(QWebEngineView) 재생 지원
"""

import sys
import os
import json
import statistics
from collections import deque
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
    QGroupBox, QSizePolicy, QLineEdit, QSpinBox,
    QCheckBox, QGraphicsView, QGraphicsScene, QToolBar,
)
from PySide6.QtCore import Qt, QUrl, QPointF, QRectF, QTimer, QSizeF
from PySide6.QtGui import QFont, QIcon, QTransform, QPolygonF
from PySide6.QtMultimedia import QMediaPlayer, QAudioOutput
from PySide6.QtMultimediaWidgets import QGraphicsVideoItem
from PySide6.QtWebEngineWidgets import QWebEngineView
from PySide6.QtWebEngineCore import (
    QWebEngineSettings, QWebEngineProfile, QWebEnginePage,
)

import audio_dsp
import net_audio
import video_delay

LOGICAL_W = 1920
LOGICAL_H = 1080

# 폰이 보고하는 지연은 초 단위로 수십 ms 씩 흔들린다. 그대로 따라가면 영상이
# 계속 앞뒤로 밀려서 떨린다. 중앙값을 쓰고, 사람이 알아챌 만큼 어긋났을 때만 움직인다.
# 립싱크는 소리가 +45ms 늦으면 감지되기 시작한다 (ITU-R BT.1359).
# 그 아래로 따라가봐야 보이지도 않는 흔들림만 만든다.
LATENCY_WINDOW = 8
DELAY_HYSTERESIS_MS = 40

# 브라우저 모드에서 바로 고를 수 있는 스트리밍 사이트
STREAMING_SITES = [
    ("넷플릭스", "https://www.netflix.com"),
    ("디즈니+", "https://www.disneyplus.com"),
    ("티빙", "https://www.tving.com"),
    ("직접 입력", ""),
]


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
        # 립싱크 보정용 지연 큐를 거쳐 화면으로 나간다 (지연 0 이면 그대로 통과)
        self.frame_delay = video_delay.FrameDelay(self.video_item.videoSink(), self)
        self.player.setVideoOutput(self.frame_delay.sink)

        self._keystone_value = 0
        self._aspect_value = 0
        self._content_mode = None  # "browser" / "video" / None
        self.dsp = None  # KeystonePlayer 가 AudioDSP 를 넣어준다

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
        if self.dsp is not None and self.dsp.is_running():
            # DSP 사용 중에는 컴프레서에 풀스케일로 넣고,
            # 볼륨은 리미터 뒤 출력 게인으로 조절한다 (브라우저 소리에도 같이 적용됨)
            self.audio.setVolume(1.0)
            self.dsp.set_volume(value)
        else:
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
            self.frame_delay.clear()

    def set_video_delay(self, ms: int):
        """립싱크 보정: 영상을 ms 만큼 늦춰서 표시 (파일 재생에만 적용)."""
        self.frame_delay.set_delay_ms(ms)

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
        self.frame_delay.clear()

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

        # 사운드 보정 (볼륨 평준화 + 최대 출력 제한)
        self.dsp = audio_dsp.AudioDSP(audio_dsp.default_storage_dir(), self)
        self.dsp.set_preset(self._settings.get("audio_preset", audio_dsp.DEFAULT_PRESET))
        self.dsp.set_ceiling_db(
            self._settings.get("audio_ceiling", audio_dsp.DEFAULT_CEILING)
        )
        self.dsp.set_volume(self.volume_value)

        # 폰으로 소리 보내기 (컨트롤 채널)
        self.control = net_audio.ControlServer(self)
        self.control.connected.connect(self._on_phone_connected)
        self.control.disconnected.connect(self._on_phone_disconnected)
        self.control.latencyReported.connect(self._on_phone_latency)
        self.control.trimRequested.connect(self._on_phone_trim)
        self.control.statusChanged.connect(self._on_phone_status)
        self.control.set_log_dir(audio_dsp.default_storage_dir())
        self._latency_window: deque = deque(maxlen=LATENCY_WINDOW)

        self._build_ui()

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
        self.mode_combo.addItem("파일", "file")
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
        self.site_combo = QComboBox()
        for label, url in STREAMING_SITES:
            self.site_combo.addItem(label, url)
        self.site_combo.currentIndexChanged.connect(self._on_site_changed)
        browser_layout.addWidget(self.site_combo)
        self.url_input = QLineEdit()
        self.url_input.setPlaceholderText("https://www.youtube.com/watch?v=...")
        self.url_input.returnPressed.connect(self._play_browser)
        btn_go = QPushButton("이동")
        btn_go.setFixedWidth(80)
        btn_go.clicked.connect(self._play_browser)
        browser_layout.addWidget(self.url_input)
        browser_layout.addWidget(btn_go)
        mode_layout.addWidget(self.browser_widget)
        self.url_input.setText(self.site_combo.currentData())
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

        # --- 사운드 보정 ---
        sound_group = QGroupBox("사운드 보정")
        sound_layout = QVBoxLayout(sound_group)

        dsp_row = QHBoxLayout()
        self.dsp_check = QCheckBox("볼륨 평준화 (대화 크게 / 액션 작게)")
        self.dsp_check.toggled.connect(self._on_dsp_toggled)
        dsp_row.addWidget(self.dsp_check)
        dsp_row.addStretch()
        dsp_row.addWidget(QLabel("강도:"))
        self.dsp_preset_combo = QComboBox()
        for key, spec in audio_dsp.PRESETS.items():
            self.dsp_preset_combo.addItem(spec[0], key)
        self.dsp_preset_combo.currentIndexChanged.connect(self._on_dsp_preset_changed)
        dsp_row.addWidget(self.dsp_preset_combo)
        sound_layout.addLayout(dsp_row)

        ceiling_row = QHBoxLayout()
        ceiling_row.addWidget(QLabel("최대 출력 제한:"))
        self.ceiling_slider = QSlider(Qt.Horizontal)
        self.ceiling_slider.setRange(-20, 0)
        self.ceiling_slider.setTickPosition(QSlider.TicksBelow)
        self.ceiling_slider.setTickInterval(2)
        self.ceiling_slider.valueChanged.connect(self._on_ceiling_changed)
        ceiling_row.addWidget(self.ceiling_slider)
        self.ceiling_label = QLabel()
        self.ceiling_label.setFixedWidth(55)
        self.ceiling_label.setAlignment(Qt.AlignCenter)
        ceiling_row.addWidget(self.ceiling_label)
        sound_layout.addLayout(ceiling_row)

        delay_row = QHBoxLayout()
        delay_row.addWidget(QLabel("영상 지연 보정:"))
        self.delay_slider = QSlider(Qt.Horizontal)
        self.delay_slider.setRange(0, video_delay.MAX_DELAY_MS)
        self.delay_slider.setTickPosition(QSlider.TicksBelow)
        self.delay_slider.setTickInterval(50)
        self.delay_slider.valueChanged.connect(self._on_video_delay_changed)
        delay_row.addWidget(self.delay_slider)
        self.delay_label = QLabel()
        self.delay_label.setFixedWidth(55)
        self.delay_label.setAlignment(Qt.AlignCenter)
        delay_row.addWidget(self.delay_label)
        sound_layout.addLayout(delay_row)

        self.dsp_status = QLabel()
        self.dsp_status.setWordWrap(True)
        sound_layout.addWidget(self.dsp_status)
        layout.addWidget(sound_group)

        # --- 폰으로 소리 보내기 ---
        net_group = QGroupBox("폰으로 소리 보내기")
        net_layout = QVBoxLayout(net_group)

        net_row = QHBoxLayout()
        self.net_check = QCheckBox("네트워크 출력 (무압축 RTP)")
        self.net_check.toggled.connect(self._on_net_toggled)
        net_row.addWidget(self.net_check)
        net_row.addStretch()
        self.net_auto_check = QCheckBox("지연 자동 보정")
        self.net_auto_check.setChecked(True)
        self.net_auto_check.toggled.connect(self._on_net_auto_toggled)
        net_row.addWidget(self.net_auto_check)
        net_layout.addLayout(net_row)

        boost_row = QHBoxLayout()
        boost_row.addWidget(QLabel("이어폰 게인:"))
        self.net_boost_spin = QSpinBox()
        self.net_boost_spin.setRange(0, int(audio_dsp.MAX_BOOST_DB))
        self.net_boost_spin.setSuffix(" dB")
        self.net_boost_spin.setToolTip(
            "폰 송출 중에는 평준화가 빠지면서 그만큼 조용해진다. 그 몫을 되돌리는 값"
        )
        self.net_boost_spin.valueChanged.connect(self._on_net_boost_changed)
        boost_row.addWidget(self.net_boost_spin)
        boost_row.addWidget(QLabel("(클리핑은 리미터가 막음)"))
        boost_row.addStretch()
        net_layout.addLayout(boost_row)

        trim_row = QHBoxLayout()
        trim_row.addWidget(QLabel("수동 보정:"))
        self.net_offset_spin = QSpinBox()
        self.net_offset_spin.setRange(-200, 300)
        self.net_offset_spin.setSingleStep(10)
        self.net_offset_spin.setSuffix(" ms")
        self.net_offset_spin.setToolTip(
            "소리가 늦게 들리면 값을 올리고, 너무 앞서면 내린다"
        )
        self.net_offset_spin.valueChanged.connect(self._on_net_offset_changed)
        trim_row.addWidget(self.net_offset_spin)
        trim_row.addWidget(QLabel("(소리가 늦으면 ＋)"))
        trim_row.addStretch()
        net_layout.addLayout(trim_row)

        self.net_addr_label = QLabel()
        net_layout.addWidget(self.net_addr_label)
        self.net_status = QLabel("꺼짐")
        self.net_status.setWordWrap(True)
        net_layout.addWidget(self.net_status)
        layout.addWidget(net_group)

        self._init_sound_controls()

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

    # ---- 사운드 보정 ----

    def _init_sound_controls(self):
        """저장된 값으로 위젯을 채운다 (시그널 없이)."""
        preset = self._settings.get("audio_preset", audio_dsp.DEFAULT_PRESET)
        idx = self.dsp_preset_combo.findData(preset)
        if idx >= 0:
            self.dsp_preset_combo.blockSignals(True)
            self.dsp_preset_combo.setCurrentIndex(idx)
            self.dsp_preset_combo.blockSignals(False)

        ceiling = int(self._settings.get("audio_ceiling", audio_dsp.DEFAULT_CEILING))
        self.ceiling_slider.blockSignals(True)
        self.ceiling_slider.setValue(ceiling)
        self.ceiling_slider.blockSignals(False)
        self.ceiling_label.setText(f"{ceiling} dB")

        delay = int(self._settings.get("video_delay", 0))
        self.delay_slider.blockSignals(True)
        self.delay_slider.setValue(delay)
        self.delay_slider.blockSignals(False)
        self.delay_label.setText(f"{delay} ms")

        problem = audio_dsp.check_requirements()
        if problem:
            self.dsp_check.setEnabled(False)
            self.dsp_preset_combo.setEnabled(False)
            self.ceiling_slider.setEnabled(False)
            self.dsp_status.setText(f"사용 불가 — {problem}")
            return

        self.dsp_status.setText("꺼짐")
        if self._settings.get("audio_dsp", False):
            self.dsp_check.setChecked(True)  # _on_dsp_toggled 가 시작시킨다

        self.net_auto_check.blockSignals(True)
        self.net_auto_check.setChecked(self._settings.get("net_auto_delay", True))
        self.net_auto_check.blockSignals(False)

        boost = self._settings.get("net_boost_db", audio_dsp.DEFAULT_BOOST_DB)
        self.net_boost_spin.blockSignals(True)
        self.net_boost_spin.setValue(int(boost))
        self.net_boost_spin.blockSignals(False)
        self.dsp.set_boost_db(boost)

        # 폰이 알 수 없는 PC 쪽 송출 버퍼를 총합에 넣어준다
        self.control.set_pc_latency(audio_dsp.RTP_LATENCY_MS)
        self.net_offset_spin.blockSignals(True)
        self.net_offset_spin.setValue(self._settings.get("net_delay_offset", 0))
        self.net_offset_spin.blockSignals(False)
        self.control.set_offset(self.net_offset_spin.value())
        if self._settings.get("net_audio", False):
            self.net_check.setChecked(True)

    def _on_dsp_toggled(self, checked: bool):
        self.dsp.set_leveling(checked)
        self._settings["audio_dsp"] = checked
        save_settings(self._settings)
        self._sync_dsp()

    def _sync_dsp(self):
        """평준화 또는 폰 송출 중 하나라도 필요하면 DSP 체인을 띄운다."""
        needed = self.dsp_check.isChecked() or self.dsp.network_target() is not None
        if needed and not self.dsp.is_running():
            error = self.dsp.start()
            if error:
                self.dsp_status.setText(f"시작 실패 — {error}")
                self.dsp_check.blockSignals(True)
                self.dsp_check.setChecked(False)
                self.dsp_check.blockSignals(False)
                return
        elif not needed and self.dsp.is_running():
            self.dsp.stop()

        if not self.dsp.is_running():
            self.dsp_status.setText("꺼짐")
        elif self.dsp.leveling_active():
            self.dsp_status.setText(
                "적용 중 — 이 앱의 소리만 처리합니다 (시스템 기본 출력은 그대로)"
            )
        else:
            self.dsp_status.setText(
                "폰 송출 중 — 평준화는 자동으로 꺼집니다 (이어폰에는 원본 그대로가 낫습니다)"
            )

        # 볼륨 적용 지점이 바뀌므로 다시 적용
        if self.projector_window:
            self.projector_window.set_volume(self.volume_value)

    def _on_dsp_preset_changed(self, index: int):
        preset = self.dsp_preset_combo.currentData()
        self.dsp.set_preset(preset)
        self._settings["audio_preset"] = preset
        save_settings(self._settings)

    def _on_video_delay_changed(self, value: int):
        self.delay_label.setText(f"{value} ms")
        self._settings["video_delay"] = value
        save_settings(self._settings)
        if self.projector_window:
            self.projector_window.set_video_delay(value)

    def _on_ceiling_changed(self, value: int):
        self.ceiling_label.setText(f"{value} dB")
        self.dsp.set_ceiling_db(float(value))
        self._settings["audio_ceiling"] = value
        save_settings(self._settings)

    # ---- 폰으로 소리 보내기 ----

    @staticmethod
    def _lan_address() -> str:
        """폰에서 입력할 이 PC 의 주소."""
        from PySide6.QtNetwork import QAbstractSocket, QNetworkInterface
        for iface in QNetworkInterface.allInterfaces():
            flags = iface.flags()
            if not (flags & QNetworkInterface.IsUp) or (flags & QNetworkInterface.IsLoopBack):
                continue
            for entry in iface.addressEntries():
                addr = entry.ip()
                if addr.protocol() == QAbstractSocket.IPv4Protocol:
                    return addr.toString()
        return "주소를 찾을 수 없음"

    def _on_net_toggled(self, checked: bool):
        if checked:
            # RTP 송출은 DSP 체인 안에서 만들어지므로 플러그인은 있어야 한다
            if not self.dsp_check.isEnabled():
                self.net_status.setText("사용 불가 — PipeWire/LADSPA 플러그인이 필요합니다")
                self.net_check.blockSignals(True)
                self.net_check.setChecked(False)
                self.net_check.blockSignals(False)
                return

            error = self.control.start()
            if error:
                self.net_status.setText(f"시작 실패 — {error}")
                self.net_check.blockSignals(True)
                self.net_check.setChecked(False)
                self.net_check.blockSignals(False)
                return
            self.net_addr_label.setText(
                f"폰에 입력할 주소: {self._lan_address()} : {net_audio.CONTROL_PORT}"
            )
        else:
            self.control.stop()
            self.dsp.clear_network_target()
            self.net_addr_label.clear()

        self._settings["net_audio"] = checked
        save_settings(self._settings)

    def _on_net_auto_toggled(self, checked: bool):
        self._settings["net_auto_delay"] = checked
        save_settings(self._settings)

    def _on_net_boost_changed(self, value: int):
        self.dsp.set_boost_db(value)
        self._settings["net_boost_db"] = value
        save_settings(self._settings)

    def _on_net_offset_changed(self, value: int):
        self.control.set_offset(value)
        self._settings["net_delay_offset"] = value
        save_settings(self._settings)

    def _on_phone_connected(self, ip: str):
        self.dsp.set_network_target(ip, net_audio.AUDIO_PORT)
        self._sync_dsp()

    def _on_phone_disconnected(self):
        self._latency_window.clear()
        self.dsp.clear_network_target()
        self._sync_dsp()
        if self.net_auto_check.isChecked():
            self.delay_slider.setValue(0)

    def _on_phone_latency(self, total_ms: int):
        if not self.net_auto_check.isChecked():
            return
        self._latency_window.append(total_ms)
        target = min(
            video_delay.MAX_DELAY_MS,
            int(statistics.median(self._latency_window)),
        )
        # 잔떨림까지 따라가면 영상이 계속 흔들린다
        if abs(target - self.delay_slider.value()) >= DELAY_HYSTERESIS_MS:
            self.delay_slider.setValue(target)

    def _on_phone_trim(self, value: int):
        """폰에서 조절한 보정값. 스핀박스에 반영하면 저장·적용까지 이어진다."""
        low, high = self.net_offset_spin.minimum(), self.net_offset_spin.maximum()
        self.net_offset_spin.setValue(max(low, min(high, value)))

    def _on_phone_status(self, text: str):
        self.net_status.setText(text)

    # ---- UI 이벤트 ----

    def _on_mode_changed(self, index: int):
        mode = self.mode_combo.currentData()
        if self.projector_window:
            self.projector_window.hide_content()
        self.playback_mode = mode
        self.file_widget.setVisible(mode == "file")
        self.browser_widget.setVisible(mode == "browser")
        self._update_playback_buttons()
        self.status_label.setText("준비")

    def _on_site_changed(self, index: int):
        url = self.site_combo.currentData()
        if url:
            self.url_input.setText(url)
        else:
            self.url_input.clear()
            self.url_input.setFocus()

    def _update_playback_buttons(self):
        is_file = self.playback_mode == "file"
        self.btn_play.setEnabled(is_file)
        self.btn_pause.setEnabled(is_file)
        self.btn_stop.setEnabled(is_file)

    def _on_emulator_toggled(self, checked: bool):
        if checked:
            self._ensure_projector_window()
        else:
            if self.projector_window:
                self.projector_window.hide_content()
                self.projector_window._stop_output()
                self.projector_window.hide()
            self.status_label.setText("준비")

    def _on_emulator_window_closed(self):
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
            self.projector_window.dsp = self.dsp
            self.projector_window.set_volume(self.volume_value)

        pw = self.projector_window
        pw.set_keystone(self.keystone_value)
        pw.set_aspect(self.aspect_value)
        pw.set_video_delay(self.delay_slider.value())
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

    def _on_keystone_changed(self, value: int):
        self.keystone_value = value
        self.ks_value_label.setText(str(value))

        # 설정 저장
        self._settings["keystone"] = value
        save_settings(self._settings)

        # 에뮬레이터/프로젝터 창에 반영
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

        self.emulator_check.setChecked(True)
        pw = self._ensure_projector_window()
        pw.show_video(self.current_file)
        self.status_label.setText(f"재생 중: {os.path.basename(self.current_file)}")

    def _play_browser(self):
        url = self.url_input.text().strip()
        if not url:
            self.status_label.setText("URL을 입력하세요")
            return
        if not url.startswith(("http://", "https://")):
            url = "https://" + url

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
        if self.projector_window:
            self.projector_window.hide_content()
        self.status_label.setText("정지됨")

    def closeEvent(self, event):
        if self.projector_window:
            self.projector_window.close()
            self.projector_window = None
        self.control.stop()
        self.dsp.stop()
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


def _app_icon() -> QIcon:
    """설치된 테마 아이콘을 먼저 쓰고, 없으면 소스 옆의 파일을 쓴다.

    ~/.local/bin 에 심볼릭 링크로 설치되므로 __file__ 은 링크 경로다. 실제 위치를
    풀어야 저장소 안의 icon.svg 를 찾는다.
    """
    themed = QIcon.fromTheme("keystone-player")
    if not themed.isNull():
        return themed
    local = os.path.join(os.path.dirname(os.path.realpath(__file__)), "icon.svg")
    return QIcon(local) if os.path.exists(local) else QIcon()


def main():
    _setup_widevine()
    app = QApplication(sys.argv)
    app.setApplicationName("Keystone Player")
    # 창·작업표시줄 아이콘. 이걸 지정하지 않으면 .desktop 의 Icon 과 무관하게
    # 기본 아이콘이 뜬다. setDesktopFileName 은 Wayland 에서 창을 .desktop 에
    # 연결해주는 값이라 둘 다 있어야 한다.
    app.setDesktopFileName("keystone-player")
    app.setWindowIcon(_app_icon())
    window = KeystonePlayer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
