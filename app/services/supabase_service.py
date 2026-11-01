from __future__ import annotations

from dataclasses import dataclass
import re
import secrets
from typing import Any
from urllib.parse import urlparse

import requests


def _normalize_supabase_url(raw: str) -> str:
    value = (raw or '').strip()
    if value.startswith('http:/') and not value.startswith('http://'):
        value = 'http://' + value[len('http:/'):]
    elif value.startswith('https:/') and not value.startswith('https://'):
        value = 'https://' + value[len('https:/'):]
    return value.rstrip('/')


def _split_supabase_url_candidates(raw: str) -> list[str]:
    parts = [
        _normalize_supabase_url(chunk)
        for chunk in str(raw or '').split(',')
    ]
    out: list[str] = []
    seen: set[str] = set()
    for part in parts:
        if not part:
            continue
        if part in seen:
            continue
        seen.add(part)
        out.append(part)
    return out


def _looks_like_valid_supabase_base_url(raw: str) -> bool:
    value = _normalize_supabase_url(raw)
    if not value:
        return False
    try:
        parsed = urlparse(value)
    except Exception:
        return False
    if parsed.scheme not in {'http', 'https'}:
        return False
    if not parsed.netloc:
        return False

    normalized_path = re.sub(r'/+', '/', (parsed.path or '').strip())
    if normalized_path in {'/auth/v1', '/rest/v1'}:
        return False
    return True


@dataclass
class SupabaseProbeResult:
    name: str
    url: str
    reachable: bool
    status_code: int | None
    error: str | None = None


