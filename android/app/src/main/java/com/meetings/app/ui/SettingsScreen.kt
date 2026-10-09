package com.meetings.app.ui

import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.ButtonDefaults
import androidx.compose.material3.Card
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.HorizontalDivider
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.LinearProgressIndicator
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.setValue
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.input.PasswordVisualTransformation
import androidx.compose.ui.unit.dp
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.meetings.app.MeetingsApp
import com.meetings.app.R
import com.meetings.app.data.AdminSettings
import com.meetings.app.data.SecretStatus
import com.meetings.app.data.Usage
import kotlinx.coroutines.launch

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun SettingsScreen(onBack: () -> Unit) {
    val context = LocalContext.current
    val app = context.applicationContext as MeetingsApp
    val scope = rememberCoroutineScope()
    val user by app.session.user.collectAsStateWithLifecycle()
    var usage by remember { mutableStateOf<Usage?>(null) }
    var current by remember { mutableStateOf("") }
    var new by remember { mutableStateOf("") }
    var message by remember { mutableStateOf<String?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var confirmDelete by remember { mutableStateOf(false) }

    LaunchedEffect(Unit) { runCatching { usage = app.api.usage() } }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.settings)) },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back)) }
                },
            )
        },
    ) { padding ->
        Column(
            Modifier.padding(padding).verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Text(user?.full_name ?: user?.email.orEmpty(), style = MaterialTheme.typography.titleLarge)
            Text(user?.email.orEmpty(), color = MaterialTheme.colorScheme.onSurfaceVariant)
            Text(app.session.serverUrl, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)

            usage?.let { u ->
                Card(Modifier.fillMaxWidth()) {
                    Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                        Text(stringResource(R.string.usage_this_month), style = MaterialTheme.typography.titleMedium)
                        UsageRow(stringResource(R.string.uploads), u.uploads.toDouble(), u.uploads_limit, "%.0f")
                        UsageRow(stringResource(R.string.audio_minutes), u.audio_minutes, u.audio_minutes_limit, "%.0f")
                        Text(stringResource(R.string.usage_resets, formatDate(u.period_end)), style = MaterialTheme.typography.bodySmall)
                    }
                }
            }

            if (user?.role == "admin") AdminKeys()

            HorizontalDivider()
            Text(stringResource(R.string.change_password), style = MaterialTheme.typography.titleMedium)
            OutlinedTextField(current, { current = it }, label = { Text(stringResource(R.string.current_password)) },
                singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
            OutlinedTextField(new, { new = it }, label = { Text(stringResource(R.string.new_password)) },
                singleLine = true, visualTransformation = PasswordVisualTransformation(),
                supportingText = { Text(stringResource(R.string.password_hint)) }, modifier = Modifier.fillMaxWidth())
            ErrorText(error)
            message?.let { Text(it, color = MaterialTheme.colorScheme.primary) }
            Button(enabled = current.isNotEmpty() && new.isNotEmpty(), onClick = {
                scope.launch {
                    error = null; message = null
                    try {
                        app.api.changePassword(current, new)
                        current = ""; new = ""
                        message = context.getString(R.string.password_changed)
                    } catch (e: Exception) {
                        error = context.errorMessage(e)
                    }
                }
            }) { Text(stringResource(R.string.change_password)) }

            HorizontalDivider()
            OutlinedButton(onClick = { scope.launch { app.api.logout() } }, modifier = Modifier.fillMaxWidth()) {
                Text(stringResource(R.string.sign_out))
            }
            TextButton(onClick = { confirmDelete = true }, modifier = Modifier.fillMaxWidth(),
                colors = ButtonDefaults.textButtonColors(contentColor = MaterialTheme.colorScheme.error)) {
                Text(stringResource(R.string.delete_account))
            }
        }
    }

    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text(stringResource(R.string.delete_account)) },
            text = { Text(stringResource(R.string.delete_account_confirm)) },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    scope.launch {
                        try {
                            app.api.deleteAccount()
                        } catch (e: Exception) {
                            error = context.errorMessage(e)
                        }
                    }
                }) { Text(stringResource(R.string.delete), color = MaterialTheme.colorScheme.error) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text(stringResource(R.string.cancel)) } },
        )
    }
}

@Composable
private fun UsageRow(label: String, used: Double, limit: Int?, format: String) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(if (limit == null) "$label: ${format.format(used)}" else "$label: ${format.format(used)} / $limit")
        if (limit != null && limit > 0) LinearProgressIndicator(progress = { (used / limit).toFloat().coerceIn(0f, 1f) }, modifier = Modifier.fillMaxWidth())
    }
}

/** Admins set the provider API keys here (also available in the web admin panel at /panel). */
@Composable
private fun AdminKeys() {
    val context = LocalContext.current
    val api = api()
    val scope = rememberCoroutineScope()
    var settings by remember { mutableStateOf<AdminSettings?>(null) }
    var groq by remember { mutableStateOf("") }
    var deepseek by remember { mutableStateOf("") }
    var error by remember { mutableStateOf<String?>(null) }
    var saved by remember { mutableStateOf(false) }
    LaunchedEffect(Unit) {
        try {
            settings = api.adminSettings()
        } catch (e: Exception) {
            error = context.errorMessage(e)
        }
    }

    @Composable
    fun status(s: SecretStatus?): String = when {
        s == null -> ""
        s.configured -> stringResource(R.string.key_set, s.hint ?: "")
        else -> stringResource(R.string.key_not_set)
    }

    HorizontalDivider()
    Text(stringResource(R.string.admin_api_keys), style = MaterialTheme.typography.titleMedium)
    OutlinedTextField(groq, { groq = it; saved = false }, label = { Text(stringResource(R.string.transcription_key)) },
        supportingText = { Text(status(settings?.transcription_api_key)) },
        singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
    OutlinedTextField(deepseek, { deepseek = it; saved = false }, label = { Text(stringResource(R.string.llm_key)) },
        supportingText = { Text(status(settings?.llm_api_key)) },
        singleLine = true, visualTransformation = PasswordVisualTransformation(), modifier = Modifier.fillMaxWidth())
    ErrorText(error)
    if (saved) Text(stringResource(R.string.keys_saved), color = MaterialTheme.colorScheme.primary)
    Button(enabled = groq.isNotBlank() || deepseek.isNotBlank(), onClick = {
        scope.launch {
            error = null
            try {
                settings = api.setApiKeys(groq, deepseek)
                groq = ""; deepseek = ""; saved = true
            } catch (e: Exception) {
                error = context.errorMessage(e)
            }
        }
    }) { Text(stringResource(R.string.save_keys)) }
}
