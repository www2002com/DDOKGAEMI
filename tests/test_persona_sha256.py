"""페르소나 해시 무결성 (§8-4, §17.1)."""

from __future__ import annotations

import shutil

import pytest

from autopilot_korea.agents.personas import GUARDRAIL_HEADING, PersonaLibrary
from autopilot_korea.settings import ConfigError, load_settings

GUARDRAIL = """## 규제 가드레일 (유사투자자문업 · 공통 블록 · 수정 금지)
- 이 출력은 자기 계정 운용을 위한 내부 리서치 자료이며, 불특정 다수에 대한 개별 투자 권유가 아니다.
- 손실보전, 이익보장, 확정 수익을 약속하거나 암시하는 표현을 쓰지 않는다.
- 특정인에게 매수나 매도를 권유하는 표현을 쓰지 않는다. 결론은 종목에 대한 평가와 근거로만 서술한다.
- 미공개 중요정보, 루머, 시세조종성 정보를 근거로 쓰지 않는다. 출처를 확인할 수 없는 정보는 "미확인"으로 표시한다.
- 최근 7일 이내의 뉴스와 공시만 새로운 정보로 취급한다. 그 이전 정보는 이미 가격에 반영되었다고 본다.
- 응답의 compliance_check 필드를 사실대로 채운다.
- 고지: 본 정보는 참고용이며 개별 투자 권유가 아닙니다.
"""


@pytest.fixture(scope="module")
def library():
    return PersonaLibrary.from_settings(load_settings())


def test_23_personas_present(library):
    assert len(library.personas) == 23
    assert [p.split("_")[0] for p in sorted(library.personas)] == [f"{i:02d}" for i in range(1, 24)]


def test_manifest_matches_files(library):
    assert library.verify() == [], "페르소나를 고쳤다면 `autopilot personas rehash`로 매니페스트를 갱신하십시오"


def test_every_persona_ends_with_identical_guardrail(library):
    for p in library.personas.values():
        text = p.path.read_text(encoding="utf-8")
        assert text.count(GUARDRAIL_HEADING) == 1, p.id
        assert text.endswith(GUARDRAIL), f"{p.id}: 공통 가드레일 블록이 마지막 섹션과 다릅니다"


def test_panels_follow_role_rules(library):
    library.validate_config(load_settings().personas)
    roles = {p.id: p.role for p in library.personas.values()}
    assert {pid for pid, r in roles.items() if r == "sentiment"} == {"16_retail_sentiment", "17_news_sentiment", "20_theme_sentiment"}
    assert {pid for pid, r in roles.items() if r == "control"} == {"01_default", "04_minimal", "05_hold_llm"}


def test_tampering_is_detected(tmp_path, library):
    copy = tmp_path / "prompts_kr"
    shutil.copytree(library.directory, copy)
    target = copy / "07_value.md"
    target.write_text(target.read_text(encoding="utf-8").replace("안전마진", "안전 마진"), encoding="utf-8")
    tampered = PersonaLibrary(copy)
    assert tampered.verify() == ["07_value.md: 해시 불일치"]
    with pytest.raises(ConfigError):
        tampered.validate_config(load_settings().personas)


def test_sentiment_persona_cannot_drive_decisions(library):
    cfg = dict(load_settings().personas)
    cfg["panels"] = dict(cfg["panels"], stage2_bull=["16_retail_sentiment"])
    with pytest.raises(ConfigError, match="감성 전용"):
        library.validate_config(cfg)
