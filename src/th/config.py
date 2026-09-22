"""설정. 값은 전부 환경변수(.env)에서만 온다.

이게 도쿄 VPS 이전을 쉽게 만드는 핵심이다. 코드 어디에도 호스트명·경로·키가
하드코딩되어 있지 않으므로, 옮길 때 .env 한 파일만 챙기면 된다.
"""

from __future__ import annotations

from functools import lru_cache
from typing import Literal

from pydantic import Field, ValidationError, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


# 각 필드는 validation_alias 로 환경변수 이름을 명시한다.
# (alias 가 아니라 validation_alias 를 쓰는 이유: populate_by_name 과 함께
#  쓰면 환경변수로도, 필드명으로도 값을 넣을 수 있어 테스트가 편하다.)
class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
        case_sensitive=False,
        populate_by_name=True,
    )

    # ── 환경 ──────────────────────────────────────────────
    env: Literal["local", "vps", "prod"] = Field("local", validation_alias="TH_ENV")
    log_level: str = Field("INFO", validation_alias="TH_LOG_LEVEL")
    owner_id: str = Field(
        "00000000-0000-0000-0000-000000000001", validation_alias="TH_OWNER_ID"
    )

    # ── DB ────────────────────────────────────────────────
    db_host: str = Field("localhost", validation_alias="TH_DB_HOST")
    db_port: int = Field(5432, validation_alias="TH_DB_PORT")
    db_user: str = Field("th", validation_alias="POSTGRES_USER")
    db_password: str = Field("", validation_alias="POSTGRES_PASSWORD")
    db_name: str = Field("trading_helper", validation_alias="POSTGRES_DB")

    # ── Redis ─────────────────────────────────────────────
    redis_host: str = Field("localhost", validation_alias="TH_REDIS_HOST")
    redis_port: int = Field(6379, validation_alias="TH_REDIS_PORT")

    # ── 수집 대상 ─────────────────────────────────────────
    symbols_raw: str = Field("SOXLUSDT", validation_alias="TH_SYMBOLS")
    underlying_map_raw: str = Field("SOXLUSDT:SOXL", validation_alias="TH_UNDERLYING_MAP")
    reference_tickers_raw: str = Field("SOXX,^VIX,SPY", validation_alias="TH_REFERENCE_TICKERS")

    # ── 바이낸스 ──────────────────────────────────────────
    binance_api_key: str = Field("", validation_alias="TH_BINANCE_API_KEY")
    binance_api_secret: str = Field("", validation_alias="TH_BINANCE_API_SECRET")
    fapi_base: str = Field("https://fapi.binance.com", validation_alias="TH_BINANCE_FAPI_BASE")
    ws_base: str = Field("wss://fstream.binance.com", validation_alias="TH_BINANCE_WS_BASE")

    # ── 캘린더 이벤트 자동 수집 (Phase 1.5) ─────────────────
    fred_api_key: str = Field("", validation_alias="TH_FRED_API_KEY")
    earnings_symbols_raw: str = Field(
        "NVDA,AVGO,AMD,TSM,INTC,QCOM,TXN,MU,LRCX,AMAT",
        validation_alias="TH_EARNINGS_SYMBOLS",
    )
    calendar_sync_days: int = Field(90, validation_alias="TH_CALENDAR_SYNC_DAYS")

    # ── 리스크 (Phase 2) ───────────────────────────────────
    # 한 번 매매에 계좌의 몇 %를 걸 것인가. 1R 의 정의이자 일일 한도의 단위.
    risk_pct: float = Field(0.5, validation_alias="TH_RISK_PCT")

    # ── 실행 모드 (자동매매 확장점) ────────────────────────
    execution_mode: Literal["none", "paper", "live"] = Field(
        "none", validation_alias="TH_EXECUTION_MODE"
    )
    # 동시에 열어둘 브래킷 수. 시그널 엔진이 4종목을 보면 4 로 올린다 (docs/15 10절).
    max_open_brackets: int = Field(1, validation_alias="TH_MAX_OPEN_BRACKETS")

    # ── v2.2 시그널 엔진 (영상 200/20 · 5분봉) — 페이퍼 전용 ─────
    signal_enabled: bool = Field(True, validation_alias="TH_SIGNAL_ENABLED")
    signal_symbols_raw: str = Field(
        "SOXLUSDT,KORUUSDT,BTCUSDT,ETHUSDT", validation_alias="TH_SIGNAL_SYMBOLS"
    )
    signal_mode: Literal["both", "pullback", "squeeze"] = Field("both", validation_alias="TH_SIGNAL_MODE")
    # 노셔널 = 시드 × 이 배수. 1.0 = "시드의 10% 를 10배" (docs/16 돈 규칙)
    signal_notional_x: float = Field(1.0, validation_alias="TH_SIGNAL_NOTIONAL_X")
    signal_hard_stop_pct: float = Field(3.0, validation_alias="TH_SIGNAL_HARD_STOP_PCT")

    # ── v3.0 뉴스·이벤트 매매 엔진 (docs/17) — 페이퍼 전용 ─────────
    news_enabled: bool = Field(False, validation_alias="TH_NEWS_ENABLED")
    # observe: 수집·판단만 · paper: 바로 페이퍼 주문 · approve: 승인된 것만 페이퍼 주문
    # live 는 아직 없다 — 설정해도 approve 로 동작하고 경고를 남긴다 (docs/17 §5)
    news_exec: Literal["observe", "paper", "approve", "live"] = Field("paper", validation_alias="TH_NEWS_EXEC")
    news_model: str = Field("claude-opus-5", validation_alias="TH_NEWS_MODEL")
    news_triage_model: str = Field("claude-haiku-4-5", validation_alias="TH_NEWS_TRIAGE_MODEL")
    # 본판단의 사고 깊이. 비용의 대부분이 사고 토큰이라 low/medium 이면 절반 이하로 준다 (PHASE9 문서 §5)
    news_effort: Literal["low", "medium", "high"] = Field("high", validation_alias="TH_NEWS_EFFORT")
    # 켤 소스. 비용은 판단 건수에 비례하고 건수는 소스가 정한다 — RSS 를 빼면 절반 이하
    news_sources_raw: str = Field("telegram,edgar,yahoo,gnews,finnhub", validation_alias="TH_NEWS_SOURCES")
    anthropic_api_key: str = Field("", validation_alias="ANTHROPIC_API_KEY")
    news_min_confidence: float = Field(0.70, validation_alias="TH_NEWS_MIN_CONFIDENCE")
    # 작전 하나의 마진 합계(시드 %) × 배율 = 노셔널. 기본 10% × 10 = 시드 1.0배 (docs/17 D6)
    news_margin_pct_max: float = Field(10.0, validation_alias="TH_NEWS_MARGIN_PCT_MAX")
    news_leverage: float = Field(10.0, validation_alias="TH_NEWS_LEVERAGE")
    news_stop_min_pct: float = Field(1.0, validation_alias="TH_NEWS_STOP_MIN_PCT")
    news_stop_max_pct: float = Field(5.0, validation_alias="TH_NEWS_STOP_MAX_PCT")
    news_max_hold_hours: int = Field(168, validation_alias="TH_NEWS_MAX_HOLD_HOURS")
    news_approval_ttl_min: int = Field(30, validation_alias="TH_NEWS_APPROVAL_TTL_MIN")
    news_daily_usd_cap: float = Field(5.0, validation_alias="TH_NEWS_DAILY_USD_CAP")
    # 텔레그램 — 채널 읽기(사용자 세션) / 알림·승인(봇). 둘은 별개 자격이다
    telegram_api_id: int = Field(0, validation_alias="TH_TELEGRAM_API_ID")
    telegram_api_hash: str = Field("", validation_alias="TH_TELEGRAM_API_HASH")
    telegram_channels_raw: str = Field("", validation_alias="TH_TELEGRAM_CHANNELS")
    telegram_bot_token: str = Field("", validation_alias="TH_TELEGRAM_BOT_TOKEN")
    telegram_chat_id: str = Field("", validation_alias="TH_TELEGRAM_CHAT_ID")
    finnhub_api_key: str = Field("", validation_alias="TH_FINNHUB_API_KEY")
    edgar_user_agent: str = Field("", validation_alias="TH_EDGAR_USER_AGENT")

    @field_validator("symbols_raw", "reference_tickers_raw")
    @classmethod
    def _strip(cls, v: str) -> str:
        return v.strip()

    # ── 파생 속성 ─────────────────────────────────────────
    @property
    def symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.symbols_raw.split(",") if s.strip()]

    @property
    def signal_symbols(self) -> list[str]:
        return [s.strip().upper() for s in self.signal_symbols_raw.split(",") if s.strip()]

    @property
    def telegram_channels(self) -> list[str]:
        """@username 또는 숫자 id. 공백·빈 항목은 버린다."""
        return [c.strip() for c in self.telegram_channels_raw.split(",") if c.strip()]

    @property
    def news_sources(self) -> list[str]:
        return [s.strip().lower() for s in self.news_sources_raw.split(",") if s.strip()]

    @property
    def news_exec_effective(self) -> str:
        """`live` 는 아직 경로가 없다 — approve 로 내린다 (docs/17 §5)."""
        return "approve" if self.news_exec == "live" else self.news_exec

    @property
    def underlying_map(self) -> dict[str, str]:
        """{'SOXLUSDT': 'SOXL', ...}"""
        out: dict[str, str] = {}
        for pair in self.underlying_map_raw.split(","):
            pair = pair.strip()
            if not pair or ":" not in pair:
                continue
            perp, ticker = pair.split(":", 1)
            out[perp.strip().upper()] = ticker.strip().upper()
        return out

    @property
    def reference_tickers(self) -> list[str]:
        return [t.strip() for t in self.reference_tickers_raw.split(",") if t.strip()]

    @property
    def earnings_symbols(self) -> list[str]:
        return [
            t.strip().upper()
            for t in self.earnings_symbols_raw.split(",")
            if t.strip()
        ]

    @property
    def all_underlying_tickers(self) -> list[str]:
        seen: list[str] = []
        for t in list(self.underlying_map.values()) + self.reference_tickers:
            if t not in seen:
                seen.append(t)
        return seen

    @property
    def dsn(self) -> str:
        return (
            f"postgresql://{self.db_user}:{self.db_password}"
            f"@{self.db_host}:{self.db_port}/{self.db_name}"
        )

    @property
    def redis_url(self) -> str:
        return f"redis://{self.redis_host}:{self.redis_port}/0"


