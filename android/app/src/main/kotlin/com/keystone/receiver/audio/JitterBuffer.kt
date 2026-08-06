package com.keystone.receiver.audio

import java.util.ArrayDeque
import java.util.TreeMap
import kotlin.math.max
import kotlin.math.min

/**
 * 적응형 지터 버퍼.
 *
 * - 시작 60 ms, 하한 20 ms, 상한 200 ms
 * - 언더런 시 목표를 20 ms 늘리고 재프리버퍼
 * - 30 초 언더런 없으면 10 ms 씩 줄인다
 * - 패킷 유실은 무음으로 채운다 (재생 위치를 앞당기지 않는다)
 * - RTP seq 를 32비트로 확장해 순서 재정렬 (WiFi 상 소량 뒤바뀜만 감안)
 *
 * `push` 는 RTP 수신 스레드, `pop` 은 오디오 재생 스레드.
 */
class JitterBuffer(
    private val samplesPerPacket: Int = 480,   // 240 frames × 2 ch
    private val packetMs: Int = 5,
    private val startMs: Int = 60,
    private val minMs: Int = 20,
    private val maxMs: Int = 200,
) {
    private val lock = Object()
    private val ready = ArrayDeque<ShortArray>()
    private val holding = TreeMap<Long, ShortArray>()

    private var nextExpectedExt: Long = -1L   // "다음에 재생할" 확장 seq
    private var lastSeenSeq16: Int = -1       // 확장용 상태
    private var lastSeenExt: Long = 0L

    private var started = false
    private var targetMs = startMs
    private var underrunCount = 0
    private var lastUnderrunNs = 0L

    fun push(seq16: Int, samples: ShortArray) {
        synchronized(lock) {
            val ext = extend(seq16)
            if (nextExpectedExt == -1L) {
                nextExpectedExt = ext
            }
            when {
                ext < nextExpectedExt - 500 -> return  // 매우 오래된 것, 드롭
                ext > nextExpectedExt + 500 -> {
                    // 큰 점프: 발신 측 리셋으로 간주하고 재시작
                    holding.clear()
                    ready.clear()
                    nextExpectedExt = ext
                    started = false
                }
                ext < nextExpectedExt -> return  // 이미 지나간 seq, 드롭
            }
            holding[ext] = samples

            // 이어지는 것들을 ready 로 이동
            while (true) {
                val entry = holding.firstEntry() ?: break
                if (entry.key != nextExpectedExt) break
                holding.remove(entry.key)
                ready.addLast(entry.value)
                nextExpectedExt++
            }

            // 홀딩이 너무 앞서면 (>4 패킷) 빈자리를 무음으로 채우고 진행
            if (holding.isNotEmpty()) {
                val maxHeld = holding.lastKey()
                if (maxHeld - nextExpectedExt > 4) {
                    while (nextExpectedExt <= maxHeld) {
                        val v = holding.remove(nextExpectedExt)
                        if (v != null) {
                            ready.addLast(v)
                        } else {
                            ready.addLast(ShortArray(samplesPerPacket))
                            underrunCount++
                        }
                        nextExpectedExt++
                    }
                }
            }

            if (!started && ready.size * packetMs >= targetMs) {
                started = true
            }
            lock.notifyAll()
        }
    }

    /** 오디오 프레임 한 패킷치를 얻는다. 아직 준비 안됐으면 null. 준비됐지만 비었으면 무음. */
    fun pop(timeoutMs: Long): ShortArray? {
        synchronized(lock) {
            if (!started) {
                if (timeoutMs > 0) lock.wait(timeoutMs)
                return null
            }
            val deadline = System.nanoTime() + timeoutMs * 1_000_000L
            while (ready.isEmpty()) {
                val remaining = deadline - System.nanoTime()
                if (remaining <= 0) {
                    onUnderrunLocked()
                    return ShortArray(samplesPerPacket)
                }
                val ms = remaining / 1_000_000L
                val ns = (remaining % 1_000_000L).toInt()
                lock.wait(ms, ns)
            }
            return ready.removeFirst()
        }
    }

    fun bufferedMs(): Int = synchronized(lock) { ready.size * packetMs }
    fun underruns(): Int = synchronized(lock) { underrunCount }
    fun targetMs(): Int = synchronized(lock) { targetMs }

    /**
     * 서비스가 주기적으로 호출. 최근 30초간 언더런이 없었으면 목표를 조금 줄인다.
     * 재생 속도로 소진하는 방식이 아니므로 목표만 낮추면 자연스럽게 다음 프리버퍼 때 반영된다.
     */
    fun maybeShrinkTarget() {
        synchronized(lock) {
            val now = System.nanoTime()
            if (lastUnderrunNs == 0L) lastUnderrunNs = now
            if (now - lastUnderrunNs > 30_000_000_000L && targetMs > minMs) {
                targetMs = max(minMs, targetMs - 10)
                lastUnderrunNs = now
            }
        }
    }

    fun clear() {
        synchronized(lock) {
            ready.clear()
            holding.clear()
            nextExpectedExt = -1L
            lastSeenSeq16 = -1
            lastSeenExt = 0L
            started = false
            targetMs = startMs
            underrunCount = 0
            lastUnderrunNs = 0L
            lock.notifyAll()
        }
    }

    private fun onUnderrunLocked() {
        underrunCount++
        lastUnderrunNs = System.nanoTime()
        targetMs = min(maxMs, targetMs + 20)
        started = false  // 재프리버퍼
    }

    /** 16비트 seq 를 세션 시작부터의 32비트 확장으로 변환. */
    private fun extend(seq16: Int): Long {
        if (lastSeenSeq16 == -1) {
            lastSeenSeq16 = seq16
            lastSeenExt = 0L
            return 0L
        }
        val diff = seqDiff16(seq16, lastSeenSeq16)
        val ext = lastSeenExt + diff
        if (ext > lastSeenExt) {
            lastSeenSeq16 = seq16
            lastSeenExt = ext
        }
        return ext
    }

    private fun seqDiff16(a: Int, b: Int): Int {
        val d = (a - b) and 0xFFFF
        return if (d >= 0x8000) d - 0x10000 else d
    }
}
