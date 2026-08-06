package com.keystone.receiver

import android.Manifest
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.provider.Settings
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.Canvas
import androidx.compose.foundation.background
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.KeyboardOptions
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Slider
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.foundation.layout.size
import androidx.compose.ui.geometry.Offset
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.graphics.Path
import androidx.compose.ui.graphics.drawscope.DrawScope
import androidx.compose.ui.graphics.drawscope.Stroke
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.font.FontWeight
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.compose.runtime.MutableIntState
import kotlin.math.abs
import kotlin.math.roundToInt

class MainActivity : ComponentActivity() {

    private val requestNotifPermission =
        registerForActivityResult(ActivityResultContracts.RequestPermission()) { /* no-op */ }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)

        if (Build.VERSION.SDK_INT >= 33) {
            val granted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) requestNotifPermission.launch(Manifest.permission.POST_NOTIFICATIONS)
        }

        setContent {
            MaterialTheme {
                Scaffold { padding ->
                    ReceiverScreen(Modifier.padding(padding))
                }
            }
        }
    }
}

@Composable
private fun ReceiverScreen(modifier: Modifier = Modifier) {
    val ctx = LocalContext.current
    val prefs = remember { ctx.getSharedPreferences("keystone", Context.MODE_PRIVATE) }
    var host by rememberSaveable { mutableStateOf(prefs.getString("host", "") ?: "") }

    val connecting by ReceiverState.connecting.collectAsState()
    val connected by ReceiverState.connected.collectAsState()
    val status by ReceiverState.status.collectAsState()
    val outputMs by ReceiverState.outputMs.collectAsState()
    val bufferMs by ReceiverState.bufferMs.collectAsState()
    val underruns by ReceiverState.underruns.collectAsState()

    Column(
        modifier = modifier
            .fillMaxWidth()
            .verticalScroll(rememberScrollState())
            .padding(20.dp),
        verticalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        Text("Keystone Receiver", style = MaterialTheme.typography.headlineMedium)
        Text(
            "PC 화면에 표시된 주소를 입력하고 접속하세요.",
            style = MaterialTheme.typography.bodyMedium,
        )

        OutlinedTextField(
            value = host,
            onValueChange = { host = it.trim() },
            label = { Text("PC IP 주소") },
            singleLine = true,
            enabled = !connected && !connecting,
            keyboardOptions = KeyboardOptions(keyboardType = KeyboardType.Uri),
            modifier = Modifier.fillMaxWidth(),
        )

        Button(
            onClick = {
                if (connected || connecting) {
                    startService(ctx, ReceiverService.ACTION_DISCONNECT, null)
                } else if (host.isNotBlank()) {
                    prefs.edit().putString("host", host).apply()
                    startService(ctx, ReceiverService.ACTION_CONNECT, host)
                }
            },
            enabled = connected || connecting || host.isNotBlank(),
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text(if (connected || connecting) "해제" else "접속")
        }

        Card(modifier = Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text("상태", style = MaterialTheme.typography.titleSmall)
                Text(status, style = MaterialTheme.typography.bodyMedium)
                Spacer(Modifier.height(4.dp))
                MetricRow("출력 지연", "$outputMs ms")
                MetricRow("지터 버퍼", "$bufferMs ms")
                MetricRow("언더런", "$underruns 회")
            }
        }

        if (connected) {
            TrimCard()
            GraphCard()
        }

        OutlinedButton(
            onClick = { openBatteryOptSettings(ctx) },
            modifier = Modifier.fillMaxWidth(),
        ) {
            Text("배터리 최적화 예외 설정 열기")
        }

        Text(
            "화면이 꺼져도 재생이 유지됩니다. 심야 시 Doze 로 죽는 것을 막으려면 위 설정에서 이 앱을 예외로 등록하세요.",
            style = MaterialTheme.typography.bodySmall,
            textAlign = TextAlign.Start,
        )
    }
}

