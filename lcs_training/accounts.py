"""Sign in / out, first-run admin setup, own account, and user management (Admin)."""
from __future__ import annotations

from typing import Optional
from urllib.parse import quote_plus

from fastapi import APIRouter, Depends, Request
from fastapi.responses import HTMLResponse, RedirectResponse
from sqlalchemy import select

from . import auth
from .models import ROLES, AuditLog, Coach, User

router = APIRouter()


def _app():
    from . import app as app_module  # imported late to avoid a circular import
    return app_module


def _safe_next(nxt: Optional[str]) -> str:
    return nxt if nxt and nxt.startswith("/") and not nxt.startswith("//") else "/"


def _set_cookie(request: Request, resp, token: str) -> None:
    resp.set_cookie(auth.COOKIE, token, max_age=auth.SESSION_HOURS * 3600 * 2, httponly=True, samesite="lax",
                    secure=request.url.scheme == "https", path="/")


def _plain(request, name, status=200, **ctx):
    ctx.update(request=request, user=None, can=lambda p: False, years=[], school_year="",
               msg=request.query_params.get("msg"), err=ctx.get("err") or request.query_params.get("err"))
    return _app().templates.TemplateResponse(request, name, ctx, status_code=status)


def get_db():
    yield from _app().get_db()


# ---------------------------------------------------------------------------
@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request, next: str = "/"):
    return _plain(request, "login.html", next=_safe_next(next))


@router.post("/login")
async def login_submit(request: Request, db=Depends(get_db)):
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    nxt = _safe_next(form.get("next"))
    key = f"{username}|{request.client.host if request.client else ''}"
    if auth.locked_out(key):
        return _plain(request, "login.html", 429, next=nxt, username=username,
                      err="Too many wrong passwords. Wait 5 minutes and try again.")
    user = auth.authenticate(db, username, form.get("password") or "")
    if not user:
        auth.record_fail(key)
        auth.log(db, None, f"FAILED SIGN-IN {username}", 401)
        return _plain(request, "login.html", 401, next=nxt, username=username,
                      err="That username and password don't match an active account.")
    auth.clear_fails(key)
    token = auth.start_session(db, user)
    auth.log(db, user, "SIGN-IN", 200)
    resp = RedirectResponse("/account" if user.must_change_password else nxt, status_code=303)
    _set_cookie(request, resp, token)
    return resp


@router.get("/logout")
@router.post("/logout")
def logout(request: Request, db=Depends(get_db)):
    auth.end_session(db, request.cookies.get(auth.COOKIE))
    resp = RedirectResponse("/login?msg=" + quote_plus("You're signed out."), status_code=303)
    resp.delete_cookie(auth.COOKIE, path="/")
    return resp


@router.get("/first-run", response_class=HTMLResponse)
def first_run_page(request: Request, db=Depends(get_db)):
    if db.scalar(select(User.id).limit(1)):
        return RedirectResponse("/login", status_code=303)
    return _plain(request, "first_run.html")


@router.post("/first-run")
async def first_run_submit(request: Request, db=Depends(get_db)):
    if db.scalar(select(User.id).limit(1)):  # only ever works while there are no users at all
        return RedirectResponse("/login", status_code=303)
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    name = (form.get("full_name") or "").strip()
    pw = form.get("password") or ""
    problem = ((not username and "Enter a username (your work email is easiest).")
               or (not name and "Enter your name.")
               or auth.password_problem(pw, username)
               or (pw != form.get("password2") and "The two passwords don't match."))
    if problem:
        return _plain(request, "first_run.html", 400, err=problem, username=username, full_name=name)
    user = User(username=username, full_name=name, role="Admin", password_hash=auth.hash_password(pw))
    db.add(user)
    db.commit()
    auth.log(db, user, "CREATED FIRST ADMIN", 200)
    token = auth.start_session(db, user)
    resp = RedirectResponse("/?msg=" + quote_plus(f"Welcome, {name}. You're the administrator."), status_code=303)
    _set_cookie(request, resp, token)
    return resp


@router.get("/account", response_class=HTMLResponse)
def account_page(request: Request, db=Depends(get_db)):
    return _app().render(request, "account.html", db)


