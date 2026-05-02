from __future__ import annotations

import json
from typing import Any

from flask import current_app

from . import db
from .models import ActivityLog
from .security import sanitize_text


def log_activity(
    *,
    actor: str,
    action: str,
    summary: str,
    entity_type: str | None = None,
    entity_id: int | None = None,
    visibility: str = "family",
    metadata: dict[str, Any] | None = None,
) -> None:
    """Best-effort activity logger.

    Only events with visibility='family' should be displayed on the shared timeline.
    """
    try:
        actor_clean = sanitize_text(actor or "")
        action_clean = sanitize_text(action or "")
        summary_clean = sanitize_text(summary or "")
        visibility_clean = sanitize_text(visibility or "family") or "family"

        md = None
        if metadata is not None:
            try:
                md = json.dumps(metadata, ensure_ascii=False)
            except Exception:
                md = None

        row = ActivityLog(
            title=(summary_clean[:256] if summary_clean else action_clean[:256]),
            author=actor_clean,
            actor=actor_clean,
            action=action_clean,
            summary=summary_clean,
            entity_type=sanitize_text(entity_type or "") or None,
            entity_id=int(entity_id) if entity_id is not None else None,
            visibility=visibility_clean,
            meta_json=md,
        )
        db.session.add(row)
        db.session.commit()
    except Exception:
        # Never break core app flows if timeline logging fails.
        try:
            db.session.rollback()
        except Exception:
            pass
        try:
            current_app.logger.exception("Activity log write failed")
        except Exception:
            pass
