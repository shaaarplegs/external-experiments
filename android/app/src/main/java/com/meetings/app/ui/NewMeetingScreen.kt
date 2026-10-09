package com.meetings.app.ui

import android.Manifest
import android.content.pm.PackageManager
import android.os.Build
import android.os.SystemClock
import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.imePadding
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.AttachFile
import androidx.compose.material.icons.filled.Mic
import androidx.compose.material.icons.filled.Pause
import androidx.compose.material.icons.filled.PlayArrow
import androidx.compose.material.icons.filled.Stop
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenuItem
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExposedDropdownMenuBox
import androidx.compose.material3.ExposedDropdownMenuDefaults
import androidx.compose.material3.FilledTonalButton
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.MenuAnchorType
import androidx.compose.material3.OutlinedButton
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.SegmentedButton
import androidx.compose.material3.SegmentedButtonDefaults
import androidx.compose.material3.SingleChoiceSegmentedButtonRow
import androidx.compose.material3.Text
import androidx.compose.material3.TopAppBar
import androidx.compose.runtime.Composable
import androidx.compose.runtime.LaunchedEffect
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableLongStateOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.content.ContextCompat
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import com.meetings.app.R
import com.meetings.app.data.Category
import com.meetings.app.recording.RecordingService
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

private val LANGUAGES = listOf("auto" to R.string.lang_auto, "ar" to R.string.lang_ar, "en" to R.string.lang_en)

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun NewMeetingScreen(onBack: () -> Unit, onCreated: (String) -> Unit) {
    val api = api()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    val recording by RecordingService.state.collectAsStateWithLifecycle()
    var picked by remember { mutableStateOf<PickedAudio?>(null) }
    var title by rememberSaveable { mutableStateOf("") }
    var attendees by rememberSaveable { mutableStateOf("") }
    var language by rememberSaveable { mutableStateOf("auto") }
    var categoryId by rememberSaveable { mutableStateOf<String?>(null) }
    var categories by remember { mutableStateOf<List<Category>>(emptyList()) }
    var busy by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }

    LaunchedEffect(Unit) { runCatching { categories = api.categories() } }

    // A finished recording becomes the file to upload.
    LaunchedEffect(recording) {
        when (val s = recording) {
            is RecordingService.State.Finished -> {
                picked = PickedAudio(s.file, "audio/mp4")
                RecordingService.reset()
            }
            is RecordingService.State.Failed -> {
                error = s.message
                RecordingService.reset()
            }
            else -> Unit
        }
    }

    val permissions = rememberLauncherForActivityResult(ActivityResultContracts.RequestMultiplePermissions()) { result ->
        if (result[Manifest.permission.RECORD_AUDIO] == true) RecordingService.start(context)
        else error = context.getString(R.string.mic_permission_needed)
    }
    val picker = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) scope.launch {
            busy = true
            error = null
            try {
                picked = withContext(Dispatchers.IO) { context.copyToCache(uri) }
            } catch (e: Exception) {
                error = context.errorMessage(e)
            } finally {
                busy = false
            }
        }
    }

    fun startRecording() {
        error = null
        val needed = buildList {
            add(Manifest.permission.RECORD_AUDIO)
            if (Build.VERSION.SDK_INT >= 33) add(Manifest.permission.POST_NOTIFICATIONS)
        }
        if (ContextCompat.checkSelfPermission(context, Manifest.permission.RECORD_AUDIO) == PackageManager.PERMISSION_GRANTED) {
            RecordingService.start(context)
        } else {
            permissions.launch(needed.toTypedArray())
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.new_meeting)) },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back)) }
                },
            )
        },
    ) { padding ->
        Column(
            Modifier.padding(padding).imePadding().verticalScroll(rememberScrollState()).padding(16.dp),
            verticalArrangement = Arrangement.spacedBy(12.dp),
        ) {
            Card(Modifier.fillMaxWidth()) {
                Column(
                    Modifier.fillMaxWidth().padding(16.dp),
                    horizontalAlignment = Alignment.CenterHorizontally,
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    val rec = recording as? RecordingService.State.Recording
                    val file = picked
                    when {
                        rec != null -> {
                            RecordingTimer(rec)
                            Row(horizontalArrangement = Arrangement.spacedBy(12.dp)) {
                                if (rec.paused) {
                                    FilledTonalButton(onClick = { RecordingService.resume(context) }) {
                                        Icon(Icons.Filled.PlayArrow, null); Text(stringResource(R.string.resume))
                                    }
                                } else {
                                    FilledTonalButton(onClick = { RecordingService.pause(context) }) {
                                        Icon(Icons.Filled.Pause, null); Text(stringResource(R.string.pause))
                                    }
                                }
                                Button(onClick = { RecordingService.stop(context) }) {
                                    Icon(Icons.Filled.Stop, null); Text(stringResource(R.string.stop))
                                }
                            }
                            Text(stringResource(R.string.recording_hint), style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                        file != null -> {
                            Text(file.file.name, style = MaterialTheme.typography.titleMedium)
                            Text("%.1f MB".format(file.file.length() / 1_048_576.0), style = MaterialTheme.typography.bodySmall)
                            OutlinedButton(onClick = { picked = null }) { Text(stringResource(R.string.discard)) }
                        }
                        else -> {
                            Button(onClick = ::startRecording, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
                                Icon(Icons.Filled.Mic, null); Text(stringResource(R.string.record))
                            }
                            OutlinedButton(onClick = { picker.launch("*/*") }, enabled = !busy, modifier = Modifier.fillMaxWidth()) {
                                Icon(Icons.Filled.AttachFile, null); Text(stringResource(R.string.choose_file))
                            }
                            Text(stringResource(R.string.choose_file_hint), style = MaterialTheme.typography.bodySmall,
                                color = MaterialTheme.colorScheme.onSurfaceVariant)
                        }
                    }
                }
            }

            OutlinedTextField(title, { title = it }, label = { Text(stringResource(R.string.title_optional)) },
                singleLine = true, modifier = Modifier.fillMaxWidth())
            CategoryPicker(categories, categoryId) { categoryId = it }
            OutlinedTextField(attendees, { attendees = it }, label = { Text(stringResource(R.string.attendees)) },
                supportingText = { Text(stringResource(R.string.attendees_hint)) }, modifier = Modifier.fillMaxWidth())
            Text(stringResource(R.string.language), style = MaterialTheme.typography.labelLarge)
            SingleChoiceSegmentedButtonRow(Modifier.fillMaxWidth()) {
                LANGUAGES.forEachIndexed { i, (code, label) ->
                    SegmentedButton(
                        selected = language == code, onClick = { language = code },
                        shape = SegmentedButtonDefaults.itemShape(i, LANGUAGES.size),
                    ) { Text(stringResource(label)) }
                }
            }
            ErrorText(error)
            Button(
                enabled = picked != null && !busy && recording !is RecordingService.State.Recording,
                modifier = Modifier.fillMaxWidth(),
                onClick = {
                    val file = picked ?: return@Button
                    if (file.file.length() > MAX_UPLOAD_BYTES) {
                        error = context.getString(R.string.file_too_large)
                        return@Button
                    }
                    busy = true
                    error = null
                    scope.launch {
                        try {
                            val meeting = api.upload(
                                file.file, file.mimeType, title, categoryId,
                                attendees.split(',', '،', '\n').map { it.trim() }.filter { it.isNotEmpty() },
                                language,
                            )
                            file.file.delete()
                            onCreated(meeting.id)
                        } catch (e: Exception) {
                            error = context.errorMessage(e)
                        } finally {
                            busy = false
                        }
                    }
                },
            ) {
                if (busy) CircularProgressIndicator(Modifier.padding(2.dp), strokeWidth = 2.dp)
                else Text(stringResource(R.string.upload_and_summarize))
            }
        }
    }
}

@Composable
private fun RecordingTimer(rec: RecordingService.State.Recording) {
    var now by remember { mutableLongStateOf(SystemClock.elapsedRealtime()) }
    LaunchedEffect(rec) {
        while (!rec.paused) {
            now = SystemClock.elapsedRealtime()
            delay(500)
        }
    }
    val end = if (rec.paused) rec.pausedAt else now
    Text(
        formatDuration(((end - rec.elapsedBase) / 1000).toInt().coerceAtLeast(0))!!,
        style = MaterialTheme.typography.displayMedium,
        color = if (rec.paused) MaterialTheme.colorScheme.onSurfaceVariant else MaterialTheme.colorScheme.error,
    )
}

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun CategoryPicker(categories: List<Category>, selected: String?, onSelect: (String?) -> Unit) {
    var expanded by remember { mutableStateOf(false) }
    val none = stringResource(R.string.no_category)
    ExposedDropdownMenuBox(expanded, { expanded = it }) {
        OutlinedTextField(
            value = categories.firstOrNull { it.id == selected }?.name ?: none,
            onValueChange = {},
            readOnly = true,
            label = { Text(stringResource(R.string.category)) },
            trailingIcon = { ExposedDropdownMenuDefaults.TrailingIcon(expanded) },
            modifier = Modifier.fillMaxWidth().menuAnchor(MenuAnchorType.PrimaryNotEditable),
        )
        ExposedDropdownMenu(expanded, { expanded = false }) {
            DropdownMenuItem({ Text(none) }, { onSelect(null); expanded = false })
            categories.forEach { c ->
                DropdownMenuItem({ Text(c.name) }, { onSelect(c.id); expanded = false })
            }
        }
    }
}
