package com.meetings.app.data

import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.encodeToString
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonArray
import kotlinx.serialization.json.JsonElement
import kotlinx.serialization.json.JsonNull
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.buildJsonObject
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import kotlinx.serialization.json.put
import okhttp3.Authenticator
import okhttp3.HttpUrl
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okhttp3.RequestBody.Companion.asRequestBody
import okhttp3.RequestBody.Companion.toRequestBody
import okhttp3.Response
import okhttp3.Route
import java.io.File
import java.io.IOException
import java.net.URLDecoder
import java.util.concurrent.TimeUnit

/** An error the server reported (its "detail"), or a network failure. */
class ApiException(val status: Int, message: String) : IOException(message)

class ApiClient(private val session: SessionStore) {
    val json = Json { ignoreUnknownKeys = true; explicitNulls = false }
    private val jsonType = "application/json".toMediaType()

    private val http = OkHttpClient.Builder()
        .connectTimeout(20, TimeUnit.SECONDS)
        .writeTimeout(5, TimeUnit.MINUTES) // uploads of long recordings
        .readTimeout(2, TimeUnit.MINUTES) // PDF rendering
        .addInterceptor { chain ->
            val request = chain.request()
            val token = session.accessToken
            if (token == null || request.header("Authorization") != null || request.tag(NoAuth::class.java) != null) {
                chain.proceed(request)
            } else {
                chain.proceed(request.newBuilder().header("Authorization", "Bearer $token").build())
            }
        }
        .authenticator(RefreshAuthenticator())
        .build()

    private object NoAuth

    /** On a 401, swap the refresh token for a new pair once and retry; sign out if that fails. */
    private inner class RefreshAuthenticator : Authenticator {
        override fun authenticate(route: Route?, response: Response): Request? {
            if (response.request.tag(NoAuth::class.java) != null || response.priorResponse != null) return null
            val sent = response.request.header("Authorization")?.removePrefix("Bearer ")
            synchronized(this) {
                val current = session.accessToken ?: return null
                if (current != sent) {
                    // Another request already refreshed while this one was in flight.
                    return response.request.newBuilder().header("Authorization", "Bearer $current").build()
                }
                val refresh = session.refreshToken ?: return null
                val body = buildJsonObject { put("refresh_token", refresh) }.toString().toRequestBody(jsonType)
                val request = Request.Builder().url(url("auth/refresh")).post(body).tag(NoAuth::class.java, NoAuth).build()
                val auth = try {
                    http.newCall(request).execute().use { r ->
                        if (r.isSuccessful) json.decodeFromString<AuthResponse>(r.body!!.string()) else null
                    }
                } catch (e: IOException) {
                    return null // offline: keep the session, fail this request
                }
                if (auth == null) {
                    session.clear()
                    return null
                }
                session.save(auth)
                return response.request.newBuilder().header("Authorization", "Bearer ${auth.access_token}").build()
            }
        }
    }

    private fun url(path: String, query: Map<String, String?> = emptyMap()): HttpUrl {
        val builder = "${session.serverUrl}/$path".toHttpUrl().newBuilder()
        query.forEach { (k, v) -> if (v != null) builder.addQueryParameter(k, v) }
        return builder.build()
    }

    private suspend fun execute(request: Request): Response = withContext(Dispatchers.IO) {
        val response = try {
            http.newCall(request).execute()
        } catch (e: IOException) {
            throw ApiException(0, e.message ?: "Network error")
        }
        if (!response.isSuccessful) {
            val text = response.body?.string().orEmpty()
            response.close()
            if (response.code == 401 && request.tag(NoAuth::class.java) == null) session.clear()
            throw ApiException(response.code, errorDetail(text) ?: "HTTP ${response.code}")
        }
        response
    }

    private fun errorDetail(body: String): String? = try {
        when (val detail = json.parseToJsonElement(body).jsonObject["detail"]) {
            is JsonPrimitive -> detail.contentOrNull
            is JsonArray -> detail.joinToString("\n") { it.jsonObject["msg"]?.jsonPrimitive?.contentOrNull.orEmpty() }
            else -> null
        }
    } catch (e: Exception) {
        null
    }

    private suspend inline fun <reified T> call(request: Request): T = execute(request).use { response ->
        withContext(Dispatchers.IO) { json.decodeFromString<T>(response.body!!.string()) }
    }

    private suspend fun callUnit(request: Request) {
        execute(request).close()
    }

    private fun jsonBody(element: JsonElement): RequestBody = element.toString().toRequestBody(jsonType)
    private fun get(path: String, query: Map<String, String?> = emptyMap()) = Request.Builder().url(url(path, query)).get().build()
    private fun post(path: String, body: JsonElement, noAuth: Boolean = false) =
        Request.Builder().url(url(path)).post(jsonBody(body)).apply { if (noAuth) tag(NoAuth::class.java, NoAuth) }.build()
    private fun patch(path: String, body: JsonElement) = Request.Builder().url(url(path)).patch(jsonBody(body)).build()
    private fun delete(path: String) = Request.Builder().url(url(path)).delete().build()

    // ------------------------------------------------------------------ auth

    suspend fun login(email: String, password: String) {
        val body = buildJsonObject { put("email", email.trim()); put("password", password) }
        session.save(call<AuthResponse>(post("auth/login", body, noAuth = true)))
    }

