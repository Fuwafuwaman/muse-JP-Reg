"""
年齢確認(CCV)を Instagram / Facebook 連携で解除するための WebUI 側進行管理。

連携には「Meta の承認 URL を人間がブラウザで開く」手順が 1 回だけ必要なので、
開始 → URL を画面に出す → 裏のスレッドで完了をポーリング → 解除
という非同期構成にしてある（`musepy.linking` の同期 API をラップする）。

解除できたら registered の ccv_status を not_required に落とす。
"""

from __future__ import annotations

import json
import logging
import threading
import time
from typing import Optional

from musepy import linking
from musepy.client import MuseClient

from . import db

logger = logging.getLogger("webui.ccv_link")

# クライアントが組める／URL が返るまでの待ち（秒）
_URL_WAIT = 45.0
_POLL_INTERVAL = 3.0


def _build_client(email: str, proxy: str = "") -> MuseClient:
    """registered の cookie で動くクライアントを組む。"""
    row = db.get_registered(email)
    if not row:
        raise linking.LinkingError(f"{email} は registered にいない")
    client = MuseClient(proxy=proxy or None, impersonate="chrome142", lang="ja",
                        timeout=60)
    for kv in (row.get("cookie_header") or "").split(";"):
        if "=" in kv:
            k, v = kv.strip().split("=", 1)
            client.s.cookies.set(k, v, domain="muse.ai")
    return client


def _default_proxies() -> list[Optional[str]]:
    """proxy_pool の先頭 → 直結 の順に試す（SOAX 等が 403 を返す場合の保険）。"""
    out: list[Optional[str]] = []
    try:
        pool = json.loads(db.get_setting("proxy_pool") or "[]")
    except Exception:
        pool = []
    for p in pool[:1]:
        if p:
            out.append(p)
    if None not in out:
        out.append(None)
    return out


def _pick_client(email: str) -> MuseClient:
    """連携 API が通るクライアントが取れるまで proxy を落としながら試す。"""
    last = None
    for proxy in _default_proxies():
        client = None
        try:
            client = _build_client(email, proxy or "")
            linking.linking_options(client)
            return client
        except Exception as exc:  # noqa: BLE001
            last = exc
            if client is not None:
                client.close()
    raise linking.LinkingError(f"接続先を確保できない: {last}")


def _resolve_account_type(client: MuseClient, wanted: str) -> str:
    """linkable の中から希望の種別を選ぶ。希望が無ければ先頭。"""
    wanted = (wanted or "").strip().lower()
    try:
        avail = linking.linking_options(client)
    except Exception:  # noqa: BLE001
        return wanted or linking.DEFAULT_ACCOUNT_TYPE
    if not avail:
        return wanted or linking.DEFAULT_ACCOUNT_TYPE
    return wanted if wanted in avail else avail[0]


class LinkSession:
    """1 件の連携試行。

    `external=False` なら自分でスレッドを回して完了までポーリングする。
    `external=True` は登録フロー側が既にポーリングしている場合の「表示だけ」の枠。
    """

    def __init__(self, email: str, account_type: str, timeout: float,
                 session_id: Optional[str] = None, external: bool = False):
        self.email = email
        self.account_type = account_type or linking.DEFAULT_ACCOUNT_TYPE
        self.timeout = timeout
        self.id = session_id or f"ccvlink:{int(time.time() * 1000)}-{email}"
        self.external = external
        self.url: Optional[str] = None
        self.expires_at = None
        self.state = "starting"   # starting / pending / 終端の next_action / error
        self.started_at = time.time()
        self.finished_at: Optional[float] = None
        self.error = ""
        self._url_ready = threading.Event()
        self._stop = threading.Event()
        if external:
            self._url_ready.set()

    @property
    def expired(self) -> bool:
        """承認 URL の寿命は実測で約 17 分。切れた試行は承認待ちとして数えない。"""
        return bool(self.expires_at) and time.time() > float(self.expires_at)

    def to_dict(self) -> dict:
        expired = self.expired
        return {
            "id": self.id,
            "email": self.email,
            "account_type": self.account_type,
            "url": self.url,
            "state": self.state,
            "hint": linking.describe(self.state) if self.state != "starting" else "開始中",
            "error": self.error,
            "external": self.external,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "expires_at": self.expires_at,
            "expired": expired,
            "done": (
                self.state in linking.TERMINAL_ACTIONS
                or self.state == "error"
                or expired
            ),
        }

    def finish(self, state: str, error: str = "") -> None:
        self.state = state
        if error:
            self.error = error
        self.finished_at = time.time()
        self._url_ready.set()

    def run(self) -> None:
        client = None
        try:
            client = _pick_client(self.email)
            self.account_type = _resolve_account_type(client, self.account_type)
            self.state = "pending"
            logger.info(f"[ccv_link] {self.email} 連携開始 ({self.account_type})")

            def _on_url(url: str, info: dict) -> None:
                self.url = url
                self.expires_at = info.get("expires_at")
                self._url_ready.set()

            res = linking.activate(
                client, self.account_type, timeout=self.timeout,
                interval=_POLL_INTERVAL,
                log=lambda m, *a: logger.info("[ccv_link] " + (m % a if a else m)),
                should_stop=self._stop.is_set,
                on_url=_on_url,
            )
            action = res.get("next_action") or "pending"
            self.state = action
            if res.get("ok"):
                db.save_registered({"email": self.email, "ccv_status": "not_required"})
                logger.info(f"[ccv_link] {self.email} 年齢確認を解除した")
            else:
                logger.info(f"[ccv_link] {self.email} 未解除: {action}（{linking.describe(action)}）")
        except Exception as exc:  # noqa: BLE001
            self.error = f"{type(exc).__name__}: {exc}"
            self.state = "error"
            logger.warning(f"[ccv_link] {self.email} 失敗: {self.error}")
        finally:
            if client is not None:
                client.close()
            self.finished_at = time.time()
            self._url_ready.set()

    def start(self) -> None:
        threading.Thread(target=self.run, name=f"ccvlink-{self.email}",
                         daemon=True).start()

    def wait_url(self, seconds: float = _URL_WAIT) -> None:
        """url が取れる（または URL が返らずに終わる）まで少し待つ。"""
        self._url_ready.wait(max(1.0, seconds))

    def cancel(self) -> None:
        self._stop.set()


