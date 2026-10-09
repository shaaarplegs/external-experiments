package com.meetings.app.ui

import androidx.activity.compose.rememberLauncherForActivityResult
import androidx.activity.result.contract.ActivityResultContracts
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.rememberScrollState
import androidx.compose.foundation.text.selection.SelectionContainer
import androidx.compose.foundation.verticalScroll
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.automirrored.filled.ArrowBack
import androidx.compose.material.icons.filled.Delete
import androidx.compose.material.icons.filled.Edit
import androidx.compose.material.icons.filled.MoreVert
import androidx.compose.material.icons.outlined.Close
import androidx.compose.material3.AlertDialog
import androidx.compose.material3.Button
import androidx.compose.material3.Card
import androidx.compose.material3.Checkbox
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.DropdownMenu
import androidx.compose.material3.DropdownMenuItem
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
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextDecoration
import androidx.compose.ui.unit.dp
import com.meetings.app.R
import com.meetings.app.data.ActionItem
import com.meetings.app.data.Attendee
import com.meetings.app.data.Category
import com.meetings.app.data.MeetingDetail
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch
import kotlinx.coroutines.withContext

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MeetingDetailScreen(id: String, onBack: () -> Unit) {
    val api = api()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var meeting by remember { mutableStateOf<MeetingDetail?>(null) }
    var error by remember { mutableStateOf<String?>(null) }
    var busy by remember { mutableStateOf(false) }
    var menu by remember { mutableStateOf(false) }
    var editing by remember { mutableStateOf(false) }
    var confirmDelete by remember { mutableStateOf(false) }

    fun action(block: suspend () -> Unit) {
        scope.launch {
            busy = true
            error = null
            try {
                block()
            } catch (e: Exception) {
                error = context.errorMessage(e)
            } finally {
                busy = false
            }
        }
    }

    // Load, then poll while the server is still working on it.
    LaunchedEffect(id, meeting?.isProcessing) {
        do {
            try {
                meeting = api.meeting(id)
                error = null
            } catch (e: Exception) {
                error = context.errorMessage(e)
            }
            if (meeting?.isProcessing != false) delay(4000)
        } while (meeting?.isProcessing != false)
    }

    val reupload = rememberLauncherForActivityResult(ActivityResultContracts.GetContent()) { uri ->
        if (uri != null) action {
            val audio = withContext(Dispatchers.IO) { context.copyToCache(uri) }
            if (audio.file.length() > MAX_UPLOAD_BYTES) throw IllegalStateException(context.getString(R.string.file_too_large))
            meeting = api.replaceAudio(id, audio.file, audio.mimeType)
            audio.file.delete()
        }
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(meeting?.title ?: stringResource(R.string.untitled), maxLines = 1) },
                navigationIcon = {
                    IconButton(onClick = onBack) { Icon(Icons.AutoMirrored.Filled.ArrowBack, stringResource(R.string.back)) }
                },
                actions = {
                    val m = meeting
                    if (m != null) {
                        IconButton(onClick = { editing = true }) { Icon(Icons.Filled.Edit, stringResource(R.string.edit)) }
                        IconButton(onClick = { menu = true }) { Icon(Icons.Filled.MoreVert, null) }
                        DropdownMenu(menu, { menu = false }) {
                            if (m.status == "completed") {
                                DropdownMenuItem({ Text(stringResource(R.string.export_pdf)) }, {
                                    menu = false
                                    action { context.shareExport(api.export(id, "pdf", false)) }
                                })
                                DropdownMenuItem({ Text(stringResource(R.string.export_word)) }, {
                                    menu = false
                                    action { context.shareExport(api.export(id, "docx", false)) }
                                })
                                DropdownMenuItem({ Text(stringResource(R.string.export_pdf_transcript)) }, {
                                    menu = false
                                    action { context.shareExport(api.export(id, "pdf", true)) }
                                })
                            }
                            if (!m.isProcessing && !m.transcript.isNullOrBlank()) {
                                DropdownMenuItem({ Text(stringResource(R.string.resummarize)) }, {
                                    menu = false
                                    action { meeting = api.resummarize(id) }
                                })
                            }
                            if (!m.isProcessing) {
                                DropdownMenuItem({ Text(stringResource(R.string.replace_audio)) }, {
                                    menu = false; reupload.launch("*/*")
                                })
                            }
                            DropdownMenuItem({ Text(stringResource(R.string.delete_meeting)) }, {
                                menu = false; confirmDelete = true
                            })
                        }
                    }
                },
            )
        },
    ) { padding ->
        val m = meeting
        Column(Modifier.padding(padding).fillMaxSize()) {
            if (busy) LinearProgressIndicator(Modifier.fillMaxWidth())
            if (m == null) {
                ErrorText(error)
                if (error == null) CircularProgressIndicator(Modifier.padding(32.dp).align(Alignment.CenterHorizontally))
                return@Column
            }
            SelectionContainer {
                Column(
                    Modifier.verticalScroll(rememberScrollState()).padding(16.dp),
                    verticalArrangement = Arrangement.spacedBy(12.dp),
                ) {
                    ErrorText(error)
                    Text(
                        listOfNotNull(formatDate(m.meeting_date), formatDuration(m.duration_seconds), m.category?.name)
                            .joinToString(" · "),
                        color = MaterialTheme.colorScheme.onSurfaceVariant,
                    )
                    if (m.attendees.isNotEmpty()) {
                        Text(stringResource(R.string.attendees_list, m.attendees.joinToString("، ") { it.name }))
                    }
                    StatusSection(m, onRetry = {
                        if (m.transcript.isNullOrBlank()) reupload.launch("*/*") else action { meeting = api.resummarize(id) }
                    })
                    m.summary?.let { s ->
                        if (s.overview.isNotBlank()) Section(stringResource(R.string.overview)) { Text(s.overview) }
                        Bullets(stringResource(R.string.what_happened), s.what_happened)
                        Bullets(stringResource(R.string.decisions), s.decisions)
                        Bullets(stringResource(R.string.open_questions), s.open_questions)
                        if (s.parties.isNotEmpty()) {
                            Bullets(stringResource(R.string.parties), s.parties.map { p -> listOfNotNull(p.name, p.role).joinToString(" — ") })
                        }
                        if (s.topics.isNotEmpty()) Section(stringResource(R.string.topics)) { Text(s.topics.joinToString(" · ")) }
                    }
                    if (m.status == "completed" || m.action_items.isNotEmpty()) {
                        ActionItems(
                            m.action_items,
                            onToggle = { item ->
                                action {
                                    val updated = api.setActionItemDone(id, item.id, !item.is_done)
                                    meeting = meeting?.copy(action_items = meeting!!.action_items.map { if (it.id == item.id) updated else it })
                                }
                            },
                            onDelete = { item ->
                                action {
                                    api.deleteActionItem(id, item.id)
                                    meeting = meeting?.copy(action_items = meeting!!.action_items.filter { it.id != item.id })
                                }
                            },
                            onAdd = { text ->
                                action {
                                    val added = api.addActionItem(id, text)
                                    meeting = meeting?.copy(action_items = meeting!!.action_items + added)
                                }
                            },
                        )
                    }
                    if (!m.transcript.isNullOrBlank()) Transcript(m.transcript)
                }
            }
        }
    }

    if (editing) meeting?.let { m ->
        EditMeetingDialog(m, onDismiss = { editing = false }) { title, categoryId, attendees ->
            editing = false
            action { meeting = api.updateMeeting(id, title, categoryId, attendees) }
        }
    }
    if (confirmDelete) {
        AlertDialog(
            onDismissRequest = { confirmDelete = false },
            title = { Text(stringResource(R.string.delete_meeting)) },
            text = { Text(stringResource(R.string.delete_meeting_confirm)) },
            confirmButton = {
                TextButton(onClick = {
                    confirmDelete = false
                    action { api.deleteMeeting(id); onBack() }
                }) { Text(stringResource(R.string.delete)) }
            },
            dismissButton = { TextButton(onClick = { confirmDelete = false }) { Text(stringResource(R.string.cancel)) } },
        )
    }
}

