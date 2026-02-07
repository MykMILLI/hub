# Option C: Complete PIN-Gated Member Switch Flow with Reset Logic

## Overview

HomeHub now implements a complete **Option C (Hybrid)** PIN-based member authentication system with full admin and self-service PIN management capabilities.

## Complete Workflow

### **Initial Setup (Admin)**
1. Admin goes to **Settings → Family Members**
2. For each family member:
   - Click **"Set PIN"** button
   - Enter 4-digit PIN
   - Confirm PIN
   - PIN is saved immediately
3. Admin communicates PIN to family member (verbally, securely, etc.)

### **First Member Selection (New Member)**
```
Family member dropdown → No PIN set → "Create Your PIN" modal appears
↓
Member enters + confirms 4-digit PIN
↓
System stores PIN in database
↓
"Welcome, [Name]" overlay
↓
Dashboard loads with member's personal data
```

### **Switching Members (With PIN)**
```
Change member dropdown → System checks for PIN
↓
PIN exists? → "Enter PIN" modal
↓
Correct PIN? → "Welcome, [Name]" overlay
↓
Dashboard reloads with new session
```

### **Admin Bypass**
- Admin member (configured in `config.yml`) **never** requires PIN
- Admin switches directly without any verification
- Admin can manage all family member PINs

### **Forgotten PIN (Member Recovery)**
```
Member: "I forgot my PIN"
↓
Admin goes to Settings → Family Members
↓
Finds member → Click "Reset PIN" button
↓
PIN is cleared from database
↓
Member selected from dropdown → "Create Your PIN" modal
↓
Member sets new PIN (same as first-time setup flow)
```

---

## Database Schema

### FamilyMember Model
```python
class FamilyMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(128), nullable=False, unique=True)
    pin = db.Column(db.String(4), nullable=True)  # NULL = no PIN set
    added_at = db.Column(db.DateTime, default=datetime.utcnow)
```

- **pin = NULL**: No PIN required, member can log in directly
- **pin = "1234"**: PIN set, member must verify before switching

---

## API Endpoints

### Member Login (Verify PIN)
```
POST /api/member-login
Body: { "name": "Mom", "pin": "1234" }
Response: 
  - Success (200): { "ok": true, "name": "Mom", "message": "Welcome, Mom!", "is_admin": false }
  - Wrong PIN (403): { "ok": false, "error": "Invalid PIN" }
  - No PIN needed (200): { "ok": true, "name": "Mom", "message": "Welcome, Mom!", "is_admin": false }
```

### Get Current Member
```
GET /api/current-member
Response: { "ok": true, "member": "Mom" }  // From session
```

### Member Sets PIN (First Time)
```
POST /api/member/set-pin
Body: { "pin": "1234" }
Requires: Logged-in session (no PIN currently set)
Response: { "ok": true, "message": "PIN set successfully for Mom" }
```

### Member Changes PIN (Has Existing PIN)
```
POST /api/member/change-pin
Body: { "current_pin": "1234", "new_pin": "5678" }
Requires: Logged-in session + correct current PIN
Response: { "ok": true, "message": "PIN changed successfully" }
```

### Admin Resets Member PIN
```
POST /api/member/{member_id}/reset-pin
Requires: Admin logged in via session
Effect: Clears member's PIN (sets to NULL)
Response: { "ok": true, "message": "PIN reset for Mom. They can set a new PIN on next login." }
```

### Get Family Members (with PIN Status)
```
GET /api/family-members
Response: {
  "ok": true,
  "members": [
    { "id": 1, "name": "Admin", "pin": false },
    { "id": 2, "name": "Mom", "pin": true },
    { "id": 3, "name": "Child", "pin": false }
  ]
}
```

### Update Family Member (Name + PIN)
```
PUT /api/family-members/{member_id}
Body: { "name": "Mom", "pin": "1234" }
Response: { "ok": true, "member": { "id": 2, "name": "Mom", "pin": "1234" } }
```

---

## User Interface Components

### 1. **Member Selector (base.html)**
- Dropdown with all family members
- On change: Checks member's PIN status via API
- If PIN exists → Show "Enter PIN" modal
- If no PIN → Show "Create Your PIN" modal

### 2. **PIN Entry Modal** (for members with existing PIN)
```
┌─────────────────────────────────┐
│  Enter PIN for Mom              │
│                                 │
│  [••••]  (password field)       │
│                                 │
│  [Cancel] [Verify]              │
└─────────────────────────────────┘
```

### 3. **First-Time PIN Setup Modal** (for members without PIN)
```
┌─────────────────────────────────┐
│  Secure Your Account            │
│  Set a 4-digit PIN              │
│                                 │
│  Create PIN:  [••••]            │
│  Confirm PIN: [••••]            │
│                                 │
│  [Skip] [Set PIN]               │
└─────────────────────────────────┘
```

### 4. **Admin PIN Management (settings.html)**
Each family member in list shows:
```
Member Name
├─ PIN Status: [✓ Set] or [Not Set]
└─ [Set PIN] button  or  [Reset PIN] button
```

