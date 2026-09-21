import os

import pytest

from th.config import Settings


def _settings(**env) -> Settings:
    base = {
        "TH_SYMBOLS": "SOXLUSDT,NVDAUSDT",
        "TH_UNDERLYING_MAP": "SOXLUSDT:SOXL,NVDAUSDT:NVDA",
        "TH_REFERENCE_TICKERS": "SOXX,^VIX",
        "POSTGRES_PASSWORD": "pw",
    }
    base.update(env)
    # 환경변수는 만든 뒤 반드시 되돌린다 — 잘못된 값(yolo)으로 Settings 가 예외를 던지면
    # 되돌리지 못한 채 남아서, 뒤에 도는 다른 테스트가 전부 ConfigError 로 죽는다 (실제 사례).
    saved = {k: os.environ.get(k) for k in base}
    for k, v in base.items():
        os.environ[k] = v
    try:
        return Settings(_env_file=None)
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


def test_symbols_parsed_and_uppercased():
    s = _settings(TH_SYMBOLS=" soxlusdt , nvdausdt ")
    assert s.symbols == ["SOXLUSDT", "NVDAUSDT"]


def test_underlying_map():
    s = _settings()
    assert s.underlying_map == {"SOXLUSDT": "SOXL", "NVDAUSDT": "NVDA"}


def test_all_underlying_tickers_dedupes_and_preserves_order():
    s = _settings(TH_REFERENCE_TICKERS="SOXL,SOXX,^VIX")
    # SOXL 이 매핑과 참조 양쪽에 있어도 한 번만
    assert s.all_underlying_tickers == ["SOXL", "NVDA", "SOXX", "^VIX"]


def test_dsn_uses_env_values():
    s = _settings(TH_DB_HOST="db", TH_DB_PORT="5555", POSTGRES_USER="u", POSTGRES_DB="d")
    assert s.dsn == "postgresql://u:pw@db:5555/d"


def test_execution_mode_defaults_to_none():
    """자동매매는 명시적으로 켜야만 켜진다."""
    s = _settings()
    assert s.execution_mode == "none"


def test_invalid_execution_mode_rejected():
    with pytest.raises(Exception):
        _settings(TH_EXECUTION_MODE="yolo")


# ── .env 오류 메시지 ────────────────────────────────────
# 초보자가 가장 자주 만나는 오류인데 기본 출력은 60줄짜리 스택 트레이스다.
# 어느 줄을 어떻게 고쳐야 하는지 한 줄로 알려줘야 한다.

def test_잘못된_설정값은_읽을_수_있는_오류가_된다():
    import os

    from th.config import ConfigError, get_settings

    saved = os.environ.get("TH_EXECUTION_MODE")
    get_settings.cache_clear()
    os.environ["TH_EXECUTION_MODE"] = "yolo"
    try:
        try:
            get_settings()
        except ConfigError as e:
            msg = str(e)
        else:
            raise AssertionError("잘못된 값인데 통과했다")
    finally:
        if saved is None:
            os.environ.pop("TH_EXECUTION_MODE", None)
        else:
            os.environ["TH_EXECUTION_MODE"] = saved
        get_settings.cache_clear()

    assert "TH_EXECUTION_MODE" in msg, "어느 항목인지 알려줘야 한다"
    assert "yolo" in msg, "어떤 값이 문제인지 알려줘야 한다"
    assert "paper" in msg, "가능한 값을 알려줘야 한다"
    assert ".env" in msg, "어느 파일을 고쳐야 하는지 알려줘야 한다"
    assert "Traceback" not in msg and "pydantic" not in msg
