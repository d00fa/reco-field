from __future__ import annotations

import secrets
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


SESSION_COOKIE = "reco_session"
CSRF_COOKIE = "reco_csrf"
UTC = timezone.utc


@dataclass(frozen=True)
class Session:
    token: str
    csrf: str
    expires_at: datetime


class SessionStore:
    def __init__(self, session_hours: int = 12) -> None:
        self.session_hours = session_hours
        self._lock = threading.RLock()
        self._sessions: dict[str, Session] = {}

    def create(self) -> Session:
        with self._lock:
            now = datetime.now(UTC)
            self._sessions = {
                token: session for token, session in self._sessions.items()
                if session.expires_at > now
            }
            if len(self._sessions) >= 1024:
                self._sessions.pop(next(iter(self._sessions)))
            session = Session(
                token=secrets.token_urlsafe(32),
                csrf=secrets.token_urlsafe(24),
                expires_at=now + timedelta(hours=self.session_hours),
            )
            self._sessions[session.token] = session
            return session

    def get_session(self, token: str | None) -> Session | None:
        if not token:
            return None
        with self._lock:
            session = self._sessions.get(token)
            if session and session.expires_at <= datetime.now(UTC):
                self._sessions.pop(token, None)
                return None
            return session
