#!/usr/bin/env python3
"""
Keystone Player - 프로젝터용 좌우 키스톤 보정 영상 플레이어
KDE Wayland 환경에서 모니터에는 컨트롤 패널, 프로젝터에는 보정된 영상 출력
"""

import sys
import os
import json
import socket
import subprocess
import time
import tempfile
from pathlib import Path

from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout,
    QPushButton, QSlider, QLabel, QFileDialog, QComboBox,
    QGroupBox, QSpinBox, QStyle, QSizePolicy
)
from PySide6.QtCore import Qt, QTimer, QProcess
from PySide6.QtGui import QFont, QScreen


class KeystonePlayer(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle("Keystone Player")
        self.setMinimumSize(520, 400)

        self.mpv_process: QProcess | None = None
        self.ipc_path = os.path.join(tempfile.gettempdir(), f"keystone-mpv-{os.getpid()}")
        self.current_file = ""
        self.keystone_value = 0  # -100 ~ +100
        self.video_width = 1920
        self.video_height = 1080
        self.is_paused = False

        self._build_ui()
        self._refresh_screens()

        # mpv 상태 폴링 타이머
        self.status_timer = QTimer()
        self.status_timer.timeout.connect(self._poll_mpv_status)
        self.status_timer.start(500)

    def _build_ui(self):
        central = QWidget()
        self.setCentralWidget(central)
        layout = QVBoxLayout(central)
        layout.setSpacing(12)

        # --- 파일 선택 ---
        file_group = QGroupBox("파일")
        file_layout = QHBoxLayout(file_group)
        self.file_label = QLabel("선택된 파일 없음")
        self.file_label.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Preferred)
        self.file_label.setWordWrap(True)
        btn_open = QPushButton("열기")
        btn_open.setFixedWidth(80)
        btn_open.clicked.connect(self._open_file)
        file_layout.addWidget(self.file_label)
        file_layout.addWidget(btn_open)
        layout.addWidget(file_group)

        # --- 디스플레이 선택 ---
        screen_group = QGroupBox("프로젝터 출력 화면")
        screen_layout = QHBoxLayout(screen_group)
        self.screen_combo = QComboBox()
        btn_refresh = QPushButton("새로고침")
        btn_refresh.setFixedWidth(80)
        btn_refresh.clicked.connect(self._refresh_screens)
        screen_layout.addWidget(self.screen_combo)
        screen_layout.addWidget(btn_refresh)
        layout.addWidget(screen_group)

        # --- 키스톤 보정 ---
        ks_group = QGroupBox("좌우 키스톤 보정")
        ks_layout = QVBoxLayout(ks_group)

        slider_row = QHBoxLayout()
        lbl_left = QLabel("◁ 좌")
        lbl_right = QLabel("우 ▷")
        self.ks_slider = QSlider(Qt.Horizontal)
        self.ks_slider.setRange(-100, 100)
        self.ks_slider.setValue(0)
        self.ks_slider.setTickPosition(QSlider.TicksBelow)
        self.ks_slider.setTickInterval(10)
        self.ks_slider.valueChanged.connect(self._on_keystone_changed)
        slider_row.addWidget(lbl_left)
        slider_row.addWidget(self.ks_slider)
        slider_row.addWidget(lbl_right)
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

        # 미세 조정
        fine_row = QHBoxLayout()
        btn_minus5 = QPushButton("-5")
        btn_minus1 = QPushButton("-1")
        btn_plus1 = QPushButton("+1")
        btn_plus5 = QPushButton("+5")
        for btn, delta in [(btn_minus5, -5), (btn_minus1, -1), (btn_plus1, 1), (btn_plus5, 5)]:
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

        # --- 상태 표시 ---
        self.status_label = QLabel("준비")
        self.status_label.setAlignment(Qt.AlignCenter)
        layout.addWidget(self.status_label)

    def _refresh_screens(self):
        self.screen_combo.clear()
        app = QApplication.instance()
        for i, screen in enumerate(app.screens()):
            geo = screen.geometry()
            name = screen.name()
            self.screen_combo.addItem(
                f"{name} ({geo.width()}x{geo.height()} @ {geo.x()},{geo.y()})",
                i
            )
        # 두 번째 화면이 있으면 기본 선택
        if self.screen_combo.count() > 1:
            self.screen_combo.setCurrentIndex(1)

    def _open_file(self):
        path, _ = QFileDialog.getOpenFileName(
            self, "영상 파일 선택", str(Path.home()),
            "영상 파일 (*.mp4 *.mkv *.avi *.mov *.wmv *.flv *.webm *.m4v *.ts *.iso);;모든 파일 (*)"
        )
        if path:
            self.current_file = path
            self.file_label.setText(os.path.basename(path))

    def _calc_perspective_filter(self, k: int) -> str:
        """키스톤 값(-100~+100)을 FFmpeg perspective 필터 문자열로 변환"""
        W = self.video_width
        H = self.video_height

        # k > 0: 오른쪽이 가까움 → 오른쪽을 줄임 (오른쪽 위아래를 안쪽으로)
        # k < 0: 왼쪽이 가까움 → 왼쪽을 줄임 (왼쪽 위아래를 안쪽으로)
        # 최대 보정량: 높이의 25%
        offset = int(abs(k) / 100.0 * H * 0.25)

        if k == 0:
            return ""

        if k > 0:
            # 오른쪽 줄임: TR 아래로, BR 위로
            x0, y0 = 0, 0           # TL
            x1, y1 = W, offset      # TR
            x2, y2 = 0, H           # BL
            x3, y3 = W, H - offset  # BR
        else:
            # 왼쪽 줄임: TL 아래로, BL 위로
            x0, y0 = 0, offset      # TL
            x1, y1 = W, 0           # TR
            x2, y2 = 0, H - offset  # BL
            x3, y3 = W, H           # BR

        return f"perspective={x0}:{y0}:{x1}:{y1}:{x2}:{y2}:{x3}:{y3}:cubic"

    def _get_target_screen_geometry(self) -> tuple[int, int, int, int]:
        app = QApplication.instance()
        idx = self.screen_combo.currentData()
        if idx is not None and idx < len(app.screens()):
            geo = app.screens()[idx].geometry()
            return geo.x(), geo.y(), geo.width(), geo.height()
        return 0, 0, 1920, 1080

    def _play(self):
        if not self.current_file:
            self.status_label.setText("파일을 먼저 선택하세요")
            return

        self._stop()
        time.sleep(0.2)

        sx, sy, sw, sh = self._get_target_screen_geometry()
        self.video_width = sw
        self.video_height = sh

        # mpv 실행 인자 구성
        args = [
            "mpv",
            "--input-ipc-server=" + self.ipc_path,
            f"--geometry={sw}x{sh}+{sx}+{sy}",
            "--fullscreen",
            "--keep-open=yes",
            "--osd-level=0",
        ]

        # 키스톤 필터 적용
        vf = self._calc_perspective_filter(self.keystone_value)
        if vf:
            args.append(f"--vf=lavfi=[{vf}]")

        args.append(self.current_file)

        self.mpv_process = QProcess(self)
        self.mpv_process.finished.connect(self._on_mpv_finished)
        self.mpv_process.start(args[0], args[1:])
        self.is_paused = False
        self.status_label.setText(f"재생 중: {os.path.basename(self.current_file)}")

    def _stop(self):
        if self.mpv_process and self.mpv_process.state() != QProcess.NotRunning:
            self._send_mpv_command(["quit"])
            self.mpv_process.waitForFinished(2000)
            if self.mpv_process.state() != QProcess.NotRunning:
                self.mpv_process.kill()
        self.mpv_process = None
        self.is_paused = False
        self.status_label.setText("정지됨")
        # IPC 소켓 정리
        try:
            os.unlink(self.ipc_path)
        except FileNotFoundError:
            pass

    def _toggle_pause(self):
        if self.mpv_process and self.mpv_process.state() == QProcess.Running:
            self._send_mpv_command(["cycle", "pause"])
            self.is_paused = not self.is_paused
            if self.is_paused:
                self.status_label.setText("일시정지")
            else:
                self.status_label.setText(f"재생 중: {os.path.basename(self.current_file)}")

    def _on_keystone_changed(self, value: int):
        self.keystone_value = value
        self.ks_value_label.setText(str(value))

        # mpv가 실행 중이면 실시간으로 필터 업데이트
        if self.mpv_process and self.mpv_process.state() == QProcess.Running:
            vf = self._calc_perspective_filter(value)
            if vf:
                self._send_mpv_command(["set_property", "vf", f"lavfi=[{vf}]"])
            else:
                self._send_mpv_command(["set_property", "vf", ""])

    def _send_mpv_command(self, cmd: list):
        """mpv IPC 소켓으로 명령 전송"""
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
        """mpv 프로세스 상태 확인"""
        if self.mpv_process and self.mpv_process.state() == QProcess.NotRunning:
            self.mpv_process = None
            self.status_label.setText("재생 완료")

    def _on_mpv_finished(self):
        self.status_label.setText("재생 완료")
        self.is_paused = False

    def closeEvent(self, event):
        self._stop()
        super().closeEvent(event)


def main():
    app = QApplication(sys.argv)
    app.setApplicationName("Keystone Player")
    window = KeystonePlayer()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
