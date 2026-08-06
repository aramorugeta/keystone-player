#!/usr/bin/env python3
"""
네트워크 오디오 컨트롤 채널

폰(안드로이드 수신 앱)과 주고받는 제어 통로. 소리 자체는 여기로 흐르지 않는다.
소리는 PipeWire 의 RTP 싱크가 UDP 로 따로 쏜다 (audio_dsp.py).

동작 순서
  1. 폰이 PC 의 TCP 46001 로 접속한다 (폰에서 PC IP 만 입력하면 됨)
  2. PC 는 소켓에서 폰의 IP 를 알아낸다 → 그 주소로 RTP 송출을 시작한다
  3. PC 가 hello 로 오디오 포맷을 알려준다
  4. 폰이 자기 출력 지연 + 지터 버퍼 크기를 주기적으로 보고한다
  5. PC 는 (보고값 + RTT/2) 만큼 영상을 늦춰서 립싱크를 맞춘다

메시지는 UTF-8 JSON 한 줄에 하나씩, 개행으로 끝난다.
프로토콜 상세는 android/PROMPT.md 참고.
"""

import csv
import json
import os
import time
from collections import deque
from datetime import datetime

from PySide6.QtCore import QObject, QTimer, Signal
from PySide6.QtNetwork import QHostAddress, QTcpServer

PROTOCOL_VERSION = 1
CONTROL_PORT = 46001
AUDIO_PORT = 46000

# RTP 송출 포맷 — 안드로이드 수신 앱과 반드시 일치해야 한다
AUDIO_FORMAT = "s16be"
AUDIO_RATE = 48000
AUDIO_CHANNELS = 2
PACKET_MS = 5

PING_INTERVAL_MS = 2000
# 왕복 시간은 최근 여러 번 중 최솟값을 쓴다. 폰의 스케줄링 지터가 섞인 값 대신
# 순수 전파 지연에 가장 가까운 값을 골라야 립싱크 보정이 안정적이다.
RTT_WINDOW = 8
# 같은 공유기 안에서 이보다 오래 걸릴 리 없다. 넘으면 폰이 응답을 늦게 처리한
# 것이므로 전파 지연의 근거가 못 된다 — 버리지 않으면 립싱크가 그만큼 튄다.
RTT_MAX_MS = 200

# 폰이 보고하는 값을 기록해두는 파일. 긴 재생에서 클럭 드리프트가 있는지
# (버퍼가 한쪽으로 계속 밀리는지) 확인하는 용도다. tools/latency_report.py 로 본다.
LOG_NAME = "latency-log.csv"
LOG_COLUMNS = [
    "time", "session", "elapsed_s",
    "output_ms", "buffer_ms", "rtt_ms", "one_way_ms", "pc_ms", "offset_ms", "total_ms",
]


