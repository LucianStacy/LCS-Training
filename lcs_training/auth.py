"""Sign-in, sessions and role permissions.

* Passwords: PBKDF2-HMAC-SHA256, 600,000 iterations, random 16-byte salt (stdlib only).
* Sessions: random 256-bit token in an HttpOnly, SameSite=Lax cookie; only its SHA-256 is
  stored, so a copy of the database can't be used to sign in. 12-hour sliding expiry.
* Permissions: every request is checked against RULES (method + path) in one place, so the
  whole access policy can be read top to bottom here. Anything not listed needs Admin.
* Cross-site form posts are refused (SameSite cookie + Origin/Referer check).
"""
from __future__ import annotations

import base64
import datetime as dt
import hashlib
import hmac
import re
import secrets
import time
from collections import defaultdict
from typing import Optional

from sqlalchemy import delete, select

from .models import AuditLog, User, UserSession

COOKIE = "lcs_session"
SESSION_HOURS = 12
MIN_PASSWORD = 10
_ITER = 600_000

# ---------------------------------------------------------------------------
# Passwords
# ---------------------------------------------------------------------------


def hash_password(pw: str) -> str:
    salt = secrets.token_bytes(16)
    dk = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, _ITER)
    return f"pbkdf2_sha256${_ITER}${base64.b64encode(salt).decode()}${base64.b64encode(dk).decode()}"


def verify_password(pw: str, stored: str) -> bool:
    try:
        algo, it, salt, dk = stored.split("$")
        if algo != "pbkdf2_sha256":
            return False
        calc = hashlib.pbkdf2_hmac("sha256", pw.encode(), base64.b64decode(salt), int(it))
        return hmac.compare_digest(calc, base64.b64decode(dk))
    except Exception:
        return False


_DUMMY = hash_password(secrets.token_hex(8))  # keeps timing the same for unknown usernames


def password_problem(pw: str, username: str = "") -> Optional[str]:
    if len(pw) < MIN_PASSWORD:
        return f"Use at least {MIN_PASSWORD} characters."
    if username and pw.lower() == username.lower():
        return "The password can't be the same as the username."
    if pw.lower() in {"password123", "lcs-training", "lcstraining1", "1234567890", "qwertyuiop"}:
        return "That password is too easy to guess."
    return None


def temp_password() -> str:
    return "-".join(secrets.token_urlsafe(4) for _ in range(3))


# ---------------------------------------------------------------------------
# Failed sign-in lockout (in memory; resets when the app restarts)
# ---------------------------------------------------------------------------
_FAILS: dict[str, list[float]] = defaultdict(list)
MAX_FAILS, WINDOW = 5, 300


def locked_out(key: str) -> bool:
    now = time.time()
    _FAILS[key] = [t for t in _FAILS[key] if now - t < WINDOW]
    return len(_FAILS[key]) >= MAX_FAILS


def record_fail(key: str) -> None:
    _FAILS[key].append(time.time())


def clear_fails(key: str) -> None:
    _FAILS.pop(key, None)


