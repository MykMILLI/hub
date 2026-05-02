from __future__ import annotations
import json
import os
from datetime import datetime, timedelta
from uuid import uuid4

from flask import render_template, request, jsonify, current_app, redirect, url_for, session, send_from_directory
from werkzeug.utils import secure_filename

from ..blueprints import main_bp
from ..models import ActivityLog, db
from ..security import sanitize_text, is_http_url


_TIMELINE_IMAGE_EXTS = {'jpg', 'jpeg', 'png', 'webp', 'gif'}


def _timeline_upload_dir() -> str:
    base_dir = os.path.abspath(os.path.join(os.path.dirname(__file__), '..', '..'))
    folder = os.path.join(base_dir, 'uploads', 'timeline')
    os.makedirs(folder, exist_ok=True)
    return folder


def _timeline_image_url(filename: str | None) -> str | None:
    if not filename:
        return None
    return url_for('main.uploaded_timeline_image', filename=filename)


def _timeline_actor_from_request(default: str = '') -> str:
    actor = sanitize_text(request.args.get('member', '') or '').strip()
    if not actor:
        payload = request.get_json(silent=True) or {}
        actor = sanitize_text(payload.get('user', '') or payload.get('member', '') or payload.get('actor', '')).strip()
    if not actor:
        actor = sanitize_text(session.get('current_member') or '').strip()
    if not actor:
        actor = default.strip()
    return actor


def _timeline_meta_dict(post: ActivityLog) -> dict:
    raw = post.meta_json
    if isinstance(raw, dict):
        return dict(raw)
    if not raw:
        return {}
    try:
        data = json.loads(raw)
        if isinstance(data, dict):
            return data
    except Exception:
        pass
    return {}


def _timeline_save_meta(post: ActivityLog, meta: dict) -> None:
    post.meta_json = json.dumps(meta, ensure_ascii=False)


def _timeline_interaction_payload(post: ActivityLog, viewer: str = '') -> dict:
    meta = _timeline_meta_dict(post)
    likes_raw = meta.get('likes') if isinstance(meta.get('likes'), list) else []
    likes = []
    seen = set()
    for item in likes_raw:
        name = sanitize_text(item or '').strip()
        if not name:
            continue
        key = name.lower()
        if key in seen:
            continue
        seen.add(key)
        likes.append(name)

    comments_raw = meta.get('comments') if isinstance(meta.get('comments'), list) else []
    comments = []
    for row in comments_raw:
        if not isinstance(row, dict):
            continue
        text = sanitize_text(row.get('text', '') or '').strip()
        author = sanitize_text(row.get('author', '') or '').strip()
        if not text or not author:
            continue
        comments.append({
            'id': sanitize_text(row.get('id', '') or '').strip() or f'c_{len(comments)+1}',
            'author': author,
            'text': text,
            'timestamp': sanitize_text(row.get('timestamp', '') or '').strip() or None,
        })

    shares = 0
    try:
        shares = int(meta.get('shares') or 0)
    except Exception:
        shares = 0
    if shares < 0:
        shares = 0

    viewer_key = viewer.strip().lower()
    liked_by_me = bool(viewer_key and any((x or '').strip().lower() == viewer_key for x in likes))
    return {
        'likes': likes,
        'likes_count': len(likes),
        'comments': comments,
        'comments_count': len(comments),
        'shares_count': shares,
        'liked_by_me': liked_by_me,
    }


def _serialize_timeline_post(row: ActivityLog, viewer: str = '') -> dict:
    meta = _timeline_meta_dict(row)
    payload = {
        'id': row.id,
        'timestamp': row.timestamp.isoformat() if row.timestamp else None,
        'actor': row.actor,
        'action': row.action,
        'summary': row.summary,
        'entity_type': row.entity_type,
        'entity_id': row.entity_id,
        'image_url': sanitize_text(meta.get('image_url') or '').strip(),
    }
    payload.update(_timeline_interaction_payload(row, viewer))
    return payload


@main_bp.route('/timeline', methods=['GET'])
def timeline_page():
    limit = request.args.get('limit', 50, type=int)
    limit = max(1, min(limit, 100))

    actor = sanitize_text(request.args.get('actor', '') or '').strip()
    start = sanitize_text(request.args.get('start', '') or '').strip()
    end = sanitize_text(request.args.get('end', '') or '').strip()

    q = ActivityLog.query.filter(ActivityLog.visibility == 'family')
    if actor:
        q = q.filter(ActivityLog.actor == actor)
    if start:
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d')
            q = q.filter(ActivityLog.timestamp >= start_dt)
        except Exception:
            pass
    if end:
        try:
            end_dt = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
            q = q.filter(ActivityLog.timestamp < end_dt)
        except Exception:
            pass

    posts = q.order_by(ActivityLog.timestamp.desc(), ActivityLog.id.desc()).limit(limit).all()
    actors = [row[0] for row in db.session.query(ActivityLog.actor).filter(ActivityLog.visibility == 'family').distinct().order_by(ActivityLog.actor.asc()).all() if row[0]]

    viewer = _timeline_actor_from_request()
    post_meta_map = {p.id: _timeline_interaction_payload(p, viewer) for p in posts}
    config = current_app.config['HOMEHUB_CONFIG']
    return render_template('timeline.html', config=config, posts=posts, actors=actors, active_actor=actor, active_start=start, active_end=end, post_meta_map=post_meta_map)


