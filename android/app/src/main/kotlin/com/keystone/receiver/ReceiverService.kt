package com.keystone.receiver

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.net.wifi.WifiManager
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import com.keystone.receiver.audio.AudioPlayer
import com.keystone.receiver.audio.JitterBuffer
import com.keystone.receiver.net.ControlClient
import com.keystone.receiver.net.RtpReceiver
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

private const val TAG = "ReceiverService"
private const val NOTIF_CHANNEL_ID = "keystone_playback"
private const val NOTIF_ID = 1
private const val CONTROL_PORT = 46001

/**
 * 화면이 꺼져도 재생을 유지하기 위한 포그라운드 서비스.
 * - WiFi LowLatency lock + partial wakelock 을 잡는다 (PROMPT.md 요구사항)
 * - hello 로 받은 오디오 포맷으로 재생을 시작한다
 * - 1초마다 output_ms / buffer_ms 를 PC 로 보고한다
 */
class ReceiverService : Service() {

    companion object {
        const val ACTION_CONNECT = "com.keystone.receiver.CONNECT"
        const val ACTION_DISCONNECT = "com.keystone.receiver.DISCONNECT"
        const val EXTRA_HOST = "host"
    }

    private val scope = CoroutineScope(Dispatchers.Default + SupervisorJob())
    private var latencyJob: Job? = null

    private var jitter: JitterBuffer? = null
    private var player: AudioPlayer? = null
    private var receiver: RtpReceiver? = null
    private var control: ControlClient? = null

    private var wifiLock: WifiManager.WifiLock? = null
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createNotifChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_CONNECT -> {
                val host = intent.getStringExtra(EXTRA_HOST)?.trim().orEmpty()
                if (host.isEmpty()) {
                    stopSelf()
                    return START_NOT_STICKY
                }
                startForegroundState("연결 중… ($host)")
                connect(host)
            }
            ACTION_DISCONNECT -> {
                // startForegroundService 규약: 어떤 진입이든 5초 안에 startForeground 를 부른 뒤 정리해야 한다
                startForegroundState("해제 중…")
                disconnect()
                stopSelfNow()
            }
            else -> {
                startForegroundState("정리 중…")
                stopSelfNow()
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        disconnect()
        scope.cancel()
        super.onDestroy()
    }

    // ---- 연결 ----

    private fun connect(host: String) {
        ReceiverState.connecting.value = true
        ReceiverState.status.value = "연결 중… ($host)"

        val jb = JitterBuffer()
        jitter = jb
        // player/receiver 는 hello 를 받은 뒤에 확정된 포맷으로 만든다.

        val client = ControlClient(host, CONTROL_PORT, object : ControlClient.Listener {
            override fun onConnected() {
                ReceiverState.status.value = "hello 대기 중"
                updateNotification("연결됨 — hello 대기 중")
            }

            override fun onHello(audio: ControlClient.AudioParams, trimMs: Int) {
                if (audio.format != "s16be" || audio.rate != 48000 || audio.channels != 2) {
                    ReceiverState.status.value =
                        "지원되지 않는 포맷: ${audio.format} ${audio.rate}Hz ${audio.channels}ch"
                    disconnectAsync()
                    return
                }
                startAudioPath(audio.port)
                ReceiverState.trimMs.value = trimMs
                ReceiverState.connecting.value = false
                ReceiverState.connected.value = true
                ReceiverState.status.value = "연결됨 — $host"
                updateNotification("재생 중 — $host")
            }

            override fun onProtocolMismatch(remoteVersion: Int) {
                ReceiverState.status.value = "PC 프로토콜 버전 $remoteVersion — 앱을 업데이트하세요"
                disconnectAsync()
            }

            override fun onStop() {
                ReceiverState.status.value = "PC 가 출력을 껐습니다"
                disconnectAsync()
            }

            override fun onDisconnected(reason: String?) {
                ReceiverState.status.value = reason?.let { "연결 끊김: $it" } ?: "연결 끊김"
                disconnectAsync()
            }
        })
        control = client
        client.start()

        acquireLocks()
        startLatencyReports()
    }

    private fun startAudioPath(rtpPort: Int) {
        val jb = jitter ?: return
        player = AudioPlayer(jb).also { it.start() }
        receiver = RtpReceiver(rtpPort, jb).also { it.start() }
        // UI 슬라이더가 이 콜백으로 트림 값을 넣는다.
        control?.let { c -> ReceiverBridge.bind { offset -> c.sendTrim(offset) } }
        Log.i(TAG, "오디오 시작 (rtp $rtpPort)")
    }

    private fun disconnectAsync() {
        scope.launch { disconnect(); stopSelfNow() }
    }

    private fun disconnect() {
        ReceiverBridge.unbind()
        latencyJob?.cancel()
        latencyJob = null
        control?.stop(); control = null
        receiver?.stop(); receiver = null
        player?.stop(); player = null
        jitter?.clear(); jitter = null
        releaseLocks()
        ReceiverState.reset()
    }

    private fun stopSelfNow() {
        if (Build.VERSION.SDK_INT >= 24) {
            stopForeground(STOP_FOREGROUND_REMOVE)
        } else {
            @Suppress("DEPRECATION")
            stopForeground(true)
        }
        stopSelf()
    }

    /** 그래프 창 길이. 5분치를 초 단위로 유지한다. */
    private val historyWindowMs = 5L * 60 * 1000

