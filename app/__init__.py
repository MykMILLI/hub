from flask import Flask, session, request, redirect, url_for, current_app
from flask_sqlalchemy import SQLAlchemy
from sqlalchemy import event
from sqlalchemy.engine import Engine
from .config import load_config
from datetime import timedelta
import os
import secrets
import sqlite3

db = SQLAlchemy()
_sqlite_pragmas_listener_installed = False


def _resolve_secret_key(base_dir: str) -> str:
    env_secret = (os.environ.get('SECRET_KEY') or '').strip()
    if env_secret:
        return env_secret

    data_dir = os.path.join(base_dir, 'data')
    os.makedirs(data_dir, exist_ok=True)
    secret_path = os.path.join(data_dir, '.secret_key')

    try:
        if os.path.exists(secret_path):
            with open(secret_path, 'r', encoding='utf-8') as f:
                stored = (f.read() or '').strip()
                if stored:
                    return stored
    except Exception:
        pass

    generated = secrets.token_hex(32)
    try:
        with open(secret_path, 'w', encoding='utf-8') as f:
            f.write(generated)
        # Best-effort: keep the file private on POSIX systems.
        os.chmod(secret_path, 0o600)
    except Exception:
        pass
    return generated


def _apply_sqlite_pragmas(dbapi_connection) -> None:
    if not isinstance(dbapi_connection, sqlite3.Connection):
        return

    cursor = dbapi_connection.cursor()
    try:
        cursor.execute('PRAGMA foreign_keys=ON;')
        cursor.execute('PRAGMA busy_timeout=5000;')
        try:
            # WAL is preferred for file-backed databases; skip if unsupported (for example, in-memory SQLite).
            cursor.execute('PRAGMA journal_mode=WAL;')
        except Exception:
            pass
    finally:
        cursor.close()


def _install_sqlite_pragmas_listener() -> None:
    global _sqlite_pragmas_listener_installed
    if _sqlite_pragmas_listener_installed:
        return

    @event.listens_for(Engine, 'connect')
    def _set_sqlite_pragmas_on_connect(dbapi_connection, _connection_record):
        _apply_sqlite_pragmas(dbapi_connection)

    _sqlite_pragmas_listener_installed = True


