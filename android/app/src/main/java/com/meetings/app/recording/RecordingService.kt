package com.meetings.app.recording

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Context
import android.content.Intent
import android.content.pm.ServiceInfo
import android.media.MediaRecorder
import android.os.Build
import android.os.IBinder
import android.os.SystemClock
import androidx.core.app.NotificationCompat
import androidx.core.app.ServiceCompat
import androidx.core.content.ContextCompat
import com.meetings.app.R
import com.meetings.app.ui.MainActivity
import kotlinx.coroutines.flow.MutableStateFlow
import kotlinx.coroutines.flow.StateFlow
import java.io.File

/**
 * Records the meeting in a foreground service so it keeps going with the screen off
 * or the app in the background. AAC mono at 16 kHz / 32 kbps: about 14 MB per hour,
 * which stays under the server's 32 MB upload limit for a little over two hours.
 */
class RecordingService : Service() {

    sealed interface State {
        data object Idle : State
        /** [elapsedBase] is the SystemClock.elapsedRealtime() the timer counts from (excludes pauses). */
        data class Recording(val file: File, val elapsedBase: Long, val paused: Boolean, val pausedAt: Long = 0) : State
        data class Finished(val file: File, val durationSeconds: Int) : State
        data class Failed(val message: String) : State
    }

    private var recorder: MediaRecorder? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_START -> start()
            ACTION_PAUSE -> pause()
            ACTION_RESUME -> resume()
            ACTION_STOP -> stop()
        }
        return START_NOT_STICKY
    }

    private fun start() {
        if (_state.value is State.Recording) return
        ServiceCompat.startForeground(
            this, NOTIFICATION_ID, notification(),
            if (Build.VERSION.SDK_INT >= 30) ServiceInfo.FOREGROUND_SERVICE_TYPE_MICROPHONE else 0,
        )
        val dir = File(filesDir, "recordings").apply { mkdirs() }
        val file = File(dir, "meeting-${System.currentTimeMillis()}.m4a")
        try {
            recorder = (if (Build.VERSION.SDK_INT >= 31) MediaRecorder(this) else @Suppress("DEPRECATION") MediaRecorder()).apply {
                setAudioSource(MediaRecorder.AudioSource.MIC)
                setOutputFormat(MediaRecorder.OutputFormat.MPEG_4)
                setAudioEncoder(MediaRecorder.AudioEncoder.AAC)
                setAudioChannels(1)
                setAudioSamplingRate(16_000)
                setAudioEncodingBitRate(32_000)
                setOutputFile(file.absolutePath)
                prepare()
                start()
            }
            _state.value = State.Recording(file, SystemClock.elapsedRealtime(), paused = false)
        } catch (e: Exception) {
            release()
            file.delete()
            _state.value = State.Failed(e.message ?: "Recording failed")
            stopSelf()
        }
    }

    private fun pause() {
        val current = _state.value as? State.Recording ?: return
        if (current.paused) return
        recorder?.pause()
        _state.value = current.copy(paused = true, pausedAt = SystemClock.elapsedRealtime())
    }

    private fun resume() {
        val current = _state.value as? State.Recording ?: return
        if (!current.paused) return
        recorder?.resume()
        _state.value = current.copy(paused = false, elapsedBase = current.elapsedBase + SystemClock.elapsedRealtime() - current.pausedAt)
    }

    private fun stop() {
        val current = _state.value as? State.Recording
        val ok = runCatching { recorder?.stop() }.isSuccess
        release()
        ServiceCompat.stopForeground(this, ServiceCompat.STOP_FOREGROUND_REMOVE)
        stopSelf()
        if (current == null) return
        val end = if (current.paused) current.pausedAt else SystemClock.elapsedRealtime()
        _state.value = if (ok && current.file.length() > 0) {
            State.Finished(current.file, ((end - current.elapsedBase) / 1000).toInt())
        } else {
            current.file.delete()
            State.Failed("The recording was too short")
        }
    }

    private fun release() {
        recorder?.release()
        recorder = null
    }

    override fun onDestroy() {
        if (_state.value is State.Recording) stop()
        super.onDestroy()
    }

    private fun notification(): Notification {
        val manager = getSystemService(NotificationManager::class.java)
        manager.createNotificationChannel(
            NotificationChannel(CHANNEL, getString(R.string.recording_channel), NotificationManager.IMPORTANCE_LOW)
        )
        val open = PendingIntent.getActivity(
            this, 0, Intent(this, MainActivity::class.java).addFlags(Intent.FLAG_ACTIVITY_SINGLE_TOP),
            PendingIntent.FLAG_IMMUTABLE,
        )
        return NotificationCompat.Builder(this, CHANNEL)
            .setSmallIcon(R.drawable.ic_mic)
            .setContentTitle(getString(R.string.recording_in_progress))
            .setContentIntent(open)
            .setOngoing(true)
            .setUsesChronometer(true)
            .build()
    }

    companion object {
        private const val CHANNEL = "recording"
        private const val NOTIFICATION_ID = 1
        private const val ACTION_START = "start"
        private const val ACTION_PAUSE = "pause"
        private const val ACTION_RESUME = "resume"
        private const val ACTION_STOP = "stop"

        private val _state = MutableStateFlow<State>(State.Idle)
        val state: StateFlow<State> = _state

        fun start(context: Context) = ContextCompat.startForegroundService(context, intent(context, ACTION_START))
        fun pause(context: Context) = context.startService(intent(context, ACTION_PAUSE))
        fun resume(context: Context) = context.startService(intent(context, ACTION_RESUME))
        fun stop(context: Context) = context.startService(intent(context, ACTION_STOP))

        /** Forget a finished or failed recording once the screen has taken it. */
        fun reset() {
            if (_state.value !is State.Recording) _state.value = State.Idle
        }

        private fun intent(context: Context, action: String) = Intent(context, RecordingService::class.java).setAction(action)
    }
}
