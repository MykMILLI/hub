# Home Hub Shared Cloud Photo/Video Sync Technical Handoff

## 1. Tech stack

- Frontend framework:
  - Web: Flask server-rendered Jinja templates with vanilla JavaScript in templates/upload.html.
  - CSS: Tailwind build pipeline defined in package.json and bundled output in static assets.
  - Mobile: Flutter app in flutter/lib/main.dart, dependencies in flutter/pubspec.yaml.

- Backend framework:
  - Flask app factory in app/__init__.py.
  - Modular routes via Blueprint in app/blueprints/__init__.py, Shared Cloud APIs in app/blueprints/uploads.py and app/blueprints/photos.py.

- Database:
  - SQLAlchemy ORM models in app/models.py.
  - SQLite database file configured at data/app.db in app/__init__.py.

- File storage method:
  - Local disk storage on server container/host.
  - Generic shared files: uploads folder in app/blueprints/uploads.py.
  - Photos/videos: uploads/photos in app/blueprints/photos.py.

- OS/runtime:
  - Python 3.12 Alpine container runtime in Dockerfile.
  - Gunicorn process starts Flask app in Dockerfile.
  - Local dev entry points: run.py and wsgi.py.

- Queue, worker, cron, websocket, background jobs:
  - No dedicated queue system (no Celery/RQ) for Shared Cloud sync.
  - No websocket implementation for Shared Cloud.
  - SSE endpoint exists at /api/stream/events in app/blueprints/notifications.py, but it streams timeline/notification state only.
  - Flutter Workmanager exists for notification polling in flutter/lib/main.dart, not photo folder sync.
  - There is an ad-hoc thread worker in media downloader code in app/blueprints/media_pdfs.py, unrelated to Shared Cloud photo sync.

## 2. Current Shared Cloud feature

- Current pages/components related to Shared Cloud:
  - Main web page route: /upload in app/blueprints/uploads.py.
  - Shared Cloud web template: templates/upload.html.
  - Mobile section registration: Shared Cloud page in flutter/lib/main.dart.
  - Mobile UI implementation: SharedCloudPage in flutter/lib/main.dart.

- Current Photos and Videos UI elements:
  - Web:
    - Upload modal buttons and form: templates/upload.html.
    - Album modal and create form: templates/upload.html.
    - Library/Albums/Watched sub-navigation: templates/upload.html.
    - Photo grid and album list render targets: templates/upload.html.
    - Watched folder modal and controls: templates/upload.html.
    - Sync success modal text: templates/upload.html.
  - Mobile:
    - Files / Family Photos / Watched Folders tabs: flutter/lib/main.dart.
    - Upload photos, create album, watch-folder CRUD actions: flutter/lib/main.dart.

- What watched folders currently do in code:
  - Persist records (member, folder_uri, scope, target_album_id, active flag, platform) via APIs in app/blueprints/photos.py.
  - Web folder picker captures a folder label and sends folder_uri to backend, then updates UI progress/notices in templates/upload.html.
  - Mobile can pick directory path and save folder metadata via API in flutter/lib/main.dart.

- What synced status currently means in code:
  - Web: progress/status is UI-calculated from active watched-folder count, not from file transfer success; see templates/upload.html.
  - Web success modal is shown immediately after watch-folder POST and reload in templates/upload.html.
  - last_synced_at is only read/displayed, not updated by sync logic in current feature paths; display in templates/upload.html, model field in app/models.py.

- Whether any real sync action occurs:
  - For watched folders: NOT IMPLEMENTED.
  - For manual photo upload: WORKING as direct upload to server via /api/photos/upload in app/blueprints/photos.py.

## 3. Watched folder implementation

- Exact files/modules/classes handling watched folders:
  - Model: PhotoWatchFolder in app/models.py.
  - Backend API handlers: app/blueprints/photos.py routes:
    - GET /api/photos/watch-folders
    - POST /api/photos/watch-folders
    - PATCH /api/photos/watch-folders/<watch_id>
    - DELETE /api/photos/watch-folders/<watch_id>
  - Web watched-folder UI logic: templates/upload.html script block.
  - Mobile watched-folder UI and API calls: flutter/lib/main.dart.
  - API client methods in HomeHubApi:
    - fetchPhotoWatchFolders
    - createPhotoWatchFolder
    - updatePhotoWatchFolder
    - deletePhotoWatchFolder

