package com.meetings.app.ui

import androidx.compose.foundation.clickable
import androidx.compose.foundation.layout.Arrangement
import androidx.compose.foundation.layout.Column
import androidx.compose.foundation.layout.Row
import androidx.compose.foundation.layout.Spacer
import androidx.compose.foundation.layout.fillMaxSize
import androidx.compose.foundation.layout.fillMaxWidth
import androidx.compose.foundation.layout.padding
import androidx.compose.foundation.layout.width
import androidx.compose.foundation.lazy.LazyColumn
import androidx.compose.foundation.lazy.LazyRow
import androidx.compose.foundation.lazy.items
import androidx.compose.material.icons.Icons
import androidx.compose.material.icons.filled.Add
import androidx.compose.material.icons.filled.Search
import androidx.compose.material.icons.outlined.Label
import androidx.compose.material.icons.outlined.Settings
import androidx.compose.material3.Card
import androidx.compose.material3.CircularProgressIndicator
import androidx.compose.material3.ExperimentalMaterial3Api
import androidx.compose.material3.ExtendedFloatingActionButton
import androidx.compose.material3.FilterChip
import androidx.compose.material3.Icon
import androidx.compose.material3.IconButton
import androidx.compose.material3.MaterialTheme
import androidx.compose.material3.OutlinedTextField
import androidx.compose.material3.Scaffold
import androidx.compose.material3.Text
import androidx.compose.material3.TextButton
import androidx.compose.material3.TopAppBar
import androidx.compose.material3.pulltorefresh.PullToRefreshBox
import androidx.compose.runtime.Composable
import androidx.compose.runtime.getValue
import androidx.compose.runtime.mutableIntStateOf
import androidx.compose.runtime.mutableStateListOf
import androidx.compose.runtime.mutableStateOf
import androidx.compose.runtime.remember
import androidx.compose.runtime.rememberCoroutineScope
import androidx.compose.runtime.saveable.rememberSaveable
import androidx.compose.runtime.setValue
import androidx.compose.ui.Alignment
import androidx.compose.ui.Modifier
import androidx.compose.ui.platform.LocalContext
import androidx.compose.ui.res.stringResource
import androidx.compose.ui.text.style.TextOverflow
import androidx.compose.ui.unit.dp
import androidx.lifecycle.Lifecycle
import androidx.lifecycle.compose.LifecycleEventEffect
import com.meetings.app.R
import com.meetings.app.data.Category
import com.meetings.app.data.MeetingListItem
import kotlinx.coroutines.delay
import kotlinx.coroutines.launch

private const val UNCATEGORIZED = "-"

