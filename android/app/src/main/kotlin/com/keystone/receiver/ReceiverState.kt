package com.keystone.receiver

import kotlinx.coroutines.flow.MutableStateFlow

/**
 * 서비스가 계산한 값을 UI 로 노출하는 단일 지점.
 * 서비스는 시스템이 재시작할 수 있으므로 프로세스 전역 싱글턴으로 둔다.
 */
object ReceiverState {
    val connecting = MutableStateFlow(false)
    val connected = MutableStateFlow(false)
    val status = MutableStateFlow("대기 중")
    val outputMs = MutableStateFlow(0)
    val bufferMs = MutableStateFlow(0)
    val underruns = MutableStateFlow(0)

    fun reset() {
        connecting.value = false
        connected.value = false
        outputMs.value = 0
        bufferMs.value = 0
        underruns.value = 0
        status.value = "대기 중"
    }
}
