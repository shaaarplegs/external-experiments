package com.meetings.app.ui

import android.content.Context
import android.content.Intent
import android.net.Uri
import android.provider.OpenableColumns
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.Text
import androidx.compose.runtime.Composable
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.unit.dp
import androidx.core.content.FileProvider
import com.meetings.app.MeetingsApp
import com.meetings.app.R
import com.meetings.app.data.ApiClient
import com.meetings.app.data.ApiException
import com.meetings.app.data.ExportedFile
import java.io.File
import java.time.OffsetDateTime
import java.time.ZoneId
import java.time.format.DateTimeFormatter
import java.time.format.FormatStyle

/** The server's 32 MB request cap, less room for the form fields. */
const val MAX_UPLOAD_BYTES = 31L * 1024 * 1024

@Composable
fun api(): ApiClient = (LocalContext.current.applicationContext as MeetingsApp).api

fun Context.errorMessage(e: Throwable): String = when {
    e is ApiException && e.status == 0 -> getString(R.string.error_network)
    else -> e.message ?: getString(R.string.error_generic)
}

@Composable
fun ErrorText(message: String?) {
    if (message != null) {
        Text(
            message,
            color = MaterialTheme.colorScheme.error,
            style = MaterialTheme.typography.bodyMedium,
            modifier = Modifier.fillMaxWidth().padding(vertical = 4.dp),
        )
    }
}

private val dateFormat = DateTimeFormatter.ofLocalizedDateTime(FormatStyle.MEDIUM, FormatStyle.SHORT)

fun formatDate(iso: String): String = try {
    OffsetDateTime.parse(iso).atZoneSameInstant(ZoneId.systemDefault()).format(dateFormat)
} catch (e: Exception) {
    iso
}

fun formatDuration(seconds: Int?): String? {
    if (seconds == null) return null
    val h = seconds / 3600
    val m = seconds % 3600 / 60
    val s = seconds % 60
    return if (h > 0) "%d:%02d:%02d".format(h, m, s) else "%d:%02d".format(m, s)
}

@Composable
fun statusLabel(status: String): String = stringResource(
    when (status) {
        "queued" -> R.string.status_queued
        "transcribing" -> R.string.status_transcribing
        "summarizing" -> R.string.status_summarizing
        "completed" -> R.string.status_completed
        else -> R.string.status_failed
    }
)

/** A picked file copied into the cache, so it can be uploaded with a known length. */
class PickedAudio(val file: File, val mimeType: String)

fun Context.copyToCache(uri: Uri): PickedAudio {
    var name = "audio"
    contentResolver.query(uri, arrayOf(OpenableColumns.DISPLAY_NAME), null, null, null)?.use { c ->
        if (c.moveToFirst()) c.getString(0)?.let { name = it }
    }
    val dir = File(cacheDir, "uploads").apply { mkdirs() }
    val file = File(dir, name.replace('/', '_'))
    contentResolver.openInputStream(uri)!!.use { input -> file.outputStream().use { input.copyTo(it) } }
    return PickedAudio(file, contentResolver.getType(uri) ?: "application/octet-stream")
}

fun Context.shareExport(export: ExportedFile) {
    val dir = File(cacheDir, "exports").apply { mkdirs() }
    val file = File(dir, export.name).apply { writeBytes(export.bytes) }
    val uri = FileProvider.getUriForFile(this, "$packageName.files", file)
    val send = Intent(Intent.ACTION_SEND)
        .setType(export.mimeType)
        .putExtra(Intent.EXTRA_STREAM, uri)
        .addFlags(Intent.FLAG_GRANT_READ_URI_PERMISSION)
    startActivity(Intent.createChooser(send, export.name))
}