@router.post("/account")
async def account_save(request: Request, db=Depends(get_db)):
    go = _app().go
    form = await request.form()
    user = db.get(User, request.state.user.id)
    if not auth.verify_password(form.get("current") or "", user.password_hash):
        return go("/account", err="Your current password isn't right.")
    pw = form.get("password") or ""
    problem = auth.password_problem(pw, user.username) or (
        pw != form.get("password2") and "The two new passwords don't match.")
    if problem:
        return go("/account", err=problem)
    user.password_hash = auth.hash_password(pw)
    user.must_change_password = False
    auth.end_all_sessions(db, user.id)
    db.commit()
    token = auth.start_session(db, user)
    resp = go("/", "Password changed. You were signed out everywhere else.")
    _set_cookie(request, resp, token)
    return resp


# ---------------------------------------------------------------------------
# Users (Admin only - enforced by auth.RULES: /users has no rule, so it needs Admin)
# ---------------------------------------------------------------------------
@router.get("/users", response_class=HTMLResponse)
def users_page(request: Request, db=Depends(get_db)):
    return _users(request, db)


def _users(request, db, new_pw=None, pw_for=None):
    """The one-time password is shown in the page itself, never put in a URL (history, logs)."""
    users = list(db.scalars(select(User).order_by(User.active.desc(), User.full_name)))
    log = list(db.scalars(select(AuditLog).order_by(AuditLog.at.desc()).limit(200)))
    return _app().render(request, "users.html", db, users=users, log=log, roles=ROLES,
                         coaches=list(db.scalars(select(Coach).order_by(Coach.coach_name))),
                         new_pw=new_pw, pw_for=pw_for)


def _other_admins(db, exclude_id) -> int:
    return sum(1 for u in db.scalars(select(User).where(User.role == "Admin", User.active)) if u.id != exclude_id)


@router.post("/users/new")
async def user_new(request: Request, db=Depends(get_db)):
    go = _app().go
    form = await request.form()
    username = (form.get("username") or "").strip().lower()
    name = (form.get("full_name") or "").strip()
    role = form.get("role")
    if not username or not name or role not in ROLES:
        return go("/users", err="Enter a username, a name and a role.")
    if db.scalar(select(User).where(User.username == username)):
        return go("/users", err="That username is already taken.")
    pw = auth.temp_password()
    db.add(User(username=username, full_name=name, role=role, password_hash=auth.hash_password(pw),
                must_change_password=True,
                coach_id=int(form["coach_id"]) if form.get("coach_id") and role == "Coach" else None))
    db.commit()
    return _users(request, db, pw, username)


@router.post("/users/{uid}")
async def user_save(request: Request, uid: int, db=Depends(get_db)):
    go = _app().go
    u = db.get(User, uid)
    if not u:
        return go("/users", err="No such user.")
    form = await request.form()
    me = request.state.user
    role = form.get("role") if form.get("role") in ROLES else u.role
    changed_access = role != u.role or (u.active and form.get("active") != "on")
    active = form.get("active") == "on"
    if u.role == "Admin" and (role != "Admin" or not active) and _other_admins(db, u.id) == 0:
        return go("/users", err="There must always be at least one active Admin.")
    if u.id == me.id and not active:
        return go("/users", err="You can't deactivate your own account.")
    u.full_name = (form.get("full_name") or "").strip() or u.full_name
    u.role, u.active = role, active
    u.coach_id = int(form["coach_id"]) if form.get("coach_id") and role == "Coach" else None
    if changed_access and u.id != me.id:
        auth.end_all_sessions(db, u.id)  # they sign in again under the new role
    db.commit()
    return go("/users", f"Saved {u.full_name}.")


@router.post("/users/{uid}/reset")
def user_reset(request: Request, uid: int, db=Depends(get_db)):
    u = db.get(User, uid)
    if not u:
        return _app().go("/users", err="No such user.")
    pw = auth.temp_password()
    u.password_hash = auth.hash_password(pw)
    u.must_change_password = True
    auth.end_all_sessions(db, u.id)
    db.commit()
    return _users(request, db, pw, u.username)
