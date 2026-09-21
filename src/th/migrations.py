"""SQL 마이그레이션 러너.

`db/init/` 은 컨테이너를 **처음 만들 때만** 실행된다. 이미 데이터가 쌓인 DB에
테이블을 추가하려면 별도 경로가 필요하고, 그게 `db/migrations/` 다.

규칙
----
* 파일명은 `001_이름.sql` 처럼 번호로 시작한다. 번호 순으로 적용된다.
* 이미 적용된 파일은 `schema_migrations` 에 기록되어 건너뛴다.
* 그래도 모든 마이그레이션은 **여러 번 돌려도 안전하게**(IF NOT EXISTS) 쓴다.
  기록이 날아가는 사고보다 중복 실행이 훨씬 흔하다.
"""

from __future__ import annotations

from pathlib import Path

from . import db
from .logging_setup import get_logger

log = get_logger(__name__)

# src/th/migrations.py → 프로젝트 루트/db/migrations
MIGRATIONS_DIR = Path(__file__).resolve().parents[2] / "db" / "migrations"

_ENSURE_TABLE = """
CREATE TABLE IF NOT EXISTS schema_migrations (
    version    text PRIMARY KEY,
    applied_at timestamptz NOT NULL DEFAULT now()
)
"""


def migration_files(directory: Path | None = None) -> list[Path]:
    d = directory or MIGRATIONS_DIR
    if not d.exists():
        return []
    return sorted(p for p in d.glob("*.sql") if p.is_file())


async def applied_versions() -> set[str]:
    async with db.acquire() as conn:
        await conn.execute(_ENSURE_TABLE)
        rows = await conn.fetch("SELECT version FROM schema_migrations")
    return {r["version"] for r in rows}


async def run_migrations(directory: Path | None = None) -> list[str]:
    """적용되지 않은 마이그레이션을 순서대로 실행하고, 적용한 목록을 반환."""
    files = migration_files(directory)
    if not files:
        log.info("마이그레이션 파일이 없습니다: %s", directory or MIGRATIONS_DIR)
        return []

    done = await applied_versions()
    applied: list[str] = []

    for path in files:
        version = path.stem
        if version in done:
            continue
        sql = path.read_text(encoding="utf-8")
        async with db.acquire() as conn:
            async with conn.transaction():
                await conn.execute(sql)
                await conn.execute(
                    "INSERT INTO schema_migrations (version) VALUES ($1) "
                    "ON CONFLICT (version) DO NOTHING",
                    version,
                )
        applied.append(version)
        log.info("마이그레이션 적용: %s", version)

    return applied


async def pending_versions(directory: Path | None = None) -> list[str]:
    done = await applied_versions()
    return [p.stem for p in migration_files(directory) if p.stem not in done]
