from . import db
from datetime import datetime

class Note(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), default='')
    content = db.Column(db.Text, nullable=False)
    creator = db.Column(db.String(64), nullable=False)
    updated_by = db.Column(db.String(64))
    note_type = db.Column(db.String(16), default='note')  # note|task|wiki
    visibility = db.Column(db.String(16), default='family')  # family|restricted|private
    allowed_members_json = db.Column(db.Text, default='[]')
    require_pin = db.Column(db.Boolean, default=False)
    is_task = db.Column(db.Boolean, default=False)
    kanban_column = db.Column(db.String(24), default='todo')  # todo|in_progress|review|done
    kanban_order = db.Column(db.Integer, default=0)
    kanban_priority = db.Column(db.String(16), default='normal')  # low|normal|high|urgent
    due_date = db.Column(db.Date)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkspaceTemplate(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    template_type = db.Column(db.String(16), nullable=False)  # project|space
    description = db.Column(db.String(512), default='')
    config_json = db.Column(db.Text, default='{}')
    created_by = db.Column(db.String(64), nullable=False)
    is_public = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class WorkspaceProject(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    project_key = db.Column(db.String(16), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, default='')
    visibility = db.Column(db.String(16), default='family')  # family|restricted|private
    allowed_members_json = db.Column(db.Text, default='[]')
    created_by = db.Column(db.String(64), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('workspace_template.id'))
    linked_space_id = db.Column(db.Integer, db.ForeignKey('workspace_space.id'))
    share_token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class ProjectColumn(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('workspace_project.id'), nullable=False, index=True)
    name = db.Column(db.String(64), nullable=False)
    position = db.Column(db.Integer, default=0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ProjectCard(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    project_id = db.Column(db.Integer, db.ForeignKey('workspace_project.id'), nullable=False, index=True)
    column_id = db.Column(db.Integer, db.ForeignKey('project_column.id'), nullable=False, index=True)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text, default='')
    priority = db.Column(db.String(16), default='normal')  # low|normal|high|urgent
    start_date = db.Column(db.Date)
    due_date = db.Column(db.Date)
    position = db.Column(db.Integer, default=0)
    creator = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class WorkspaceSpace(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    space_key = db.Column(db.String(16), nullable=False, unique=True, index=True)
    description = db.Column(db.Text, default='')
    visibility = db.Column(db.String(16), default='family')  # family|restricted|private
    allowed_members_json = db.Column(db.Text, default='[]')
    created_by = db.Column(db.String(64), nullable=False)
    template_id = db.Column(db.Integer, db.ForeignKey('workspace_template.id'))
    linked_project_id = db.Column(db.Integer, db.ForeignKey('workspace_project.id'))
    share_token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    archived = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SpacePage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    space_id = db.Column(db.Integer, db.ForeignKey('workspace_space.id'), nullable=False, index=True)
    title = db.Column(db.String(256), nullable=False)
    content = db.Column(db.Text, default='')
    media_json = db.Column(db.Text, default='[]')
    created_by = db.Column(db.String(64), nullable=False)
    updated_by = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class AppPermissionScheme(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    resource_type = db.Column(db.String(24), nullable=False, index=True)  # app_module|project|space
    resource_key = db.Column(db.String(64), index=True)  # module key or entity key
    resource_id = db.Column(db.Integer, index=True)
    visibility = db.Column(db.String(16), default='family')  # family|restricted|private
    allowed_members_json = db.Column(db.Text, default='[]')
    updated_by = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PermissionGroup(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True, index=True)
    description = db.Column(db.String(512), default='')
    created_by = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PermissionGroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey('permission_group.id'), nullable=False, index=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    added_by = db.Column(db.String(64), nullable=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (db.UniqueConstraint('group_id', 'member_name', name='uniq_group_member'),)


class ResourcePermission(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    resource_type = db.Column(db.String(24), nullable=False, index=True)  # app_module|project|space
    resource_key = db.Column(db.String(64), index=True)  # module key or entity key
    resource_id = db.Column(db.Integer, index=True)  # entity id when applicable
    subject_type = db.Column(db.String(16), nullable=False, index=True)  # member|group
    subject_value = db.Column(db.String(128), nullable=False, index=True)  # member name or group name
    can_read = db.Column(db.Boolean, default=True)
    can_write = db.Column(db.Boolean, default=False)
    can_admin = db.Column(db.Boolean, default=False)
    updated_by = db.Column(db.String(64), nullable=False)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('resource_type', 'resource_key', 'resource_id', 'subject_type', 'subject_value', name='uniq_resource_subject_perm'),
    )


class MealPlanEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False, index=True)
    meal_type = db.Column(db.String(32), nullable=False)  # breakfast|morning_snack|lunch|afternoon_snack|dinner
    recipe_id = db.Column(db.Integer, db.ForeignKey('recipe.id'), nullable=True)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    creator = db.Column(db.String(64), nullable=False)
    visibility = db.Column(db.String(16), default='family')  # family|restricted|private
    allowed_members_json = db.Column(db.Text, default='[]')
    require_pin = db.Column(db.Boolean, default=False)
    audience = db.Column(db.String(64), default='family')  # family|adults|kids|custom
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class File(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256), nullable=False)
    creator = db.Column(db.String(64), nullable=False)
    upload_time = db.Column(db.DateTime, default=datetime.utcnow)
    category = db.Column(db.String(64), default='uncategorized')


class PhotoAlbum(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    description = db.Column(db.String(512), default='')
    creator = db.Column(db.String(64), nullable=False, index=True)
    visibility = db.Column(db.String(16), default='private')  # private|family
    shared_with_json = db.Column(db.Text, default='[]')
    cover_photo_id = db.Column(db.Integer, db.ForeignKey('photo_asset.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PhotoAsset(db.Model):
    __table_args__ = (
        db.Index('idx_photo_asset_content_sha256', 'content_sha256'),
        db.Index('idx_photo_asset_source_device_path', 'source_device_id', 'source_path'),
    )

    id = db.Column(db.Integer, primary_key=True)
    album_id = db.Column(db.Integer, db.ForeignKey('photo_album.id'), index=True)
    filename = db.Column(db.String(256), nullable=False, unique=True)
    original_filename = db.Column(db.String(256), nullable=False)
    uploader = db.Column(db.String(64), nullable=False, index=True)
    visibility = db.Column(db.String(16), default='private')  # private|family
    shared_with_json = db.Column(db.Text, default='[]')
    mime_type = db.Column(db.String(128))
    file_size = db.Column(db.Integer)
    is_video = db.Column(db.Boolean, default=False)
    captured_at = db.Column(db.DateTime)
    content_sha256 = db.Column(db.String(64), index=True)
    source_device_id = db.Column(db.Integer, db.ForeignKey('sync_device.id'))
    source_path = db.Column(db.String(1024))
    source_mtime_utc = db.Column(db.DateTime)
    ingest_job_id = db.Column(db.Integer, db.ForeignKey('sync_job.id'))
    origin_kind = db.Column(db.String(32), nullable=False, default='manual_upload')
    dedupe_reason = db.Column(db.String(128))
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class PhotoDownloadJob(db.Model):
    __table_args__ = (
        db.Index('idx_photo_dl_job_member_state', 'requested_by_member', 'state'),
        db.Index('idx_photo_dl_job_expires', 'expires_at'),
        db.Index('idx_photo_dl_job_created', 'created_at'),
    )

    id = db.Column(db.Integer, primary_key=True)
    requested_by_member = db.Column(db.String(64), nullable=False, index=True)
    mode = db.Column(db.String(24), nullable=False, default='selected_assets')  # selected_assets|album
    album_id = db.Column(db.Integer, db.ForeignKey('photo_album.id'), index=True)
    state = db.Column(db.String(24), nullable=False, default='queued', index=True)
    total_assets = db.Column(db.Integer, nullable=False, default=0)
    included_assets = db.Column(db.Integer, nullable=False, default=0)
    skipped_assets = db.Column(db.Integer, nullable=False, default=0)
    total_bytes = db.Column(db.BigInteger, nullable=False, default=0)
    zip_size_bytes = db.Column(db.BigInteger)
    zip_filename = db.Column(db.String(256))
    zip_storage_name = db.Column(db.String(256))
    zip_rel_path = db.Column(db.String(1024))
    error_code = db.Column(db.String(64))
    error_message = db.Column(db.Text)
    started_at = db.Column(db.DateTime)
    completed_at = db.Column(db.DateTime)
    expires_at = db.Column(db.DateTime, index=True)
    cleanup_after_at = db.Column(db.DateTime)
    cleaned_at = db.Column(db.DateTime)
    last_accessed_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class PhotoDownloadJobAsset(db.Model):
    __table_args__ = (
        db.UniqueConstraint('job_id', 'photo_asset_id', name='uniq_photo_dl_job_asset'),
        db.Index('idx_photo_dl_job_asset_job', 'job_id'),
        db.Index('idx_photo_dl_job_asset_asset', 'photo_asset_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('photo_download_job.id'), nullable=False, index=True)
    photo_asset_id = db.Column(db.Integer, db.ForeignKey('photo_asset.id'), nullable=False, index=True)
    original_filename = db.Column(db.String(256), nullable=False)
    storage_filename = db.Column(db.String(256), nullable=False)
    mime_type = db.Column(db.String(128))
    file_size = db.Column(db.BigInteger, nullable=False, default=0)
    content_sha256 = db.Column(db.String(64), index=True)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class SyncDevice(db.Model):
    __table_args__ = (
        db.Index('idx_sync_device_member_platform', 'member_name', 'platform'),
    )

    id = db.Column(db.Integer, primary_key=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    platform = db.Column(db.String(32), nullable=False, index=True)
    device_label = db.Column(db.String(128), nullable=False)
    trust_level = db.Column(db.String(16), nullable=False, default='trusted')
    send_enabled = db.Column(db.Boolean, default=True, nullable=False)
    receive_only = db.Column(db.Boolean, default=False, nullable=False)
    app_instance_id = db.Column(db.String(128), unique=True, index=True)
    last_seen_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncSource(db.Model):
    __table_args__ = (
        db.Index('idx_sync_source_device_active', 'device_id', 'is_active'),
        db.Index('idx_sync_source_type', 'source_type'),
        db.UniqueConstraint('device_id', 'source_type', 'source_uri', name='uniq_sync_source_per_device'),
    )

    id = db.Column(db.Integer, primary_key=True)
    device_id = db.Column(db.Integer, db.ForeignKey('sync_device.id'), nullable=False, index=True)
    source_type = db.Column(db.String(32), nullable=False, index=True)
    source_uri = db.Column(db.String(1024), nullable=False)
    scope = db.Column(db.String(16), nullable=False, default='private')
    target_album_id = db.Column(db.Integer, db.ForeignKey('photo_album.id'))
    is_active = db.Column(db.Boolean, default=True, nullable=False)
    requires_client_scan = db.Column(db.Boolean, default=True, nullable=False)
    source_cursor = db.Column(db.Text)
    last_scan_at = db.Column(db.DateTime)
    last_success_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncJob(db.Model):
    __table_args__ = (
        db.Index('idx_sync_job_source_state', 'source_id', 'state'),
        db.Index('idx_sync_job_created', 'created_at'),
    )

    id = db.Column(db.Integer, primary_key=True)
    source_id = db.Column(db.Integer, db.ForeignKey('sync_source.id'), nullable=False, index=True)
    trigger_type = db.Column(db.String(24), nullable=False, default='manual')
    state = db.Column(db.String(24), nullable=False, default='queued', index=True)
    total_items = db.Column(db.Integer, nullable=False, default=0)
    processed_items = db.Column(db.Integer, nullable=False, default=0)
    ingested_items = db.Column(db.Integer, nullable=False, default=0)
    deduped_items = db.Column(db.Integer, nullable=False, default=0)
    failed_items = db.Column(db.Integer, nullable=False, default=0)
    started_at = db.Column(db.DateTime)
    finished_at = db.Column(db.DateTime)
    error_summary = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SyncJobItem(db.Model):
    __table_args__ = (
        db.Index('idx_sync_item_job_state', 'job_id', 'state'),
        db.Index('idx_sync_item_sha256', 'content_sha256'),
        db.UniqueConstraint('job_id', 'source_rel_path', 'size_bytes', 'mtime_utc', name='uniq_sync_job_item_hint'),
    )

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('sync_job.id'), nullable=False, index=True)
    client_item_id = db.Column(db.String(128))
    source_rel_path = db.Column(db.String(1024), nullable=False)
    filename = db.Column(db.String(256), nullable=False)
    size_bytes = db.Column(db.Integer, nullable=False)
    mime_type = db.Column(db.String(128))
    mtime_utc = db.Column(db.DateTime)
    quick_fingerprint = db.Column(db.String(128), index=True)
    content_sha256 = db.Column(db.String(64), index=True)
    state = db.Column(db.String(24), nullable=False, default='queued', index=True)
    attempts = db.Column(db.Integer, nullable=False, default=0)
    last_error = db.Column(db.Text)
    photo_asset_id = db.Column(db.Integer, db.ForeignKey('photo_asset.id'))
    uploaded_blob_path = db.Column(db.String(1024))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class MediaContentIndex(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content_sha256 = db.Column(db.String(64), nullable=False, unique=True, index=True)
    canonical_photo_asset_id = db.Column(db.Integer, db.ForeignKey('photo_asset.id'), nullable=False)
    file_size = db.Column(db.Integer, nullable=False, default=0)
    mime_type = db.Column(db.String(128))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MediaAlias(db.Model):
    __table_args__ = (
        db.Index('idx_media_alias_photo', 'photo_asset_id'),
        db.Index('idx_media_alias_device', 'alias_device_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    photo_asset_id = db.Column(db.Integer, db.ForeignKey('photo_asset.id'), nullable=False, index=True)
    alias_filename = db.Column(db.String(256), nullable=False)
    alias_source_path = db.Column(db.String(1024))
    alias_device_id = db.Column(db.Integer, db.ForeignKey('sync_device.id'))
    alias_kind = db.Column(db.String(32), nullable=False, default='renamed_duplicate')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class SyncEventLog(db.Model):
    __table_args__ = (
        db.Index('idx_sync_event_type', 'event_type'),
    )

    id = db.Column(db.Integer, primary_key=True)
    job_id = db.Column(db.Integer, db.ForeignKey('sync_job.id'), index=True)
    item_id = db.Column(db.Integer, db.ForeignKey('sync_job_item.id'), index=True)
    source_id = db.Column(db.Integer, db.ForeignKey('sync_source.id'), index=True)
    level = db.Column(db.String(16), nullable=False, default='info')
    event_type = db.Column(db.String(64), nullable=False, index=True)
    message = db.Column(db.String(512), nullable=False)
    payload_json = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class PhotoWatchFolder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_name = db.Column(db.String(64), nullable=False, index=True)
    folder_uri = db.Column(db.String(512), nullable=False)
    scope = db.Column(db.String(16), default='private')  # private|family
    target_album_id = db.Column(db.Integer, db.ForeignKey('photo_album.id'))
    is_active = db.Column(db.Boolean, default=True, index=True)
    last_synced_at = db.Column(db.DateTime)
    platform = db.Column(db.String(32), default='android')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Media(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256))
    url = db.Column(db.String(512))
    creator = db.Column(db.String(64))
    download_time = db.Column(db.DateTime, default=datetime.utcnow)
    filepath = db.Column(db.String(512))
    status = db.Column(db.String(32), default='done')  # pending, done, error
    progress = db.Column(db.Text)  # latest progress line or JSON

class PDF(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    filename = db.Column(db.String(256))
    creator = db.Column(db.String(64))
    upload_time = db.Column(db.DateTime, default=datetime.utcnow)
    compressed_path = db.Column(db.String(512))

class ShoppingItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item = db.Column(db.String(256), nullable=False)
    list_id = db.Column(db.Integer, db.ForeignKey('shopping_list.id'), nullable=True)
    repo_item_id = db.Column(db.Integer, db.ForeignKey('shopping_repo_item.id'), nullable=True)
    checked = db.Column(db.Boolean, default=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # JSON-encoded list of tags (e.g., ["Costco", "Dairy"]) for filtering/grouping
    tags = db.Column(db.Text, default='[]')
    price = db.Column(db.Float)  # price per unit
    quantity = db.Column(db.Float, default=1.0)  # quantity to purchase
    purchase_photo_filename = db.Column(db.String(256))
    size_requirements = db.Column(db.String(256))
    target_price = db.Column(db.Float)
    budget_ceiling = db.Column(db.Float)
    planning_notes = db.Column(db.Text)
    purchase_category = db.Column(db.String(64))


class ShoppingList(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False)
    color = db.Column(db.String(32), default='blue')
    list_type = db.Column(db.String(32), default='standard')
    creator = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class ShoppingRepoItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False)
    store = db.Column(db.String(128))
    category = db.Column(db.String(64))
    default_price = db.Column(db.Float)
    last_used = db.Column(db.DateTime)
    created_by = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class GroceryHistory(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    item = db.Column(db.String(256), nullable=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class CommonItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), unique=True, nullable=False)
    default_price = db.Column(db.Float)
    category = db.Column(db.String(64))
    last_used = db.Column(db.DateTime)

class HomeStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    status = db.Column(db.String(16), default='Away')

class Chore(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.Text, nullable=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    done = db.Column(db.Boolean, default=False)
    # JSON-encoded list of tags (e.g., ["Alice", "Weekend"]) for assignment/filtering
    tags = db.Column(db.Text, default='[]')
    task_type = db.Column(db.String(24), default='chore')  # chore|todo|task
    assignee = db.Column(db.String(128), index=True)
    due_date = db.Column(db.Date, index=True)
    status = db.Column(db.String(32), default='todo', index=True)
    status_options_json = db.Column(db.Text, default='["todo","in_progress","completed"]')
    mentions_json = db.Column(db.Text, default='[]')
    details_html = db.Column(db.Text)
    checklist_json = db.Column(db.Text, default='[]')
    chart_data_json = db.Column(db.Text, default='[]')
    table_data_json = db.Column(db.Text, default='{"headers":[],"rows":[]}')
    completed_at = db.Column(db.DateTime)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class Recipe(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    link = db.Column(db.String(512))
    photo_url = db.Column(db.String(1024))
    ingredients = db.Column(db.Text)
    instructions = db.Column(db.Text)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # JSON-encoded list of tags (e.g., ["Dessert", "Quick", "Vegetarian"]) for filtering/grouping
    tags = db.Column(db.Text, default='[]')

class ExpiryItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(256), nullable=False)
    expiry_date = db.Column(db.Date)
    creator = db.Column(db.String(64))
    invited_members_json = db.Column(db.Text, default='[]')
    reminder_offsets_json = db.Column(db.Text, default='[30,14,7,3,1,0]')
    notify_channels_json = db.Column(db.Text, default='["in_app"]')
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class ExpiryReminderNotification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    expiry_item_id = db.Column(db.Integer, db.ForeignKey('expiry_item.id'), nullable=False, index=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    reminder_offset_days = db.Column(db.Integer, nullable=False)
    due_date = db.Column(db.Date, nullable=False, index=True)
    acknowledged = db.Column(db.Boolean, default=False)
    acknowledged_at = db.Column(db.DateTime)
    snooze_count = db.Column(db.Integer, default=0)
    snoozed_until = db.Column(db.Date)
    email_sent = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('expiry_item_id', 'member_name', 'reminder_offset_days', 'due_date', name='uniq_expiry_member_offset_due'),
    )


class AppNotification(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(64), nullable=False, index=True)
    title = db.Column(db.String(256), nullable=False)
    message = db.Column(db.Text, default='')
    target_member = db.Column(db.String(128), index=True)
    payload_json = db.Column(db.Text, default='{}')
    channels_json = db.Column(db.Text, default='["in_app"]')
    created_by = db.Column(db.String(64))
    is_read = db.Column(db.Boolean, default=False)
    read_at = db.Column(db.DateTime)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class NotificationDeliveryLog(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    notification_id = db.Column(db.Integer, db.ForeignKey('app_notification.id'), nullable=False, index=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    channel = db.Column(db.String(16), nullable=False, index=True)  # in_app|email|mobile
    status = db.Column(db.String(16), default='sent')  # sent|failed|skipped
    detail = db.Column(db.String(512), default='')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class MobilePushToken(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    device_token = db.Column(db.String(512), nullable=False, unique=True, index=True)
    platform = db.Column(db.String(32), default='unknown')
    enabled = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FamilyChatMessage(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    sender = db.Column(db.String(128), nullable=False, index=True)
    recipient = db.Column(db.String(128), nullable=False, index=True)
    message = db.Column(db.Text, default='')
    attachment_filename = db.Column(db.String(256))
    attachment_mime = db.Column(db.String(128))
    is_read = db.Column(db.Boolean, default=False, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class FamilyCallSignal(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    session_id = db.Column(db.String(64), nullable=False, index=True)
    sender = db.Column(db.String(128), nullable=False, index=True)
    recipient = db.Column(db.String(128), nullable=False, index=True)
    signal_type = db.Column(db.String(32), nullable=False, index=True)
    payload_json = db.Column(db.Text, default='{}')
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


class FamilyPoll(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    creator = db.Column(db.String(128), nullable=False, index=True)
    question = db.Column(db.String(512), nullable=False)
    options_json = db.Column(db.Text, default='[]')
    recipients_json = db.Column(db.Text, default='[]')
    channels_json = db.Column(db.Text, default='["in_app"]')
    is_active = db.Column(db.Boolean, default=True, index=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    closed_at = db.Column(db.DateTime)


class FamilyPollResponse(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    poll_id = db.Column(db.Integer, db.ForeignKey('family_poll.id'), nullable=False, index=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    selected_option = db.Column(db.String(512))
    response_text = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('poll_id', 'member_name', name='uniq_poll_member_response'),
    )


class SmtpSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    enabled = db.Column(db.Boolean, default=False)
    host = db.Column(db.String(256), default='')
    port = db.Column(db.Integer, default=587)
    secure = db.Column(db.Boolean, default=False)
    username = db.Column(db.String(256), default='')
    password = db.Column(db.String(512), default='')
    sender = db.Column(db.String(256), default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class ShortURL(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    original_url = db.Column(db.String(512), nullable=False)
    short_code = db.Column(db.String(16), unique=True, nullable=False)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class QRCode(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    text = db.Column(db.Text, nullable=False)
    filename = db.Column(db.String(256), nullable=False)
    original_input = db.Column(db.Text)  # what user typed (for history display)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Notice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    content = db.Column(db.Text, default='')
    updated_by = db.Column(db.String(64))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)


class FamilyNotice(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), default='Family Notice')
    message = db.Column(db.Text, nullable=False)
    sender = db.Column(db.String(128), nullable=False, index=True)
    recipients_json = db.Column(db.Text, default='[]')
    channels_json = db.Column(db.Text, default='["in_app","email","mobile"]')
    require_ack = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class FamilyNoticeAcknowledgement(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    notice_id = db.Column(db.Integer, db.ForeignKey('family_notice.id'), nullable=False, index=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    acknowledged_at = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('notice_id', 'member_name', name='uniq_family_notice_member_ack'),
    )

class Reminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    time = db.Column(db.String(5))  # HH:MM (optional)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    creator = db.Column(db.String(64))
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    # New fields (phase 1) - added via auto-migration if missing
    category = db.Column(db.String(64))  # key referencing configured category
    color = db.Column(db.String(16))     # optional override hex color
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    # Tie back to a recurring rule (if generated)
    recurring_id = db.Column(db.Integer)

class MemberStatus(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(64), nullable=False)
    text = db.Column(db.Text, default='')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow)

class RecurringExpense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    unit_price = db.Column(db.Float, default=0.0)
    default_quantity = db.Column(db.Float, default=1.0)
    frequency = db.Column(db.String(16), default='daily')  # daily|weekly|monthly
    category = db.Column(db.String(64))
    monthly_mode = db.Column(db.String(16), default='day_of_month')  # calendar|day_of_month
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    last_generated_date = db.Column(db.Date)
    effective_from = db.Column(db.Date)  # apply changes from this date forward
    starting_balance = db.Column(db.Float, default=0.0)  # carryover from before app tracking
    creator = db.Column(db.String(64))
    include_in_family_total = db.Column(db.Boolean, default=True)
    shared_group_id = db.Column(db.String(64), index=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class RecurringReminder(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    creator = db.Column(db.String(64))
    # Legacy fields kept for backward compatibility
    frequency = db.Column(db.String(16), default='daily')  # daily|weekly|monthly (legacy)
    monthly_mode = db.Column(db.String(16), default='day_of_month')  # calendar|day_of_month (legacy)
    # New flexible recurrence
    interval = db.Column(db.Integer, default=1)  # e.g., 1,2,3
    unit = db.Column(db.String(8), default='day')  # 'day'|'week'|'month'|'year'
    time = db.Column(db.String(5))  # optional HH:MM
    category = db.Column(db.String(64))
    color = db.Column(db.String(16))
    start_date = db.Column(db.Date)
    end_date = db.Column(db.Date)
    last_generated_date = db.Column(db.Date)
    effective_from = db.Column(db.Date)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class ExpenseEntry(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(256), nullable=False)
    category = db.Column(db.String(64))
    unit_price = db.Column(db.Float)
    quantity = db.Column(db.Float)
    amount = db.Column(db.Float, nullable=False)
    payer = db.Column(db.String(64))
    include_in_family_total = db.Column(db.Boolean, default=True)
    recurring_id = db.Column(db.Integer, db.ForeignKey('recurring_expense.id'))
    shared_group_id = db.Column(db.String(64), index=True)
    amount_paid = db.Column(db.Float, default=0.0)
    carryover_from_entry_id = db.Column(db.Integer)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class SharedRecurringBill(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    category = db.Column(db.String(64))
    total_amount = db.Column(db.Float, nullable=False)
    frequency = db.Column(db.String(16), nullable=False, default='monthly')
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    active = db.Column(db.Boolean, default=True)
    created_by = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class SharedRecurringBillParticipant(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bill_id = db.Column(db.Integer, db.ForeignKey('shared_recurring_bill.id'), nullable=False, index=True)
    member_name = db.Column(db.String(64), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    recurring_expense_id = db.Column(db.Integer, db.ForeignKey('recurring_expense.id'))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('bill_id', 'member_name', name='uniq_shared_recurring_bill_member'),
    )

class IncomeEntry(db.Model):
    """Track income entries for the family"""
    id = db.Column(db.Integer, primary_key=True)
    date = db.Column(db.Date, nullable=False)
    title = db.Column(db.String(256), nullable=False)
    source = db.Column(db.String(128))  # e.g., "Salary", "Freelance"
    category = db.Column(db.String(64))
    amount = db.Column(db.Float, nullable=False)
    recipient = db.Column(db.String(64))  # who received the income
    notes = db.Column(db.Text)
    include_in_family_total = db.Column(db.Boolean, default=True)  # Toggle for family finance calculation
    recurring_income_id = db.Column(db.Integer, db.ForeignKey('recurring_income.id'))
    auto_generated = db.Column(db.Boolean, default=False)
    needs_confirmation = db.Column(db.Boolean, default=False)
    confirmed_at = db.Column(db.DateTime)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)


class RecurringIncome(db.Model):
    """Recurring paycheck schedule for automatic income generation."""
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(256), nullable=False)
    source = db.Column(db.String(128))
    recipient = db.Column(db.String(64), nullable=False)
    expected_amount = db.Column(db.Float, nullable=False)
    frequency = db.Column(db.String(16), nullable=False, default='biweekly')  # weekly|biweekly|monthly
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date)
    payday_day = db.Column(db.Integer)  # 1-31 for monthly schedules
    include_in_family_total = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    last_generated_date = db.Column(db.Date)
    created_by = db.Column(db.String(64))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class RecurringBillPayment(db.Model):
    """Track payment status for recurring bills by month"""
    id = db.Column(db.Integer, primary_key=True)
    recurring_expense_id = db.Column(db.Integer, db.ForeignKey('recurring_expense.id'), nullable=False)
    year = db.Column(db.Integer, nullable=False)
    month = db.Column(db.Integer, nullable=False)
    is_paid = db.Column(db.Boolean, default=False)
    paid_by = db.Column(db.String(64))  # who marked it as paid
    paid_date = db.Column(db.DateTime)  # when it was marked as paid
    notes = db.Column(db.Text)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    
    __table_args__ = (db.UniqueConstraint('recurring_expense_id', 'year', 'month', name='unique_bill_payment'),)

class HubSettings(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    hub_name = db.Column(db.String(256), default='HomeHub')
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class HouseholdRoutineState(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    bedtime_anchor_date = db.Column(db.Date, nullable=False)
    bedtime_anchor_reader = db.Column(db.String(64), nullable=False, default='Rachel')
    bath_mode = db.Column(db.Integer, nullable=False, default=0)
    updated_by = db.Column(db.String(64))
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class LibraryBookLoan(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    child_name = db.Column(db.String(128), nullable=False, index=True)
    title = db.Column(db.String(256), nullable=False)
    due_date = db.Column(db.Date)
    reminder_date = db.Column(db.Date)
    reminder_time = db.Column(db.String(5), default='08:00')
    calendar_event_id = db.Column(db.Integer, db.ForeignKey('family_calendar_event.id', ondelete='SET NULL'), unique=True, index=True)
    created_by = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FamilyMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    role = db.Column(db.String(16), nullable=False, default='standard')  # admin|standard
    pin = db.Column(db.String(4), nullable=True)  # 4-digit PIN for member login
    email = db.Column(db.String(256), nullable=True)
    birthday = db.Column(db.Date, nullable=True)
    favorite_color = db.Column(db.String(64), nullable=True)
    favorite_song = db.Column(db.String(256), nullable=True)
    amazon_wishlist_url = db.Column(db.String(512), nullable=True)
    mini_slideshow_enabled = db.Column(db.Boolean, default=False)
    status = db.Column(db.String(50), default='home')  # home, away, at_work, traveling, or custom
    status_updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    last_hub_login_at = db.Column(db.DateTime)
    last_hub_seen_at = db.Column(db.DateTime)
    avatar = db.Column(db.Text, nullable=True)  # base64 encoded avatar image
    birthday_notifications_enabled = db.Column(db.Boolean, default=True)
    notifications_enabled = db.Column(db.Boolean, default=True)
    chat_notifications_enabled = db.Column(db.Boolean, default=True)
    media_notifications_enabled = db.Column(db.Boolean, default=True)
    browser_notifications_enabled = db.Column(db.Boolean, default=True)
    weather_latitude = db.Column(db.Float, nullable=True)
    weather_longitude = db.Column(db.Float, nullable=True)
    weather_location_name = db.Column(db.String(256), nullable=True)
    weather_use_gps_location = db.Column(db.Boolean, default=False)
    added_at = db.Column(db.DateTime, default=datetime.utcnow)


class FamilyCalendarEvent(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    event_type = db.Column(db.String(24), nullable=False, default='event')  # event|reminder|birthday
    title = db.Column(db.String(256), nullable=False)
    description_html = db.Column(db.Text)
    color = db.Column(db.String(16), default='#3b82f6')
    start_date = db.Column(db.Date, nullable=False)
    end_date = db.Column(db.Date, nullable=False)
    start_time = db.Column(db.String(5))
    end_time = db.Column(db.String(5))
    reminder_at = db.Column(db.DateTime)
    is_all_day = db.Column(db.Boolean, default=True)
    visibility = db.Column(db.String(16), default='family')  # family|restricted|private
    is_recurring = db.Column(db.Boolean, default=False)
    recurrence_frequency = db.Column(db.String(16))  # daily|weekly|monthly|yearly
    recurrence_interval = db.Column(db.Integer, default=1)
    recurrence_until = db.Column(db.Date)
    recurrence_count = db.Column(db.Integer)
    created_by = db.Column(db.String(64), nullable=False)
    invited_members_json = db.Column(db.Text, default='[]')  # JSON list of member names
    external_uid = db.Column(db.String(256), index=True)
    external_source_url = db.Column(db.Text)
    photo_filename = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyAgendaLocation(db.Model):
    __table_args__ = (
        db.Index('idx_daily_agenda_location_name', 'name'),
    )

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(160), nullable=False)
    address = db.Column(db.String(512), nullable=False)
    notes = db.Column(db.Text)
    created_by = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class DailyAgendaEntry(db.Model):
    __table_args__ = (
        db.Index('idx_daily_agenda_entry_date_member', 'agenda_date', 'member_name'),
    )

    id = db.Column(db.Integer, primary_key=True)
    member_name = db.Column(db.String(128), nullable=False, index=True)
    agenda_date = db.Column(db.Date, nullable=False, index=True)
    start_time = db.Column(db.String(5), nullable=False)
    end_time = db.Column(db.String(5))
    location_id = db.Column(db.Integer, db.ForeignKey('daily_agenda_location.id', ondelete='SET NULL'), index=True)
    location_name = db.Column(db.String(160))
    location_address = db.Column(db.String(512))
    location_notes = db.Column(db.Text)
    details = db.Column(db.Text)
    calendar_event_id = db.Column(db.Integer, db.ForeignKey('family_calendar_event.id', ondelete='SET NULL'), unique=True, index=True)
    created_by = db.Column(db.String(128), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FamilyCalendarShareLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    label = db.Column(db.String(128), default='Family Calendar Share')
    created_by = db.Column(db.String(64), nullable=False)
    filter_title_keywords_json = db.Column(db.Text, default='[]')
    filter_event_types_json = db.Column(db.Text, default='[]')
    filter_created_by_json = db.Column(db.Text, default='[]')
    include_private_events = db.Column(db.Boolean, default=False)
    include_descriptions = db.Column(db.Boolean, default=True)
    active = db.Column(db.Boolean, default=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    expires_at = db.Column(db.DateTime)

class FamilyBudget(db.Model):
    """Shared family budget for a specific month/year"""
    id = db.Column(db.Integer, primary_key=True)
    budget_name = db.Column(db.String(256), nullable=False)
    month = db.Column(db.Integer, nullable=False)  # 1-12
    year = db.Column(db.Integer, nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    created_by = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    notes = db.Column(db.Text)
    categories = db.relationship('BudgetCategory', backref='budget', lazy=True, cascade='all, delete-orphan')

class BudgetCategory(db.Model):
    """Allocated budget for a specific category within a family budget"""
    id = db.Column(db.Integer, primary_key=True)
    budget_id = db.Column(db.Integer, db.ForeignKey('family_budget.id'), nullable=False)
    category_name = db.Column(db.String(128), nullable=False)
    allocated_amount = db.Column(db.Float, nullable=False)
    spent_amount = db.Column(db.Float, default=0.0)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class SharedExpense(db.Model):
    """Expense split between multiple family members"""
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(256), nullable=False)
    total_amount = db.Column(db.Float, nullable=False)
    paid_by = db.Column(db.String(64), nullable=False)  # who paid
    date = db.Column(db.Date, nullable=False)
    category = db.Column(db.String(64))
    # JSON list of participants: [{"name": "Alice", "amount": 50.0}, {"name": "Bob", "amount": 50.0}]
    participants = db.Column(db.Text, nullable=False)  # JSON
    receipt_filename = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    settled = db.Column(db.Boolean, default=False)

class DebtAccount(db.Model):
    """Track personal debts within the family"""
    id = db.Column(db.Integer, primary_key=True)
    description = db.Column(db.String(256), nullable=False)
    debtor = db.Column(db.String(64), nullable=False)  # who owes
    creditor = db.Column(db.String(64), nullable=False)  # who is owed
    initial_amount = db.Column(db.Float, nullable=False)
    current_balance = db.Column(db.Float, nullable=False)
    interest_rate = db.Column(db.Float, default=0.0)
    payoff_date = db.Column(db.Date)  # target payoff date
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)


class ActivityLog(db.Model):
    """Family-visible timeline of non-personal events."""
    __tablename__ = 'timeline_post'
    id = db.Column(db.Integer, primary_key=True)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow, index=True)
    post_type = db.Column(db.String(32), nullable=False, default='activity')
    title = db.Column(db.String(256), nullable=False, default='')
    author = db.Column(db.String(64), nullable=False, default='')
    actor = db.Column(db.String(64), nullable=False, default='')
    action = db.Column(db.String(64), nullable=False, default='')
    summary = db.Column(db.String(512), nullable=False, default='')
    entity_type = db.Column(db.String(64))
    entity_id = db.Column(db.Integer)
    visibility = db.Column(db.String(16), nullable=False, default='family')  # family|personal
    meta_json = db.Column(db.Text)  # JSON

class DebtPayment(db.Model):
    """Record of debt payment"""
    id = db.Column(db.Integer, primary_key=True)
    debt_id = db.Column(db.Integer, db.ForeignKey('debt_account.id'), nullable=False)
    payment_date = db.Column(db.Date, nullable=False)
    amount_paid = db.Column(db.Float, nullable=False)
    principal = db.Column(db.Float)  # portion towards principal
    interest = db.Column(db.Float)  # portion towards interest
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class FinancialGoal(db.Model):
    """Track financial goals (savings, investment targets, etc)"""
    id = db.Column(db.Integer, primary_key=True)
    goal_name = db.Column(db.String(256), nullable=False)
    description = db.Column(db.Text)
    target_amount = db.Column(db.Float, nullable=False)
    current_amount = db.Column(db.Float, default=0.0)
    target_date = db.Column(db.Date)
    category = db.Column(db.String(64))  # vacation, emergency fund, car, house, etc
    priority = db.Column(db.Integer, default=0)  # 1=high, 2=medium, 3=low
    created_by = db.Column(db.String(64), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)

class GoalContribution(db.Model):
    """Track contributions towards a financial goal"""
    id = db.Column(db.Integer, primary_key=True)
    goal_id = db.Column(db.Integer, db.ForeignKey('financial_goal.id'), nullable=False)
    contribution_date = db.Column(db.Date, nullable=False)
    amount = db.Column(db.Float, nullable=False)
    contributor = db.Column(db.String(64), nullable=False)
    notes = db.Column(db.Text)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)


class TransactionCategoryRule(db.Model):
    """Rule-based category override for recurring merchants/descriptions."""
    id = db.Column(db.Integer, primary_key=True)
    rule_name = db.Column(db.String(128), nullable=False)
    match_field = db.Column(db.String(24), nullable=False, default='merchant')  # merchant|description|title
    match_type = db.Column(db.String(24), nullable=False, default='contains')  # contains|equals|startswith|endswith
    match_value = db.Column(db.String(256), nullable=False)
    category = db.Column(db.String(64), nullable=False)
    active = db.Column(db.Boolean, default=True)
    priority = db.Column(db.Integer, default=100)
    created_by = db.Column(db.String(64), default='Administrator')
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)

class PlaidAccount(db.Model):
    """Linked bank account via Plaid"""
    __table_args__ = (
        db.UniqueConstraint('item_account_key', name='uniq_plaid_item_account_key'),
        db.UniqueConstraint('account_id', name='uniq_plaid_account_account_id'),
        db.Index('idx_plaid_account_item', 'plaid_item_id'),
    )

    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(64), nullable=False)  # family member
    access_token = db.Column(db.String(512), nullable=False)  # encrypted Plaid access token
    # Legacy field kept for backward compatibility with existing rows.
    item_id = db.Column(db.String(256), nullable=True)
    # True Plaid item id (shared by multiple accounts from one institution link).
    plaid_item_id = db.Column(db.String(256), index=True)
    # Unique row key for one Plaid item + one account within that item.
    item_account_key = db.Column(db.String(512), nullable=False)
    account_id = db.Column(db.String(256), nullable=False, unique=True)
    account_name = db.Column(db.String(256))
    account_type = db.Column(db.String(64))  # checking, savings, credit_card, etc
    account_subtype = db.Column(db.String(64))
    balance = db.Column(db.Float)
    currency = db.Column(db.String(3), default='USD')
    synced_at = db.Column(db.DateTime)
    last_transaction_id = db.Column(db.String(256))  # for incremental sync
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    active = db.Column(db.Boolean, default=True)

class PlaidTransaction(db.Model):
    """Transactions synced from Plaid"""
    id = db.Column(db.Integer, primary_key=True)
    plaid_account_id = db.Column(db.Integer, db.ForeignKey('plaid_account.id'), nullable=False)
    plaid_transaction_id = db.Column(db.String(256), unique=True, nullable=False)
    transaction_date = db.Column(db.Date, nullable=False)
    posted_date = db.Column(db.Date)
    amount = db.Column(db.Float, nullable=False)
    currency = db.Column(db.String(3), default='USD')
    description = db.Column(db.String(512))
    merchant = db.Column(db.String(256))
    category = db.Column(db.String(64))  # Plaid category
    pending = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    synced_at = db.Column(db.DateTime, default=datetime.utcnow)
