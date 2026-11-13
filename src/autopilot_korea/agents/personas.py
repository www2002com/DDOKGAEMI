"""페르소나 = 데이터 파일 (§0-1). prompts_kr/*.md를 읽고 sha256 매니페스트로 무결성을 고정한다."""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

from autopilot_korea.settings import ConfigError, Settings

MANIFEST_NAME = "manifest.json"
GUARDRAIL_HEADING = "## 규제 가드레일 (유사투자자문업 · 공통 블록 · 수정 금지)"
ROLES = ("analyst", "control", "sentiment")


@dataclass(frozen=True)
class Persona:
    id: str
    name: str
    name_en: str
    role: str
    default_stance: str
    key_variables: tuple[int, ...]
    body: str
    sha256: str
    path: Path


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def parse_persona(path: Path) -> Persona:
    text = path.read_text(encoding="utf-8")
    if not text.startswith("---\n"):
        raise ConfigError(f"페르소나 front matter가 없습니다: {path.name}")
    _, front, body = text.split("---\n", 2)
    meta: dict[str, Any] = yaml.safe_load(front) or {}
    persona = Persona(
        id=str(meta["id"]),
        name=str(meta["name"]),
        name_en=str(meta.get("name_en", "")),
        role=str(meta["role"]),
        default_stance=str(meta.get("default_stance", "neutral")),
        key_variables=tuple(int(n) for n in meta.get("key_variables", [])),
        body=body.strip(),
        sha256=sha256_file(path),
        path=path,
    )
    if persona.id != path.stem:
        raise ConfigError(f"페르소나 id({persona.id})가 파일 이름({path.stem})과 다릅니다")
    if persona.role not in ROLES:
        raise ConfigError(f"{path.name}: role은 {ROLES} 중 하나여야 합니다")
    return persona


class PersonaLibrary:
    def __init__(self, directory: Path):
        self.directory = directory
        files = sorted(p for p in directory.glob("*.md") if p.name != "README.md")
        if not files:
            raise ConfigError(f"페르소나 파일이 없습니다: {directory}")
        self.personas: dict[str, Persona] = {}
        for path in files:
            persona = parse_persona(path)
            self.personas[persona.id] = persona

    @classmethod
    def from_settings(cls, settings: Settings) -> "PersonaLibrary":
        directory = Path(settings.personas["persona_dir"])
        if not directory.is_absolute():
            directory = settings.project_root / directory
        return cls(directory)

    def get(self, persona_id: str) -> Persona:
        try:
            return self.personas[persona_id]
        except KeyError as exc:
            raise ConfigError(f"알 수 없는 페르소나: {persona_id}") from exc

    # ── 무결성 (§8-4) ──────────────────────────────────────────────────

    @property
    def manifest_path(self) -> Path:
        return self.directory / MANIFEST_NAME

    def current_hashes(self) -> dict[str, str]:
        return {p.path.name: p.sha256 for p in sorted(self.personas.values(), key=lambda p: p.id)}

    def verify(self) -> list[str]:
        """매니페스트와 다른 파일 목록. 비어 있으면 무결성 정상."""
        if not self.manifest_path.exists():
            return [f"{MANIFEST_NAME} 없음 — `autopilot personas rehash` 실행 필요"]
        recorded = json.loads(self.manifest_path.read_text(encoding="utf-8"))["files"]
        current = self.current_hashes()
        problems = [f"{name}: 해시 불일치" for name, h in current.items() if recorded.get(name) not in (None, h)]
        problems += [f"{name}: 매니페스트에 없음" for name in current if name not in recorded]
        problems += [f"{name}: 파일 없음" for name in recorded if name not in current]
        return problems

    def rehash(self) -> Path:
        payload = {"algorithm": "sha256", "files": self.current_hashes()}
        self.manifest_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        return self.manifest_path

    # ── 패널 구성 검증 ─────────────────────────────────────────────────

    def validate_config(self, cfg: dict[str, Any], *, require_integrity: bool = True) -> None:
        """personas.yaml의 패널이 실제 파일·역할 규칙과 맞는지 시작 시 확인한다."""
        if require_integrity and (problems := self.verify()):
            raise ConfigError("페르소나 무결성 검증 실패: " + "; ".join(problems))
        disabled = set(cfg.get("disabled") or [])
        panels: dict[str, list[str]] = cfg.get("panels", {})
        stage_personas: dict[str, str] = cfg.get("stage_personas", {})
        used = {pid for ids in panels.values() for pid in ids} | set(stage_personas.values())
        for pid in used:
            self.get(pid)
        decision_ids = set(panels.get("stage2_bull", [])) | set(panels.get("stage2_bear", [])) | set(panels.get("operational", []))
        decision_ids |= {v for k, v in stage_personas.items() if k.startswith("stage")}
        if bad := sorted(pid for pid in decision_ids if self.get(pid).role == "sentiment"):
            raise ConfigError(f"감성 전용 페르소나는 의사결정 단계에 쓸 수 없습니다: {bad}")
        stage2 = set(panels.get("stage2_bull", [])) | set(panels.get("stage2_bear", []))
        if bad := sorted(pid for pid in stage2 if self.get(pid).role == "control"):
            raise ConfigError(f"통제군 페르소나는 Stage 2 패널에 쓸 수 없습니다: {bad}")
        if bad := sorted(used & disabled):
            raise ConfigError(f"비활성화된 페르소나가 패널에 있습니다: {bad}")
