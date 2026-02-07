# PIN-Gated Member Switch Flow

## Overview

HomeHub now implements a PIN-based lightweight "login" system for family members. When switching from one member to another, users are prompted to enter a 4-digit PIN to authenticate as that family member.

## Architecture

### Backend

**Database (models.py)**
- `FamilyMember.pin` - Stores 4-digit PIN (nullable, no PIN = no requirement)

**API Endpoints (blueprints/settings.py)**
- `POST /api/member-login` - Verify PIN and set session
- `POST /api/member-logout` - Clear session
- `GET /api/current-member` - Get logged-in member from session
- `PUT /api/family-members/<id>` - Update name and/or PIN (PIN validation: 4 digits)

**Session Management**
- `session['current_member']` - Stores current member name server-side
- Admin member bypasses PIN requirement automatically

### Frontend

**templates/base.html**
- PIN modal with input field (4-digit input, masked password field)
- Welcome overlay with member name
- Member selector now triggers PIN flow instead of direct switch
- Admin detection: admin member switches without PIN prompt

**JavaScript Flow**
1. User changes member dropdown
2. If admin → direct switch
3. If family member → show PIN modal
4. PIN submitted → `/api/member-login` verification
5. On success → show welcome overlay → reload page
6. On failure → show error, clear input, allow retry

## Usage

### Setting a PIN for a Family Member

**Via Settings Page:**
1. Go to Settings
2. Find family member in list
3. Click "Set PIN" or edit button
4. Enter 4-digit PIN
5. Save

**Via API:**
```bash
curl -X PUT http://localhost:5000/api/family-members/1 \
  -H "Content-Type: application/json" \
  -d '{"name": "Mom", "pin": "1234"}'
```

**Response:**
```json
{
  "ok": true,
  "member": {
    "id": 1,
    "name": "Mom",
    "pin": "1234"
  }
}
```

### Member Login Flow

**User Experience:**
1. User selects different member from dropdown
2. PIN modal appears (unless member is admin or has no PIN)
3. User enters 4-digit PIN
4. System verifies on server
5. On success: "Welcome, {name}" overlay shows briefly
6. Dashboard reloads with member's personal data
7. All subsequent requests scoped to logged-in member

### No PIN Mode

If a family member has no PIN set:
- They can be selected from dropdown without verification
- This is useful for children or low-security members

### Admin Bypass

The admin member (configured in config.yml as `admin_name`):
- Never requires PIN verification
- Always switches immediately
- Can manage PINs for other members

## Session Scope

Once a member is authenticated:
- `session['current_member']` stores the member name server-side
- All personal data (notes, expenses, etc.) must be scoped to this member
- Shared family data (family budget, shared reminders) unaffected
- Session persists across page reloads
- Logout clears session; next page load requires re-login or admin selection

## Frontend Integration

### JavaScript Helper Functions

```javascript
// Show PIN modal for switching
window.showMemberPINModal(memberName);

// Pin verification handled by POST /api/member-login
```

### Template Variable

```html
<!-- Available in all templates -->
{{ current_member }}  <!-- Current logged-in member from session -->
```

## Data Scoping Rules

**Personal Data (Scoped to member):**
- Notes (created_by filter)
- Personal reminders
- Personal media/photos
- Shopping lists (created_by filter)
- Personal status

**Family Data (Shared across all):**
- Shared family budget
- Shared recipes
- Shared chores (optional, creator-based)
- Shared expenses (optional, family budget view)
- Family notice board

## Security Considerations

1. **PIN Storage:** Stored as plaintext in database (simple 4-digit PIN, not password)
   - For production: consider hashing with bcrypt
   - Session-based verification only

2. **Session Security:**
   - Relies on Flask's secure session cookies
   - Set `SESSION_COOKIE_SECURE=True` and `SESSION_COOKIE_HTTPONLY=True` in production

3. **Localhost Assumption:**
   - Current implementation assumes local home network
   - For internet exposure: upgrade PIN storage, add rate limiting

## Database Migration

When upgrading existing HomeHub installations:

1. Database auto-migration adds `pin` column to `family_member` table
2. All existing members have `pin=NULL` (no PIN required)
3. Admins can set PINs via Settings UI or API

## Configuration (config.yml)

No new config.yml entries needed. PIN management is entirely database/API-driven.

```yaml
# Existing config (unchanged)
admin_name: "Administrator"
family_members:
  - Mom
  - Dad
  - Child1
```

## Troubleshooting

### PIN Modal Doesn't Appear
- Check browser console for JavaScript errors
- Verify `window.showMemberPINModal` function exists
- Check network tab: `/api/family-members` call succeeds

### PIN Verification Fails
- Verify PIN is exactly 4 digits
- Check API response: `/api/member-login` returns 403 on wrong PIN
- Clear localStorage and try again
- Check server logs for database errors

### Member Data Not Switching
- Verify `session['current_member']` set correctly
- Check template uses `current_member` context variable
- Ensure routes filter by member (e.g., `creator == current_member`)
- Page reload required for some features to pick up new session

## Future Enhancements

1. **PIN Hashing:** Use bcrypt for production security
2. **Biometric Login:** Add fingerprint/face ID via browser API
3. **Auto-Logout:** Timeout after inactivity period
4. **Login History:** Track who logged in when
5. **Rate Limiting:** Prevent PIN brute-force attempts
6. **Two-Factor:** Add optional email/SMS verification
