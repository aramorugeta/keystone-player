package com.keystone.receiver.net

import android.os.Build
import android.util.Log
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.launch
import org.json.JSONObject
import java.io.BufferedReader
import java.io.InputStreamReader
import java.io.OutputStreamWriter
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.atomic.AtomicBoolean

private const val TAG = "ControlClient"
private const val PROTOCOL_VERSION = 1
private const val CONNECT_TIMEOUT_MS = 5000

/**
 * TCP JSON 컨트롤 채널. 한 줄에 한 메시지, `\n` 종결, UTF-8.
 *
 * PROMPT.md: "ping 응답을 지연시키면 립싱크가 틀어진다."
 * → pong 은 리더 스레드에서 즉시 보낸다 (스케줄링 지연 최소화).
 */
class ControlClient(
    private val host: String,
    private val port: Int,
    private val listener: Listener,
) {
    interface Listener {
        fun onHello(audio: AudioParams, trimMs: Int)
        fun onProtocolMismatch(remoteVersion: Int)
        fun onConnected()
        fun onStop()
        fun onDisconnected(reason: String?)
    }

    data class AudioParams(
        val transport: String,
        val port: Int,
        val format: String,
        val rate: Int,
        val channels: Int,
        val packetMs: Int,
    )

    private val running = AtomicBoolean(false)
    private val scope = CoroutineScope(Dispatchers.IO + SupervisorJob())
    private var readerJob: Job? = null
    private var socket: Socket? = null
    private var writer: OutputStreamWriter? = null
    private val writeLock = Object()

    fun start() {
        if (running.getAndSet(true)) return
        readerJob = scope.launch { runLoop() }
    }

    fun stop() {
        if (!running.getAndSet(false)) return
        sendSync(JSONObject().apply { put("type", "bye") })
        try { socket?.close() } catch (_: Exception) {}
        socket = null
        writer = null
    }

    fun reportLatency(outputMs: Int, bufferMs: Int) {
        sendSync(JSONObject().apply {
            put("type", "latency")
            put("output_ms", outputMs)
            put("buffer_ms", bufferMs)
        })
    }

    /** 소파에서 폰으로 직접 립싱크를 맞출 때. PC 가 즉시 영상 지연에 반영한다. */
    fun sendTrim(offsetMs: Int) {
        sendSync(JSONObject().apply {
            put("type", "trim")
            put("offset_ms", offsetMs)
        })
    }

    private fun runLoop() {
        try {
            val s = Socket()
            s.tcpNoDelay = true
            s.connect(InetSocketAddress(host, port), CONNECT_TIMEOUT_MS)
            socket = s
            writer = OutputStreamWriter(s.getOutputStream(), Charsets.UTF_8)
            listener.onConnected()

            sendSync(JSONObject().apply {
                put("type", "ready")
                put("device", "${Build.MANUFACTURER} ${Build.MODEL}")
                put("app", "1.0")
            })

            val reader = BufferedReader(InputStreamReader(s.getInputStream(), Charsets.UTF_8))
            while (running.get()) {
                val line = reader.readLine() ?: break
                if (line.isBlank()) continue
                handle(line)
            }
            listener.onDisconnected(null)
        } catch (e: Exception) {
            if (running.get()) {
                Log.w(TAG, "TCP 오류", e)
                listener.onDisconnected(e.message)
            }
        } finally {
            running.set(false)
            try { socket?.close() } catch (_: Exception) {}
            socket = null
            writer = null
        }
    }

    private fun handle(line: String) {
        val msg = try {
            JSONObject(line)
        } catch (e: Exception) {
            Log.w(TAG, "잘못된 JSON: $line")
            return
        }
        when (msg.optString("type")) {
            "hello" -> {
                val version = msg.optInt("protocol", -1)
                if (version != PROTOCOL_VERSION) {
                    listener.onProtocolMismatch(version)
                    return
                }
                val audio = msg.optJSONObject("audio") ?: return
                val trim = msg.optInt("trim_ms", 0)
                listener.onHello(
                    AudioParams(
                        transport = audio.optString("transport", "rtp"),
                        port = audio.optInt("port", 46000),
                        format = audio.optString("format", "s16be"),
                        rate = audio.optInt("rate", 48000),
                        channels = audio.optInt("channels", 2),
                        packetMs = audio.optInt("packet_ms", 5),
                    ),
                    trim,
                )
            }
            "ping" -> {
                // 그대로 되돌린다. 값을 바꾸면 안 된다.
                val pong = JSONObject().apply {
                    put("type", "pong")
                    put("t", msg.opt("t"))
                }
                sendSync(pong)
            }
            "stop" -> listener.onStop()
        }
    }

    private fun sendSync(msg: JSONObject) {
        val w = writer ?: return
        try {
            synchronized(writeLock) {
                w.write(msg.toString())
                w.write("\n")
                w.flush()
            }
        } catch (e: Exception) {
            Log.w(TAG, "송신 실패", e)
        }
    }
}