@Composable
private fun StatusSection(m: MeetingDetail, onRetry: () -> Unit) {
    when {
        m.isProcessing -> Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(statusLabel(m.status), style = MaterialTheme.typography.titleMedium)
                LinearProgressIndicator(Modifier.fillMaxWidth())
                Text(stringResource(R.string.processing_hint), style = MaterialTheme.typography.bodySmall)
            }
        }
        m.status == "failed" -> Card(Modifier.fillMaxWidth()) {
            Column(Modifier.padding(16.dp), verticalArrangement = Arrangement.spacedBy(8.dp)) {
                Text(statusLabel(m.status), color = MaterialTheme.colorScheme.error, style = MaterialTheme.typography.titleMedium)
                m.error_message?.let { Text(it) }
                Button(onClick = onRetry) {
                    Text(stringResource(if (m.transcript.isNullOrBlank()) R.string.replace_audio else R.string.resummarize))
                }
            }
        }
    }
}

@Composable
private fun Section(title: String, content: @Composable () -> Unit) {
    Column(verticalArrangement = Arrangement.spacedBy(4.dp)) {
        Text(title, style = MaterialTheme.typography.titleMedium, color = MaterialTheme.colorScheme.primary)
        content()
    }
}

@Composable
private fun Bullets(title: String, lines: List<String>) {
    if (lines.isEmpty()) return
    Section(title) { lines.forEach { Text("•  $it") } }
}