    private fun startLatencyReports() {
        latencyJob?.cancel()
        latencyJob = scope.launch {
            val samples = ArrayDeque<ReceiverState.Sample>()
            val underrunTimes = ArrayDeque<Long>()
            var lastUnderrun = 0
            val startNs = System.nanoTime()

            while (isActive) {
                delay(1000)
                val jb = jitter ?: continue
                val ap = player ?: continue
                jb.maybeShrinkTarget()
                val outMs = ap.outputMsMedian()
                val bufMs = jb.bufferedMs()
                val curUnderruns = jb.underruns()
                val nowMs = (System.nanoTime() - startNs) / 1_000_000L

                control?.reportLatency(outMs, bufMs)

                // 상단 표시용 값
                ReceiverState.outputMs.value = outMs
                ReceiverState.bufferMs.value = bufMs
                ReceiverState.underruns.value = curUnderruns

                // 그래프 이력
                samples.addLast(ReceiverState.Sample(nowMs, bufMs, outMs))
                while (samples.isNotEmpty() && nowMs - samples.first().tMs > historyWindowMs) {
                    samples.removeFirst()
                }
                if (curUnderruns > lastUnderrun) {
                    underrunTimes.addLast(nowMs)
                    lastUnderrun = curUnderruns
                }
                while (underrunTimes.isNotEmpty() && nowMs - underrunTimes.first() > historyWindowMs) {
                    underrunTimes.removeFirst()
                }

                ReceiverState.history.value = samples.toList()
                ReceiverState.underrunEvents.value = underrunTimes.toList()

                // 드리프트: 5분 이상 데이터가 쌓였을 때만
                ReceiverState.drift.value = computeDrift(samples)
            }
        }
    }

    /**
     * 버퍼 깊이의 최소제곱 기울기 → ppm.
     * ppm = (ms/hour) / 3.6  (PROMPT2.md 공식)
     */
    private fun computeDrift(samples: Collection<ReceiverState.Sample>): ReceiverState.Drift? {
        if (samples.size < 60) return null   // 최소 1분치 있어야 회귀가 의미 있다
        val first = samples.first().tMs
        val last = samples.last().tMs
        if (last - first < 5L * 60 * 1000) return null  // 5분 미만이면 표시 안함

        var n = 0.0
        var sx = 0.0; var sy = 0.0; var sxx = 0.0; var sxy = 0.0
        for (s in samples) {
            val x = (s.tMs - first) / 1000.0   // 초 단위
            val y = s.bufferMs.toDouble()
            n += 1.0; sx += x; sy += y; sxx += x * x; sxy += x * y
        }
        val denom = n * sxx - sx * sx
        if (denom <= 0) return null
        val slopeMsPerSec = (n * sxy - sx * sy) / denom
        val msPerHour = slopeMsPerSec * 3600.0
        val ppm = msPerHour / 3.6
        val projected2h = (msPerHour * 2).toInt()
        return ReceiverState.Drift(ppm = ppm, projected2hMs = projected2h)
    }

    // ---- Wake / WiFi locks ----

    private fun acquireLocks() {
        val wifi = applicationContext.getSystemService(Context.WIFI_SERVICE) as WifiManager
        val mode = if (Build.VERSION.SDK_INT >= 29) {
            WifiManager.WIFI_MODE_FULL_LOW_LATENCY
        } else {
            WifiManager.WIFI_MODE_FULL_HIGH_PERF
        }
        wifiLock = wifi.createWifiLock(mode, "keystone:wifi").apply {
            setReferenceCounted(false)
            acquire()
        }
        val pm = applicationContext.getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "keystone:wake").apply {
            setReferenceCounted(false)
            acquire(6L * 60 * 60 * 1000)  // 심야 상영 기준 6시간 안전장치
        }
    }

    private fun releaseLocks() {
        try { wifiLock?.release() } catch (_: Exception) {}
        try { wakeLock?.release() } catch (_: Exception) {}
        wifiLock = null
        wakeLock = null
    }

    // ---- Notification ----

    private fun createNotifChannel() {
        val nm = getSystemService(NotificationManager::class.java)
        if (nm.getNotificationChannel(NOTIF_CHANNEL_ID) == null) {
            val ch = NotificationChannel(
                NOTIF_CHANNEL_ID,
                getString(R.string.notif_channel_name),
                NotificationManager.IMPORTANCE_LOW,
            ).apply {
                description = getString(R.string.notif_channel_desc)
                setShowBadge(false)
            }
            nm.createNotificationChannel(ch)
        }
    }

    private fun startForegroundState(text: String) {
        val notif = buildNotification(text)
        if (Build.VERSION.SDK_INT >= 34) {
            startForeground(NOTIF_ID, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_MEDIA_PLAYBACK)
        } else {
            startForeground(NOTIF_ID, notif)
        }
    }

    private fun updateNotification(text: String) {
        val nm = getSystemService(NotificationManager::class.java)
        nm.notify(NOTIF_ID, buildNotification(text))
    }

    private fun buildNotification(text: String): Notification {
        val openIntent = PendingIntent.getActivity(
            this, 0,
            Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        val disconnectIntent = PendingIntent.getService(
            this, 1,
            Intent(this, ReceiverService::class.java).setAction(ACTION_DISCONNECT),
            PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE,
        )
        return Notification.Builder(this, NOTIF_CHANNEL_ID)
            .setContentTitle(getString(R.string.app_name))
            .setContentText(text)
            .setSmallIcon(android.R.drawable.ic_media_play)
            .setContentIntent(openIntent)
            .setOngoing(true)
            .addAction(
                Notification.Action.Builder(
                    android.R.drawable.ic_media_pause.let { android.graphics.drawable.Icon.createWithResource(this, it) },
                    "해제",
                    disconnectIntent,
                ).build()
            )
            .build()
    }
}