class LinkManager:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._sessions: dict[str, LinkSession] = {}

    def start(self, email: str, account_type: str = "", timeout: float = 0.0,
              wait_url: float = _URL_WAIT) -> dict:
        email = (email or "").strip().lower()
        if not email:
            return {"ok": False, "error": "email が空"}
        if not db.get_registered(email):
            return {"ok": False, "error": f"{email} は registered にいない"}

        existing = self.active_for(email)
        if existing is not None:
            return {"ok": True, "reused": True, **existing.to_dict()}

        opts = db.get_ccv_options()
        sess = LinkSession(email, account_type or opts["ccv_link_account_type"],
                           timeout or opts["ccv_link_timeout"])
        with self._lock:
            self._sessions[sess.id] = sess
        sess.start()
        sess.wait_url(wait_url)
        out = {"ok": True, **sess.to_dict()}
        if sess.error and not sess.url:
            out["ok"] = False
            out["error"] = sess.error
        return out

    def active_for(self, email: str) -> Optional[LinkSession]:
        """同じメールでまだ終わっていない（URL も生きている）試行があれば返す。"""
        email = (email or "").strip().lower()
        with self._lock:
            for sess in self._sessions.values():
                if sess.email == email and not sess.to_dict()["done"]:
                    return sess
        return None

    def rearm(self, email: str, account_type: str = "", timeout: float = 0.0,
              wait_url: float = _URL_WAIT) -> dict:
        """切れた／放置した試行を捨てて、同じメールで承認 URL を出し直す。"""
        email = (email or "").strip().lower()
        if not email:
            return {"ok": False, "error": "email が空"}
        if not db.get_registered(email):
            return {"ok": False, "error": f"{email} は registered にいない"}

        # 古い試行は捨てるだけにする。まだ生きている URL を人間が踏んだケースでも、
        # その裏のスレッドが解除を拾えるように cancel せず走らせておく。
        with self._lock:
            gone = [s for s in self._sessions.values() if s.email == email]
            for k in [k for k, s in self._sessions.items() if s.email == email]:
                self._sessions.pop(k, None)

        opts = db.get_ccv_options()
        sess = LinkSession(email, account_type or opts["ccv_link_account_type"],
                           timeout or opts["ccv_link_timeout"])
        with self._lock:
            self._sessions[sess.id] = sess
        sess.start()
        sess.wait_url(wait_url)
        out = {"ok": True, "rearmed": True, "replaced": len(gone), **sess.to_dict()}
        if sess.error and not sess.url:
            out["ok"] = False
            out["error"] = sess.error
        return out

    # ── 登録フロー側がポーリングする場合の「表示だけ」登録 ──────────
    def track(self, email: str, url: str, account_type: str = "",
              expires_at=None, timeout: float = 0.0) -> dict:
        """フロー側が既に承認待ちしている試行を画面に出す。"""
        email = (email or "").strip().lower()
        existing = self.active_for(email)
        if existing is not None:
            existing.url = url or existing.url
            existing.account_type = account_type or existing.account_type
            existing.state = "pending"
            return existing.to_dict()
        sess = LinkSession(email, account_type, timeout, external=True)
        sess.url = url
        sess.expires_at = expires_at
        sess.state = "pending"
        with self._lock:
            self._sessions[sess.id] = sess
        logger.info(f"[ccv_link] {email} の承認 URL を画面に登録した")
        return sess.to_dict()

    def finish_for(self, email: str, state: str, error: str = "") -> int:
        """フロー完了時に、そのメールの未完了セッションを終端状態にする。"""
        email = (email or "").strip().lower()
        n = 0
        with self._lock:
            for sess in self._sessions.values():
                if sess.email == email and not sess.to_dict()["done"]:
                    sess.finish(state, error)
                    n += 1
        return n

    def get(self, session_id: str) -> Optional[LinkSession]:
        with self._lock:
            return self._sessions.get(session_id)

    def list(self, include_done: bool = True) -> list[dict]:
        with self._lock:
            sessions = list(self._sessions.values())
        out = [s.to_dict() for s in sessions]
        if not include_done:
            out = [d for d in out if not d["done"]]
        out.sort(key=lambda d: d["started_at"])
        return out

    def cancel(self, session_id: str) -> bool:
        sess = self.get(session_id)
        if not sess:
            return False
        sess.cancel()
        return True

    def clear_done(self) -> int:
        with self._lock:
            gone = [k for k, s in self._sessions.items() if s.to_dict()["done"]]
            for k in gone:
                self._sessions.pop(k, None)
        return len(gone)


MANAGER = LinkManager()


def pending_accounts() -> list[dict]:
    """年齢確認が未解除（または不明）の registered を返す（画面の「対象一覧」用）。"""
    out = []
    for row in db.list_registered_full(limit=5000):
        ccv = str((row.get("extra") or {}).get("ccv_status") or "").strip()
        if ccv == "not_required":
            continue
        out.append({
            "email": row.get("email"),
            "ccv_status": ccv or "unknown",
            "status": row.get("status") or "",
            "created_at": row.get("created_at") or 0,
        })
    out.sort(key=lambda d: d["created_at"], reverse=True)
    return out
