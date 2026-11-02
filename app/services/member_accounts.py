import re

PLACEHOLDER_DOMAIN = 'homehub.local'
MIN_PASSWORD_LENGTH = 6


def is_placeholder_email(email: str | None) -> bool:
    return str(email or '').strip().lower().endswith('@' + PLACEHOLDER_DOMAIN)


def placeholder_email_for(name: str) -> str:
    slug = re.sub(r'[^a-z0-9]+', '.', str(name or '').strip().lower()).strip('.') or 'member'
    return f'{slug}@{PLACEHOLDER_DOMAIN}'
