"""환경 변수(.env)와 config/*.yaml을 읽어 하나의 Settings 객체로 묶는다."""

from __future__ import annotations

import os
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

import yaml
from dotenv import load_dotenv
from pydantic import BaseModel

PROJECT_ROOT = Path(__file__).resolve().parents[2]
MODES = ("dry_run", "paper", "live")


class ConfigError(RuntimeError):
    """설정이 잘못되어 시스템을 시작하면 안 되는 경우."""


# ── 타입이 있는 제약 설정 (constraints.yaml) ──────────────────────────────

class CashEtf(BaseModel):
    ticker: str
    name: str


class PortfolioRules(BaseModel):
    initial_capital_krw: int
    benchmark: str
    target_positions: int
    min_equity_positions: int
    max_position_weight: float
    max_sector_weight: float
    max_cash_etf_weight: float
    require_positive_expected_return: bool
    tie_breaker: str
    weight_tolerance: float
    cash_etfs: list[CashEtf]

    @property
    def cash_etf_tickers(self) -> set[str]:
        return {e.ticker for e in self.cash_etfs}


class TurnoverRules(BaseModel):
    monthly_cap: float


class RiskRules(BaseModel):
    portfolio_mdd_review: float
    drawdown_cash_target: float
    position_thesis_review: float
    liquidity_exclude_bottom_pct: float
    max_order_pct_of_adv: float
    blocked_flags: list[str]


class ExecutionRules(BaseModel):
    auto_approve_modes: list[str]
    live_auto_max_orders: int
    live_auto_max_gross_pct: float
    live_reprice_max_pct: float
    market_open: str
    market_close: str
    default_order_type: str
    limit_offset_pct: float
    min_trade_value_krw: float
    min_weight_change: float
    max_order_value_krw: float
    paper_commission_rate: float
    paper_sell_tax_rate: float
    paper_slippage_pct: float


class PipelineRules(BaseModel):
    news_window_days: int
    screening_top_n: int
    bull_agents: int
    bear_agents: int
    scenario_agents: int
    scenario_max_candidates: int
    optimizer_max_retries: int
    swap_score_gap: float
    max_concurrency: int


class Constraints(BaseModel):
    portfolio: PortfolioRules
    turnover: TurnoverRules
    risk: RiskRules
    execution: ExecutionRules
    pipeline: PipelineRules


@dataclass(frozen=True)
class Settings:
    mode: str
    db_url: str
    config_dir: Path
    project_root: Path
    anthropic_api_key: str | None
    dart_api_key: str | None
    krx_id: str | None
    krx_pw: str | None
    naver_client_id: str | None
    naver_client_secret: str | None
    ecos_api_key: str | None
    toss_app_key: str | None
    toss_app_secret: str | None
    toss_account_no: str | None
    toss_base_url: str
    celery_broker_url: str
    constraints: Constraints
    schedule: dict[str, Any]
    personas: dict[str, Any]
    features: dict[str, Any]
    models: dict[str, Any]
    sectors: dict[str, Any]

    def feature(self, name: str) -> bool:
        return bool(self.features.get(name, False))

    @property
    def halt_file(self) -> Path:
        """비상 정지 스위치 파일. 있으면 자동 승인과 주문 제출을 모두 멈춘다 (`autopilot halt` / `autopilot resume`)."""
        return self.project_root / "data" / "HALT"

    def halted(self) -> bool:
        return self.halt_file.exists() or os.environ.get("AUTOPILOT_HALT", "").strip() == "1"

    def validate(self) -> None:
        """시작 전에 운영 규칙 위반을 막는다 (§10 SELF_MODIFY 운영 금지)."""
        if self.mode not in MODES:
            raise ConfigError(f"AUTOPILOT_MODE는 {MODES} 중 하나여야 합니다: {self.mode!r}")
        if self.mode in ("paper", "live") and self.feature("SELF_MODIFY"):
            raise ConfigError("SELF_MODIFY는 운영 금지 토글입니다. paper/live 모드에서는 끄십시오 (§10).")
        if self.mode == "live":
            missing = [k for k in ("toss_app_key", "toss_app_secret") if not getattr(self, k)]
            if missing:
                raise ConfigError(f"live 모드에는 토스증권 자격 증명이 필요합니다: {missing}")


def _load_yaml(config_dir: Path, name: str) -> dict[str, Any]:
    path = config_dir / name
    if not path.exists():
        raise ConfigError(f"설정 파일이 없습니다: {path}")
    with path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def _env(name: str) -> str | None:
    value = os.environ.get(name, "").strip()
    return value or None


def load_settings(config_dir: Path | None = None, env_file: Path | None = None) -> Settings:
    load_dotenv(env_file or PROJECT_ROOT / ".env", override=False)
    config_dir = Path(config_dir or os.environ.get("AUTOPILOT_CONFIG_DIR") or PROJECT_ROOT / "config")
    db_url = _env("DB_URL") or f"sqlite:///{PROJECT_ROOT / 'data' / 'autopilot.db'}"
    return Settings(
        mode=(_env("AUTOPILOT_MODE") or "dry_run").lower(),
        db_url=db_url,
        config_dir=config_dir,
        project_root=PROJECT_ROOT,
        anthropic_api_key=_env("ANTHROPIC_API_KEY"),
        dart_api_key=_env("DART_API_KEY"),
        krx_id=_env("KRX_ID"),
        krx_pw=_env("KRX_PW"),
        naver_client_id=_env("NAVER_CLIENT_ID"),
        naver_client_secret=_env("NAVER_CLIENT_SECRET"),
        ecos_api_key=_env("ECOS_API_KEY"),
        toss_app_key=_env("TOSS_APP_KEY"),
        toss_app_secret=_env("TOSS_APP_SECRET"),
        toss_account_no=_env("TOSS_ACCOUNT_NO"),
        toss_base_url=_env("TOSS_BASE_URL") or "https://openapi.tossinvest.com",
        celery_broker_url=_env("CELERY_BROKER_URL") or "redis://localhost:6379/0",
        constraints=Constraints.model_validate(_load_yaml(config_dir, "constraints.yaml")),
        schedule=_load_yaml(config_dir, "schedule.yaml"),
        personas=_load_yaml(config_dir, "personas.yaml"),
        features=_load_yaml(config_dir, "features.yaml"),
        models=_load_yaml(config_dir, "models.yaml"),
        sectors=_load_yaml(config_dir, "sectors.yaml"),
    )


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    return load_settings()
