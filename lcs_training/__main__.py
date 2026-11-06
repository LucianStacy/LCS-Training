"""python -m lcs_training                     -> start the app and open it in the browser
python -m lcs_training reset-password USER  -> give USER a new temporary password (and re-activate them)
python -m lcs_training list-users           -> show who has an account
"""
import os
import sys
import threading
import webbrowser


def _serve() -> None:
    import uvicorn

    from .config import HOST, PORT
    url = f"http://{'127.0.0.1' if HOST in ('0.0.0.0', '') else HOST}:{PORT}"
    if not os.environ.get("LCS_NO_BROWSER"):
        threading.Timer(1.5, lambda: webbrowser.open(url)).start()
    print(f"LCS-Training is running at {url}  (Ctrl+C to stop)")
    uvicorn.run("lcs_training.app:app", host=HOST, port=PORT, log_level="info")


def _reset(username: str) -> int:
    from sqlalchemy import select

    from . import auth
    from .models import SessionLocal, User, init_db
    init_db()
    with SessionLocal() as db:
        u = db.scalar(select(User).where(User.username == username.strip().lower()))
        if not u:
            print(f"No user named {username!r}. Run 'list-users' to see them.")
            return 1
        pw = auth.temp_password()
        u.password_hash, u.must_change_password, u.active = auth.hash_password(pw), True, True
        auth.end_all_sessions(db, u.id)
        db.commit()
        auth.log(db, None, f"PASSWORD RESET FROM COMMAND LINE {u.username}", 200)
    print(f"Temporary password for {u.username} ({u.role}): {pw}")
    print("They'll be asked to choose a new one when they sign in.")
    return 0


def _list() -> int:
    from sqlalchemy import select

    from .models import SessionLocal, User, init_db
    init_db()
    with SessionLocal() as db:
        for u in db.scalars(select(User).order_by(User.username)):
            print(f"{u.username:40} {u.role:13} {'active' if u.active else 'inactive'}")
    return 0


def main() -> None:
    args = sys.argv[1:]
    if not args:
        _serve()
    elif args[0] == "reset-password" and len(args) == 2:
        sys.exit(_reset(args[1]))
    elif args[0] == "list-users":
        sys.exit(_list())
    else:
        print(__doc__)
        sys.exit(2)


if __name__ == "__main__":
    main()