@Composable
private fun TrimCard() {
    val trimMs by ReceiverState.trimMs.collectAsState()

    // 슬라이더 드래그 중 로컬 상태 (recomposition 사이에서 부드럽게)
    var sliderValue by remember(trimMs) { mutableStateOf(trimMs.toFloat()) }
    val lastSent = remember { mutableIntStateOf(trimMs) }

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
                verticalAlignment = Alignment.CenterVertically,
            ) {
                Text("지연 보정", style = MaterialTheme.typography.titleSmall)
                Text(
                    "${sliderValue.roundToInt().signed()} ms",
                    fontFamily = FontFamily.Monospace,
                    fontWeight = FontWeight.Bold,
                    style = MaterialTheme.typography.titleMedium,
                )
            }
            Text(
                "＋: 소리가 늦게 들릴 때  /  −: 소리가 먼저 들릴 때",
                style = MaterialTheme.typography.bodySmall,
            )

            Row(
                Modifier.fillMaxWidth(),
                verticalAlignment = Alignment.CenterVertically,
                horizontalArrangement = Arrangement.spacedBy(8.dp),
            ) {
                OutlinedButton(
                    onClick = {
                        val newVal = (sliderValue.roundToInt() - 10).coerceAtLeast(-200)
                        sliderValue = newVal.toFloat()
                        sendIfChanged(newVal, lastSent)
                    },
                ) { Text("−10ms") }

                Slider(
                    value = sliderValue,
                    onValueChange = { v ->
                        val rounded = (v / 10f).roundToInt() * 10
                        sliderValue = rounded.toFloat()
                        sendIfChanged(rounded, lastSent)
                    },
                    valueRange = -200f..300f,
                    steps = 49,          // 10 ms 단위, 총 51 위치
                    modifier = Modifier.weight(1f),
                )

                OutlinedButton(
                    onClick = {
                        val newVal = (sliderValue.roundToInt() + 10).coerceAtMost(300)
                        sliderValue = newVal.toFloat()
                        sendIfChanged(newVal, lastSent)
                    },
                ) { Text("+10ms") }
            }
            Row(
                Modifier.fillMaxWidth(),
                horizontalArrangement = Arrangement.SpaceBetween,
            ) {
                Text("−200", style = MaterialTheme.typography.bodySmall)
                Text("+300", style = MaterialTheme.typography.bodySmall)
            }
        }
    }
}

private fun sendIfChanged(rounded: Int, lastSent: MutableIntState) {
    if (rounded != lastSent.intValue) {
        lastSent.intValue = rounded
        ReceiverBridge.sendTrim(rounded)
    }
}

@Composable
private fun GraphCard() {
    val history by ReceiverState.history.collectAsState()
    val events by ReceiverState.underrunEvents.collectAsState()
    val drift by ReceiverState.drift.collectAsState()

    Card(modifier = Modifier.fillMaxWidth()) {
        Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
            Text("지연 (최근 5분)", style = MaterialTheme.typography.titleSmall)
            LatencyGraph(
                samples = history,
                underrunTimes = events,
                modifier = Modifier
                    .fillMaxWidth()
                    .height(200.dp),
            )
            LegendRow()
            when {
                drift == null -> Text(
                    "드리프트 판정 대기 중 (5분 필요)",
                    style = MaterialTheme.typography.bodySmall,
                )
                abs(drift!!.ppm) < 5 -> Text(
                    "안정 (드리프트 ${"%+.1f".format(drift!!.ppm)} ppm)",
                    color = Color(0xFF2E7D32),
                    style = MaterialTheme.typography.bodyMedium,
                )
                else -> Text(
                    "드리프트 ${"%+.1f".format(drift!!.ppm)} ppm — 2시간에 약 ${drift!!.projected2hMs.signed()}ms 밀림",
                    color = Color(0xFFEF6C00),
                    style = MaterialTheme.typography.bodyMedium,
                )
            }
        }
    }
}

@Composable
private fun LegendRow() {
    Row(
        Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.spacedBy(12.dp),
    ) {
        LegendDot(Color(0xFF1565C0), "버퍼")
        LegendDot(Color(0xFF2E7D32), "출력")
        LegendDot(Color(0xFF616161), "합계")
        LegendDot(Color(0xFFC62828), "언더런")
    }
}

@Composable
private fun LegendDot(color: Color, label: String) {
    Row(verticalAlignment = Alignment.CenterVertically, horizontalArrangement = Arrangement.spacedBy(4.dp)) {
        Spacer(Modifier.size(width = 12.dp, height = 4.dp).background(color))
        Text(label, style = MaterialTheme.typography.bodySmall)
    }
}

private const val WINDOW_MS = 5L * 60 * 1000

