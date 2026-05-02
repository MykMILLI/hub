package net.millitop.homehub.companion

import android.content.Context
import android.net.Uri
import androidx.documentfile.provider.DocumentFile
import androidx.work.CoroutineWorker
import androidx.work.WorkerParameters
import okhttp3.MediaType.Companion.toMediaTypeOrNull
import okhttp3.MultipartBody
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody
import okio.BufferedSink
import okio.source
import java.util.ArrayDeque
import java.util.Locale

class PhotoAutoSyncWorker(
    appContext: Context,
    params: WorkerParameters,
) : CoroutineWorker(appContext, params) {

    private val client = OkHttpClient()

    override suspend fun doWork(): Result {
        val homeUrl = inputData.getString(PhotoAutoSyncConfig.KEY_HOME_URL).orEmpty().trim().trimEnd('/')
        val treeUriRaw = inputData.getString(PhotoAutoSyncConfig.KEY_TREE_URI).orEmpty().trim()
        val member = inputData.getString(PhotoAutoSyncConfig.KEY_MEMBER).orEmpty().trim()
        val visibility = inputData.getString(PhotoAutoSyncConfig.KEY_VISIBILITY).orEmpty().trim().lowercase(Locale.ROOT)
        val cookie = inputData.getString(PhotoAutoSyncConfig.KEY_COOKIE).orEmpty().trim()

        if (homeUrl.isBlank() || treeUriRaw.isBlank() || member.isBlank()) return Result.success()

        val treeUri = Uri.parse(treeUriRaw)
        val root = DocumentFile.fromTreeUri(applicationContext, treeUri) ?: return Result.retry()
        val prefs = applicationContext.getSharedPreferences(PhotoAutoSyncConfig.PREFS_NAME, Context.MODE_PRIVATE)
        val lastSyncMs = prefs.getLong(PhotoAutoSyncConfig.PREF_LAST_SYNC_MS, 0L)

        val candidates = collectChangedMedia(root, lastSyncMs)
        if (candidates.isEmpty()) return Result.success()

        var maxSeen = lastSyncMs
        var hadError = false

        candidates.forEach { file ->
            val modified = file.lastModified()
            if (modified > maxSeen) maxSeen = modified
            val uploaded = uploadOne(
                homeUrl = homeUrl,
                cookie = cookie,
                member = member,
                visibility = if (visibility == "family") "family" else "private",
                file = file
            )
            if (!uploaded) hadError = true
        }

        if (maxSeen > lastSyncMs) {
            prefs.edit().putLong(PhotoAutoSyncConfig.PREF_LAST_SYNC_MS, maxSeen).apply()
        }

        return if (hadError) Result.retry() else Result.success()
    }

    private fun collectChangedMedia(root: DocumentFile, sinceMs: Long): List<DocumentFile> {
        val queue: ArrayDeque<DocumentFile> = ArrayDeque()
        val found = mutableListOf<DocumentFile>()
        queue.add(root)

        while (queue.isNotEmpty()) {
            val current = queue.removeFirst()
            current.listFiles().forEach { child ->
                if (child.isDirectory) {
                    queue.add(child)
                } else if (child.isFile && isMediaFile(child.name) && child.lastModified() >= sinceMs) {
                    found.add(child)
                }
            }
        }

        return found
            .sortedBy { it.lastModified() }
            .takeLast(24)
    }

    private fun isMediaFile(name: String?): Boolean {
        if (name.isNullOrBlank()) return false
        val lower = name.lowercase(Locale.ROOT)
        return lower.endsWith(".jpg") ||
            lower.endsWith(".jpeg") ||
            lower.endsWith(".png") ||
            lower.endsWith(".webp") ||
            lower.endsWith(".heic") ||
            lower.endsWith(".mp4") ||
            lower.endsWith(".mov") ||
            lower.endsWith(".mkv")
    }

    private fun uploadOne(
        homeUrl: String,
        cookie: String,
        member: String,
        visibility: String,
        file: DocumentFile,
    ): Boolean {
        val fileName = file.name ?: "upload.bin"
        val contentType = applicationContext.contentResolver.getType(file.uri)?.toMediaTypeOrNull()
            ?: "application/octet-stream".toMediaTypeOrNull()

        val body = MultipartBody.Builder()
            .setType(MultipartBody.FORM)
            .addFormDataPart("creator", member)
            .addFormDataPart("visibility", visibility)
            .addFormDataPart(
                "files",
                fileName,
                ContentUriRequestBody(applicationContext, file.uri, contentType)
            )
            .build()

        val reqBuilder = Request.Builder()
            .url("$homeUrl/api/photos/upload")
            .post(body)

        if (cookie.isNotBlank()) {
            reqBuilder.header("Cookie", cookie)
        }

        return try {
            client.newCall(reqBuilder.build()).execute().use { resp ->
                resp.isSuccessful
            }
        } catch (_: Exception) {
            false
        }
    }

    private class ContentUriRequestBody(
        private val context: Context,
        private val uri: Uri,
        private val mediaType: okhttp3.MediaType?,
    ) : RequestBody() {
        override fun contentType() = mediaType

        override fun contentLength(): Long {
            return try {
                context.contentResolver.openAssetFileDescriptor(uri, "r")?.use { it.length } ?: -1L
            } catch (_: Exception) {
                -1L
            }
        }

        override fun writeTo(sink: BufferedSink) {
            context.contentResolver.openInputStream(uri)?.use { input ->
                sink.writeAll(input.source())
            } ?: throw IllegalStateException("Unable to open uri stream")
        }
    }
}
