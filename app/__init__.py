from flask import Flask, session, request, redirect, url_for
from flask_sqlalchemy import SQLAlchemy
from .config import load_config
import os
import secrets

db = SQLAlchemy()


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
    # Generate a strong SECRET_KEY if not provided via env
    secret = os.environ.get('SECRET_KEY')
    if not secret:
        import secrets as _secrets
        secret = _secrets.token_hex(32)
    app.config['SECRET_KEY'] = secret
    # Explicitly disable CSRF (forms are simple and app runs on home network)
    app.config['WTF_CSRF_ENABLED'] = False

    # Load config.yml
    app.config['HOMEHUB_CONFIG'] = load_config()

    # Allow tests to override configuration (database, testing flag, etc.)
    if test_config:
        app.config.update(test_config)

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
                ensure_column('family_member', 'avatar', 'TEXT', None)
                ensure_column('family_member', 'birthday', 'DATE', None)
                ensure_column('family_member', 'favorite_color', 'TEXT', None)
                ensure_column('family_member', 'favorite_song', 'TEXT', None)
                ensure_column('family_member', 'amazon_wishlist_url', 'TEXT', None)
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
                ensure_column('note', 'visibility', 'TEXT', 'family')
                ensure_column('note', 'allowed_members_json', 'TEXT', '[]')
                ensure_column('note', 'require_pin', 'INTEGER', 0)
                ensure_column('note', 'title', 'TEXT', '')
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
    from .blueprints import meal_planner  # noqa: F401
    from .blueprints import notifications  # noqa: F401
    from .blueprints import communication  # noqa: F401
    from .blueprints import photos  # noqa: F401
    app.register_blueprint(main_bp)
    app.register_blueprint(money.money_bp)

    @app.before_request
    def enforce_module_permissions():
        path = (request.path or '').rstrip('/') or '/'
        if path.startswith('/static/') or path.startswith('/uploads/') or path.startswith('/sw.js') or path.startswith('/manifest.webmanifest'):
            return None
        if path.startswith('/shared/project/') or path.startswith('/shared/space/'):
            return None
        if path.startswith('/calendar/share/'):
            return None
        if path.startswith('/api/calendar/shared/'):
            return None
        if path.startswith('/api/calendar/ics/'):
            return None
        if path.startswith('/login') or path.startswith('/logout'):
            return None
        if path.startswith('/api/auth/') or path.startswith('/api/member-login') or path.startswith('/api/member/set-pin') or path.startswith('/api/member-logout') or path.startswith('/api/current-member'):
            return None

        # Mobile browsers can land without a bound member session even when local UI state exists.
        # Do not force-redirect module routes in that state; let route-level behavior/auth handle it.
        if not session.get('current_member'):
            return None

        exact_route_map = {
            '/': 'welcome',
            '/timeline': 'timeline',
            '/notes': 'shared_notes',
            '/projects': 'shared_notes',
            '/spaces': 'shared_notes',
            '/upload': 'shared_cloud',
            '/shopping': 'shopping_list',
            '/chores': 'chores',
            '/recipes': 'recipes',
            '/expiry': 'expiry_tracker',
            '/shorten': 'url_shortener',
            '/media': 'media_downloader',
            '/pdfs': 'pdf_compressor',
            '/qr': 'qr_generator',
            '/expenses': 'expense_tracker',
            '/calendar': 'shared_calendar',
            '/meal-planner': 'meal_planner',
            '/photos': 'photos',
            '/notifications': 'notifications',
            '/messages': 'notifications',
            '/polls': 'notifications',
            '/settings': 'settings',
        }

        prefix_route_map = {
            '/api/workspaces': 'shared_notes',
            '/api/expenses': 'expense_tracker',
            '/api/money': 'expense_tracker',
            '/money': 'expense_tracker',
            '/api/calendar': 'shared_calendar',
            '/api/meal-planner': 'meal_planner',
            '/api/photos': 'photos',
            '/api/notifications': 'notifications',
            '/api/calls': 'notifications',
            '/api/messages': 'notifications',
            '/api/polls': 'notifications',
            '/api/permission-schemes': 'settings',
            '/api/permission-groups': 'settings',
            '/api/permission-grants': 'settings',
            '/api/menu-visibility': 'settings',
            '/api/settings': 'settings',
            '/api/member': 'settings',
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