@Composable
private fun LatencyGraph(
    samples: List<ReceiverState.Sample>,
    underrunTimes: List<Long>,
    modifier: Modifier = Modifier,
) {
    val axis = Color(0xFFBDBDBD)
    val grid = Color(0xFFEEEEEE)
    val colBuffer = Color(0xFF1565C0)
    val colOutput = Color(0xFF2E7D32)
    val colTotal = Color(0xFF9E9E9E)
    val colUnderrun = Color(0xFFC62828)

    Canvas(modifier = modifier.background(Color(0xFFFAFAFA))) {
        val w = size.width
        val h = size.height
        if (w <= 0 || h <= 0) return@Canvas

        // Y 축: 자동 스케일. 최소 200ms 부터 시작해서 데이터에 맞게 확장.
        var maxY = 200
        for (s in samples) {
            val total = s.bufferMs + s.outputMs
            if (total > maxY) maxY = total
        }
        // 50 단위로 반올림 (아래는 올림)
        maxY = ((maxY + 49) / 50) * 50

        // 가로 그리드: 1분마다
        val nowMs = samples.lastOrNull()?.tMs ?: 0L
        val startMs = nowMs - WINDOW_MS
        for (i in 1..4) {
            val fraction = i / 5f
            val x = fraction * w
            drawLine(grid, Offset(x, 0f), Offset(x, h), strokeWidth = 1f)
        }
        // 세로 그리드: 50ms 마다
        val steps = maxY / 50
        for (i in 1..steps) {
            val y = h - (i * 50f / maxY) * h
            drawLine(grid, Offset(0f, y), Offset(w, y), strokeWidth = 1f)
        }
        // 좌/하 축
        drawLine(axis, Offset(0f, h), Offset(w, h), strokeWidth = 2f)
        drawLine(axis, Offset(0f, 0f), Offset(0f, h), strokeWidth = 2f)

        if (samples.size < 2) return@Canvas

        fun xOf(tMs: Long): Float =
            (((tMs - startMs).coerceIn(0, WINDOW_MS)).toFloat() / WINDOW_MS) * w
        fun yOf(ms: Int): Float =
            (h - (ms.toFloat() / maxY) * h).coerceIn(0f, h)

        // 언더런 세로선을 뒤에 깔고
        for (t in underrunTimes) {
            if (t < startMs) continue
            val x = xOf(t)
            drawLine(colUnderrun.copy(alpha = 0.4f),
                Offset(x, 0f), Offset(x, h), strokeWidth = 1.5f)
        }

        drawLineSeries(samples, ::xOf, ::yOf, colBuffer, 2f) { it.bufferMs }
        drawLineSeries(samples, ::xOf, ::yOf, colOutput, 2f) { it.outputMs }
        drawLineSeries(samples, ::xOf, ::yOf, colTotal, 1.5f) { it.bufferMs + it.outputMs }
    }
}

/** 그래프 시리즈 하나를 Path 로 그린다. */
private fun DrawScope.drawLineSeries(
    samples: List<ReceiverState.Sample>,
    xOf: (Long) -> Float,
    yOf: (Int) -> Float,
    color: Color,
    strokeWidth: Float,
    value: (ReceiverState.Sample) -> Int,
) {
    val path = Path()
    var first = true
    for (s in samples) {
        val x = xOf(s.tMs)
        val y = yOf(value(s))
        if (first) { path.moveTo(x, y); first = false } else path.lineTo(x, y)
    }
    drawPath(path, color = color, style = Stroke(width = strokeWidth))
}

@Composable
private fun MetricRow(label: String, value: String) {
    Row(
        modifier = Modifier.fillMaxWidth(),
        horizontalArrangement = Arrangement.SpaceBetween,
        verticalAlignment = Alignment.CenterVertically,
    ) {
        Text(label, style = MaterialTheme.typography.bodyMedium)
        Text(value, fontFamily = FontFamily.Monospace, style = MaterialTheme.typography.bodyMedium)
    }
}

private fun Int.signed(): String = if (this >= 0) "+$this" else "$this"

private fun startService(ctx: Context, action: String, host: String?) {
    val intent = Intent(ctx, ReceiverService::class.java).apply {
        this.action = action
        if (host != null) putExtra(ReceiverService.EXTRA_HOST, host)
    }
    ContextCompat.startForegroundService(ctx, intent)
}

private fun openBatteryOptSettings(ctx: Context) {
    try {
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
            data = Uri.parse("package:${ctx.packageName}")
            addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        }
        ctx.startActivity(intent)
    } catch (_: Exception) {
        val intent = Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS)
            .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
        ctx.startActivity(intent)
    }
}