**Set PIN Modal:**
- Admin enters new PIN
- Admin confirms PIN
- System saves to database
- Admin communicates to member

**Reset PIN Modal:**
- Confirmation message: "PIN will be cleared. Member sets new on next login."
- Admin confirms reset
- System clears PIN (sets to NULL)
- Member must create new PIN on next login

### 5. **Welcome Overlay** (on successful login)
```
┌─────────────────────────────────┐
│           👋                    │
│                                 │
│  Welcome, Mom!                  │
│  Switching to your dashboard... │
│                                 │
│  (Shows for 1.5 seconds)        │
└─────────────────────────────────┘
```

---

## Session Management

### Session Variables
```javascript
session['current_member']  // Set when member logs in
// Example: 'Mom', 'Dad', 'Administrator'
```

### Template Context
```html
{{ current_member }}  <!-- Available in all templates -->
<!-- Example: 'Mom' if logged in as Mom -->
```

### Data Scoping
All personal data must filter by current member:
```python
# Example in routes
@app.route('/notes')
def notes():
    current_member = session.get('current_member')
    notes = Note.query.filter_by(creator=current_member).all()
    return render_template('notes.html', notes=notes)
```

---

## Security Considerations

### PIN Storage
- **Current:** Plaintext 4-digit PIN in database (simple home network)
- **Production:** Use bcrypt hashing
  ```python
  from bcrypt import hashpw, checkpw
  member.pin = hashpw(pin.encode(), bcrypt.gensalt())
  ```

### Session Security
- Set in `app.config`:
  ```python
  SESSION_COOKIE_SECURE = True      # HTTPS only
  SESSION_COOKIE_HTTPONLY = True    # JS can't access
  SESSION_COOKIE_SAMESITE = 'Lax'   # CSRF protection
  ```

### Rate Limiting (Optional Enhancement)
- Implement rate limiting on `/api/member-login` to prevent brute force
- Max 3 attempts per minute per member

---

## Implementation Summary

### Backend Changes
✅ `models.py`: Added `pin` field to `FamilyMember`
✅ `blueprints/settings.py`: Added 5 new API endpoints
✅ `__init__.py`: Added PIN auto-migration, updated context processor
✅ `app/__init__.py`: Auto-migration adds `pin` column if missing

### Frontend Changes
✅ `templates/base.html`: 
  - PIN verification modal
  - First-time PIN setup modal
  - Member selector enhanced with PIN check logic
  - PIN modal handlers

✅ `templates/settings.html`:
  - Admin family member PIN management UI
  - Set PIN modal for admin
  - Reset PIN modal for admin
  - JavaScript handlers for PIN operations

### Database Migration
✅ Auto-migration handles existing installations:
  - Adds `pin` column if missing
  - All existing members have `pin=NULL` (no PIN required initially)

---

## Usage Examples

### Admin Setting PIN for New Member
```
1. Go to Settings
2. Find "John" in family members list
3. Click "Set PIN" button
4. Enter "1234", confirm "1234"
5. PIN saved
6. Admin tells John: "Your PIN is 1234"
```

### Member First Login (No PIN Set)
```
1. Member selected from dropdown
2. "Create Your PIN" modal appears
3. Enters "5678", confirms "5678"
4. PIN saved to database
5. "Welcome, John!" overlay
6. Dashboard loads
```

### Member Login with Existing PIN
```
1. Member selected from dropdown
2. "Enter PIN for John" modal appears
3. Enters "5678"
4. System verifies: matches database ✓
5. "Welcome, John!" overlay
6. Dashboard loads
```

### Admin Resetting Member's Forgotten PIN
```
1. John: "I forgot my PIN"
2. Admin goes to Settings
3. Finds John → Click "Reset PIN"
4. Confirms: "PIN will be cleared"
5. John's PIN cleared from database
6. John selects dropdown
7. "Create Your PIN" modal appears
8. Sets new PIN "9999"
9. Logged in successfully
```

### Member Changing Their PIN
```
Future Enhancement: Add "Account Settings" page where member can:
1. Click "Change PIN"
2. Enter current PIN: "5678"
3. Enter new PIN: "9999", confirm
4. PIN updated
5. "Your PIN has been changed"
```

---

## Testing Checklist

- [ ] Admin can set PIN for family member
- [ ] Family member with PIN must enter PIN to log in
- [ ] Wrong PIN shows error, allows retry
- [ ] Family member without PIN can set PIN on first login
- [ ] Member can skip PIN setup and set later
- [ ] Admin can reset member's forgotten PIN
- [ ] Member with reset PIN sees setup modal on next login
- [ ] Admin switches without PIN requirement
- [ ] Session persists across page reloads
- [ ] Logout clears session
- [ ] Personal data scoped to logged-in member

---

## Future Enhancements

1. **PIN Hashing:** Upgrade to bcrypt for production
2. **Biometric:** Add fingerprint/face ID support
3. **Change PIN:** Member self-service PIN changes
4. **2FA:** Optional email/SMS second factor
5. **Login History:** Track who logged in when
6. **Rate Limiting:** Brute-force protection
7. **Auto-Logout:** Timeout after inactivity
8. **PIN Hints:** Optional security questions
