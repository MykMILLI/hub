import random
import string


def get_member_avatar_map() -> dict[str, str]:
    try:
        from .models import FamilyMember
        members = FamilyMember.query.all()
        return {
            (m.name or '').strip(): (m.avatar or '').strip()
            for m in members
            if (m.name or '').strip()
        }
    except Exception:
        return {}


def get_member_avatar(name: str | None) -> str:
    clean_name = (name or '').strip()
    if not clean_name:
        return ''
    return get_member_avatar_map().get(clean_name, '')

def generate_short_code(length=6):
    return ''.join(random.choices(string.ascii_letters + string.digits, k=length))

# Add more utility functions as needed