@main_bp.route('/api/timeline', methods=['GET'])
def api_timeline():
    limit = request.args.get('limit', 50, type=int)
    limit = max(1, min(limit, 100))

    before_id = request.args.get('before_id', type=int)
    actor = sanitize_text(request.args.get('actor', '') or '').strip()
    start = sanitize_text(request.args.get('start', '') or '').strip()
    end = sanitize_text(request.args.get('end', '') or '').strip()
    q = ActivityLog.query.filter(ActivityLog.visibility == 'family')
    if before_id:
        q = q.filter(ActivityLog.id < before_id)
    if actor:
        q = q.filter(ActivityLog.actor == actor)
    if start:
        try:
            start_dt = datetime.strptime(start, '%Y-%m-%d')
            q = q.filter(ActivityLog.timestamp >= start_dt)
        except Exception:
            pass
    if end:
        try:
            end_dt = datetime.strptime(end, '%Y-%m-%d') + timedelta(days=1)
            q = q.filter(ActivityLog.timestamp < end_dt)
        except Exception:
            pass

    rows = q.order_by(ActivityLog.timestamp.desc(), ActivityLog.id.desc()).limit(limit + 1).all()
    has_more = len(rows) > limit
    rows = rows[:limit]
    viewer = _timeline_actor_from_request()

    return jsonify({
        'ok': True,
        'has_more': has_more,
        'posts': [_serialize_timeline_post(r, viewer) for r in rows]
    })


@main_bp.route('/api/timeline', methods=['POST'])
def api_create_timeline_post():
    data = request.get_json(silent=True) or {}
    actor = sanitize_text(data.get('actor', '') or '').strip()
    summary = sanitize_text(data.get('summary', '') or '').strip()
    action = sanitize_text(data.get('action', '') or 'timeline.manual').strip() or 'timeline.manual'
    image_url = sanitize_text(data.get('image_url') or '').strip()

    if not actor:
        return jsonify({'ok': False, 'error': 'Actor is required'}), 400
    if not summary:
        return jsonify({'ok': False, 'error': 'Post text is required'}), 400
    if image_url and not is_http_url(image_url):
        return jsonify({'ok': False, 'error': 'Image URL must be http or https'}), 400

    post = ActivityLog(
        actor=actor,
        action=action,
        summary=summary,
        entity_type='timeline',
        entity_id=None,
        visibility='family',
    )
    db.session.add(post)
    if image_url:
        _timeline_save_meta(post, {'image_url': image_url})
    db.session.commit()

    return jsonify({'ok': True, 'post': _serialize_timeline_post(post, actor)})