class SupabaseService:
    def __init__(self):
        self._enabled = False
        self._auth_enabled = False
        self._raw_url = ''
        self._url_candidates: list[str] = []
        self._active_url = ''
        self._anon_key = ''
        self._service_role_key = ''
        self._schema = 'public'
        self._timeout_seconds = 8
        self._chat_enabled = True
        self._chat_table = 'homehub_chat_messages'

    def init_app(self, app):
        cfg = app.config.get('HOMEHUB_CONFIG') or {}
        supa_cfg = cfg.get('supabase') if isinstance(cfg, dict) else {}
        if not isinstance(supa_cfg, dict):
            supa_cfg = {}

        self._enabled = bool(supa_cfg.get('enabled', False))
        self._auth_enabled = bool(supa_cfg.get('auth_enabled', False))
        self._raw_url = str(supa_cfg.get('url') or '')
        self._url_candidates = [
            url for url in _split_supabase_url_candidates(self._raw_url)
            if _looks_like_valid_supabase_base_url(url)
        ]
        self._active_url = self._url_candidates[0] if self._url_candidates else ''
        self._anon_key = str(supa_cfg.get('anon_key') or '').strip()
        self._service_role_key = str(supa_cfg.get('service_role_key') or '').strip()
        self._schema = str(supa_cfg.get('schema') or 'public').strip() or 'public'
        self._chat_enabled = bool(supa_cfg.get('chat_enabled', True))
        chat_table_raw = str(supa_cfg.get('chat_table') or 'homehub_chat_messages').strip().lower()
        if re.match(r'^[a-z0-9_]+$', chat_table_raw):
            self._chat_table = chat_table_raw
        else:
            self._chat_table = 'homehub_chat_messages'

        timeout_raw = str(supa_cfg.get('timeout_seconds') or '').strip()
        if timeout_raw:
            try:
                self._timeout_seconds = max(2, min(30, int(timeout_raw)))
            except Exception:
                self._timeout_seconds = 8

        # Environment values are already merged in app.config.load_config, but we
        # keep this service defensive and self-contained if called in isolation.
        if (not self._enabled) and self._url_candidates and self._anon_key:
            self._enabled = True
        if (not self._auth_enabled) and self._enabled and self._url_candidates and self._anon_key:
            self._auth_enabled = True

    @property
    def enabled(self) -> bool:
        return bool(self._enabled)

    @property
    def auth_enabled(self) -> bool:
        return bool(self._auth_enabled and self._enabled)

    @property
    def url(self) -> str:
        return self._active_url or (self._url_candidates[0] if self._url_candidates else '')

    @property
    def urls(self) -> list[str]:
        return list(self._url_candidates)

    @property
    def schema(self) -> str:
        return self._schema

    def is_configured(self) -> bool:
        return self.enabled and bool(self._anon_key) and bool(self._url_candidates)

    def configuration_issue(self) -> str | None:
        if not self.enabled:
            return 'Supabase integration is disabled.'
        if not str(self._raw_url or '').strip() or not self._anon_key:
            return 'Missing SUPABASE_URL and/or SUPABASE_ANON_KEY.'
        if not self._url_candidates:
            return 'SUPABASE_URL is invalid. Use one or more project root URLs separated by commas (no /auth/v1 or /rest/v1 suffix).'
        return None

    def status(self) -> dict:
        selected = self.url
        return {
            'enabled': self.enabled,
            'auth_enabled': self.auth_enabled,
            'configured': self.is_configured(),
            'url': self._public_url_summary(selected),
            'urls': [self._public_url_summary(value) for value in self._url_candidates],
            'schema': self._schema,
            'anon_key_present': bool(self._anon_key),
            'service_role_key_present': bool(self._service_role_key),
            'chat_store_enabled': self.chat_store_enabled(),
            'chat_table': self._chat_table,
            'realtime_url': f"{selected}/realtime/v1" if selected else '',
            'configuration_issue': self.configuration_issue(),
        }

    def client_config(self) -> dict:
        summary = self.status()
        return {
            'enabled': summary.get('enabled', False),
            'auth_enabled': summary.get('auth_enabled', False),
            'configured': summary.get('configured', False),
            'url': summary.get('url', ''),
            'urls': summary.get('urls', []),
            # Anon key is intended for public clients.
            'anon_key': self._anon_key if summary.get('configured') else '',
            'schema': summary.get('schema', 'public'),
            'chat_store_enabled': summary.get('chat_store_enabled', False),
            'chat_table': summary.get('chat_table', 'homehub_chat_messages'),
            'configuration_issue': summary.get('configuration_issue'),
        }

    def _public_url_summary(self, raw_url: str) -> str:
        value = _normalize_supabase_url(raw_url)
        if not value:
            return ''
        try:
            parsed = urlparse(value)
        except Exception:
            return ''
        host = parsed.hostname or ''
        if parsed.port:
            host = f"{host}:{parsed.port}"
        path = (parsed.path or '').rstrip('/')
        return f"{parsed.scheme}://{host}{path}"

    def _probe_get(self, name: str, url: str, headers: dict[str, str] | None = None) -> SupabaseProbeResult:
        try:
            response = requests.get(
                url,
                headers=headers or {},
                timeout=self._timeout_seconds,
            )
            code = int(response.status_code)
            # 2xx/3xx/4xx indicate reachability; hard network errors are failures.
            reachable = code < 500
            return SupabaseProbeResult(
                name=name,
                url=url,
                reachable=reachable,
                status_code=code,
            )
        except requests.RequestException as ex:
            return SupabaseProbeResult(
                name=name,
                url=url,
                reachable=False,
                status_code=None,
                error=str(ex),
            )

    def _build_candidate_probes(self, base_url: str) -> list[SupabaseProbeResult]:
        return [
            self._probe_get(
                name='auth_settings',
                url=f"{base_url}/auth/v1/settings",
                headers={'apikey': self._anon_key},
            ),
            self._probe_get(
                name='rest_root',
                url=f"{base_url}/rest/v1/",
                headers={
                    'apikey': self._anon_key,
                    'Authorization': f"Bearer {self._anon_key}",
                },
            ),
            self._probe_get(
                name='realtime_health',
                url=f"{base_url}/realtime/v1/",
                headers={'apikey': self._anon_key},
            ),
        ]

    def _auth_headers(self, access_token: str | None = None) -> dict[str, str]:
        headers = {
            'apikey': self._anon_key,
            'Content-Type': 'application/json',
        }
        if access_token:
            headers['Authorization'] = f"Bearer {access_token}"
        return headers

    def _admin_headers(self) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
        }
        if self._service_role_key:
            headers['apikey'] = self._service_role_key
            headers['Authorization'] = f"Bearer {self._service_role_key}"
        return headers

    def _admin_ready(self) -> bool:
        return bool(self.auth_enabled and self.is_configured() and self._service_role_key)

    def chat_store_enabled(self) -> bool:
        return bool(self._chat_enabled and self.enabled and self.is_configured() and self._service_role_key)

    def _chat_headers(self, *, return_representation: bool = False) -> dict[str, str]:
        headers = {
            'Content-Type': 'application/json',
            'apikey': self._service_role_key,
            'Authorization': f"Bearer {self._service_role_key}",
        }
        if return_representation:
            headers['Prefer'] = 'return=representation'
        return headers

    def _chat_request(
        self,
        method: str,
        *,
        params: dict[str, Any] | None = None,
        json_body: dict[str, Any] | None = None,
        return_representation: bool = False,
    ) -> tuple[bool, dict[str, Any]]:
        if not self.chat_store_enabled():
            return False, {'error': 'Supabase chat store disabled'}

        last_error = 'Supabase chat request failed.'
        for base_url in self._url_candidates:
            endpoint = f"{base_url}/rest/v1/{self._chat_table}"
            try:
                response = requests.request(
                    method=method,
                    url=endpoint,
                    headers=self._chat_headers(return_representation=return_representation),
                    params=params,
                    json=json_body,
                    timeout=self._timeout_seconds,
                )
            except requests.RequestException as ex:
                last_error = str(ex)
                continue

            payload: Any = None
            try:
                payload = response.json() if response.content else None
            except Exception:
                payload = None

            if response.status_code >= 200 and response.status_code < 300:
                self._active_url = base_url
                return True, {'data': payload}

            if isinstance(payload, dict):
                last_error = str(payload.get('message') or payload.get('error') or payload.get('hint') or last_error)
            else:
                last_error = f"Supabase chat request failed ({response.status_code})"

        return False, {'error': last_error}

    def chat_create_message(
        self,
        *,
        sender: str,
        recipient: str,
        message: str,
        attachment_filename: str | None,
        attachment_mime: str | None,
    ) -> dict[str, Any]:
        payload = {
            'sender': str(sender or '').strip(),
            'recipient': str(recipient or '').strip(),
            'message': str(message or ''),
            'attachment_filename': str(attachment_filename or '').strip() or None,
            'attachment_mime': str(attachment_mime or '').strip() or None,
            'is_read': False,
        }
        ok, out = self._chat_request('POST', json_body=payload, return_representation=True)
        if not ok:
            return {'ok': False, 'error': out.get('error') or 'Unable to write chat message'}
        rows = out.get('data') if isinstance(out.get('data'), list) else []
        row = rows[0] if rows else {}
        return {'ok': True, 'row': row}

    def chat_fetch_thread(self, *, member: str, other_member: str, limit: int = 300) -> dict[str, Any]:
        max_rows = max(1, min(int(limit or 300), 500))

        params_a = {
            'select': '*',
            'sender': f'eq.{member}',
            'recipient': f'eq.{other_member}',
            'order': 'created_at.asc,id.asc',
            'limit': max_rows,
        }
        params_b = {
            'select': '*',
            'sender': f'eq.{other_member}',
            'recipient': f'eq.{member}',
            'order': 'created_at.asc,id.asc',
            'limit': max_rows,
        }

        ok_a, out_a = self._chat_request('GET', params=params_a)
        ok_b, out_b = self._chat_request('GET', params=params_b)
        if not ok_a and not ok_b:
            return {'ok': False, 'error': out_a.get('error') or out_b.get('error') or 'Unable to read thread'}

        rows_a = out_a.get('data') if ok_a and isinstance(out_a.get('data'), list) else []
        rows_b = out_b.get('data') if ok_b and isinstance(out_b.get('data'), list) else []
        combined = rows_a + rows_b

        deduped: dict[str, dict[str, Any]] = {}
        for row in combined:
            if not isinstance(row, dict):
                continue
            key = str(row.get('id') or f"{row.get('sender')}:{row.get('recipient')}:{row.get('created_at')}")
            deduped[key] = row

        def _sort_id(row: dict[str, Any]) -> int:
            try:
                return int(row.get('id') or 0)
            except Exception:
                return 0

        rows = list(deduped.values())
        rows.sort(key=lambda r: (str(r.get('created_at') or ''), _sort_id(r)))
        rows = rows[-max_rows:]
        return {'ok': True, 'rows': rows}

    def chat_fetch_member_messages(self, *, member: str, limit: int = 500) -> dict[str, Any]:
        max_rows = max(1, min(int(limit or 500), 1000))

        params_sent = {
            'select': '*',
            'sender': f'eq.{member}',
            'order': 'created_at.desc,id.desc',
            'limit': max_rows,
        }
        params_received = {
            'select': '*',
            'recipient': f'eq.{member}',
            'order': 'created_at.desc,id.desc',
            'limit': max_rows,
        }

        ok_sent, out_sent = self._chat_request('GET', params=params_sent)
        ok_received, out_received = self._chat_request('GET', params=params_received)
        if not ok_sent and not ok_received:
            return {'ok': False, 'error': out_sent.get('error') or out_received.get('error') or 'Unable to read member messages'}

        rows_sent = out_sent.get('data') if ok_sent and isinstance(out_sent.get('data'), list) else []
        rows_received = out_received.get('data') if ok_received and isinstance(out_received.get('data'), list) else []
        combined = rows_sent + rows_received

        deduped: dict[str, dict[str, Any]] = {}
        for row in combined:
            if not isinstance(row, dict):
                continue
            key = str(row.get('id') or f"{row.get('sender')}:{row.get('recipient')}:{row.get('created_at')}")
            deduped[key] = row

        def _sort_id(row: dict[str, Any]) -> int:
            try:
                return int(row.get('id') or 0)
            except Exception:
                return 0

        rows = list(deduped.values())
        rows.sort(key=lambda r: (str(r.get('created_at') or ''), _sort_id(r)), reverse=True)
        rows = rows[:max_rows]
        return {'ok': True, 'rows': rows}

    def chat_mark_thread_read(self, *, member: str, other_member: str) -> dict[str, Any]:
        params = {
            'sender': f'eq.{other_member}',
            'recipient': f'eq.{member}',
            'is_read': 'eq.false',
        }
        ok, out = self._chat_request(
            'PATCH',
            params=params,
            json_body={'is_read': True},
            return_representation=True,
        )
        if not ok:
            return {'ok': False, 'error': out.get('error') or 'Unable to mark thread as read'}
        rows = out.get('data') if isinstance(out.get('data'), list) else []
        return {'ok': True, 'count': len(rows)}

    def chat_count_unread(self, *, member: str) -> dict[str, Any]:
        params = {
            'select': 'id',
            'recipient': f'eq.{member}',
            'is_read': 'eq.false',
            'limit': 2000,
        }
        ok, out = self._chat_request('GET', params=params)
        if not ok:
            return {'ok': False, 'error': out.get('error') or 'Unable to count unread chat'}
        rows = out.get('data') if isinstance(out.get('data'), list) else []
        return {'ok': True, 'count': len(rows)}

    def chat_latest_member_message_id(self, *, member: str) -> dict[str, Any]:
        params_sent = {
            'select': 'id',
            'sender': f'eq.{member}',
            'order': 'created_at.desc,id.desc',
            'limit': 1,
        }
        params_received = {
            'select': 'id',
            'recipient': f'eq.{member}',
            'order': 'created_at.desc,id.desc',
            'limit': 1,
        }
        ok_sent, out_sent = self._chat_request('GET', params=params_sent)
        ok_received, out_received = self._chat_request('GET', params=params_received)
        if not ok_sent and not ok_received:
            return {'ok': False, 'error': out_sent.get('error') or out_received.get('error') or 'Unable to read latest chat marker'}

        rows_sent = out_sent.get('data') if ok_sent and isinstance(out_sent.get('data'), list) else []
        rows_received = out_received.get('data') if ok_received and isinstance(out_received.get('data'), list) else []
        rows = rows_sent + rows_received
        latest_id = 0
        if rows:
            for row in rows:
                try:
                    latest_id = max(latest_id, int((row or {}).get('id') or 0))
                except Exception:
                    continue
        return {'ok': True, 'latest_id': latest_id}

    def _admin_request_json(self, method: str, url: str, *, json_body: dict[str, Any] | None = None) -> tuple[bool, int, dict[str, Any]]:
        try:
            response = requests.request(
                method=method,
                url=url,
                headers=self._admin_headers(),
                json=json_body,
                timeout=self._timeout_seconds,
            )
        except requests.RequestException as ex:
            return False, 0, {'error': str(ex)}

        payload: dict[str, Any] = {}
        try:
            parsed = response.json()
            if isinstance(parsed, dict):
                payload = parsed
        except Exception:
            payload = {}
        ok = response.status_code >= 200 and response.status_code < 300
        if not ok and not payload.get('error'):
            payload['error'] = f"Supabase admin request failed ({response.status_code})"
        return ok, response.status_code, payload

    def _admin_find_user_by_email(self, base_url: str, email: str) -> dict[str, Any] | None:
        clean_email = str(email or '').strip().lower()
        if not clean_email:
            return None
        try:
            response = requests.get(
                f"{base_url}/auth/v1/admin/users",
                headers=self._admin_headers(),
                params={'email': clean_email},
                timeout=self._timeout_seconds,
            )
            if response.status_code < 200 or response.status_code >= 300:
                return None
            payload = response.json()
            users = payload.get('users') if isinstance(payload, dict) else None
            if not isinstance(users, list):
                return None
            for row in users:
                if not isinstance(row, dict):
                    continue
                row_email = str(row.get('email') or '').strip().lower()
                if row_email == clean_email:
                    return row
        except Exception:
            return None
        return None

    def _request_auth_user(self, base_url: str, access_token: str) -> dict[str, Any] | None:
        try:
            response = requests.get(
                f"{base_url}/auth/v1/user",
                headers=self._auth_headers(access_token=access_token),
                timeout=self._timeout_seconds,
            )
            if response.status_code >= 200 and response.status_code < 300:
                payload = response.json()
                return payload if isinstance(payload, dict) else None
        except Exception:
            return None
        return None

    def _parse_auth_error(self, response: requests.Response) -> str:
        try:
            payload = response.json()
            if isinstance(payload, dict):
                return str(payload.get('msg') or payload.get('error_description') or payload.get('error') or '').strip() or f"Supabase auth failed ({response.status_code})"
        except Exception:
            pass
        return f"Supabase auth failed ({response.status_code})"

    def auth_password_login(self, *, email: str, password: str) -> dict[str, Any]:
        user_email = str(email or '').strip()
        user_password = str(password or '')

        if not self.auth_enabled:
            return {'ok': False, 'error': 'Supabase authentication is disabled.'}
        if not user_email or not user_password:
            return {'ok': False, 'error': 'Email and password are required.'}
        if not self.is_configured():
            return {'ok': False, 'error': self.configuration_issue() or 'Supabase is not configured.'}

        last_error = 'Unable to authenticate with Supabase.'
        for candidate in self._url_candidates:
            try:
                response = requests.post(
                    f"{candidate}/auth/v1/token?grant_type=password",
                    headers=self._auth_headers(),
                    json={'email': user_email, 'password': user_password},
                    timeout=self._timeout_seconds,
                )
            except requests.RequestException as ex:
                last_error = str(ex)
                continue

            if response.status_code >= 200 and response.status_code < 300:
                data = response.json() if response.content else {}
                if not isinstance(data, dict):
                    data = {}
                access_token = str(data.get('access_token') or '').strip()
                refresh_token = str(data.get('refresh_token') or '').strip()
                user = data.get('user') if isinstance(data.get('user'), dict) else None
                if (not user) and access_token:
                    user = self._request_auth_user(candidate, access_token)
                if access_token:
                    self._active_url = candidate
                return {
                    'ok': True,
                    'url': self._public_url_summary(candidate),
                    'access_token': access_token,
                    'refresh_token': refresh_token,
                    'user': user or {},
                }

            # Invalid credentials should surface immediately.
            if response.status_code in {400, 401, 422}:
                return {'ok': False, 'error': self._parse_auth_error(response)}

            last_error = self._parse_auth_error(response)

        return {'ok': False, 'error': last_error}

    def account_from_user(self, user: dict[str, Any], *, fallback_email: str = '') -> dict[str, Any]:
        payload = user if isinstance(user, dict) else {}
        user_meta = payload.get('user_metadata') if isinstance(payload.get('user_metadata'), dict) else {}
        app_meta = payload.get('app_metadata') if isinstance(payload.get('app_metadata'), dict) else {}

        email = str(payload.get('email') or fallback_email or '').strip().lower()
        display_name = str(
            user_meta.get('full_name')
            or user_meta.get('name')
            or user_meta.get('preferred_username')
            or user_meta.get('username')
            or ''
        ).strip()
        if not display_name and email:
            display_name = email.split('@', 1)[0]
        if not display_name:
            display_name = 'HomeHub User'

        role = str(user_meta.get('role') or app_meta.get('role') or 'standard').strip().lower()
        if role not in {'admin', 'standard', 'child'}:
            role = 'standard'

        homehub_access_raw = user_meta.get('homehub_access', app_meta.get('homehub_access', False))
        if isinstance(homehub_access_raw, str):
            homehub_access = homehub_access_raw.strip().lower() in {'1', 'true', 'yes', 'on'}
        else:
            homehub_access = bool(homehub_access_raw)

        homehub_member_name = str(
            user_meta.get('homehub_member_name')
            or app_meta.get('homehub_member_name')
            or ''
        ).strip()

        return {
            'id': str(payload.get('id') or '').strip(),
            'email': email,
            'display_name': display_name,
            'role': role,
            'homehub_access': homehub_access,
            'homehub_member_name': homehub_member_name,
        }

    def provision_or_update_member_user(
        self,
        *,
        email: str,
        member_name: str,
        role: str,
        actor: str = '',
        previous_email: str = '',
    ) -> dict[str, Any]:
        if not self._admin_ready():
            return {'ok': False, 'synced': False, 'reason': 'service_role_key_missing_or_auth_disabled'}

        clean_email = str(email or '').strip().lower()
        clean_prev_email = str(previous_email or '').strip().lower()
        clean_member_name = str(member_name or '').strip()
        clean_role = str(role or 'standard').strip().lower()
        if clean_role not in {'admin', 'standard', 'child'}:
            clean_role = 'standard'
        if not clean_email or not clean_member_name:
            return {'ok': False, 'synced': False, 'reason': 'missing_email_or_member_name'}

        metadata_user = {
            'full_name': clean_member_name,
            'name': clean_member_name,
            'role': clean_role,
            'homehub_access': True,
            'homehub_member_name': clean_member_name,
            'homehub_updated_by': actor,
        }
        metadata_app = {
            'role': clean_role,
            'homehub_access': True,
            'homehub_member_name': clean_member_name,
        }

        last_error = 'Unable to sync Supabase user.'
        for base_url in self._url_candidates:
            user_row = self._admin_find_user_by_email(base_url, clean_email)
            if (not user_row) and clean_prev_email and clean_prev_email != clean_email:
                user_row = self._admin_find_user_by_email(base_url, clean_prev_email)

            if user_row:
                user_id = str(user_row.get('id') or '').strip()
                if not user_id:
                    continue
                ok, _, payload = self._admin_request_json(
                    'PUT',
                    f"{base_url}/auth/v1/admin/users/{user_id}",
                    json_body={
                        'email': clean_email,
                        'user_metadata': metadata_user,
                        'app_metadata': metadata_app,
                        'email_confirm': True,
                    },
                )
                if ok:
                    self._active_url = base_url
                    return {
                        'ok': True,
                        'synced': True,
                        'created': False,
                        'user_id': user_id,
                        'email': clean_email,
                    }
                last_error = str(payload.get('error') or last_error)
                continue

            temp_password = f"Hh!{secrets.token_urlsafe(10)}"
            ok, _, payload = self._admin_request_json(
                'POST',
                f"{base_url}/auth/v1/admin/users",
                json_body={
                    'email': clean_email,
                    'password': temp_password,
                    'email_confirm': True,
                    'user_metadata': metadata_user,
                    'app_metadata': metadata_app,
                },
            )
            if ok:
                created_user = payload.get('user') if isinstance(payload.get('user'), dict) else payload
                user_id = str((created_user or {}).get('id') or '').strip()
                self._active_url = base_url
                return {
                    'ok': True,
                    'synced': True,
                    'created': True,
                    'user_id': user_id,
                    'email': clean_email,
                    'temporary_password': temp_password,
                }
            last_error = str(payload.get('error') or last_error)

        return {'ok': False, 'synced': False, 'reason': 'admin_request_failed', 'error': last_error}

    def revoke_member_user_access(self, *, email: str, actor: str = '') -> dict[str, Any]:
        if not self._admin_ready():
            return {'ok': False, 'synced': False, 'reason': 'service_role_key_missing_or_auth_disabled'}

        clean_email = str(email or '').strip().lower()
        if not clean_email:
            return {'ok': False, 'synced': False, 'reason': 'missing_email'}

        last_error = 'Unable to revoke Supabase user access.'
        found_user = False
        for base_url in self._url_candidates:
            user_row = self._admin_find_user_by_email(base_url, clean_email)
            if not user_row:
                continue

            found_user = True

            user_id = str(user_row.get('id') or '').strip()
            if not user_id:
                continue

            current_user_meta = user_row.get('user_metadata') if isinstance(user_row.get('user_metadata'), dict) else {}
            current_app_meta = user_row.get('app_metadata') if isinstance(user_row.get('app_metadata'), dict) else {}
            current_user_meta.update({'homehub_access': False, 'homehub_updated_by': actor})
            current_app_meta.update({'homehub_access': False})

            ok, _, payload = self._admin_request_json(
                'PUT',
                f"{base_url}/auth/v1/admin/users/{user_id}",
                json_body={
                    'user_metadata': current_user_meta,
                    'app_metadata': current_app_meta,
                },
            )
            if ok:
                self._active_url = base_url
                return {
                    'ok': True,
                    'synced': True,
                    'found': True,
                    'email': clean_email,
                    'user_id': user_id,
                }
            last_error = str(payload.get('error') or last_error)

        if not found_user:
            return {'ok': True, 'synced': True, 'found': False, 'email': clean_email}

        return {'ok': False, 'synced': False, 'reason': 'admin_request_failed', 'error': last_error}

    def probe(self) -> dict:
        summary = self.status()
        if not summary['configured']:
            return {
                'ok': False,
                **summary,
                'probes': [],
                'candidate_probes': [],
            }

        candidate_rows: list[dict] = []
        selected_candidate: str | None = None
        selected_probe_rows: list[dict] = []
        ok = False

        for candidate in self._url_candidates:
            probes = self._build_candidate_probes(candidate)
            probe_rows = [
                {
                    'name': item.name,
                    'url': item.url,
                    'reachable': item.reachable,
                    'status_code': item.status_code,
                    'error': item.error,
                }
                for item in probes
            ]
            candidate_ok = all(item.reachable for item in probes)
            candidate_rows.append(
                {
                    'base_url': self._public_url_summary(candidate),
                    'ok': candidate_ok,
                    'probes': probe_rows,
                }
            )
            if selected_candidate is None:
                selected_candidate = candidate
                selected_probe_rows = probe_rows
            if candidate_ok:
                selected_candidate = candidate
                selected_probe_rows = probe_rows
                ok = True
                break

        if selected_candidate:
            self._active_url = selected_candidate

        selected_public = self._public_url_summary(selected_candidate or '')
        return {
            'ok': ok,
            **summary,
            'url': selected_public or summary.get('url') or '',
            'realtime_url': f"{selected_candidate}/realtime/v1" if selected_candidate else (summary.get('realtime_url') or ''),
            'probes': selected_probe_rows,
            'candidate_probes': candidate_rows,
        }


supabase_service = SupabaseService()