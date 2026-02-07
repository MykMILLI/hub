from flask import Flask, session
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
    media_dir = os.path.join(base_dir, 'media')
    pdfs_dir = os.path.join(base_dir, 'pdfs')
    os.makedirs(data_dir, exist_ok=True)
    os.makedirs(uploads_dir, exist_ok=True)
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
                ensure_column('family_member', 'pin', 'TEXT', None)
                ensure_column('family_member', 'status', 'TEXT', 'home')
                ensure_column('family_member', 'status_updated_at', 'TIMESTAMP', None)
                ensure_column('family_member', 'avatar', 'TEXT', None)
                ensure_column(_RecurringExpense.__tablename__, 'monthly_mode', 'TEXT', 'day_of_month')
                ensure_column(_RecurringExpense.__tablename__, 'category', 'TEXT', None)
                ensure_column(_RecurringExpense.__tablename__, 'effective_from', 'DATE', None)
                ensure_column(_QRCode.__tablename__, 'original_input', 'TEXT', None)
                ensure_column(_Reminder.__tablename__, 'recurring_id', 'INTEGER', None)
                ensure_column('recipe', 'tags', 'TEXT', '[]')
                ensure_column('file', 'category', 'TEXT', 'uncategorized')
                ensure_column('shopping_item', 'tags', 'TEXT', '[]')
                ensure_column('shopping_item', 'price', 'REAL', None)
                ensure_column('shopping_item', 'quantity', 'REAL', 1.0)
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
                
                conn.close()
                print("[SCHEMA] Migration complete")
            except Exception as e:
                print(f"[SCHEMA FATAL] Database schema migration failed: {e}")
                import traceback
                traceback.print_exc()
                # Don't silently fail - this is critical

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
    app.register_blueprint(main_bp)
    app.register_blueprint(money.money_bp)

    @app.context_processor
    def inject_auth_state():
        return {
            'is_authed': bool(session.get('authed')),
            'current_member': session.get('current_member', None),
            'config': app.config['HOMEHUB_CONFIG']
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