@Composable
private fun ActionItems(items: List<ActionItem>, onToggle: (ActionItem) -> Unit, onDelete: (ActionItem) -> Unit, onAdd: (String) -> Unit) {
    var newItem by remember { mutableStateOf("") }
    Section(stringResource(R.string.action_items)) {
        items.forEach { item ->
            Row(verticalAlignment = Alignment.CenterVertically) {
                Checkbox(item.is_done, { onToggle(item) })
                Column(Modifier.weight(1f)) {
                    Text(item.description, textDecoration = if (item.is_done) TextDecoration.LineThrough else null)
                    val meta = listOfNotNull(item.assignee, item.due_date ?: item.due_text, item.priority).joinToString(" · ")
                    if (meta.isNotEmpty()) {
                        Text(meta, style = MaterialTheme.typography.bodySmall, color = MaterialTheme.colorScheme.onSurfaceVariant)
                    }
                }
                IconButton(onClick = { onDelete(item) }) { Icon(Icons.Outlined.Close, stringResource(R.string.delete)) }
            }
        }
        Row(verticalAlignment = Alignment.CenterVertically) {
            OutlinedTextField(newItem, { newItem = it }, placeholder = { Text(stringResource(R.string.add_action_item)) },
                singleLine = true, modifier = Modifier.weight(1f))
            TextButton(enabled = newItem.isNotBlank(), onClick = { onAdd(newItem); newItem = "" }) {
                Text(stringResource(R.string.add))
            }
        }
    }
}

@Composable
private fun Transcript(text: String) {
    var open by remember { mutableStateOf(false) }
    HorizontalDivider()
    OutlinedButton(onClick = { open = !open }) {
        Text(stringResource(if (open) R.string.hide_transcript else R.string.show_transcript))
    }
    if (open) Text(text, style = MaterialTheme.typography.bodyMedium)
}

@Composable
private fun EditMeetingDialog(m: MeetingDetail, onDismiss: () -> Unit, onSave: (String, String?, List<Attendee>) -> Unit) {
    val api = api()
    var title by remember { mutableStateOf(m.title.orEmpty()) }
    var categoryId by remember { mutableStateOf(m.category?.id) }
    var attendees by remember { mutableStateOf(m.attendees.joinToString(", ") { it.name }) }
    var categories by remember { mutableStateOf<List<Category>>(emptyList()) }
    LaunchedEffect(Unit) { runCatching { categories = api.categories() } }
    AlertDialog(
        onDismissRequest = onDismiss,
        title = { Text(stringResource(R.string.edit)) },
        text = {
            Column(verticalArrangement = Arrangement.spacedBy(8.dp)) {
                OutlinedTextField(title, { title = it }, label = { Text(stringResource(R.string.title)) }, singleLine = true)
                CategoryPicker(categories, categoryId) { categoryId = it }
                OutlinedTextField(attendees, { attendees = it }, label = { Text(stringResource(R.string.attendees)) },
                    supportingText = { Text(stringResource(R.string.attendees_hint)) })
            }
        },
        confirmButton = {
            TextButton(onClick = {
                // Keep the email and role of attendees whose names didn't change.
                val existing = m.attendees.associateBy { it.name }
                val list = attendees.split(',', '،', '\n').map { it.trim() }.filter { it.isNotEmpty() }
                    .map { existing[it] ?: Attendee(name = it) }
                onSave(title, categoryId, list)
            }) { Text(stringResource(R.string.save)) }
        },
        dismissButton = { TextButton(onClick = onDismiss) { Text(stringResource(R.string.cancel)) } },
    )
}