class ConfigError(Exception):
    """.env 값이 잘못됐을 때. 메시지를 그대로 사용자에게 보여준다."""


def _explain(err) -> str:
    """pydantic 의 오류 덩어리를 '어느 줄을 어떻게 고쳐라'로 바꾼다.

    설정 오류는 초보자가 가장 자주 만나는 오류인데, 기본 출력은 60줄짜리
    스택 트레이스라 어디를 고쳐야 하는지 알 수 없다. 여기서 한 줄로 줄인다.
    """
    lines = ["`.env` 파일에 잘못된 값이 있습니다.", ""]
    for e in err.errors():
        key = ".".join(str(x) for x in e["loc"]) or "?"
        got = e.get("input")
        lines.append(f"  {key} = {got!r}")
        ctx = e.get("ctx") or {}
        expected = ctx.get("expected")
        if expected:
            lines.append(f"    → 가능한 값: {expected}")
        else:
            lines.append(f"    → {e.get('msg', '')}")
        lines.append("")
    lines.append("`.env` 를 메모장으로 열어 위 줄을 고친 뒤 다시 실행하세요.")
    return "\n".join(lines)


@lru_cache(maxsize=1)
def get_settings() -> Settings:
    try:
        return Settings()
    except ValidationError as e:
        raise ConfigError(_explain(e)) from None