    suspend fun register(email: String, password: String, fullName: String) {
        val body = buildJsonObject {
            put("email", email.trim()); put("password", password)
            if (fullName.isNotBlank()) put("full_name", fullName.trim())
        }
        session.save(call<AuthResponse>(post("auth/register", body, noAuth = true)))
    }

    suspend fun logout() {
        val refresh = session.refreshToken
        session.clear()
        if (refresh != null) {
            runCatching { callUnit(post("auth/logout", buildJsonObject { put("refresh_token", refresh) }, noAuth = true)) }
        }
    }

    suspend fun usage(): Usage = call(get("users/me/usage"))

    suspend fun changePassword(current: String, new: String) = callUnit(
        post("users/me/password", buildJsonObject { put("current_password", current); put("new_password", new) })
    )

    suspend fun deleteAccount() {
        callUnit(delete("users/me"))
        session.clear()
    }

    // ------------------------------------------------------------ categories

    suspend fun categories(): List<Category> = call(get("categories"))

    suspend fun createCategory(name: String): Category = call(post("categories", buildJsonObject { put("name", name.trim()) }))

    suspend fun renameCategory(id: String, name: String): Category =
        call(patch("categories/$id", buildJsonObject { put("name", name.trim()) }))

    suspend fun deleteCategory(id: String) = callUnit(delete("categories/$id"))

    // -------------------------------------------------------------- meetings

    suspend fun meetings(query: String?, categoryId: String?, uncategorized: Boolean, offset: Int): MeetingPage =
        call(
            get(
                "meetings",
                mapOf(
                    "q" to query?.takeIf { it.isNotBlank() },
                    "category_id" to categoryId,
                    "uncategorized" to if (uncategorized) "true" else null,
                    "limit" to "20",
                    "offset" to offset.toString(),
                ),
            )
        )

    suspend fun meeting(id: String): MeetingDetail = call(get("meetings/$id"))

    suspend fun upload(
        audio: File,
        mimeType: String,
        title: String?,
        categoryId: String?,
        attendees: List<String>,
        language: String?,
    ): MeetingDetail {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("audio", audio.name, audio.asRequestBody(mimeType.toMediaType()))
        title?.takeIf { it.isNotBlank() }?.let { body.addFormDataPart("title", it.trim()) }
        categoryId?.let { body.addFormDataPart("category_id", it) }
        language?.let { body.addFormDataPart("language", it) }
        if (attendees.isNotEmpty()) {
            body.addFormDataPart("attendees", json.encodeToString(attendees.map { Attendee(name = it) }))
        }
        return call(Request.Builder().url(url("meetings")).post(body.build()).build())
    }

    suspend fun replaceAudio(id: String, audio: File, mimeType: String): MeetingDetail {
        val body = MultipartBody.Builder().setType(MultipartBody.FORM)
            .addFormDataPart("audio", audio.name, audio.asRequestBody(mimeType.toMediaType()))
            .build()
        return call(Request.Builder().url(url("meetings/$id/audio")).post(body).build())
    }

    suspend fun resummarize(id: String): MeetingDetail = call(post("meetings/$id/summarize", JsonObject(emptyMap())))

    /** Edit title, category (null = none) and attendees. */
    suspend fun updateMeeting(id: String, title: String, categoryId: String?, attendees: List<Attendee>): MeetingDetail {
        val body = buildJsonObject {
            put("title", title.trim())
            put("category_id", categoryId?.let { JsonPrimitive(it) } ?: JsonNull)
            put("attendees", json.encodeToJsonElement(kotlinx.serialization.builtins.ListSerializer(Attendee.serializer()), attendees))
        }
        return call(patch("meetings/$id", body))
    }

    suspend fun deleteMeeting(id: String) = callUnit(delete("meetings/$id"))

    suspend fun addActionItem(meetingId: String, description: String): ActionItem =
        call(post("meetings/$meetingId/action-items", buildJsonObject { put("description", description.trim()) }))

    suspend fun setActionItemDone(meetingId: String, itemId: String, done: Boolean): ActionItem =
        call(patch("meetings/$meetingId/action-items/$itemId", buildJsonObject { put("is_done", done) }))

    suspend fun deleteActionItem(meetingId: String, itemId: String) =
        callUnit(delete("meetings/$meetingId/action-items/$itemId"))

    suspend fun export(id: String, format: String, includeTranscript: Boolean): ExportedFile {
        val request = get("meetings/$id/export", mapOf("format" to format, "include_transcript" to includeTranscript.toString()))
        return execute(request).use { response ->
            val bytes = withContext(Dispatchers.IO) { response.body!!.bytes() }
            val mime = response.header("Content-Type")?.substringBefore(';') ?: "application/octet-stream"
            ExportedFile(fileName(response.header("Content-Disposition")) ?: "meeting.$format", mime, bytes)
        }
    }

    private fun fileName(disposition: String?): String? {
        if (disposition == null) return null
        Regex("filename\\*=UTF-8''([^;]+)").find(disposition)?.let {
            return URLDecoder.decode(it.groupValues[1], "UTF-8").replace('/', '_')
        }
        return Regex("filename=\"([^\"]+)\"").find(disposition)?.groupValues?.get(1)
    }
}
