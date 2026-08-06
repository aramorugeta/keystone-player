package com.keystone.receiver.audio

import android.media.AudioAttributes
import android.media.AudioFormat
import android.media.AudioManager
import android.media.AudioTimestamp
import android.media.AudioTrack
import android.util.Log
import java.util.ArrayDeque
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "AudioPlayer"

/**
 * AudioTrack LOW_LATENCY 로 재생하고 실제 출력 지연을 getTimestamp() 로 측정한다.
 * output_ms 는 최근 표본의 중앙값 (스케줄링 지터에 흔들리지 않도록).
 *
 * PROMPT.md: "추정값을 하드코딩하지 말고 실제로 측정해서 넣을 것."
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

    private var writtenFrames: Long = 0L
    private val timestamp = AudioTimestamp()
    private val outputMsHistory = ArrayDeque<Int>()
    private val historyLock = Object()

    fun start() {
        if (running.getAndSet(true)) return

        val minBuf = AudioTrack.getMinBufferSize(
            sampleRate,
            AudioFormat.CHANNEL_OUT_STEREO,
            AudioFormat.ENCODING_PCM_16BIT,
        )
        // 20ms 정도의 track buffer. 너무 크면 지연이 늘고, 너무 작으면 언더런.
        val desired = sampleRate * bytesPerFrame * 20 / 1000
        val bufBytes = maxOf(minBuf, desired)

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
        writtenFrames = 0L
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
        synchronized(historyLock) { outputMsHistory.clear() }
    }

    /** 최근 표본의 중앙값. 값이 튀지 않도록. */
    fun outputMsMedian(): Int = synchronized(historyLock) {
        if (outputMsHistory.isEmpty()) return 0
        val sorted = outputMsHistory.toIntArray().also { it.sort() }
        sorted[sorted.size / 2]
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
                writtenFrames += written / channels
                sampleLatency(t)
            } else if (written < 0) {
                Log.w(TAG, "AudioTrack.write 에러코드 $written")
                break
            }
        }
    }

    /**
     * 프레임을 쓴 시점의 지연 = (지금까지 쓴 프레임 수 - 하드웨어가 이미 낸 프레임 수) / rate.
     * 여기에 프레임과 나노초 타임스탬프의 간극도 반영해 순간 편차를 줄인다.
     */
    private fun sampleLatency(t: AudioTrack) {
        if (!t.getTimestamp(timestamp)) return
        val nowNs = System.nanoTime()
        val elapsedNs = nowNs - timestamp.nanoTime
        val playedFrames = timestamp.framePosition + (elapsedNs * sampleRate / 1_000_000_000L)
        val aheadFrames = writtenFrames - playedFrames
        val ms = (aheadFrames * 1000L / sampleRate).toInt().coerceAtLeast(0)
        synchronized(historyLock) {
            outputMsHistory.addLast(ms)
            while (outputMsHistory.size > 32) outputMsHistory.removeFirst()
        }
    }
}
