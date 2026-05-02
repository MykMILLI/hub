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
    uploaded_at = db.Column(db.DateTime, default=datetime.utcnow, index=True)


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
    status = db.Column(db.String(50), default='home')  # home, away, at_work, traveling, or custom
    status_updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)
    avatar = db.Column(db.Text, nullable=True)  # base64 encoded avatar image
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
    photo_filename = db.Column(db.String(256))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class FamilyCalendarShareLink(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    token = db.Column(db.String(64), nullable=False, unique=True, index=True)
    label = db.Column(db.String(128), default='Family Calendar Share')
    created_by = db.Column(db.String(64), nullable=False)
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

class PlaidAccount(db.Model):
    """Linked bank account via Plaid"""
    id = db.Column(db.Integer, primary_key=True)
    user = db.Column(db.String(64), nullable=False)  # family member
    access_token = db.Column(db.String(512), nullable=False)  # encrypted Plaid access token
    item_id = db.Column(db.String(256), nullable=False, unique=True)
    account_id = db.Column(db.String(256), nullable=False)
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
