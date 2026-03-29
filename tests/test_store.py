from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from codex_dashboard.models import Base, SessionLock, User
from codex_dashboard.security import hash_password
from codex_dashboard.store import acquire_lock, release_lock


def make_db() -> Session:
    engine = create_engine("sqlite:///:memory:", future=True)
    Base.metadata.create_all(engine)
    return Session(engine)


def test_session_lock_lifecycle() -> None:
    db = make_db()
    user = User(username="admin", password_hash=hash_password("pw"))
    other = User(username="other", password_hash=hash_password("pw"))
    db.add_all([user, other])
    db.commit()
    db.refresh(user)
    db.refresh(other)

    ok, message = acquire_lock(db, "abc", user.id)
    assert ok
    assert "Lock" in message
    lock = db.get(SessionLock, "abc")
    assert lock is not None
    assert lock.user_id == user.id

    ok, _ = acquire_lock(db, "abc", other.id)
    assert not ok

    ok, _ = release_lock(db, "abc", other.id)
    assert not ok

    ok, _ = release_lock(db, "abc", user.id)
    assert ok
    assert db.get(SessionLock, "abc") is None