@main_bp.route('/api/timeline/<int:post_id>/interactions', methods=['GET'])
def api_timeline_interactions(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    viewer = _timeline_actor_from_request()
    return jsonify({'ok': True, 'interactions': _timeline_interaction_payload(post, viewer)})


@main_bp.route('/api/timeline/<int:post_id>/like', methods=['POST'])
def api_timeline_like(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    user = _timeline_actor_from_request()
    if not user:
        return jsonify({'ok': False, 'error': 'User is required'}), 400

    meta = _timeline_meta_dict(post)
    likes = [sanitize_text(x or '').strip() for x in (meta.get('likes') if isinstance(meta.get('likes'), list) else [])]
    likes = [x for x in likes if x]
    lower = {x.lower() for x in likes}
    if user.lower() in lower:
        likes = [x for x in likes if x.lower() != user.lower()]
        liked = False
    else:
        likes.append(user)
        liked = True

    meta['likes'] = likes
    _timeline_save_meta(post, meta)
    db.session.add(post)
    db.session.commit()
    return jsonify({'ok': True, 'liked': liked, 'interactions': _timeline_interaction_payload(post, user)})


@main_bp.route('/api/timeline/<int:post_id>/comment', methods=['POST'])
def api_timeline_comment(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    user = _timeline_actor_from_request()
    if not user:
        return jsonify({'ok': False, 'error': 'User is required'}), 400

    data = request.get_json(silent=True) or {}
    text = sanitize_text(data.get('text', '') or '').strip()
    if not text:
        return jsonify({'ok': False, 'error': 'Comment text is required'}), 400
    if len(text) > 400:
        text = text[:400]

    meta = _timeline_meta_dict(post)
    comments = meta.get('comments') if isinstance(meta.get('comments'), list) else []
    now_iso = datetime.utcnow().isoformat()
    comments.append({
        'id': f"c_{int(datetime.utcnow().timestamp() * 1000)}_{len(comments) + 1}",
        'author': user,
        'text': text,
        'timestamp': now_iso,
    })
    if len(comments) > 200:
        comments = comments[-200:]
    meta['comments'] = comments

    _timeline_save_meta(post, meta)
    db.session.add(post)
    db.session.commit()
    return jsonify({'ok': True, 'interactions': _timeline_interaction_payload(post, user)})


@main_bp.route('/api/timeline/<int:post_id>/share', methods=['POST'])
def api_timeline_share(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    user = _timeline_actor_from_request()
    meta = _timeline_meta_dict(post)
    shares = 0
    try:
        shares = int(meta.get('shares') or 0)
    except Exception:
        shares = 0
    meta['shares'] = max(0, shares) + 1
    _timeline_save_meta(post, meta)
    db.session.add(post)
    db.session.commit()
    return jsonify({'ok': True, 'interactions': _timeline_interaction_payload(post, user)})


def _can_delete_timeline_post(user: str, post: ActivityLog) -> bool:
    admin_name = current_app.config['HOMEHUB_CONFIG'].get('admin_name', 'Administrator')
    admin_aliases = {admin_name, 'Administrator', 'admin'}
    return user in admin_aliases or user == (post.actor or '')


@main_bp.route('/api/timeline/upload-image', methods=['POST'])
def api_upload_timeline_image():
    user = sanitize_text(request.form.get('user') or request.form.get('member') or request.form.get('actor') or '').strip()
    if not user:
        user = sanitize_text(session.get('current_member') or '').strip()
    if not user:
        return jsonify({'ok': False, 'error': 'User is required'}), 400

    if 'image' not in request.files:
        return jsonify({'ok': False, 'error': 'Image is required'}), 400
    image = request.files['image']
    if not image or not image.filename:
        return jsonify({'ok': False, 'error': 'Image is required'}), 400

    filename = secure_filename(image.filename)
    ext = filename.rsplit('.', 1)[-1].lower() if '.' in filename else ''
    if ext not in _TIMELINE_IMAGE_EXTS:
        return jsonify({'ok': False, 'error': 'Unsupported image format'}), 400

    request_max = request.content_length or 0
    if request_max > 8 * 1024 * 1024:
        return jsonify({'ok': False, 'error': 'Image too large (max 8MB)'}), 400

    stored = f"timeline-{datetime.utcnow().strftime('%Y%m%d%H%M%S')}-{uuid4().hex[:8]}.{ext}"
    image.save(os.path.join(_timeline_upload_dir(), stored))
    return jsonify({'ok': True, 'filename': stored, 'url': _timeline_image_url(stored)})


@main_bp.route('/uploads/timeline/<path:filename>')
def uploaded_timeline_image(filename: str):
    return send_from_directory(_timeline_upload_dir(), filename, as_attachment=False)


@main_bp.route('/api/timeline/<int:post_id>/image', methods=['PUT'])
def api_update_timeline_post_image(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user') or data.get('member') or data.get('actor') or '').strip()
    if not _can_delete_timeline_post(user, post):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    image_url = sanitize_text(data.get('image_url') or '').strip()
    if image_url and not is_http_url(image_url):
        return jsonify({'ok': False, 'error': 'Image URL must be http or https'}), 400

    meta = _timeline_meta_dict(post)
    if image_url:
        meta['image_url'] = image_url
    else:
        meta.pop('image_url', None)

    _timeline_save_meta(post, meta)
    db.session.add(post)
    db.session.commit()
    return jsonify({'ok': True, 'post': _serialize_timeline_post(post, user)})


@main_bp.route('/timeline/delete/<int:post_id>', methods=['POST'])
def delete_timeline_post(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    user = sanitize_text(request.form.get('user', '') or '')
    if not _can_delete_timeline_post(user, post):
        return redirect(url_for('main.timeline_page'))

    db.session.delete(post)
    db.session.commit()
    return redirect(url_for('main.timeline_page'))


@main_bp.route('/api/timeline/<int:post_id>', methods=['DELETE'])
def api_delete_timeline_post(post_id: int):
    post = ActivityLog.query.get_or_404(post_id)
    data = request.get_json(silent=True) or {}
    user = sanitize_text(data.get('user', '') or '')
    if not _can_delete_timeline_post(user, post):
        return jsonify({'ok': False, 'error': 'Not allowed'}), 403

    db.session.delete(post)
    db.session.commit()
    return jsonify({'ok': True, 'message': 'Timeline post deleted'})
