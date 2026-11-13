"""DB 엔진·세션. DB_URL 하나로 SQLite(P1)와 PostgreSQL(운영)을 전환한다."""

from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path
from typing import Iterator

from sqlalchemy import create_engine
from sqlalchemy.engine import Engine
from sqlalchemy.orm import Session, sessionmaker
from sqlalchemy.pool import StaticPool

from autopilot_korea.db.models import Base


class Database:
    def __init__(self, url: str):
        memory = url in ("sqlite://", "sqlite:///:memory:")
        if url.startswith("sqlite:///") and not memory:
            Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        kwargs: dict = ({"connect_args": {"check_same_thread": False, "timeout": 30}} if url.startswith("sqlite")
                        else {"pool_pre_ping": True})
        if memory:
            kwargs["poolclass"] = StaticPool  # 메모리 DB는 연결 하나를 모든 스레드가 공유해야 같은 DB를 본다
        self.engine: Engine = create_engine(url, future=True, **kwargs)
        self._factory = sessionmaker(bind=self.engine, expire_on_commit=False, future=True)

    def create_all(self) -> None:
        Base.metadata.create_all(self.engine)

    @contextmanager
    def session(self) -> Iterator[Session]:
        s = self._factory()
        try:
            yield s
            s.commit()
        except Exception:
            s.rollback()
            raise
        finally:
            s.close()
