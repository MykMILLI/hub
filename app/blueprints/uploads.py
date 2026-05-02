import os
from flask import render_template, request, redirect, url_for, send_from_directory, current_app, make_response, jsonify, session
from werkzeug.utils import secure_filename
from ..models import db, File
from ..blueprints import main_bp
from ..security import sanitize_text
from ..models import FamilyMember
from ..services.notification_service import publish_notification


BASE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
UPLOAD_FOLDER = os.path.join(BASE_DIR, 'uploads')

# File categorization by extension
FILE_CATEGORIES = {
    'photos_videos': {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.ico', '.svg', '.mp4', '.webm', '.avi', '.mov', '.mkv', '.flv'},
    'documents': {'.pdf', '.txt', '.docx', '.doc', '.xlsx', '.xls', '.pptx', '.ppt', '.odt', '.ods', '.odp'},
    'audio': {'.mp3', '.wav', '.ogg', '.flac', '.aac', '.m4a', '.wma'},
    'archives': {'.zip', '.rar', '.7z', '.tar', '.gz', '.bz2'},
    'other': set()
}

def get_file_category(filename):
    """Determine file category based on extension"""
    ext = os.path.splitext(filename)[1].lower()
    for category, extensions in FILE_CATEGORIES.items():
        if ext in extensions:
            return category
    return 'other'


def _current_member_name() -> str:
    raw = sanitize_text(session.get('current_member') or '').strip()
    if raw:
        return raw
    return sanitize_text(current_app.config.get('HOMEHUB_CONFIG', {}).get('admin_name', 'Administrator')).strip() or 'Administrator'


@main_bp.route('/upload', methods=['GET', 'POST'])
def upload():
    if request.method == 'POST':
        files = request.files.getlist('files') or ([request.files['file']] if 'file' in request.files else [])
        creator = sanitize_text(request.form['creator'])
        uploaded_files = []
        for file in files:
            if not file or not getattr(file, 'filename', ''):
                continue
            filename = secure_filename(file.filename)
            file.save(os.path.join(UPLOAD_FOLDER, filename))
            category = get_file_category(filename)
            db_file = File(filename=filename, creator=creator, category=category)
            db.session.add(db_file)
            uploaded_files.append(db_file)
        db.session.commit()

        if uploaded_files:
            count = len(uploaded_files)
            first_name = uploaded_files[0].filename
            targets = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all() if m.name and m.name != creator]
            publish_notification(
                event_type='shared_cloud_add',
                title='New shared cloud upload',
                message=f"{creator} uploaded {count} file{'s' if count != 1 else ''} to Shared Cloud ({first_name}{'...' if count > 1 else ''}).",
                target_members=targets,
                created_by=creator,
                payload={'count': count},
                channels=['in_app'],
            )
        return redirect(url_for('main.upload'))
    files = File.query.order_by(File.upload_time.desc()).all()
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('upload.html', files=files, config=config, current_member_name=_current_member_name())


@main_bp.route('/uploads/<filename>')
def uploaded_file(filename):
    return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)


@main_bp.route('/uploads/preview/<filename>')
def preview_file(filename):
    """Serve file for preview (inline) - restricted to safe types only"""
    # Only allow preview for known-safe file types to prevent XSS attacks
    # HTML, SVG, XML and other executable content must be downloaded, not previewed
    safe_extensions = {'.jpg', '.jpeg', '.png', '.gif', '.webp', '.bmp', '.ico','.pdf', '.txt', '.docx', '.xlsx', '.pptx', '.odt', '.ods', '.odp','.mp4', '.mp3', '.wav', '.ogg', '.webm'}
    ext = os.path.splitext(filename)[1].lower()
    
    if ext not in safe_extensions:
        # Force download for potentially dangerous files (HTML, SVG, JS, etc.)
        return send_from_directory(UPLOAD_FOLDER, filename, as_attachment=True)
    
    # Add security headers to prevent script execution even for allowed types
    response = make_response(send_from_directory(UPLOAD_FOLDER, filename, as_attachment=False))
    response.headers['Content-Security-Policy'] = "default-src 'none'; style-src 'unsafe-inline'; img-src 'self'; media-src 'self'"
    response.headers['X-Content-Type-Options'] = 'nosniff'
    response.headers['X-Frame-Options'] = 'DENY'
    return response


