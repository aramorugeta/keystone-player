package com.keystone.receiver.net

import android.util.Log
import com.keystone.receiver.audio.JitterBuffer
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.nio.ByteBuffer
import java.nio.ByteOrder
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "RtpReceiver"

/**
 * UDP 46000 에서 RTP 패킷을 받아 페이로드를 JitterBuffer 로 넘긴다.
 *
 * 프로토콜 고정값 (PROMPT.md / net_audio.py):
 *  - 972 바이트 = 12 헤더 + 960 페이로드
 *  - v2, PT 127, CSRC/확장 없음
 *  - S16BE, 48000 Hz, stereo interleaved
 */
class RtpReceiver(
    private val port: Int,
    private val jitter: JitterBuffer,
) {
    private val running = AtomicBoolean(false)
    private var thread: Thread? = null
    private var socket: DatagramSocket? = null

    fun start() {
        if (running.getAndSet(true)) return
        thread = Thread({ loop() }, "keystone-rtp").apply {
            priority = Thread.NORM_PRIORITY + 2
            start()
        }
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        socket?.close()
        thread?.join(500)
        thread = null
        socket = null
    }

    private fun loop() {
        val sock = try {
            DatagramSocket(port).apply {
                receiveBufferSize = 1 shl 18  // 256 KB — 지터에 여유
                soTimeout = 500
            }
        } catch (e: Exception) {
            Log.e(TAG, "UDP $port 열기 실패", e)
            running.set(false)
            return
        }
        socket = sock

        val buf = ByteArray(2048)
        val packet = DatagramPacket(buf, buf.size)
        while (running.get()) {
            packet.length = buf.size
            try {
                sock.receive(packet)
            } catch (e: java.net.SocketTimeoutException) {
                continue
            } catch (e: Exception) {
                if (running.get()) Log.w(TAG, "recv 오류", e)
                break
            }
            val len = packet.length
            if (len < 14) continue  // 헤더 + 최소 1 샘플

            val version = (buf[0].toInt() ushr 6) and 0x3
            val payloadType = buf[1].toInt() and 0x7F
            if (version != 2 || payloadType != 127) continue

            val seq = ((buf[2].toInt() and 0xFF) shl 8) or (buf[3].toInt() and 0xFF)

            // 프롬프트 대로 헤더 12 바이트만 버린다 (확장/CSRC 없음)
            val payloadOff = 12
            val payloadLen = len - payloadOff
            if (payloadLen <= 0 || (payloadLen and 1) != 0) continue

            val samples = ShortArray(payloadLen / 2)
            ByteBuffer.wrap(buf, payloadOff, payloadLen)
                .order(ByteOrder.BIG_ENDIAN)
                .asShortBuffer()
                .get(samples)

            jitter.push(seq, samples)
        }
        try { sock.close() } catch (_: Exception) {}
    }
}