def authenticate(db, username: str, pw: str) -> Optional[User]:
    user = db.scalar(select(User).where(User.username == username.strip().lower()))
    if user is None:
        verify_password(pw, _DUMMY)
        return None
    if not user.active or not verify_password(pw, user.password_hash):
        return None
    return user


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------
def _h(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def start_session(db, user: User) -> str:
    token = secrets.token_urlsafe(32)
    db.add(UserSession(token_hash=_h(token), user_id=user.id,
                       expires_at=dt.datetime.now() + dt.timedelta(hours=SESSION_HOURS)))
    user.last_login = dt.datetime.now()
    db.execute(delete(UserSession).where(UserSession.expires_at < dt.datetime.now()))
    db.commit()
    return token


def user_for_token(db, token: Optional[str]) -> Optional[User]:
    if not token:
        return None
    s = db.get(UserSession, _h(token))
    if not s or s.expires_at < dt.datetime.now():
        return None
    user = db.get(User, s.user_id)
    if not user or not user.active:
        return None
    if s.expires_at - dt.datetime.now() < dt.timedelta(hours=SESSION_HOURS / 2):
        s.expires_at = dt.datetime.now() + dt.timedelta(hours=SESSION_HOURS)
        db.commit()
    return user


def end_session(db, token: Optional[str]) -> None:
    if token:
        db.execute(delete(UserSession).where(UserSession.token_hash == _h(token)))
        db.commit()


def end_all_sessions(db, user_id: int) -> None:
    db.execute(delete(UserSession).where(UserSession.user_id == user_id))


# ---------------------------------------------------------------------------
# Permissions
# ---------------------------------------------------------------------------
ROLE_PERMS = {
    "Admin": {"*"},
    "Office Staff": {"view", "view_ops", "view_sales", "view_money", "export", "edit_crm", "edit_sales",
                     "edit_contracts", "edit_billing", "edit_onboarding", "edit_delivery", "edit_plans",
                     "edit_coaches", "edit_timeoff"},
    "Coach": {"view", "view_ops", "edit_onboarding", "edit_delivery", "edit_plans", "edit_timeoff"},
    "Viewer": {"view", "view_ops", "view_sales", "view_money"},
}

PUBLIC = re.compile(r"^/(static/|login$|logout$|first-run$|favicon)")
ANY_USER = re.compile(r"^/(account(/.*)?|year)$")

# (method, path pattern, permission) - first match wins; no match = Admin only.
RULES = [
    ("GET", r"^/$", "view"),
    ("GET", r"^/(leads|quotes|prices)(/|$)", "view_sales"),
    ("GET", r"^/installments/\d+/invoice$", "view_money"),
    ("GET", r"^/export/", "export"),
    ("GET", r"^/(clients|contacts|contracts|deliveries|plans|coaches)(/|$|\?)", "view_ops"),
    ("POST", r"^/(clients|contacts|tags)(/|$)", "edit_crm"),
    ("POST", r"^/(leads|quotes)(/|$)", "edit_sales"),
    ("POST", r"^/contracts/\d+/onboarding(/generate)?$", "edit_onboarding"),
    ("POST", r"^/contracts/\d+/plans/new$", "edit_plans"),
    ("POST", r"^/contracts/\d+/(billing-settings|billing/.*|value)$", "edit_billing"),
    ("POST", r"^/installments/\d+$", "edit_billing"),
    ("POST", r"^/(contracts|services)(/|$)", "edit_contracts"),
    ("POST", r"^/deliveries/", "edit_delivery"),
    ("POST", r"^/plans/", "edit_plans"),
    ("POST", r"^/(coaches/\d+/unavailability|unavailability/\d+/delete)$", "edit_timeoff"),
    ("POST", r"^/coaches(/|$)", "edit_coaches"),
]
_RULES = [(m, re.compile(p), perm) for m, p, perm in RULES]


def has_perm(user: Optional[User], perm: str) -> bool:
    if user is None:
        return False
    perms = ROLE_PERMS.get(user.role, set())
    return "*" in perms or perm in perms


def required_perm(method: str, path: str) -> str:
    method = "GET" if method in ("GET", "HEAD") else "POST"
    for m, rx, perm in _RULES:
        if m == method and rx.search(path):
            return perm
    return "admin"


def same_origin(request) -> bool:
    """Refuse form posts that come from another site."""
    origin = request.headers.get("origin") or request.headers.get("referer")
    if not origin:
        return True  # same-site browser posts always send one; scripts/CLI tools don't need protecting
    m = re.match(r"^https?://([^/]+)", origin)
    return bool(m) and m.group(1) == request.headers.get("host")


def log(db, user: Optional[User], action: str, status: int) -> None:
    db.add(AuditLog(user_id=user.id if user else None, user_name=user.full_name if user else None,
                    action=action[:300], status=status))
    db.commit()
