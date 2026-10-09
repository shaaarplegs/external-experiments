package com.meetings.app.ui

import android.os.Bundle
import androidx.activity.ComponentActivity
import androidx.activity.compose.setContent
import androidx.activity.enableEdgeToEdge
import androidx.compose.foundation.isSystemInDarkTheme
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.darkColorScheme
import androidx.compose.material3.dynamicDarkColorScheme
import androidx.compose.material3.dynamicLightColorScheme
import androidx.compose.material3.lightColorScheme
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.ui.graphics.Color
import androidx.compose.ui.platform.LocalContext
import androidx.lifecycle.compose.collectAsStateWithLifecycle
import androidx.navigation.compose.NavHost
import androidx.navigation.compose.composable
import androidx.navigation.compose.rememberNavController
import com.meetings.app.MeetingsApp
import android.os.Build

class MainActivity : ComponentActivity() {
    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        enableEdgeToEdge()
        val session = (application as MeetingsApp).session
        setContent {
            AppTheme {
                val user by session.user.collectAsStateWithLifecycle()
                if (user == null) AuthScreen() else AppNavigation()
            }
        }
    }
}

@Composable
private fun AppNavigation() {
    val nav = rememberNavController()
    NavHost(nav, startDestination = "meetings") {
        composable("meetings") {
            MeetingsScreen(
                onOpen = { nav.navigate("meeting/$it") },
                onNew = { nav.navigate("new") },
                onCategories = { nav.navigate("categories") },
                onSettings = { nav.navigate("settings") },
            )
        }
        composable("new") {
            NewMeetingScreen(
                onBack = { nav.popBackStack() },
                onCreated = { id -> nav.navigate("meeting/$id") { popUpTo("meetings") } },
            )
        }
        composable("meeting/{id}") { entry ->
            MeetingDetailScreen(entry.arguments!!.getString("id")!!, onBack = { nav.popBackStack() })
        }
        composable("categories") { CategoriesScreen(onBack = { nav.popBackStack() }) }
        composable("settings") { SettingsScreen(onBack = { nav.popBackStack() }) }
    }
}

@Composable
private fun AppTheme(content: @Composable () -> Unit) {
    val dark = isSystemInDarkTheme()
    val context = LocalContext.current
    val colors = when {
        Build.VERSION.SDK_INT >= 31 -> if (dark) dynamicDarkColorScheme(context) else dynamicLightColorScheme(context)
        dark -> darkColorScheme(primary = Color(0xFF3987E5))
        else -> lightColorScheme(primary = Color(0xFF2A78D6))
    }
    MaterialTheme(colorScheme = colors, content = content)
}
