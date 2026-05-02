package net.millitop.homehub.companion

import android.Manifest
import android.annotation.SuppressLint
import android.content.ActivityNotFoundException
import android.content.Context
import android.content.Intent
import android.content.pm.PackageManager
import android.graphics.Bitmap
import android.net.Uri
import android.os.Bundle
import android.text.InputType
import android.view.Menu
import android.view.MenuItem
import android.webkit.CookieManager
import android.webkit.GeolocationPermissions
import android.webkit.PermissionRequest
import android.webkit.URLUtil
import android.webkit.ValueCallback
import android.webkit.WebChromeClient
import android.webkit.WebResourceError
import android.webkit.WebResourceRequest
import android.webkit.WebSettings
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.Toast
import android.widget.LinearLayout
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.appcompat.app.AlertDialog
import androidx.core.content.ContextCompat
import androidx.swiperefreshlayout.widget.SwipeRefreshLayout
import androidx.work.Constraints
import androidx.work.Data
import androidx.work.ExistingPeriodicWorkPolicy
import androidx.work.ExistingWorkPolicy
import androidx.work.NetworkType
import androidx.work.OneTimeWorkRequestBuilder
import androidx.work.PeriodicWorkRequestBuilder
import androidx.work.WorkManager
import com.google.android.material.appbar.MaterialToolbar
import com.google.android.material.button.MaterialButton
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import org.json.JSONArray
import org.json.JSONObject
import java.util.Locale
import java.util.concurrent.TimeUnit

class MainActivity : AppCompatActivity() {

    private lateinit var webView: WebView
    private lateinit var swipeRefresh: SwipeRefreshLayout
    private lateinit var loadingOverlay: LinearLayout
    private lateinit var offlineOverlay: LinearLayout
    private lateinit var retryButton: MaterialButton

    private var fileChooserCallback: ValueCallback<Array<Uri>>? = null
    private var pendingPermissionRequest: PermissionRequest? = null
    private var pendingGeolocationOrigin: String? = null
    private var pendingGeolocationCallback: GeolocationPermissions.Callback? = null
    private val httpClient = OkHttpClient()

    private val syncPrefs by lazy {
        getSharedPreferences(PhotoAutoSyncConfig.PREFS_NAME, Context.MODE_PRIVATE)
    }

    private val filePickerLauncher = registerForActivityResult(
        ActivityResultContracts.GetContent()
    ) { uri: Uri? ->
        val cb = fileChooserCallback
        fileChooserCallback = null
        if (cb == null) return@registerForActivityResult
        if (uri == null) cb.onReceiveValue(null) else cb.onReceiveValue(arrayOf(uri))
    }

    private val cameraAudioPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        val request = pendingPermissionRequest
        pendingPermissionRequest = null
        if (request == null) return@registerForActivityResult

        val cameraGranted = result[Manifest.permission.CAMERA] == true
        val audioGranted = result[Manifest.permission.RECORD_AUDIO] == true

        val approvedResources = request.resources.filter { resource ->
            when (resource) {
                PermissionRequest.RESOURCE_VIDEO_CAPTURE -> cameraGranted
                PermissionRequest.RESOURCE_AUDIO_CAPTURE -> audioGranted
                else -> false
            }
        }