- Whether filesystem watching is implemented:
  - NOT IMPLEMENTED in production Shared Cloud code.

- Whether polling is used:
  - Web polling: setInterval loadAll every 10 seconds in templates/upload.html.
  - Mobile polling: Timer.periodic every 10 seconds for SharedCloudPage in flutter/lib/main.dart.

- What folders can be watched:
  - Any folder path/URI string the user submits.
  - Web stores synthetic folder URI from browser picker, format folder://<root> in templates/upload.html.
  - Mobile stores selected local directory path text in flutter/lib/main.dart.

- How a new file event is detected:
  - NOT IMPLEMENTED for ongoing watched-folder monitoring.
  - Web only detects the one-time selection event from file input change in templates/upload.html.

- What happens after detection:
  - For folder selection event: UI progress text + notice updates, then watch-folder record POST.
  - No automatic file ingestion pipeline is started from watched folder.

## 4. Actual media pipeline

For watched-folder driven sync of a newly added photo/video:

- detection: NOT IMPLEMENTED
- queue/job creation: NOT IMPLEMENTED
- metadata extraction: NOT IMPLEMENTED
- hashing/fingerprinting: NOT IMPLEMENTED
- duplicate check: NOT IMPLEMENTED
- copy/upload/move action: NOT IMPLEMENTED
- database write: PARTIAL (watch-folder record is written, media item is not written)
- thumbnail generation: NOT IMPLEMENTED
- album assignment: PARTIAL (target_album_id saved on watch folder, no media assignment occurs)
- sync to another device or shared area: NOT IMPLEMENTED
- final UI update: UI ONLY

Manual upload pipeline that does exist:

- detection: WORKING (user file selection in upload form)
- queue/job creation: NOT IMPLEMENTED
- metadata extraction: PARTIAL (mime_type and content_length only)
- hashing/fingerprinting: NOT IMPLEMENTED
- duplicate check: NOT IMPLEMENTED
- copy/upload action: WORKING via file.save in app/blueprints/photos.py
- database write: WORKING via PhotoAsset insert in app/blueprints/photos.py
- thumbnail generation: NOT IMPLEMENTED (preview uses original file endpoint)
- album assignment: WORKING if album_id supplied
- sync to another device: PARTIAL (other devices can fetch same central records; no device-to-device replication engine)
- final UI update: WORKING through client refresh/polling and local reload calls

## 5. Database/storage model

Relevant entities and fields for sync domain:

- users:
  - FamilyMember table in app/models.py
  - Fields: id, name, role, pin, email, birthday, favorite_color, favorite_song, amazon_wishlist_url, status, status_updated_at, avatar, added_at

- devices:
  - No dedicated device inventory table for Shared Cloud sync.
  - Closest table is MobilePushToken in app/models.py
  - Fields: id, member_name, device_token, platform, enabled, created_at, updated_at

- watched folders:
  - PhotoWatchFolder in app/models.py
  - Fields: id, member_name, folder_uri, scope, target_album_id, is_active, last_synced_at, platform, created_at, updated_at

- media items:
  - PhotoAsset in app/models.py
  - Fields: id, album_id, filename, original_filename, uploader, visibility, shared_with_json, mime_type, file_size, is_video, captured_at, uploaded_at
  - Generic File in app/models.py for non-photos Shared Cloud items

- albums:
  - PhotoAlbum in app/models.py
  - Fields: id, name, description, creator, visibility, shared_with_json, cover_photo_id, created_at, updated_at

- sync jobs:
  - NOT IMPLEMENTED (no sync job table)

- sync events/logs:
  - NOT IMPLEMENTED for media sync.
  - NotificationDeliveryLog in app/models.py exists for notification channels, not media sync pipeline logs.

- duplicate tracking:
  - NOT IMPLEMENTED (no hash/fingerprint or dedupe table)