@OptIn(ExperimentalMaterial3Api::class)
@Composable
fun MeetingsScreen(onOpen: (String) -> Unit, onNew: () -> Unit, onCategories: () -> Unit, onSettings: () -> Unit) {
    val api = api()
    val context = LocalContext.current
    val scope = rememberCoroutineScope()
    var query by rememberSaveable { mutableStateOf("") }
    var filter by rememberSaveable { mutableStateOf<String?>(null) } // category id, UNCATEGORIZED or null = all
    val items = remember { mutableStateListOf<MeetingListItem>() }
    var categories by remember { mutableStateOf<List<Category>>(emptyList()) }
    var total by remember { mutableIntStateOf(0) }
    var loading by remember { mutableStateOf(false) }
    var error by remember { mutableStateOf<String?>(null) }
    var reload by remember { mutableIntStateOf(0) }

    suspend fun load(append: Boolean) {
        loading = true
        error = null
        try {
            val page = api.meetings(
                query, filter?.takeIf { it != UNCATEGORIZED }, filter == UNCATEGORIZED,
                offset = if (append) items.size else 0,
            )
            if (!append) items.clear()
            items.addAll(page.items)
            total = page.total
        } catch (e: Exception) {
            error = context.errorMessage(e)
        } finally {
            loading = false
        }
    }

    LifecycleEventEffect(Lifecycle.Event.ON_RESUME) { reload++ }
    androidx.compose.runtime.LaunchedEffect(query, filter, reload) {
        if (reload == 0) return@LaunchedEffect
        delay(if (query.isNotEmpty()) 350 else 0) // debounce typing
        runCatching { categories = api.categories() }
        load(append = false)
    }

    Scaffold(
        topBar = {
            TopAppBar(
                title = { Text(stringResource(R.string.meetings)) },
                actions = {
                    IconButton(onClick = onCategories) { Icon(Icons.Outlined.Label, stringResource(R.string.categories)) }
                    IconButton(onClick = onSettings) { Icon(Icons.Outlined.Settings, stringResource(R.string.settings)) }
                },
            )
        },
        floatingActionButton = {
            ExtendedFloatingActionButton(
                onClick = onNew,
                icon = { Icon(Icons.Filled.Add, null) },
                text = { Text(stringResource(R.string.new_meeting)) },
            )
        },
    ) { padding ->
        Column(Modifier.padding(padding).fillMaxSize()) {
            OutlinedTextField(
                query, { query = it },
                placeholder = { Text(stringResource(R.string.search_meetings)) },
                leadingIcon = { Icon(Icons.Filled.Search, null) },
                singleLine = true,
                modifier = Modifier.fillMaxWidth().padding(horizontal = 16.dp),
            )
            LazyRow(
                horizontalArrangement = Arrangement.spacedBy(8.dp),
                contentPadding = androidx.compose.foundation.layout.PaddingValues(horizontal = 16.dp, vertical = 8.dp),
            ) {
                item { FilterChip(filter == null, { filter = null }, { Text(stringResource(R.string.all)) }) }
                items(categories, key = { it.id }) { c ->
                    FilterChip(filter == c.id, { filter = c.id }, { Text(c.name) })
                }
                item { FilterChip(filter == UNCATEGORIZED, { filter = UNCATEGORIZED }, { Text(stringResource(R.string.uncategorized)) }) }
            }
            ErrorText(error)
            PullToRefreshBox(
                isRefreshing = loading && items.isNotEmpty(),
                onRefresh = { scope.launch { load(append = false) } },
                modifier = Modifier.fillMaxSize(),
            ) {
                LazyColumn(
                    Modifier.fillMaxSize(),
                    contentPadding = androidx.compose.foundation.layout.PaddingValues(start = 16.dp, end = 16.dp, bottom = 96.dp),
                    verticalArrangement = Arrangement.spacedBy(8.dp),
                ) {
                    if (items.isEmpty() && !loading && error == null) {
                        item {
                            Text(
                                stringResource(R.string.no_meetings),
                                color = MaterialTheme.colorScheme.onSurfaceVariant,
                                modifier = Modifier.padding(vertical = 32.dp),
                            )
                        }
                    }
                    if (items.isEmpty() && loading) {
                        item { CircularProgressIndicator(Modifier.padding(32.dp)) }
                    }
                    items(items, key = { it.id }) { MeetingCard(it, onClick = { onOpen(it.id) }) }
                    if (items.size < total) {
                        item {
                            TextButton(onClick = { scope.launch { load(append = true) } }, enabled = !loading,
                                modifier = Modifier.fillMaxWidth()) { Text(stringResource(R.string.load_more)) }
                        }
                    }
                }
            }
        }
    }
}

@Composable
private fun MeetingCard(m: MeetingListItem, onClick: () -> Unit) {
    Card(Modifier.fillMaxWidth().clickable(onClick = onClick)) {
        Column(Modifier.padding(14.dp), verticalArrangement = Arrangement.spacedBy(4.dp)) {
            Text(
                m.title ?: stringResource(R.string.untitled),
                style = MaterialTheme.typography.titleMedium, maxLines = 2, overflow = TextOverflow.Ellipsis,
            )
            Row(verticalAlignment = Alignment.CenterVertically) {
                Text(
                    listOfNotNull(formatDate(m.meeting_date), formatDuration(m.duration_seconds), m.category?.name)
                        .joinToString(" · "),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                    modifier = Modifier.weight(1f),
                )
                Spacer(Modifier.width(8.dp))
                StatusBadge(m.status)
            }
            m.overview?.takeIf { it.isNotBlank() }?.let {
                Text(it, style = MaterialTheme.typography.bodyMedium, maxLines = 3, overflow = TextOverflow.Ellipsis)
            }
            if (m.action_items_total > 0) {
                Text(
                    stringResource(R.string.action_items_progress, m.action_items_done, m.action_items_total),
                    style = MaterialTheme.typography.bodySmall,
                    color = MaterialTheme.colorScheme.onSurfaceVariant,
                )
            }
        }
    }
}

@Composable
fun StatusBadge(status: String) {
    val color = when (status) {
        "completed" -> MaterialTheme.colorScheme.primary
        "failed" -> MaterialTheme.colorScheme.error
        else -> MaterialTheme.colorScheme.tertiary
    }
    Text(statusLabel(status), color = color, style = MaterialTheme.typography.labelMedium)
}