@main_bp.route('/api/files/move/<int:file_id>', methods=['POST'])
def move_file(file_id):
    """Move file to a different category"""
    db_file = File.query.get_or_404(file_id)
    data = request.get_json() or {}
    new_category = data.get('category', '').strip()
    
    if new_category not in FILE_CATEGORIES:
        return jsonify({'ok': False, 'error': 'Invalid category'}), 400
    
    db_file.category = new_category
    db.session.commit()
    return jsonify({'ok': True, 'category': new_category})


@main_bp.route('/api/files', methods=['GET'])
def api_files_list():
    rows = File.query.order_by(File.upload_time.desc()).all()
    return jsonify({
        'ok': True,
        'items': [
            {
                'id': row.id,
                'filename': row.filename,
                'creator': row.creator,
                'category': row.category or get_file_category(row.filename or ''),
                'upload_time': row.upload_time.isoformat() if row.upload_time else None,
                'download_url': f"/uploads/{row.filename}",
                'preview_url': f"/uploads/preview/{row.filename}",
            }
            for row in rows
        ],
    })


@main_bp.route('/api/files/upload', methods=['POST'])
def api_files_upload():
    files = request.files.getlist('files') or ([request.files['file']] if 'file' in request.files else [])
    creator = sanitize_text(request.form.get('creator') or '').strip()
    if not creator:
        return jsonify({'ok': False, 'error': 'Creator is required'}), 400
    if not files:
        return jsonify({'ok': False, 'error': 'No files uploaded'}), 400

    uploaded_items = []
    for file in files:
        if not file or not getattr(file, 'filename', ''):
            continue
        filename = secure_filename(file.filename)
        file.save(os.path.join(UPLOAD_FOLDER, filename))
        category = get_file_category(filename)
        db_file = File(filename=filename, creator=creator, category=category)
        db.session.add(db_file)
        uploaded_items.append(db_file)

    db.session.commit()

    if uploaded_items:
        count = len(uploaded_items)
        first_name = uploaded_items[0].filename
        targets = [m.name for m in FamilyMember.query.order_by(FamilyMember.name.asc()).all() if m.name and m.name != creator]
        publish_notification(
            event_type='shared_cloud_add',
            title='New shared cloud upload',
            message=f"{creator} uploaded {count} file{'s' if count != 1 else ''} to Shared Cloud ({first_name}{'...' if count > 1 else ''}).",
            target_members=targets,
            created_by=creator,
            payload={'count': count},
            channels=['in_app'],
        )

    return jsonify({
        'ok': True,
        'uploaded_count': len(uploaded_items),
        'items': [
            {
                'id': row.id,
                'filename': row.filename,
                'creator': row.creator,
                'category': row.category,
                'upload_time': row.upload_time.isoformat() if row.upload_time else None,
                'download_url': f"/uploads/{row.filename}",
                'preview_url': f"/uploads/preview/{row.filename}",
            }
            for row in uploaded_items
        ],
    })


@main_bp.route('/api/files/<int:file_id>', methods=['DELETE'])
def api_delete_file(file_id: int):
    db_file = File.query.get_or_404(file_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user') or '').strip()
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user not in admin_aliases and user != db_file.creator:
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    try:
        os.remove(os.path.join(UPLOAD_FOLDER, db_file.filename))
    except Exception:
        pass

    db.session.delete(db_file)
    db.session.commit()
    return jsonify({'ok': True, 'message': 'File deleted'})

@main_bp.route('/upload/delete/<int:file_id>', methods=['POST'])
def delete_file(file_id):
    db_file = File.query.get_or_404(file_id)
    user = sanitize_text(request.form['user'])
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    if user in admin_aliases or user == db_file.creator:
        try:
            os.remove(os.path.join(UPLOAD_FOLDER, db_file.filename))
        except Exception:
            pass
        db.session.delete(db_file)
        db.session.commit()
    return redirect(url_for('main.upload'))
