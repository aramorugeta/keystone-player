#!/usr/bin/env python3
"""
영상 지연 (립싱크 보정)

소리를 네트워크로 다른 기기에 보내면 100~300ms 늦게 도착한다.
Qt 는 오디오 출력 지연을 영상 동기에 반영하지 않는 것으로 실측 확인됐으므로
(오디오 버퍼를 341ms 로 키워도 영상은 37ms 만 움직였다),
영상 쪽을 직접 붙잡아뒀다가 내보내서 맞춘다.

QMediaPlayer 가 뱉는 프레임을 중간에서 받아 delay_ms 뒤에 표시용 싱크로 넘긴다.
실측상 요청 지연 대비 오차 ±1ms, 지터 증가 없음.

브라우저(QWebEngineView) 재생에는 적용되지 않는다. Chromium 이 내부에서
직접 렌더링하기 때문에 프레임을 가로챌 수 없다.
"""

from collections import deque

from PySide6.QtCore import Qt, QElapsedTimer, QObject, QTimer
from PySide6.QtMultimedia import QVideoSink

MAX_DELAY_MS = 500
FLUSH_INTERVAL_MS = 2


class FrameDelay(QObject):
    """입력 싱크로 들어온 프레임을 delay_ms 뒤에 출력 싱크로 넘긴다."""

    def __init__(self, out_sink: QVideoSink, parent=None):
        super().__init__(parent)
        self._out = out_sink
        self._delay_ms = 0
        self._queue: deque = deque()
        self._clock = QElapsedTimer()
        self._clock.start()

        # QMediaPlayer 는 여기로 출력한다
        self.sink = QVideoSink(self)
        self.sink.videoFrameChanged.connect(self._on_frame)

        self._timer = QTimer(self)
        self._timer.setTimerType(Qt.PreciseTimer)
        self._timer.timeout.connect(self._flush)

    def delay_ms(self) -> int:
        return self._delay_ms

    def set_delay_ms(self, value: int):
        value = max(0, min(MAX_DELAY_MS, int(value)))
        if value == self._delay_ms:
            return
        self._delay_ms = value
        if value == 0:
            self._timer.stop()
            self.clear()
        elif not self._timer.isActive():
            self._timer.start(FLUSH_INTERVAL_MS)

    def clear(self):
        """정지/일시정지 시 남은 프레임을 버린다 (다음 재생에 옛 화면이 뜨지 않도록)."""
        self._queue.clear()

    def _on_frame(self, frame):
        if not frame.isValid():
            return
        if self._delay_ms <= 0:
            self._out.setVideoFrame(frame)  # 지연 0 이면 큐를 거치지 않는다
            return
        self._queue.append((self._clock.elapsed(), frame))

    def _flush(self):
        now = self._clock.elapsed()
        while self._queue and now - self._queue[0][0] >= self._delay_ms:
            self._out.setVideoFrame(self._queue.popleft()[1])