- shared library/master library records:
  - NOT IMPLEMENTED as separate master library entity.
  - Effective shared library is central PhotoAsset table filtered by visibility/permissions.

Storage layout:

- data/app.db mounted by compose in compose.yml.
- uploads root in app/blueprints/uploads.py.
- uploads/photos in app/blueprints/photos.py.
- media and pdfs mounted in compose.yml.

## 6. Current sync logic

Status labels use: WORKING, PARTIAL, NOT IMPLEMENTED, UI ONLY.

- upload-only devices: PARTIAL
- send-only behavior: NOT IMPLEMENTED
- receive-only behavior: NOT IMPLEMENTED
- two-way sync: NOT IMPLEMENTED
- one-way sync: PARTIAL
- merged shared album: PARTIAL
- master library aggregation: PARTIAL
- deduplication by hash: NOT IMPLEMENTED
- deduplication by filename only: NOT IMPLEMENTED
- conflict handling: NOT IMPLEMENTED
- delete propagation: PARTIAL
- rename handling: NOT IMPLEMENTED
- retry logic: PARTIAL
- offline device catch-up: PARTIAL
- UI sync status only: UI ONLY

## 7. Known bugs / current failures

Every known reason it may show synced but not actually sync:

- UI state only:
  - Web progress and synced text are set from active watched-folder records and UI events, not transfer completion.

- no watcher events:
  - No filesystem observer in Shared Cloud backend/mobile production paths.

- no worker running:
  - No background sync worker or queue for photo watch folders.

- queue not processed:
  - No queue exists for this feature.

- file paths wrong:
  - Web watcher uses synthetic folder URI like folder://root from browser picker, not server-accessible filesystem path.

- no DB persistence:
  - Watch-folder metadata persistence exists.
  - Media ingestion persistence from watched-folder events does not exist.

- duplicate logic blocking everything:
  - No duplicate logic exists at all in watch-folder flow.

- API not wired to backend:
  - Watch-folder CRUD is wired.
  - Real sync/injest API from watch folders is missing.

- storage path mismatch:
  - Browser-selected client folders are not mount paths inside backend container, so backend cannot read them even if watcher existed.

- frontend never calls backend:
  - Frontend does call backend for watch-folder registration.
  - Frontend never sends selected folder files to /api/photos/upload during watch-folder save flow.

- backend logs errors silently:
  - Multiple broad exception swallowing patterns exist, including file delete try/except pass and SSE loop exception swallow.

## 8. Code map

- app/blueprints/uploads.py
  - Purpose: generic Shared Cloud file APIs and page route
  - Routes:
    - /upload GET/POST
    - /api/files GET
    - /api/files/upload POST
    - /api/files/<file_id> DELETE
    - /api/files/move/<file_id> POST

- app/blueprints/photos.py
  - Purpose: photos/videos albums/assets/watch-folder APIs
  - Routes:
    - /photos GET, /photos/albums/<album_id> GET, /photos/slideshow GET
    - /uploads/photos/<filename> GET
    - /uploads/photos/preview/<filename> GET
    - /api/photos/albums GET/POST
    - /api/photos/albums/<album_id> GET
    - /api/photos/albums/<album_id>/cover POST
    - /api/photos/assets GET
    - /api/photos/assets/batch POST
    - /api/photos/upload POST
    - /api/photos/watch-folders GET/POST
    - /api/photos/watch-folders/<watch_id> PATCH/DELETE

- app/models.py
  - Purpose: persistence models
  - Relevant models:
    - File
    - PhotoAlbum
    - PhotoAsset
    - PhotoWatchFolder
    - FamilyMember
    - MobilePushToken

- templates/upload.html
  - Purpose: complete Shared Cloud web UI (files + integrated photos/videos + watched-folder panels)
  - Handlers:
    - file category filter
    - move file modal
    - create album submit
    - upload photos submit
    - watched folder submit
    - watch list toggle/delete
    - loadAll polling every 10 seconds