def create_app(test_config: dict | None = None):
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
    templates_dir = os.path.join(base_dir, 'templates')
    static_dir = os.path.join(base_dir, 'static')

    app = Flask(
        __name__,
        template_folder=templates_dir,
        static_folder=static_dir,
    )

    # Paths
    data_dir = os.path.join(base_dir, 'data')
    uploads_dir = os.path.join(base_dir, 'uploads')
    photos_dir = os.path.join(uploads_dir, 'photos')
    media_dir = os.path.join(base_dir, 'media')
    pdfs_dir = os.path.join(base_dir, 'pdfs')
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(uploads_dir, exist_ok=True)
    os.makedirs(photos_dir, exist_ok=True)
    os.makedirs(media_dir, exist_ok=True)
    os.makedirs(pdfs_dir, exist_ok=True)

    # SQLite DB file at an absolute path to avoid driver path issues
    db_path = os.path.join(base_dir, 'data', 'app.db')
    app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///' + db_path
    app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
    # Use a stable secret key so sessions survive app restarts/deploys.
    app.config['SECRET_KEY'] = _resolve_secret_key(base_dir)
    session_days_raw = (os.environ.get('SESSION_LIFETIME_DAYS') or '').strip()
    try:
        session_days = max(1, int(session_days_raw)) if session_days_raw else 30
    except Exception:
        session_days = 30
    app.config.setdefault('SESSION_PERMANENT', True)
    app.config.setdefault('PERMANENT_SESSION_LIFETIME', timedelta(days=session_days))
    app.config.setdefault('SESSION_REFRESH_EACH_REQUEST', True)
    app.config.setdefault('SESSION_COOKIE_SAMESITE', 'Lax')
    # Explicitly disable CSRF (forms are simple and app runs on home network)
    app.config['WTF_CSRF_ENABLED'] = False

    # Load config.yml and make sure tests / environment overrides preserve the
    # user-facing defaults expected by the templates (theme, instance_name, etc.).
    hub_cfg = load_config()
    app.config['HOMEHUB_CONFIG'] = hub_cfg

    # Allow tests to override configuration (database, testing flag, etc.) while
    # preserving the default HomeHub config structure. This keeps template access
    # such as config.theme.primary_color and config.instance_name working.
    if test_config:
        app.config.update(test_config)
        merged_cfg = load_config()
        if isinstance(app.config.get('HOMEHUB_CONFIG'), dict):
            merged_cfg.update(app.config['HOMEHUB_CONFIG'])
        app.config['HOMEHUB_CONFIG'] = merged_cfg

    # Expose the user-facing HomeHub config values at the top level so templates can
    # access helpers like config.theme.primary_color and config.instance_name.
    hub_cfg = app.config.get('HOMEHUB_CONFIG') or {}
    for key, value in hub_cfg.items():
        if key not in app.config or value is not None:
            app.config[key] = value

    from .services.supabase_service import supabase_service
    supabase_service.init_app(app)

    _install_sqlite_pragmas_listener()
    db.init_app(app)

    # Ensure models are imported before creating tables
    with app.app_context():
        from . import models  # noqa: F401 ensures model metadata is registered
        db.create_all()
        
        # SCHEMA MIGRATION: Ensure all required columns exist
        # This runs EVERY startup to catch missing columns from schema changes
        if not app.config.get('TESTING'):
            try:
                import sqlite3
                conn = sqlite3.connect(db_path)
                cur = conn.cursor()
                from .models import RecurringExpense as _RecurringExpense, QRCode as _QRCode, Reminder as _Reminder
                _apply_sqlite_pragmas(conn)
                
                def ensure_column(table, col, type_spec, default=None):
                    """Add column if it doesn't exist"""
                    try:
                        cur.execute(f"PRAGMA table_info({table})")
                        cols = [row[1] for row in cur.fetchall()]
                        if col not in cols:
                            print(f"[SCHEMA] Adding {col} to {table}")
                            cur.execute(f"ALTER TABLE {table} ADD COLUMN {col} {type_spec}")
                            if default is not None:
                                cur.execute(f"UPDATE {table} SET {col}=? WHERE {col} IS NULL", (default,))
                            conn.commit()
                    except Exception as e:
                        print(f"[SCHEMA ERROR] Failed to add {col} to {table}: {e}")

                def ensure_index(index_name, table, columns, unique=False):
                    try:
                        cur.execute("SELECT name FROM sqlite_master WHERE type='index' AND name=?", (index_name,))
                        exists = cur.fetchone() is not None
                        if exists:
                            return
                        unique_sql = 'UNIQUE ' if unique else ''
                        cur.execute(f"CREATE {unique_sql}INDEX {index_name} ON {table} ({columns})")
                        conn.commit()
                    except Exception as e:
                        print(f"[SCHEMA ERROR] Failed to create index {index_name} on {table}: {e}")
                
                # CRITICAL COLUMNS - Add these to prevent 500 errors
                ensure_column('income_entry', 'include_in_family_total', 'INTEGER', 1)
                ensure_column('expense_entry', 'include_in_family_total', 'INTEGER', 1)
                ensure_column('expense_entry', 'amount_paid', 'REAL', 0.0)
                ensure_column('expense_entry', 'carryover_from_entry_id', 'INTEGER', None)
                ensure_column('income_entry', 'recurring_income_id', 'INTEGER', None)
                ensure_column('income_entry', 'auto_generated', 'INTEGER', 0)
                ensure_column('income_entry', 'needs_confirmation', 'INTEGER', 0)
                ensure_column('income_entry', 'confirmed_at', 'TIMESTAMP', None)
                ensure_column('income_entry', 'category', 'TEXT', None)
                ensure_column('family_member', 'pin', 'TEXT', None)
                ensure_column('family_member', 'role', 'TEXT', 'standard')
                ensure_column('family_member', 'status', 'TEXT', 'home')
                ensure_column('family_member', 'status_updated_at', 'TIMESTAMP', None)
                ensure_column('family_member', 'last_hub_login_at', 'TIMESTAMP', None)
                ensure_column('family_member', 'last_hub_seen_at', 'TIMESTAMP', None)
                ensure_column('family_member', 'avatar', 'TEXT', None)
                ensure_column('family_member', 'birthday', 'DATE', None)
                ensure_column('family_member', 'favorite_color', 'TEXT', None)
                ensure_column('family_member', 'favorite_song', 'TEXT', None)
                ensure_column('family_member', 'amazon_wishlist_url', 'TEXT', None)
                ensure_column('family_member', 'mini_slideshow_enabled', 'INTEGER', 0)
                ensure_column('family_member', 'birthday_notifications_enabled', 'INTEGER', 1)
                ensure_column('family_member', 'notifications_enabled', 'INTEGER', 1)
                ensure_column('family_member', 'chat_notifications_enabled', 'INTEGER', 1)
                ensure_column('family_member', 'media_notifications_enabled', 'INTEGER', 1)
                ensure_column('family_member', 'browser_notifications_enabled', 'INTEGER', 1)
                ensure_column('family_member', 'weather_latitude', 'REAL', None)
                ensure_column('family_member', 'weather_longitude', 'REAL', None)
                ensure_column('family_member', 'weather_location_name', 'TEXT', None)
                ensure_column('family_member', 'weather_use_gps_location', 'INTEGER', 0)
                ensure_column('plaid_account', 'plaid_item_id', 'TEXT', None)
                ensure_column('plaid_account', 'item_account_key', 'TEXT', None)
                ensure_column(_RecurringExpense.__tablename__, 'monthly_mode', 'TEXT', 'day_of_month')
                ensure_column(_RecurringExpense.__tablename__, 'category', 'TEXT', None)
                ensure_column(_RecurringExpense.__tablename__, 'effective_from', 'DATE', None)
                ensure_column(_RecurringExpense.__tablename__, 'starting_balance', 'REAL', 0.0)
                ensure_column(_RecurringExpense.__tablename__, 'include_in_family_total', 'INTEGER', 1)
                ensure_column(_RecurringExpense.__tablename__, 'shared_group_id', 'TEXT', None)
                ensure_column('expense_entry', 'shared_group_id', 'TEXT', None)
                ensure_column('recurring_income', 'include_in_family_total', 'INTEGER', 1)
                ensure_column(_QRCode.__tablename__, 'original_input', 'TEXT', None)
                ensure_column(_Reminder.__tablename__, 'recurring_id', 'INTEGER', None)
                ensure_column('recipe', 'tags', 'TEXT', '[]')
                ensure_column('recipe', 'photo_url', 'TEXT', None)
                ensure_column('file', 'category', 'TEXT', 'uncategorized')
                ensure_column('shopping_item', 'tags', 'TEXT', '[]')
                ensure_column('shopping_item', 'price', 'REAL', None)
                ensure_column('shopping_item', 'quantity', 'REAL', 1.0)
                ensure_column('shopping_item', 'list_id', 'INTEGER', None)
                ensure_column('shopping_item', 'repo_item_id', 'INTEGER', None)
                ensure_column('shopping_item', 'purchase_photo_filename', 'TEXT', None)
                ensure_column('shopping_item', 'size_requirements', 'TEXT', None)
                ensure_column('shopping_item', 'target_price', 'REAL', None)
                ensure_column('shopping_item', 'budget_ceiling', 'REAL', None)
                ensure_column('shopping_item', 'planning_notes', 'TEXT', None)
                ensure_column('shopping_item', 'purchase_category', 'TEXT', None)
                ensure_column('shopping_list', 'list_type', 'TEXT', 'standard')
                ensure_column('chore', 'tags', 'TEXT', '[]')
                ensure_column('chore', 'done', 'INTEGER', 0)
                ensure_column('chore', 'task_type', 'TEXT', 'chore')
                ensure_column('chore', 'assignee', 'TEXT', None)
                ensure_column('chore', 'assignees_json', 'TEXT', '[]')
                ensure_column('chore', 'watchers_json', 'TEXT', '[]')
                ensure_column('chore', 'days_of_week_json', 'TEXT', '[]')
                ensure_column('chore', 'due_date', 'DATE', None)
                ensure_column('chore', 'status', 'TEXT', 'todo')
                ensure_column('chore', 'status_options_json', 'TEXT', '["todo","in_progress","completed"]')
                ensure_column('chore', 'mentions_json', 'TEXT', '[]')
                ensure_column('chore', 'details_html', 'TEXT', None)
                ensure_column('chore', 'checklist_json', 'TEXT', '[]')
                ensure_column('chore', 'checklist_meta_json', 'TEXT', '{}')
                ensure_column('chore', 'chart_data_json', 'TEXT', '[]')
                ensure_column('chore', 'table_data_json', 'TEXT', '{"headers":[],"rows":[]}')
                ensure_column('chore', 'completed_at', 'TIMESTAMP', None)
                ensure_column('chore', 'updated_at', 'TIMESTAMP', None)
                ensure_column('chore', 'reward', 'TEXT', None)
                ensure_column('chore_weekly_completion', 'completed_on', 'DATE', None)
                ensure_column('media', 'status', 'TEXT', 'done')
                ensure_column('media', 'progress', 'TEXT', None)
                ensure_column('reminder', 'category', 'TEXT', None)
                ensure_column('reminder', 'color', 'TEXT', None)
                ensure_column('reminder', 'updated_at', 'TIMESTAMP', None)
                ensure_column('reminder', 'time', 'TEXT', None)
                ensure_column('recurring_reminder', 'interval', 'INTEGER', 1)
                ensure_column('recurring_reminder', 'unit', 'TEXT', 'day')
                ensure_column('shared_expense', 'receipt_filename', 'TEXT', None)
                ensure_column('family_calendar_event', 'is_recurring', 'INTEGER', 0)
                ensure_column('family_calendar_event', 'visibility', 'TEXT', 'family')
                ensure_column('family_calendar_event', 'start_time', 'TEXT', None)
                ensure_column('family_calendar_event', 'end_time', 'TEXT', None)
                ensure_column('family_calendar_event', 'recurrence_frequency', 'TEXT', None)
                ensure_column('family_calendar_event', 'recurrence_interval', 'INTEGER', 1)
                ensure_column('family_calendar_event', 'recurrence_until', 'DATE', None)
                ensure_column('family_calendar_event', 'recurrence_count', 'INTEGER', None)
                ensure_column('family_calendar_event', 'external_uid', 'TEXT', None)
                ensure_column('family_calendar_event', 'external_source_url', 'TEXT', None)
                ensure_column('family_calendar_share_link', 'label', 'TEXT', 'Family Calendar Share')
                ensure_column('family_calendar_share_link', 'filter_title_keywords_json', 'TEXT', '[]')
                ensure_column('family_calendar_share_link', 'filter_event_types_json', 'TEXT', '[]')
                ensure_column('family_calendar_share_link', 'filter_created_by_json', 'TEXT', '[]')
                ensure_column('family_calendar_share_link', 'include_private_events', 'INTEGER', 0)
                ensure_column('family_calendar_share_link', 'include_descriptions', 'INTEGER', 1)
                ensure_column('note', 'visibility', 'TEXT', 'family')
                ensure_column('note', 'allowed_members_json', 'TEXT', '[]')
                ensure_column('note', 'require_pin', 'INTEGER', 0)
                ensure_column('note', 'title', 'TEXT', '')

                # Plaid migration: maintain one row per item+account and avoid account duplicates.
                try:
                    cur.execute(
                        """
                        UPDATE plaid_account
                        SET item_account_key =
                            CASE
                                WHEN COALESCE(TRIM(item_account_key), '') != '' THEN item_account_key
                                WHEN COALESCE(TRIM(plaid_item_id), '') != '' THEN plaid_item_id || ':' || account_id
                                ELSE COALESCE(item_id, account_id)
                            END
                        WHERE COALESCE(TRIM(item_account_key), '') = ''
                        """
                    )
                    conn.commit()
                except Exception as e:
                    print(f"[SCHEMA ERROR] Failed to backfill plaid_account.item_account_key: {e}")

                ensure_index('idx_plaid_account_item_account_key_uq', 'plaid_account', 'item_account_key', unique=True)
                ensure_index('idx_plaid_account_account_id_uq', 'plaid_account', 'account_id', unique=True)
                ensure_column('note', 'note_type', 'TEXT', 'note')
                ensure_column('note', 'updated_by', 'TEXT', None)
                ensure_column('note', 'is_task', 'INTEGER', 0)
                ensure_column('note', 'kanban_column', 'TEXT', 'todo')
                ensure_column('note', 'kanban_order', 'INTEGER', 0)
                ensure_column('note', 'kanban_priority', 'TEXT', 'normal')
                ensure_column('note', 'due_date', 'DATE', None)
                ensure_column('note', 'updated_at', 'TIMESTAMP', None)
                ensure_column('workspace_project', 'archived', 'INTEGER', 0)
                ensure_column('workspace_space', 'archived', 'INTEGER', 0)
                ensure_column('project_card', 'start_date', 'DATE', None)

                # Meal planner
                ensure_column('meal_plan_entry', 'date', 'DATE', None)
                ensure_column('meal_plan_entry', 'meal_type', 'TEXT', None)
                ensure_column('meal_plan_entry', 'recipe_id', 'INTEGER', None)
                ensure_column('meal_plan_entry', 'title', 'TEXT', '')
                ensure_column('meal_plan_entry', 'description', 'TEXT', None)
                ensure_column('meal_plan_entry', 'creator', 'TEXT', None)
                ensure_column('meal_plan_entry', 'visibility', 'TEXT', 'family')
                ensure_column('meal_plan_entry', 'allowed_members_json', 'TEXT', '[]')
                ensure_column('meal_plan_entry', 'require_pin', 'INTEGER', 0)
                ensure_column('meal_plan_entry', 'audience', 'TEXT', 'family')
                ensure_column('meal_plan_entry', 'updated_at', 'TIMESTAMP', None)

                # Expiry tracker + notification platform
                ensure_column('expiry_item', 'invited_members_json', 'TEXT', '[]')
                ensure_column('expiry_item', 'reminder_offsets_json', 'TEXT', '[30,14,7,3,1,0]')
                ensure_column('expiry_item', 'notify_channels_json', 'TEXT', '["in_app"]')
                ensure_column('family_member', 'email', 'TEXT', None)
                ensure_column('app_notification', 'is_read', 'INTEGER', 0)
                ensure_column('app_notification', 'read_at', 'TIMESTAMP', None)
                ensure_column('expiry_reminder_notification', 'snooze_count', 'INTEGER', 0)
                ensure_column('expiry_reminder_notification', 'snoozed_until', 'DATE', None)

                # Timeline posts (family activity feed)
                ensure_column('timeline_post', 'timestamp', 'TIMESTAMP', None)
                ensure_column('timeline_post', 'post_type', 'TEXT', 'activity')
                ensure_column('timeline_post', 'title', 'TEXT', '')
                ensure_column('timeline_post', 'author', 'TEXT', '')
                ensure_column('timeline_post', 'actor', 'TEXT', '')
                ensure_column('timeline_post', 'action', 'TEXT', '')
                ensure_column('timeline_post', 'summary', 'TEXT', '')
                ensure_column('timeline_post', 'entity_type', 'TEXT', None)
                ensure_column('timeline_post', 'entity_id', 'INTEGER', None)
                ensure_column('timeline_post', 'visibility', 'TEXT', 'family')
                ensure_column('timeline_post', 'meta_json', 'TEXT', None)

                # Phase 1 media sync columns
                ensure_column('photo_asset', 'content_sha256', 'TEXT', None)
                ensure_column('photo_asset', 'source_device_id', 'INTEGER', None)
                ensure_column('photo_asset', 'source_path', 'TEXT', None)
                ensure_column('photo_asset', 'source_mtime_utc', 'TIMESTAMP', None)
                ensure_column('photo_asset', 'ingest_job_id', 'INTEGER', None)
                ensure_column('photo_asset', 'origin_kind', 'TEXT', 'manual_upload')
                ensure_column('photo_asset', 'dedupe_reason', 'TEXT', None)

                # Ensure Phase 1 indexes exist
                try:
                    cur.execute('CREATE INDEX IF NOT EXISTS idx_photo_asset_content_sha256 ON photo_asset(content_sha256)')
                    cur.execute('CREATE INDEX IF NOT EXISTS idx_photo_asset_source_device_path ON photo_asset(source_device_id, source_path)')
                    conn.commit()
                except Exception as idx_err:
                    print(f"[SCHEMA WARN] Failed to create sync indexes: {idx_err}")

                # Backfill existing photo_asset rows in batches.
                try:
                    from .services.media_fingerprint import sha256_file
                    uploads_photos = os.path.join(base_dir, 'uploads', 'photos')
                    cur.execute("SELECT id, filename FROM photo_asset WHERE (content_sha256 IS NULL OR TRIM(content_sha256)='')")
                    rows = cur.fetchmany(200)
                    while rows:
                        for asset_id, filename in rows:
                            if not filename:
                                continue
                            abs_path = os.path.join(uploads_photos, str(filename))
                            if not os.path.exists(abs_path):
                                continue
                            try:
                                digest = sha256_file(abs_path)
                            except Exception:
                                continue
                            cur.execute("UPDATE photo_asset SET content_sha256=?, origin_kind=COALESCE(origin_kind, 'manual_upload') WHERE id=?", (digest, asset_id))
                        conn.commit()
                        rows = cur.fetchmany(200)
                except Exception as backfill_err:
                    print(f"[SCHEMA WARN] Hash backfill failed: {backfill_err}")
                
                conn.close()
                print("[SCHEMA] Migration complete")
            except Exception as e:
                print(f"[SCHEMA FATAL] Database schema migration failed: {e}")
                import traceback
                traceback.print_exc()
                # Don't silently fail - this is critical

        try:
            from .models import SmtpSettings
            smtp_row = SmtpSettings.query.order_by(SmtpSettings.id.asc()).first()
            if not smtp_row:
                smtp_row = SmtpSettings()
                db.session.add(smtp_row)

            env_enabled = os.environ.get('SMTP_ENABLED')
            env_host = os.environ.get('SMTP_HOST')
            env_port = os.environ.get('SMTP_PORT')
            env_secure = os.environ.get('SMTP_SECURE')
            env_user = os.environ.get('SMTP_USER')
            env_pass = os.environ.get('SMTP_PASS')
            env_from = os.environ.get('SMTP_FROM')

            cfg_smtp = app.config.get('HOMEHUB_CONFIG', {}).get('smtp', {}) or {}

            def _to_bool(value, default=False):
                if value is None:
                    return default
                return str(value).strip().lower() in {'1', 'true', 'yes', 'on'}

            smtp_row.enabled = _to_bool(env_enabled, bool(cfg_smtp.get('enabled', False)))
            smtp_row.host = str(env_host if env_host is not None else cfg_smtp.get('host', '')).strip()
            try:
                smtp_row.port = int(env_port if env_port is not None else cfg_smtp.get('port', 587))
            except Exception:
                smtp_row.port = 587
            smtp_row.secure = _to_bool(env_secure, bool(cfg_smtp.get('secure', False)))
            smtp_row.username = str(env_user if env_user is not None else cfg_smtp.get('username', '')).strip()
            smtp_row.password = str(env_pass if env_pass is not None else cfg_smtp.get('password', '')).strip()
            smtp_row.sender = str(env_from if env_from is not None else cfg_smtp.get('sender_email', '')).strip()

            db.session.commit()
        except Exception as e:
            print(f"[SMTP INIT] Failed to initialize SMTP settings row: {e}")

        # Convert legacy watched-folder rows into Phase 1 sync sources.
        try:
            from .models import PhotoWatchFolder, SyncDevice, SyncSource
            legacy_rows = PhotoWatchFolder.query.order_by(PhotoWatchFolder.id.asc()).all()
            for legacy in legacy_rows:
                member = (legacy.member_name or '').strip() or 'Administrator'
                platform = (legacy.platform or 'web').strip().lower()
                if platform not in {'web', 'android', 'ios', 'server'}:
                    platform = 'web'
                label = f"legacy-{platform}-{member}"
                device = SyncDevice.query.filter_by(member_name=member, platform=platform, device_label=label).first()
                if not device:
                    device = SyncDevice(
                        member_name=member,
                        platform=platform,
                        device_label=label,
                        trust_level='trusted',
                        send_enabled=True,
                        receive_only=False,
                    )
                    db.session.add(device)
                    db.session.flush()

                exists = SyncSource.query.filter_by(
                    device_id=device.id,
                    source_type='legacy_watch_folder',
                    source_uri=legacy.folder_uri,
                ).first()
                if exists:
                    continue
                db.session.add(SyncSource(
                    device_id=device.id,
                    source_type='legacy_watch_folder',
                    source_uri=legacy.folder_uri,
                    scope=(legacy.scope or 'private'),
                    target_album_id=legacy.target_album_id,
                    is_active=bool(legacy.is_active),
                    requires_client_scan=True,
                    last_success_at=legacy.last_synced_at,
                ))
            db.session.commit()
        except Exception as legacy_err:
            db.session.rollback()
            print(f"[SYNC LEGACY] Failed to map PhotoWatchFolder rows: {legacy_err}")

    # Initialize Plaid service
    from .plaid_service import plaid_service
    plaid_service.init_app(app)

    from .blueprints import main_bp
    from .blueprints import money  # noqa: F401
    
    # Register modular route modules to attach endpoints to main_bp
    from .blueprints import auth  # noqa: F401
    from .blueprints import dashboard  # noqa: F401
    from .blueprints import notes  # noqa: F401
    from .blueprints import uploads  # noqa: F401
    from .blueprints import shortener  # noqa: F401
    from .blueprints import shopping  # noqa: F401
    from .blueprints import recipes  # noqa: F401
    from .blueprints import expiry  # noqa: F401
    from .blueprints import media_pdfs  # noqa: F401
    from .blueprints import expenses  # noqa: F401
    from .blueprints import chores  # noqa: F401
    from .blueprints import qr  # noqa: F401
    from .blueprints import weather  # noqa: F401
    from .blueprints import settings  # noqa: F401
    from .blueprints import timeline  # noqa: F401
    from .blueprints import family_calendar  # noqa: F401
    from .blueprints import daily_agenda  # noqa: F401
    from .blueprints import meal_planner  # noqa: F401
    from .blueprints import notifications  # noqa: F401
    from .blueprints import communication  # noqa: F401
    from .blueprints import photos  # noqa: F401
    from .blueprints import photos_sync  # noqa: F401
    from .blueprints import immich  # noqa: F401
    from .blueprints import home_assistant  # noqa: F401
    from .blueprints import cameras  # noqa: F401
    from .blueprints import supabase  # noqa: F401
    app.register_blueprint(main_bp)
    app.register_blueprint(money.money_bp)

    # Phase 1 only: this is an in-process worker bootstrap.
    # Exactly one process/container should run it.
    # Multi-process production should move to a separate worker process/queue.
    env_sync_worker = os.environ.get('SYNC_WORKER_ENABLED')
    if env_sync_worker is None:
        app.config.setdefault('SYNC_WORKER_ENABLED', True)
    else:
        app.config['SYNC_WORKER_ENABLED'] = str(env_sync_worker).strip().lower() in {'1', 'true', 'yes', 'on'}

    # Start Phase 1 sync worker thread once per process, only when explicitly enabled.
    if not app.config.get('TESTING') and app.config.get('SYNC_WORKER_ENABLED'):
        try:
            from .services.sync_worker import start_sync_worker
            started = start_sync_worker(app)
            if started:
                print('[SYNC WORKER] started')
        except Exception as worker_err:
            print(f"[SYNC WORKER] failed to start: {worker_err}")

    # Background scheduler so reminders fire even when no client is polling.
    if not app.config.get('TESTING') and str(os.environ.get('NOTIFY_SCHEDULER_ENABLED', '1')).strip().lower() in {'1', 'true', 'yes', 'on'}:
        try:
            from .services.notification_scheduler import start_notification_scheduler
            if start_notification_scheduler(app):
                print('[NOTIFY SCHEDULER] started')
        except Exception as sched_err:
            print(f"[NOTIFY SCHEDULER] failed to start: {sched_err}")

    @app.before_request
    def enforce_module_permissions():
        kiosk_admin_names = {'rachel', 'mykal'}
        path = (request.path or '').rstrip('/') or '/'
        if path.startswith('/static/') or path.startswith('/uploads/') or path.startswith('/sw.js') or path.startswith('/manifest.webmanifest'):
            return None
        if path.startswith('/shared/project/') or path.startswith('/shared/space/'):
            return None
        if path.startswith('/login') or path.startswith('/logout'):
            return None
        if path.startswith('/api/auth/') or path.startswith('/api/member-login') or path.startswith('/api/member/set-pin') or path.startswith('/api/member-logout') or path.startswith('/api/current-member'):
            return None
        if path == '/api/notifications/mobile/poll':
            return None
        if path.startswith('/api/supabase/auth/') or path.startswith('/api/supabase/client-config'):
            return None

        if current_app.config.get('TESTING') and not path.startswith('/api/app-modules') and not session.get('kid_kiosk_mode'):
            return None

        # Feature hard-disables (kept in codebase, but intentionally turned off).
        disabled_prefixes = (
            '/home-assistant',
            '/expenses',
            '/money',
            '/api/expenses',
            '/api/money',
            '/photos',
        )
        if any(path == prefix or path.startswith(prefix + '/') for prefix in disabled_prefixes):
            if path.startswith('/api/'):
                return {'ok': False, 'error': 'This feature is currently disabled'}, 403
            return redirect(url_for('main.index'))

        if not session.get('current_member'):
            if path.startswith('/api/'):
                return {'ok': False, 'error': 'Authentication required'}, 401
            return redirect(url_for('main.login'))

        current_member_name = str(session.get('current_member') or '').strip().lower()
        kiosk_mode_enabled = bool(session.get('kid_kiosk_mode'))
        if kiosk_mode_enabled and current_member_name not in kiosk_admin_names:
            allowed_exact_paths = {
                '/kids-stars',
                '/api/current-member',
                '/api/member-logout',
            }
            allowed_prefixes = (
                '/api/rewards/state',
                '/api/rewards/claim',
            )
            if path in allowed_exact_paths or any(path == prefix or path.startswith(prefix + '/') for prefix in allowed_prefixes):
                return None
            if path.startswith('/api/'):
                return {'ok': False, 'error': 'This tablet is locked to Kid Star Board mode.'}, 403
            return redirect(url_for('main.kids_stars_tablet'))

        exact_route_map = {
            '/': 'welcome',
            '/timeline': 'timeline',
            '/notes': 'shared_notes',
            '/projects': 'shared_notes',
            '/spaces': 'shared_notes',
            '/upload': 'shared_cloud',
            '/shopping': 'shopping_list',
            '/chores': 'chores',
            '/gold-stars': 'reward_system',
            '/kids-stars': 'reward_system',
            '/recipes': 'recipes',
            '/expiry': 'expiry_tracker',
            '/shorten': 'url_shortener',
            '/media': 'media_downloader',
            '/pdfs': 'pdf_compressor',
            '/qr': 'qr_generator',
            '/expenses': 'expense_tracker',
            '/calendar': 'shared_calendar',
            '/daily-agenda': 'daily_agenda',
            '/meal-planner': 'meal_planner',
            '/photos': 'photos',
            '/notifications': 'notifications',
            '/messages': 'notifications',
            '/polls': 'notifications',
            '/home-assistant': 'home_assistant',
            '/cameras': 'cameras',
            '/settings': 'settings',
        }

        prefix_route_map = {
            '/api/workspaces': 'shared_notes',
            '/api/rewards': 'reward_system',
            '/api/gold-stars': 'reward_system',
            '/api/expenses': 'expense_tracker',
            '/api/money': 'expense_tracker',
            '/money': 'expense_tracker',
            '/api/calendar': 'shared_calendar',
            '/api/daily-agenda': 'daily_agenda',
            '/api/meal-planner': 'meal_planner',
            '/api/photos': 'photos',
            '/api/notifications': 'notifications',
            '/api/calls': 'notifications',
            '/api/messages': 'notifications',
            '/api/polls': 'notifications',
            '/api/supabase': 'settings',
            '/api/permission-schemes': 'settings',
            '/api/permission-groups': 'settings',
            '/api/permission-grants': 'settings',
            '/api/menu-visibility': 'settings',
            '/api/settings': 'settings',
            '/api/member': 'settings',
            '/api/cameras': 'cameras',
        }

        module_key = None
        for prefix, key in exact_route_map.items():
            if path == prefix or path.startswith(prefix + '/'):
                module_key = key
                break
        if not module_key:
            for prefix, key in prefix_route_map.items():
                if path == prefix or path.startswith(prefix + '/'):
                    module_key = key
                    break
        if not module_key:
            return None

        from .permissions import can_access_app_module, has_resource_permission
        if can_access_app_module(module_key) or has_resource_permission('app_module', resource_key=module_key, action='read'):
            return None

        if path.startswith('/api/'):
            return {'ok': False, 'error': 'Access denied by permission scheme'}, 403
        if path == '/':
            return 'Access denied by permission scheme', 403
        return redirect(url_for('main.index'))

    @app.context_processor
    def inject_auth_state():
        from .utils import get_member_avatar_map
        from .permissions import get_app_module_access_map
        avatar_map = get_member_avatar_map()
        current_member = session.get('current_member', None)
        asset_version = os.environ.get('SW_CACHE_VERSION') or '1'
        return {
            'is_authed': bool(session.get('authed')),
            'current_member': current_member,
            'current_member_avatar': avatar_map.get(current_member or '', ''),
            'avatar_map': avatar_map,
            'config': app.config['HOMEHUB_CONFIG'],
            'asset_version': asset_version,
            'app_module_access': get_app_module_access_map(current_member),
        }
    
    # Add Jinja2 filter for JSON parsing
    @app.template_filter('from_json')
    def from_json_filter(s):
        import json
        try:
            return json.loads(s) if s else []
        except (ValueError, TypeError):
            return []

    return app
