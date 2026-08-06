package com.keystone.receiver

import kotlinx.coroutines.flow.MutableStateFlow

/**
 * 서비스가 계산한 값을 UI 로 노출하는 단일 지점.
 * 서비스는 시스템이 재시작할 수 있으므로 프로세스 전역 싱글턴으로 둔다.
 */
object ReceiverState {
    /** 그래프 1샘플 = 1초 간격의 (버퍼 깊이, 출력 지연). */
    data class Sample(val tMs: Long, val bufferMs: Int, val outputMs: Int)

    /** 최근 5분 동안 계산된 클럭 드리프트. 5분 미만이면 null. */
    data class Drift(val ppm: Double, val projected2hMs: Int)

    val connecting = MutableStateFlow(false)
    val connected = MutableStateFlow(false)
    val status = MutableStateFlow("대기 중")
    val outputMs = MutableStateFlow(0)
    val bufferMs = MutableStateFlow(0)
    val underruns = MutableStateFlow(0)

    /** 현재 트림 값 (PC 가 영상을 이만큼 늦춘다). ＋ 는 "소리가 늦게 들릴 때". */
    val trimMs = MutableStateFlow(0)

    /** 그래프용 최근 5분 표본. 매초 서비스가 새 리스트로 교체. */
    val history = MutableStateFlow<List<Sample>>(emptyList())

    /** 언더런이 발생한 monotonic ms 목록 (그래프 세로선용). */
    val underrunEvents = MutableStateFlow<List<Long>>(emptyList())

    /** 최소제곱으로 구한 드리프트 판정. 5분 이상 쌓인 뒤에만 설정. */
    val drift = MutableStateFlow<Drift?>(null)

    fun reset() {
        connecting.value = false
        connected.value = false
        outputMs.value = 0
        bufferMs.value = 0
        underruns.value = 0
        status.value = "대기 중"
        history.value = emptyList()
        underrunEvents.value = emptyList()
        drift.value = null
        // trimMs 는 세션이 바뀌어도 유지 — hello 로 다시 덮어씌워진다
    }
}

/**
 * UI 가 서비스의 ControlClient 로 트림 값을 보낼 때 쓰는 얇은 브릿지.
 * 서비스가 연결되어 있을 때만 sender 가 세팅된다.
 */
object ReceiverBridge {
    @Volatile private var trimSender: ((Int) -> Unit)? = null

    fun bind(sender: (Int) -> Unit) { trimSender = sender }
    fun unbind() { trimSender = null }
    fun sendTrim(offsetMs: Int) { trimSender?.invoke(offsetMs) }
}