class ControlServer(QObject):
    """폰 한 대와 연결되는 제어 서버."""

    connected = Signal(str)          # 폰 IP
    disconnected = Signal()
    latencyReported = Signal(int)    # 총 지연 ms (영상을 이만큼 늦추면 된다)
    trimRequested = Signal(int)      # 폰에서 직접 조절한 수동 보정값
    statusChanged = Signal(str)      # UI 표시용 한 줄

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._socket = None
        self._buffer = b""
        self._rtts: deque = deque(maxlen=RTT_WINDOW)
        self._device = ""
        # PC 쪽에서 이미 알고 있는 지연 (RTP 송출 버퍼 등). 폰은 이걸 모른다.
        self._pc_latency_ms = 0
        # 남는 오차를 사용자가 직접 맞추는 값
        self._offset_ms = 0
        self._last_report: tuple[int, int] | None = None

        self._log_dir: str | None = None
        self._log_file = None
        self._log_writer = None
        self._session = ""
        self._session_start = 0.0

        self._ping_timer = QTimer(self)
        self._ping_timer.timeout.connect(self._send_ping)

    # ---- 서버 ----

    def start(self) -> str | None:
        if self._server.isListening():
            return None
        if not self._server.listen(QHostAddress.Any, CONTROL_PORT):
            return f"포트 {CONTROL_PORT} 를 열 수 없습니다: {self._server.errorString()}"
        self.statusChanged.emit(f"대기 중 — 폰에서 이 PC 로 접속하세요 (포트 {CONTROL_PORT})")
        return None

    def stop(self):
        self._ping_timer.stop()
        if self._socket is not None:
            self._send({"type": "stop"})
            self._socket.disconnectFromHost()
            self._socket = None
        if self._server.isListening():
            self._server.close()
        self.statusChanged.emit("꺼짐")

    def is_listening(self) -> bool:
        return self._server.isListening()

    def peer_ip(self) -> str | None:
        if self._socket is None:
            return None
        return self._socket.peerAddress().toString().removeprefix("::ffff:")

    # ---- 연결 ----

    def _on_new_connection(self):
        socket = self._server.nextPendingConnection()
        if socket is None:
            return
        if self._socket is not None:
            # 한 번에 한 대만 받는다
            socket.disconnectFromHost()
            return

        self._socket = socket
        self._buffer = b""
        socket.readyRead.connect(self._on_ready_read)
        socket.disconnected.connect(self._on_disconnected)

        ip = self.peer_ip()
        self._session = datetime.now().isoformat(timespec="seconds")
        self._session_start = time.monotonic()
        self._open_log()
        self._send({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
            "trim_ms": self._offset_ms,
            "audio": {
                "transport": "rtp",
                "port": AUDIO_PORT,
                "format": AUDIO_FORMAT,
                "rate": AUDIO_RATE,
                "channels": AUDIO_CHANNELS,
                "packet_ms": PACKET_MS,
            },
        })
        self._ping_timer.start(PING_INTERVAL_MS)
        self.connected.emit(ip)
        self.statusChanged.emit(f"연결됨 — {ip}")

    def _on_disconnected(self):
        self._ping_timer.stop()
        self._close_log()
        self._last_report = None
        self._socket = None
        self._device = ""
        self._rtt_ms = 0
        self.disconnected.emit()
        self.statusChanged.emit(f"연결 끊김 — 대기 중 (포트 {CONTROL_PORT})")

    # ---- 메시지 ----

    def _on_ready_read(self):
        if self._socket is None:
            return
        self._buffer += bytes(self._socket.readAll())
        while b"\n" in self._buffer:
            line, self._buffer = self._buffer.split(b"\n", 1)
            line = line.strip()
            if not line:
                continue
            try:
                self._handle(json.loads(line.decode("utf-8")))
            except (json.JSONDecodeError, UnicodeDecodeError):
                continue

    def _handle(self, msg: dict):
        kind = msg.get("type")
        if kind == "ready":
            self._device = str(msg.get("device", "알 수 없는 기기"))
            self.statusChanged.emit(f"연결됨 — {self._device} ({self.peer_ip()})")

        elif kind == "pong":
            sent = msg.get("t")
            if isinstance(sent, (int, float)):
                rtt = max(0, int(self._now_ms() - sent))
                if rtt <= RTT_MAX_MS:
                    self._rtts.append(rtt)

        elif kind == "latency":
            output = self._as_int(msg.get("output_ms"))
            buffer_ms = self._as_int(msg.get("buffer_ms"))
            if output is None or buffer_ms is None:
                return
            self._last_report = (output, buffer_ms)
            self._publish(log=True)

        elif kind == "trim":
            # 소파에서 폰으로 직접 맞출 수 있게 한다. PC 앞까지 갈 필요가 없다.
            value = msg.get("offset_ms")
            if isinstance(value, (int, float)):
                self.trimRequested.emit(int(value))

        elif kind == "bye":
            if self._socket is not None:
                self._socket.disconnectFromHost()

    # ---- 기록 ----

    def set_log_dir(self, path: str):
        self._log_dir = path

    def log_path(self) -> str | None:
        if self._log_dir is None:
            return None
        return os.path.join(self._log_dir, LOG_NAME)

    def _open_log(self):
        path = self.log_path()
        if path is None:
            return
        try:
            os.makedirs(self._log_dir, exist_ok=True)
            is_new = not os.path.exists(path) or os.path.getsize(path) == 0
            self._log_file = open(path, "a", newline="")
            self._log_writer = csv.writer(self._log_file)
            if is_new:
                self._log_writer.writerow(LOG_COLUMNS)
        except OSError:
            self._log_file = None
            self._log_writer = None

    def _close_log(self):
        if self._log_file is not None:
            try:
                self._log_file.close()
            except OSError:
                pass
        self._log_file = None
        self._log_writer = None

    def _log_row(self, output, buffer_ms, rtt, one_way, total):
        if self._log_writer is None:
            return
        try:
            self._log_writer.writerow([
                datetime.now().isoformat(timespec="seconds"),
                self._session,
                round(time.monotonic() - self._session_start, 1),
                output, buffer_ms, rtt, one_way, self._pc_latency_ms,
                self._offset_ms, total,
            ])
            self._log_file.flush()
        except (OSError, ValueError):
            self._close_log()

    # ---- 지연 계산 ----

    def set_pc_latency(self, ms: int):
        """PC 쪽 송출 버퍼 등 폰이 알 수 없는 지연."""
        self._pc_latency_ms = max(0, int(ms))
        self._publish()

    def set_offset(self, ms: int):
        """자동 계산으로 안 맞는 나머지를 사용자가 직접 더하는 값."""
        self._offset_ms = int(ms)
        self._publish()

    def _publish(self, log: bool = False):
        """지금까지 아는 값으로 총 지연을 다시 계산해서 알린다."""
        if self._last_report is None:
            return
        output, buffer_ms = self._last_report
        rtt = min(self._rtts) if self._rtts else 0
        one_way = rtt // 2
        total = max(0, output + buffer_ms + one_way + self._pc_latency_ms + self._offset_ms)
        self.latencyReported.emit(total)
        if log:
            self._log_row(output, buffer_ms, rtt, one_way, total)
        name = self._device or self.peer_ip()
        offset_text = f" {self._offset_ms:+d}ms 보정" if self._offset_ms else ""
        self.statusChanged.emit(
            f"연결됨 — {name} | 폰 출력 {output} + 버퍼 {buffer_ms} + 편도 {one_way} "
            f"+ PC {self._pc_latency_ms}{offset_text} = 총 {total}ms"
        )

    @staticmethod
    def _as_int(value):
        try:
            return max(0, int(value))
        except (TypeError, ValueError):
            return None

    @staticmethod
    def _now_ms() -> int:
        import time
        return int(time.monotonic() * 1000)

    def _send_ping(self):
        self._send({"type": "ping", "t": self._now_ms()})

    def _send(self, msg: dict):
        if self._socket is None:
            return
        self._socket.write(json.dumps(msg).encode("utf-8") + b"\n")