- flutter/lib/main.dart
  - Purpose: API client methods and SharedCloudPage UI/state
  - Relevant methods:
    - fetchSharedCloudFiles, uploadSharedCloudFiles, deleteSharedCloudFile, moveSharedCloudFile
    - fetchPhotoAlbums, fetchPhotoAssets, createPhotoAlbum, uploadPhotoAssets, deletePhotoAsset
    - fetchPhotoWatchFolders, createPhotoWatchFolder, updatePhotoWatchFolder, deletePhotoWatchFolder
    - SharedCloudPage state methods: _load, _addWatchFolder, _toggleWatchFolder, _deleteWatchFolder

- app/blueprints/notifications.py
  - Purpose: SSE stream and notification APIs
  - Route:
    - /api/stream/events GET

- flutter/lib/main.dart
  - Purpose: SSE client parser and event consumption via _sseConnect and page-specific _startSse handlers.

## 9. Logs and diagnostics

Observed from current environment context:

- Deployment:
  - scripts/deploy-225.ps1 exited 0.

- Shared Cloud page smoke test:
  - Python test client GET /upload exited 0 and returned 200 status.

- Mobile runtime:
  - flutter run --release exited 0.

Relevant runtime messages in code:

- Web watch setup failure and success notifications:
  - Sync setup failed / Watched folder saved / Sync ready modal text in templates/upload.html.

- Mobile watch setup failure messages:
  - Save watch folder failed, Update watch folder failed, Delete watch folder failed in flutter/lib/main.dart.

Sample payloads used by current code:

- POST /api/photos/watch-folders
  - {
      "member": "<member>",
      "folder_uri": "folder://<selected-root>" or local path,
      "scope": "private|family",
      "platform": "web|android|ios|mobile",
      "target_album_id": <int|null>
    }

- POST /api/photos/upload (multipart)
  - files[]
  - creator
  - visibility
  - album_id optional

Sample DB record shape for watched folder:

- id: 17
- member_name: Alex
- folder_uri: folder://FamilyCameraRoll
- scope: family
- target_album_id: 3
- is_active: true
- last_synced_at: null
- platform: web
- created_at: 2026-05-02T11:44:00Z
- updated_at: 2026-05-02T11:44:00Z

Sample DB record shape for media item:

- id: 812
- album_id: 3
- filename: 20260502114432999999_ab12cd34_IMG_1457.JPG
- original_filename: IMG_1457.JPG
- uploader: Alex
- visibility: family
- shared_with_json: []
- mime_type: image/jpeg
- file_size: 0
- is_video: false
- captured_at: null
- uploaded_at: 2026-05-02T11:44:33Z

Current watcher/queue/worker state for media sync:

- watcher: NOT IMPLEMENTED
- queue: NOT IMPLEMENTED
- worker: NOT IMPLEMENTED for Shared Cloud photo sync

## 10. Gap analysis

Compared to intended Syncthing-like behavior:

- What already exists:
  - Central server upload and storage.
  - Albums and visibility controls.
  - Watched-folder metadata CRUD.
  - Cross-client refresh via polling/SSE-triggered reload.

- What is fake/UI-only:
  - Watched-folder sync progress percentages and synced language.
  - Sync success modal semantics.
  - Active watched-folder count interpreted as active sync status.

- Missing backend pieces:
  - Real folder watcher tied to PhotoWatchFolder records.
  - Sync job model and processing engine.
  - Hash/fingerprint dedupe layer.
  - Conflict handling logic.
  - Master library aggregation model.
  - Durable sync event logs.
  - Real updates to last_synced_at based on successful ingest.

- Minimal working version first fixes:
  - Implement actual ingestion from watched source to PhotoAsset write path.
  - Add hash-based dedupe before insert.
  - Add persistent sync jobs/events and execution loop.
  - Drive UI synced status from backend job state.

## 11. Recommendation

REPLACE CURRENT SYNC ENGINE

Why:

The current watched-folder feature is metadata CRUD with UI progress simulation, not a real sync engine. There is no watcher, no sync queue, no deduplication, no conflict resolver, and no transfer/reconciliation pipeline. To reproduce Syncthing-like send/receive behavior with merged deduplicated family libraries, the sync foundation must be implemented as a true engine rather than extending the current UI-only sync flow.