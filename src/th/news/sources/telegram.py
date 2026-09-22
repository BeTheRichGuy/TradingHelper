"""텔레그램 채널 읽기 — **사용자 계정 세션** (Telethon).

봇은 채널을 읽을 수 없다. 사용자가 구독한 채널을 읽으려면 사용자 계정으로 로그인한 세션이
필요하다 (docs/17 D3). 첫 로그인은 전화번호·인증코드가 필요하므로 **사용자가 직접**
`th news telegram-login` 을 한 번 친다. 세션 파일은 `data/telegram.session` — git 밖이다.

`telethon` 은 함수 안에서만 임포트한다.
"""

from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path
from typing import Sequence

from ...logging_setup import get_logger
from .. import tickers
from ..models import SOURCE_TELEGRAM, NewsItem
from .base import Sink

log = get_logger(__name__)

DEFAULT_SESSION = Path("data") / "telegram"      # Telethon 이 .session 을 붙인다
_RECONNECT_WAIT_S = 60
_MAX_TITLE = 200
_MAX_BODY = 4000


def message_to_item(*, chat_id: int, chat_title: str, username: str | None, msg_id: int,
                    date: datetime | None, text: str | None) -> NewsItem | None:
    """텔레그램 메시지 → NewsItem. 순수 함수. 본문이 없으면 None (사진만 있는 메시지 등)."""
    text = (text or "").strip()
    if not text:
        return None
    lines = [ln.strip() for ln in text.splitlines() if ln.strip()]
    title = lines[0][:_MAX_TITLE]
    body = "\n".join(lines[1:])[:_MAX_BODY]
    ts = date or datetime.now(UTC)
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=UTC)
    url = f"https://t.me/{username}/{msg_id}" if username else ""
    return NewsItem(
        source=SOURCE_TELEGRAM, source_id=f"{chat_id}:{msg_id}", ts=ts.astimezone(UTC),
        title=title, body=body, url=url, channel=(chat_title or str(chat_id))[:120],
        symbols=tickers.extract(text), raw={"chat_id": chat_id, "msg_id": msg_id},
    )


class TelegramSource:
    """구독 채널의 새 메시지를 푸시로 받는다. `PushSource`."""

    name = SOURCE_TELEGRAM

    def __init__(self, api_id: int, api_hash: str, channels: Sequence[str], *,
                 session: Path | str = DEFAULT_SESSION) -> None:
        self.api_id = int(api_id)
        self.api_hash = api_hash
        self.channels = tuple(c.strip() for c in channels if c.strip())
        self.session = Path(session)
        self.connected = False
        self.resolved: dict[int, str] = {}      # chat id → 표시 이름

    def configured(self) -> tuple[bool, str]:
        if not self.api_id or not self.api_hash:
            return False, "TH_TELEGRAM_API_ID / TH_TELEGRAM_API_HASH 가 없습니다"
        if not self.channels:
            return False, "TH_TELEGRAM_CHANNELS 가 비어 있습니다"
        return True, ""

    async def run(self, stop_evt, sink: Sink) -> None:
        ok, why = self.configured()
        if not ok:
            log.warning("텔레그램 수집을 건너뜁니다 — %s", why)
            return
        try:
            from telethon import TelegramClient, events
        except ImportError:
            log.error("telethon 이 설치되어 있지 않습니다: pip install -e \".[news]\"")
            return

        self.session.parent.mkdir(parents=True, exist_ok=True)
        while not stop_evt.is_set():
            client = TelegramClient(str(self.session), self.api_id, self.api_hash)
            try:
                await client.connect()
                if not await client.is_user_authorized():
                    log.error("텔레그램 세션이 로그인돼 있지 않습니다 — 터미널에서 `th news telegram-login` 을 한 번 치세요")
                    return
                chats = []
                for ch in self.channels:
                    try:
                        ent = await client.get_entity(ch)
                        chats.append(ent)
                        self.resolved[int(ent.id)] = getattr(ent, "title", None) or ch
                    except Exception as e:  # noqa: BLE001
                        log.warning("텔레그램 채널 %s 를 못 찾았습니다: %s", ch, e)
                if not chats:
                    log.error("읽을 수 있는 텔레그램 채널이 하나도 없습니다")
                    return

                @client.on(events.NewMessage(chats=chats))
                async def _on_msg(ev):  # noqa: ANN001
                    try:
                        chat = await ev.get_chat()
                        item = message_to_item(
                            chat_id=int(ev.chat_id), chat_title=getattr(chat, "title", "") or "",
                            username=getattr(chat, "username", None), msg_id=int(ev.message.id),
                            date=ev.message.date, text=ev.message.message,
                        )
                        if item is not None:
                            await sink(item)
                    except Exception:  # noqa: BLE001
                        log.exception("텔레그램 메시지 처리 실패")

                self.connected = True
                log.info("텔레그램 채널 %d개 수신 중: %s", len(chats), ", ".join(self.resolved.values()))
                stop_task = asyncio.create_task(stop_evt.wait())
                dc_task = asyncio.create_task(client.disconnected)
                done, _ = await asyncio.wait({stop_task, dc_task}, return_when=asyncio.FIRST_COMPLETED)
                for t in (stop_task, dc_task):
                    if not t.done():
                        t.cancel()
                if stop_task in done:
                    return
                log.warning("텔레그램 연결이 끊겼습니다 — %d초 뒤 다시 붙습니다", _RECONNECT_WAIT_S)
            except Exception as e:  # noqa: BLE001
                log.warning("텔레그램 연결 실패 (%s) — %d초 뒤 재시도", e, _RECONNECT_WAIT_S)
            finally:
                self.connected = False
                try:
                    await client.disconnect()
                except Exception:  # noqa: BLE001
                    pass
            try:
                await asyncio.wait_for(stop_evt.wait(), timeout=_RECONNECT_WAIT_S)
            except (TimeoutError, asyncio.TimeoutError):
                pass


async def login(api_id: int, api_hash: str, *, session: Path | str = DEFAULT_SESSION) -> list[tuple[str, str]]:
    """대화형 로그인 (전화번호·코드는 Telethon 이 터미널에서 묻는다). 채널 목록을 돌려준다."""
    from telethon import TelegramClient

    session = Path(session)
    session.parent.mkdir(parents=True, exist_ok=True)
    client = TelegramClient(str(session), int(api_id), api_hash)
    await client.start()
    me = await client.get_me()
    log.info("텔레그램 로그인: %s", getattr(me, "username", None) or getattr(me, "first_name", ""))
    chans: list[tuple[str, str]] = []
    async for d in client.iter_dialogs():
        if getattr(d, "is_channel", False):
            uname = getattr(d.entity, "username", None)
            chans.append((d.name or "", f"@{uname}" if uname else str(d.entity.id)))
    await client.disconnect()
    return chans
