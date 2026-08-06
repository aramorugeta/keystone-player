package com.keystone.receiver.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioTimestamp
import android.media.AudioTrack
import android.util.Log
import java.util.concurrent.atomic.AtomicBoolean
import java.util.concurrent.atomic.AtomicLong

private const val TAG = "AudioPlayer"

/**
 * AudioTrack LOW_LATENCY 로 재생하고 실제 출력 지연을 getTimestamp() 로 측정한다.
 *
 * 아키텍처 원칙 (PROMPT2.md 반영):
 *  - 지터 쿠션은 JitterBuffer.ready 에 둔다 (그래야 buffer_ms 가 실제 값으로 보고된다)
 *  - AudioTrack 내부 버퍼는 **최소**로 유지 — 크게 잡으면 쿠션이 AT 로 흘러들어가
 *    buffer_ms 는 0 이 되고 output_ms 만 커져서 드리프트 진단이 불가능해진다
 *  - output_ms 는 getTimestamp() 로 순수 하드웨어 파이프라인만 측정
 */
class AudioPlayer(
    private val jitter: JitterBuffer,
    private val sampleRate: Int = 48000,
    private val channels: Int = 2,
) {
    private val bytesPerFrame = 2 * channels  // 16-bit
    private val running = AtomicBoolean(false)
    private var thread: Thread? = null
    private var track: AudioTrack? = null

    // 다른 스레드에서 읽으므로 원자적으로 다뤄야 한다
    private val writtenFrames = AtomicLong(0L)
    private val timestamp = AudioTimestamp()

    fun start() {
        if (running.getAndSet(true)) return

        val minBuf = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_STEREO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        // AT 버퍼는 최소. 쿠션은 JitterBuffer 가 담당한다.
        // 최소값이 너무 작아 언더런 나는 기기가 있을 수 있어 하한을 두 packet 분(=10ms)으로 잡는다.
        val floorBytes = sampleRate * bytesPerFrame * 10 / 1000
        val bufBytes = maxOf(minBuf, floorBytes)

        val attrs = AudioAttributes.Builder()
            .setUsage(AudioAttributes.USAGE_MEDIA)
            .setContentType(AudioAttributes.CONTENT_TYPE_MOVIE)
            .build()
        val format = AudioFormat.Builder()
            .setSampleRate(sampleRate)
            .setChannelMask(AudioFormat.CHANNEL_OUT_STEREO)
            .setEncoding(AudioFormat.ENCODING_PCM_16BIT)
            .build()

        val t = AudioTrack.Builder()
            .setAudioAttributes(attrs)
            .setAudioFormat(format)
            .setBufferSizeInBytes(bufBytes)
            .setTransferMode(AudioTrack.MODE_STREAM)
            .setPerformanceMode(AudioTrack.PERFORMANCE_MODE_LOW_LATENCY)
            .build()

        if (t.state != AudioTrack.STATE_INITIALIZED) {
            Log.e(TAG, "AudioTrack init 실패")
            running.set(false)
            return
        }
        track = t
        writtenFrames.set(0L)
        Log.i(TAG, "AT 버퍼 요청 $bufBytes B, 실제 ${t.bufferSizeInFrames} frames "
                + "(${t.bufferSizeInFrames * 1000 / sampleRate}ms)")
        t.play()

        thread = Thread({ writeLoop() }, "keystone-audio").apply {
            priority = Thread.MAX_PRIORITY
            start()
        }
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        // stop() 을 먼저 불러 WRITE_BLOCKING 에 걸린 쓰기를 풀어준 뒤 join
        val t = track
        if (t != null) {
            try { t.stop() } catch (_: IllegalStateException) {}
        }
        thread?.join(500)
        thread = null
        try { t?.release() } catch (_: Exception) {}
        track = null
    }

    /**
     * 요청 시점의 순간 출력 지연 (ms). 스무딩은 상위 (ReceiverService) 에서 8개 중앙값으로 한다.
     * getTimestamp 가 아직 준비 안됐거나 실패하면 0.
     */
    fun snapshotOutputMs(): Int {
        val t = track ?: return 0
        if (!t.getTimestamp(timestamp)) return 0
        val nowNs = System.nanoTime()
        val elapsedNs = nowNs - timestamp.nanoTime
        val playedFrames = timestamp.framePosition +
            (elapsedNs * sampleRate / 1_000_000_000L)
        val aheadFrames = (writtenFrames.get() - playedFrames).coerceAtLeast(0L)
        return (aheadFrames * 1000L / sampleRate).toInt()
    }

    private fun writeLoop() {
        val t = track ?: return
        while (running.get()) {
            val samples = jitter.pop(timeoutMs = 100) ?: continue
            val written = try {
                t.write(samples, 0, samples.size, AudioTrack.WRITE_BLOCKING)
            } catch (e: IllegalStateException) {
                Log.w(TAG, "write 실패", e)
                break
            }
            if (written > 0) {
                writtenFrames.addAndGet((written / channels).toLong())
            } else if (written < 0) {
                Log.w(TAG, "AudioTrack.write 에러코드 $written")
                break
            }
        }
    }
}
