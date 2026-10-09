package com.meetings.app.data

import kotlinx.serialization.Serializable

@Serializable
data class User(
    val id: String,
    val email: String,
    val full_name: String? = null,
    val role: String = "user",
)

@Serializable
data class AuthResponse(
    val access_token: String,
    val refresh_token: String,
    val expires_in: Int,
    val user: User,
)

@Serializable
data class Usage(
    val uploads: Int,
    val uploads_limit: Int? = null,
    val audio_minutes: Double,
    val audio_minutes_limit: Int? = null,
    val period_end: String,
)

@Serializable
data class CategoryRef(val id: String, val name: String, val color: String? = null)

@Serializable
data class Category(
    val id: String,
    val name: String,
    val color: String? = null,
    val meeting_count: Int = 0,
)

@Serializable
data class MeetingListItem(
    val id: String,
    val title: String? = null,
    val category: CategoryRef? = null,
    val meeting_date: String,
    val duration_seconds: Int? = null,
    val status: String,
    val error_message: String? = null,
    val attendee_count: Int = 0,
    val action_items_total: Int = 0,
    val action_items_done: Int = 0,
    val overview: String? = null,
)

@Serializable
data class MeetingPage(val items: List<MeetingListItem>, val total: Int, val limit: Int, val offset: Int)

@Serializable
data class Attendee(val name: String, val email: String? = null, val role: String? = null)

@Serializable
data class Party(val name: String, val role: String? = null)

@Serializable
data class Summary(
    val title: String = "",
    val language: String = "",
    val overview: String = "",
    val what_happened: List<String> = emptyList(),
    val decisions: List<String> = emptyList(),
    val parties: List<Party> = emptyList(),
    val mentioned_date: String? = null,
    val open_questions: List<String> = emptyList(),
    val topics: List<String> = emptyList(),
)

@Serializable
data class ActionItem(
    val id: String,
    val description: String,
    val assignee: String? = null,
    val due_date: String? = null,
    val due_text: String? = null,
    val priority: String? = null,
    val is_done: Boolean = false,
    val source: String = "ai",
)

@Serializable
data class MeetingDetail(
    val id: String,
    val title: String? = null,
    val category: CategoryRef? = null,
    val meeting_date: String,
    val duration_seconds: Int? = null,
    val language: String? = null,
    val status: String,
    val error_message: String? = null,
    val attendees: List<Attendee> = emptyList(),
    val transcript: String? = null,
    val summary: Summary? = null,
    val action_items: List<ActionItem> = emptyList(),
) {
    val isProcessing get() = status in setOf("queued", "transcribing", "summarizing")
}

/** A download from GET /meetings/{id}/export. */
class ExportedFile(val name: String, val mimeType: String, val bytes: ByteArray)
