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

import json
from collections import deque

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


class ControlServer(QObject):
    """폰 한 대와 연결되는 제어 서버."""

    connected = Signal(str)          # 폰 IP
    disconnected = Signal()
    latencyReported = Signal(int)    # 총 지연 ms (영상을 이만큼 늦추면 된다)
    statusChanged = Signal(str)      # UI 표시용 한 줄

    def __init__(self, parent=None):
        super().__init__(parent)
        self._server = QTcpServer(self)
        self._server.newConnection.connect(self._on_new_connection)
        self._socket = None
        self._buffer = b""
        self._rtts: deque = deque(maxlen=RTT_WINDOW)
        self._device = ""

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
        self._send({
            "type": "hello",
            "protocol": PROTOCOL_VERSION,
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
                self._rtts.append(max(0, int(self._now_ms() - sent)))

        elif kind == "latency":
            output = self._as_int(msg.get("output_ms"))
            buffer_ms = self._as_int(msg.get("buffer_ms"))
            if output is None or buffer_ms is None:
                return
            one_way = (min(self._rtts) // 2) if self._rtts else 0
            total = output + buffer_ms + one_way
            self.latencyReported.emit(total)
            name = self._device or self.peer_ip()
            self.statusChanged.emit(
                f"연결됨 — {name} | 출력 {output}ms + 버퍼 {buffer_ms}ms "
                f"+ 편도 {one_way}ms = 총 {total}ms"
            )

        elif kind == "bye":
            if self._socket is not None:
                self._socket.disconnectFromHost()

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
