package com.meetings.app

import android.app.Application
import com.meetings.app.data.ApiClient
import com.meetings.app.data.SessionStore

class MeetingsApp : Application() {
    lateinit var session: SessionStore
        private set
    lateinit var api: ApiClient
        private set

    override fun onCreate() {
        super.onCreate()
        session = SessionStore(this)
        api = ApiClient(session)
    }
}