        if (approvedResources.isNotEmpty()) {
            request.grant(approvedResources.toTypedArray())
        } else {
            request.deny()
        }
    }

    private val locationPermissionLauncher = registerForActivityResult(
        ActivityResultContracts.RequestMultiplePermissions()
    ) { result ->
        val callback = pendingGeolocationCallback
        val origin = pendingGeolocationOrigin
        pendingGeolocationCallback = null
        pendingGeolocationOrigin = null
        if (callback == null || origin.isNullOrBlank()) return@registerForActivityResult

        val fineGranted = result[Manifest.permission.ACCESS_FINE_LOCATION] == true
        val coarseGranted = result[Manifest.permission.ACCESS_COARSE_LOCATION] == true
        callback.invoke(origin, fineGranted || coarseGranted, false)
    }

    private val watchFolderPickerLauncher = registerForActivityResult(
        ActivityResultContracts.OpenDocumentTree()
    ) { uri: Uri? ->
        if (uri == null) return@registerForActivityResult
        val flags = Intent.FLAG_GRANT_READ_URI_PERMISSION or Intent.FLAG_GRANT_PERSISTABLE_URI_PERMISSION
        try {
            contentResolver.takePersistableUriPermission(uri, flags)
        } catch (_: SecurityException) {
        }

        syncPrefs.edit().putString(PhotoAutoSyncConfig.PREF_TREE_URI, uri.toString()).apply()
        Toast.makeText(this, getString(R.string.sync_folder_saved), Toast.LENGTH_SHORT).show()
        enqueuePhotoSync(immediate = true)
        registerWatchFolderOnServer(uri.toString())
    }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        setContentView(R.layout.activity_main)

        val toolbar = findViewById<MaterialToolbar>(R.id.toolbar)
        setSupportActionBar(toolbar)

        webView = findViewById(R.id.webView)
        swipeRefresh = findViewById(R.id.swipeRefresh)
        loadingOverlay = findViewById(R.id.loadingOverlay)
        offlineOverlay = findViewById(R.id.offlineOverlay)
        retryButton = findViewById(R.id.retryButton)

        setupWebView()

        swipeRefresh.setColorSchemeResources(
            com.google.android.material.R.color.material_dynamic_primary50,
            com.google.android.material.R.color.material_dynamic_secondary50,
            com.google.android.material.R.color.material_dynamic_tertiary50,
        )
        swipeRefresh.setDistanceToTriggerSync(140)
        swipeRefresh.setOnChildScrollUpCallback { _, _ -> webView.canScrollVertically(-1) }

        swipeRefresh.setOnRefreshListener {
            webView.reload()
        }

        retryButton.setOnClickListener {
            offlineOverlay.visibility = LinearLayout.GONE
            webView.reload()
        }

        if (savedInstanceState != null) {
            webView.restoreState(savedInstanceState)
        } else {
            webView.loadUrl(normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL))
        }

        enqueuePhotoSync(immediate = false)
    }

    override fun onCreateOptionsMenu(menu: Menu?): Boolean {
        menuInflater.inflate(R.menu.main_menu, menu)
        return true
    }

    override fun onOptionsItemSelected(item: MenuItem): Boolean {
        return when (item.itemId) {
            R.id.action_welcome_home -> {
                navigateToPath("/")
                true
            }
            R.id.action_welcome_weather -> {
                navigateToPath("/")
                scrollToWeatherSection()
                true
            }
            R.id.action_welcome_post_update -> {
                showWelcomePostDialog()
                true
            }
            R.id.action_welcome_switch_user -> {
                showSwitchUserDialog()
                true
            }
            R.id.action_welcome_profile_settings -> {
                navigateToPath("/settings")
                true
            }
            R.id.action_configure_sync -> {
                showSyncConfigDialog()
                true
            }
            R.id.action_pick_watch_folder -> {
                watchFolderPickerLauncher.launch(null)
                true
            }
            R.id.action_sync_now -> {
                if (!isSyncConfigured()) {
                    Toast.makeText(this, getString(R.string.sync_needs_setup), Toast.LENGTH_SHORT).show()
                } else {
                    enqueuePhotoSync(immediate = true)
                    Toast.makeText(this, getString(R.string.sync_started), Toast.LENGTH_SHORT).show()
                }
                true
            }
            else -> super.onOptionsItemSelected(item)
        }
    }

    @SuppressLint("SetJavaScriptEnabled")
    private fun setupWebView() {
        val settings = webView.settings
        settings.javaScriptEnabled = true
        settings.domStorageEnabled = true
        settings.databaseEnabled = true
        settings.javaScriptCanOpenWindowsAutomatically = true
        settings.mediaPlaybackRequiresUserGesture = false
        settings.setGeolocationEnabled(true)
        settings.allowFileAccess = true
        settings.allowContentAccess = true
        settings.loadsImagesAutomatically = true
        settings.cacheMode = WebSettings.LOAD_NO_CACHE
        settings.builtInZoomControls = false
        settings.displayZoomControls = false
        settings.setSupportZoom(false)
        settings.useWideViewPort = true
        settings.loadWithOverviewMode = false
        settings.textZoom = 100
        settings.userAgentString = settings.userAgentString + " HomeHubCompanion/1.0"

        CookieManager.getInstance().setAcceptCookie(true)
        CookieManager.getInstance().setAcceptThirdPartyCookies(webView, true)

        WebView.setWebContentsDebuggingEnabled(false)

        webView.webViewClient = object : WebViewClient() {
            override fun shouldOverrideUrlLoading(view: WebView?, request: WebResourceRequest?): Boolean {
                val uri = request?.url ?: return false
                val url = uri.toString()

                if (!URLUtil.isNetworkUrl(url)) {
                    return true
                }

                val homeHost = Uri.parse(normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL)).host?.lowercase(Locale.ROOT)
                val requestHost = uri.host?.lowercase(Locale.ROOT)

                return if (homeHost != null && requestHost == homeHost) {
                    false
                } else {
                    try {
                        startActivity(Intent(Intent.ACTION_VIEW, uri))
                    } catch (_: ActivityNotFoundException) {
                    }
                    true
                }
            }

            override fun onPageStarted(view: WebView?, url: String?, favicon: Bitmap?) {
                super.onPageStarted(view, url, favicon)
                loadingOverlay.visibility = LinearLayout.VISIBLE
            }

            override fun onPageFinished(view: WebView?, url: String?) {
                super.onPageFinished(view, url)
                view?.evaluateJavascript(
                    """
                    (function(){
                        try {
                            var meta = document.querySelector('meta[name="viewport"]');
                            if (!meta) {
                                meta = document.createElement('meta');
                                meta.setAttribute('name', 'viewport');
                                document.head && document.head.appendChild(meta);
                            }
                            meta.setAttribute('content', 'width=device-width, initial-scale=1, viewport-fit=cover');
                            document.documentElement.classList.add('android-companion-shell');
                            if (document.body) document.body.classList.add('android-companion-shell');
                        } catch (_) {}
                    })();
                    """.trimIndent(),
                    null
                )
                loadingOverlay.visibility = LinearLayout.GONE
                offlineOverlay.visibility = LinearLayout.GONE
                swipeRefresh.isRefreshing = false
                swipeRefresh.isEnabled = !webView.canScrollVertically(-1)
            }

            override fun onReceivedError(
                view: WebView?,
                request: WebResourceRequest?,
                error: WebResourceError?
            ) {
                super.onReceivedError(view, request, error)
                if (request?.isForMainFrame == true) {
                    loadingOverlay.visibility = LinearLayout.GONE
                    offlineOverlay.visibility = LinearLayout.VISIBLE
                    swipeRefresh.isRefreshing = false
                }
            }
        }

        webView.webChromeClient = object : WebChromeClient() {
            override fun onPermissionRequest(request: PermissionRequest?) {
                if (request == null) return

                val requestOriginHost = request.origin?.host?.lowercase(Locale.ROOT)
                val homeHost = Uri.parse(normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL)).host?.lowercase(Locale.ROOT)
                if (requestOriginHost == null || homeHost == null || requestOriginHost != homeHost) {
                    request.deny()
                    return
                }

                val wantsVideo = request.resources.contains(PermissionRequest.RESOURCE_VIDEO_CAPTURE)
                val wantsAudio = request.resources.contains(PermissionRequest.RESOURCE_AUDIO_CAPTURE)

                if (!wantsVideo && !wantsAudio) {
                    request.deny()
                    return
                }

                val neededPermissions = mutableListOf<String>()
                if (wantsVideo && ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.CAMERA) != PackageManager.PERMISSION_GRANTED) {
                    neededPermissions.add(Manifest.permission.CAMERA)
                }
                if (wantsAudio && ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.RECORD_AUDIO) != PackageManager.PERMISSION_GRANTED) {
                    neededPermissions.add(Manifest.permission.RECORD_AUDIO)
                }

                if (neededPermissions.isEmpty()) {
                    request.grant(request.resources)
                } else {
                    pendingPermissionRequest = request
                    cameraAudioPermissionLauncher.launch(neededPermissions.toTypedArray())
                }
            }

            override fun onGeolocationPermissionsShowPrompt(origin: String?, callback: GeolocationPermissions.Callback?) {
                if (origin.isNullOrBlank() || callback == null) {
                    callback?.invoke(origin, false, false)
                    return
                }

                val requestOriginHost = Uri.parse(origin).host?.lowercase(Locale.ROOT)
                val homeHost = Uri.parse(normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL)).host?.lowercase(Locale.ROOT)
                if (requestOriginHost == null || homeHost == null || requestOriginHost != homeHost) {
                    callback.invoke(origin, false, false)
                    return
                }

                val fineGranted = ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.ACCESS_FINE_LOCATION) == PackageManager.PERMISSION_GRANTED
                val coarseGranted = ContextCompat.checkSelfPermission(this@MainActivity, Manifest.permission.ACCESS_COARSE_LOCATION) == PackageManager.PERMISSION_GRANTED

                if (fineGranted || coarseGranted) {
                    callback.invoke(origin, true, false)
                } else {
                    pendingGeolocationOrigin = origin
                    pendingGeolocationCallback = callback
                    locationPermissionLauncher.launch(
                        arrayOf(
                            Manifest.permission.ACCESS_FINE_LOCATION,
                            Manifest.permission.ACCESS_COARSE_LOCATION,
                        )
                    )
                }
            }

            override fun onShowFileChooser(
                webView: WebView?,
                filePathCallback: ValueCallback<Array<Uri>>?,
                fileChooserParams: FileChooserParams?
            ): Boolean {
                if (filePathCallback == null) return false
                fileChooserCallback?.onReceiveValue(null)
                fileChooserCallback = filePathCallback
                val mimeType = fileChooserParams?.acceptTypes
                    ?.firstOrNull { !it.isNullOrBlank() }
                    ?.takeIf { it != "*/*" }
                    ?: "*/*"
                try {
                    filePickerLauncher.launch(mimeType)
                } catch (_: Exception) {
                    fileChooserCallback?.onReceiveValue(null)
                    fileChooserCallback = null
                    Toast.makeText(this@MainActivity, "No file picker available", Toast.LENGTH_SHORT).show()
                    return false
                }
                return true
            }
        }

        webView.setOnScrollChangeListener { _, _, scrollY, _, _ ->
            if (!swipeRefresh.isRefreshing) {
                swipeRefresh.isEnabled = scrollY <= 0
            }
        }
    }

    private fun normalizeHomeHubUrl(raw: String): String {
        val trimmed = raw.trim()
        if (trimmed.startsWith("https://") || trimmed.startsWith("http://")) return trimmed
        return "https://$trimmed"
    }

    private fun baseHomeHubUrl(): String = normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL).trimEnd('/')

    private fun navigateToPath(path: String) {
        val url = "${baseHomeHubUrl()}$path"
        webView.loadUrl(url)
    }

    private fun scrollToWeatherSection() {
        val script = """
            (function(){
              try {
                var node = document.querySelector('.weather-widget') || document.getElementById('weather-card') || document.getElementById('weather-loading');
                if (node && typeof node.scrollIntoView === 'function') {
                  node.scrollIntoView({ behavior: 'smooth', block: 'start' });
                }
              } catch (_) {}
            })();
        """.trimIndent()
        webView.postDelayed({ webView.evaluateJavascript(script, null) }, 350)
    }

    private fun showWelcomePostDialog() {
        val input = EditText(this).apply {
            minLines = 3
            maxLines = 5
            inputType = InputType.TYPE_CLASS_TEXT or InputType.TYPE_TEXT_FLAG_CAP_SENTENCES or InputType.TYPE_TEXT_FLAG_MULTI_LINE
            hint = getString(R.string.welcome_post_hint)
        }

        AlertDialog.Builder(this)
            .setTitle(getString(R.string.welcome_post_title))
            .setView(input)
            .setNegativeButton(getString(R.string.welcome_post_cancel), null)
            .setPositiveButton(getString(R.string.welcome_post_save)) { _, _ ->
                val summary = input.text?.toString().orEmpty().trim()
                if (summary.isNotBlank()) {
                    postWelcomeTimelineUpdate(summary)
                }
            }
            .show()
    }

    private fun postWelcomeTimelineUpdate(summary: String) {
        Thread {
            try {
                val actor = fetchCurrentMemberName().orEmpty()
                if (actor.isBlank()) {
                    runOnUiThread {
                        Toast.makeText(this, getString(R.string.welcome_update_failed), Toast.LENGTH_SHORT).show()
                    }
                    return@Thread
                }

                val payload = JSONObject()
                    .put("actor", actor)
                    .put("summary", summary)
                    .put("action", "timeline.manual")

                val req = authRequestBuilder("${baseHomeHubUrl()}/api/timeline")
                    .post(payload.toString().toRequestBody("application/json; charset=utf-8".toMediaType()))
                    .build()

                val success = httpClient.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@use false
                    val body = resp.body?.string().orEmpty()
                    try {
                        JSONObject(body).optBoolean("ok", false)
                    } catch (_: Exception) {
                        false
                    }
                }

                runOnUiThread {
                    if (success) {
                        Toast.makeText(this, getString(R.string.welcome_update_posted), Toast.LENGTH_SHORT).show()
                        navigateToPath("/")
                    } else {
                        Toast.makeText(this, getString(R.string.welcome_update_failed), Toast.LENGTH_SHORT).show()
                    }
                }
            } catch (_: Exception) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.welcome_update_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun showSwitchUserDialog() {
        Thread {
            try {
                val req = authRequestBuilder("${baseHomeHubUrl()}/api/family-members")
                    .get()
                    .build()
                val payload = httpClient.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@use null
                    resp.body?.string()
                }

                if (payload.isNullOrBlank()) {
                    runOnUiThread {
                        Toast.makeText(this, getString(R.string.welcome_switch_failed), Toast.LENGTH_SHORT).show()
                    }
                    return@Thread
                }

                val root = JSONObject(payload)
                val arr = root.optJSONArray("members") ?: JSONArray()
                if (arr.length() == 0) {
                    runOnUiThread {
                        Toast.makeText(this, getString(R.string.welcome_no_members), Toast.LENGTH_SHORT).show()
                    }
                    return@Thread
                }

                val names = ArrayList<String>()
                val memberHasPin = HashMap<String, Boolean>()
                for (i in 0 until arr.length()) {
                    val row = arr.optJSONObject(i) ?: continue
                    val name = row.optString("name").trim()
                    if (name.isBlank()) continue
                    names.add(name)
                    memberHasPin[name] = row.optBoolean("pin", false)
                }

                if (names.isEmpty()) {
                    runOnUiThread {
                        Toast.makeText(this, getString(R.string.welcome_no_members), Toast.LENGTH_SHORT).show()
                    }
                    return@Thread
                }

                runOnUiThread {
                    AlertDialog.Builder(this)
                        .setTitle(getString(R.string.welcome_choose_user))
                        .setItems(names.toTypedArray()) { _, which ->
                            val selected = names[which]
                            val hasPin = memberHasPin[selected] == true
                            if (hasPin) {
                                requestPinBasedSwitch(selected)
                            } else {
                                switchUserWithoutPin(selected)
                            }
                        }
                        .show()
                }
            } catch (_: Exception) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.welcome_switch_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun requestPinBasedSwitch(memberName: String) {
        val script = """
            (function(){
              try {
                if (typeof window.showMemberPINModal === 'function') {
                  window.showMemberPINModal(${JSONObject.quote(memberName)});
                }
              } catch (_) {}
            })();
        """.trimIndent()
        webView.evaluateJavascript(script, null)
    }

    private fun switchUserWithoutPin(memberName: String) {
        Thread {
            try {
                val payload = JSONObject()
                    .put("name", memberName)
                    .put("pin", "")
                val req = authRequestBuilder("${baseHomeHubUrl()}/api/member-login")
                    .post(payload.toString().toRequestBody("application/json; charset=utf-8".toMediaType()))
                    .build()

                val ok = httpClient.newCall(req).execute().use { resp ->
                    if (!resp.isSuccessful) return@use false
                    val body = resp.body?.string().orEmpty()
                    try {
                        JSONObject(body).optBoolean("ok", false)
                    } catch (_: Exception) {
                        false
                    }
                }

                runOnUiThread {
                    if (!ok) {
                        Toast.makeText(this, getString(R.string.welcome_switch_failed), Toast.LENGTH_SHORT).show()
                        return@runOnUiThread
                    }
                    val script = """
                        (function(){
                          try {
                            if (typeof window.startMemberWelcomeFlow === 'function') {
                              window.startMemberWelcomeFlow(${JSONObject.quote(memberName)});
                            } else {
                              localStorage.setItem('username', ${JSONObject.quote(memberName)});
                              location.reload();
                            }
                          } catch (_) {
                            location.reload();
                          }
                        })();
                    """.trimIndent()
                    webView.evaluateJavascript(script, null)
                }
            } catch (_: Exception) {
                runOnUiThread {
                    Toast.makeText(this, getString(R.string.welcome_switch_failed), Toast.LENGTH_SHORT).show()
                }
            }
        }.start()
    }

    private fun authRequestBuilder(url: String): Request.Builder {
        val builder = Request.Builder().url(url)
        val cookie = CookieManager.getInstance().getCookie(baseHomeHubUrl()).orEmpty()
        if (cookie.isNotBlank()) {
            builder.header("Cookie", cookie)
        }
        return builder
    }

    private fun fetchCurrentMemberName(): String? {
        val req = authRequestBuilder("${baseHomeHubUrl()}/api/current-member")
            .get()
            .build()
        return try {
            httpClient.newCall(req).execute().use { resp ->
                if (!resp.isSuccessful) return@use null
                val body = resp.body?.string().orEmpty()
                val json = JSONObject(body)
                val member = json.optString("member").trim()
                if (member.isBlank()) null else member
            }
        } catch (_: Exception) {
            null
        }
    }

    private fun normalizeVisibility(raw: String): String {
        return if (raw.trim().lowercase(Locale.ROOT) == "family") "family" else "private"
    }

    private fun isSyncConfigured(): Boolean {
        val member = syncPrefs.getString(PhotoAutoSyncConfig.PREF_MEMBER, "").orEmpty().trim()
        val folder = syncPrefs.getString(PhotoAutoSyncConfig.PREF_TREE_URI, "").orEmpty().trim()
        return member.isNotBlank() && folder.isNotBlank()
    }

    private fun showSyncConfigDialog() {
        val memberInput = EditText(this).apply {
            hint = "Family member name"
            setText(syncPrefs.getString(PhotoAutoSyncConfig.PREF_MEMBER, ""))
        }
        val visibilityInput = EditText(this).apply {
            hint = "Visibility (private/family)"
            setText(syncPrefs.getString(PhotoAutoSyncConfig.PREF_VISIBILITY, "private"))
        }

        val container = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            val margin = (16 * resources.displayMetrics.density).toInt()
            setPadding(margin, margin / 2, margin, 0)
            addView(memberInput, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT))
            addView(visibilityInput, FrameLayout.LayoutParams(FrameLayout.LayoutParams.MATCH_PARENT, FrameLayout.LayoutParams.WRAP_CONTENT))
        }

        AlertDialog.Builder(this)
            .setTitle("Configure Photo Sync")
            .setView(container)
            .setNegativeButton("Cancel", null)
            .setPositiveButton("Save") { _, _ ->
                val member = memberInput.text?.toString().orEmpty().trim()
                val visibility = normalizeVisibility(visibilityInput.text?.toString().orEmpty())
                if (member.isBlank()) {
                    Toast.makeText(this, "Member name is required", Toast.LENGTH_SHORT).show()
                    return@setPositiveButton
                }
                syncPrefs.edit()
                    .putString(PhotoAutoSyncConfig.PREF_MEMBER, member)
                    .putString(PhotoAutoSyncConfig.PREF_VISIBILITY, visibility)
                    .apply()
                Toast.makeText(this, getString(R.string.sync_saved), Toast.LENGTH_SHORT).show()
                enqueuePhotoSync(immediate = false)
            }
            .show()
    }

    private fun buildWorkerData(): Data {
        val homeUrl = normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL)
        val treeUri = syncPrefs.getString(PhotoAutoSyncConfig.PREF_TREE_URI, "").orEmpty()
        val member = syncPrefs.getString(PhotoAutoSyncConfig.PREF_MEMBER, "").orEmpty()
        val visibility = normalizeVisibility(syncPrefs.getString(PhotoAutoSyncConfig.PREF_VISIBILITY, "private").orEmpty())
        val cookie = CookieManager.getInstance().getCookie(homeUrl).orEmpty()

        return Data.Builder()
            .putString(PhotoAutoSyncConfig.KEY_HOME_URL, homeUrl)
            .putString(PhotoAutoSyncConfig.KEY_TREE_URI, treeUri)
            .putString(PhotoAutoSyncConfig.KEY_MEMBER, member)
            .putString(PhotoAutoSyncConfig.KEY_VISIBILITY, visibility)
            .putString(PhotoAutoSyncConfig.KEY_COOKIE, cookie)
            .build()
    }

    private fun enqueuePhotoSync(immediate: Boolean) {
        val data = buildWorkerData()
        val constraints = Constraints.Builder()
            .setRequiredNetworkType(NetworkType.CONNECTED)
            .build()
        val workManager = WorkManager.getInstance(this)

        val periodic = PeriodicWorkRequestBuilder<PhotoAutoSyncWorker>(15, TimeUnit.MINUTES)
            .setInputData(data)
            .setConstraints(constraints)
            .build()

        workManager.enqueueUniquePeriodicWork(
            PhotoAutoSyncConfig.PERIODIC_WORK_NAME,
            ExistingPeriodicWorkPolicy.UPDATE,
            periodic
        )

        if (immediate) {
            val oneShot = OneTimeWorkRequestBuilder<PhotoAutoSyncWorker>()
                .setInputData(data)
                .setConstraints(constraints)
                .build()
            workManager.enqueueUniqueWork(
                PhotoAutoSyncConfig.IMMEDIATE_WORK_NAME,
                ExistingWorkPolicy.REPLACE,
                oneShot
            )
        }
    }

    private fun registerWatchFolderOnServer(folderUri: String) {
        val homeUrl = normalizeHomeHubUrl(BuildConfig.HOMEHUB_URL).trimEnd('/')
        val member = syncPrefs.getString(PhotoAutoSyncConfig.PREF_MEMBER, "").orEmpty().trim()
        if (member.isBlank()) return

        val visibility = normalizeVisibility(syncPrefs.getString(PhotoAutoSyncConfig.PREF_VISIBILITY, "private").orEmpty())
        val payload = JSONObject()
            .put("member", member)
            .put("folder_uri", folderUri)
            .put("scope", visibility)
            .put("platform", "android")

        val reqBuilder = Request.Builder()
            .url("$homeUrl/api/photos/watch-folders")
            .post(payload.toString().toRequestBody("application/json; charset=utf-8".toMediaType()))

        val cookie = CookieManager.getInstance().getCookie(homeUrl).orEmpty()
        if (cookie.isNotBlank()) {
            reqBuilder.header("Cookie", cookie)
        }

        Thread {
            try {
                httpClient.newCall(reqBuilder.build()).execute().close()
            } catch (_: Exception) {
            }
        }.start()
    }

    override fun onSaveInstanceState(outState: Bundle) {
        webView.saveState(outState)
        super.onSaveInstanceState(outState)
    }

    override fun onBackPressed() {
        if (webView.canGoBack()) {
            webView.goBack()
        } else {
            super.onBackPressed()
        }
    }

    override fun onPause() {
        super.onPause()
        webView.onPause()
    }

    override fun onResume() {
        super.onResume()
        webView.onResume()
    }

    override fun onDestroy() {
        super.onDestroy()
        fileChooserCallback?.onReceiveValue(null)
        fileChooserCallback = null
        pendingPermissionRequest?.deny()
        pendingPermissionRequest = null
    }
}
