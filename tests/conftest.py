from __future__ import annotations

import dataclasses
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

from autopilot_korea.db import Database  # noqa: E402
from autopilot_korea.settings import load_settings  # noqa: E402


@pytest.fixture
def settings(tmp_path):
    """실제 config/*.yaml을 쓰되, 보고서·데이터는 임시 폴더에 쓰도록 project_root만 바꾼다."""
    base = load_settings()
    personas = dict(base.personas)
    personas["persona_dir"] = str(base.project_root / base.personas["persona_dir"])
    return dataclasses.replace(base, project_root=tmp_path, personas=personas, db_url=f"sqlite:///{tmp_path}/t.db")


@pytest.fixture
def db(settings):
    database = Database(settings.db_url)
    database.create_all()
    return database
