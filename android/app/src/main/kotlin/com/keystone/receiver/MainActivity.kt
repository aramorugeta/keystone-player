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
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.height
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.runtime.collectAsState
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.text.font.FontFamily
import androidx.compose.ui.text.input.KeyboardType
import androidx.compose.ui.text.style.TextAlign
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.compose.foundation.text.KeyboardOptions

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
