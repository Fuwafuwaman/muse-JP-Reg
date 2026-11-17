"""Muse 全自动批量调度控制器（对齐 musepy/runner.py 调度语义）。

核心调度特性：
- Worker 独占 sticky 代理（claim / release）：不抢其他 worker 的代理，池耗尽才共享。
- max_per_ip：按每个代理的验证成功数计数（默认 6），达上限自动释放并切换其他代理。
- pre-send 传输失败换代理重试：在发出 OTP 前（ip_check / bootstrap / restart）遇到网络失败，
  零副作用释放坏代理、标记内存黑名单，同邮箱换代理重试最多 3 次，不浪费邮箱。
- stop_on_reject（默认开启）：遇到 region_waitlist（地区受限，账号已保留）、server_rejected、
  OTP 超时或邀请码耗尽时自动暂停整批并推送告警横幅，防止烧号；用户排查后可随时点击恢复。
"""
from __future__ import annotations

import logging
import queue
import random
import threading
import time
from typing import Optional

from mail_providers import MailProviderError, get_provider_class
from musepy.runner import proxy_endpoint

from . import db, registrar

logger = logging.getLogger("auto_loop")


class AutoLoopState:
    STOPPED = "stopped"
    RUNNING = "running"
    PAUSED = "paused"


def _parse_proxy_pool(text: str) -> list[str]:
    out: list[str] = []
    seen: set[str] = set()
    for line in (text or "").splitlines():
        s = line.strip()
        if s and not s.startswith("#") and s not in seen:
            seen.add(s)
            out.append(s)
    return out


class AutoLoopController:
    def __init__(self):
        self._lock = threading.RLock()
        self._state = AutoLoopState.STOPPED
        self._manage_thread: Optional[threading.Thread] = None
        self._workers: list[threading.Thread] = []
        self._options: dict = {}
        self._stop_event = threading.Event()
        self._pause_event = threading.Event()

        self._started_at: float = 0.0
        self._registered_ok = 0
        self._registered_waitlist = 0
        self._registered_pending = 0
        self._registered_fail = 0
        self._ip_switches = 0

        self._worker_status: dict[int, dict] = {}
        self._last_message = ""
        self._subscribers: list[queue.Queue] = []

        self._proxy_pool: list[str] = []
        self._proxy_claims: set[str] = set()
        self._pool_bad: set[str] = set()
        self._concurrency: int = 1
        self._max_per_ip: int = 6
        self._target_count: int = 0
        self._stop_on_reject: bool = True

    def start(self, options: dict) -> dict:
        with self._lock:
            if self._state in (AutoLoopState.RUNNING, AutoLoopState.PAUSED):
                return {"ok": False, "error": f"已经在运行中 (state={self._state})"}

            self._stop_event.clear()
            self._pause_event.clear()
            self._options = dict(options or {})
            self._state = AutoLoopState.RUNNING
            self._started_at = time.time()
            self._registered_ok = 0
            self._registered_waitlist = 0
            self._registered_pending = 0
            self._registered_fail = 0
            self._ip_switches = 0
            self._worker_status.clear()
            self._proxy_claims.clear()
            self._pool_bad.clear()

            self._concurrency = max(1, min(20, int(self._options.get("concurrency") or 1)))
            self._max_per_ip = max(1, int(self._options.get("max_per_ip") or 6))
            self._target_count = max(0, int(self._options.get("target_count") or 0))
            self._stop_on_reject = bool(self._options.get("stop_on_reject", True))
            self._proxy_pool = _parse_proxy_pool(self._options.get("proxy_pool") or "")
            self._last_message = (
                f"批量调度已启动 (并发={self._concurrency}, "
                f"代理池={len(self._proxy_pool)}, max_per_ip={self._max_per_ip})"
            )

            self._manage_thread = threading.Thread(
                target=self._manage_loop, daemon=True, name="auto-loop-manage"
            )
            self._manage_thread.start()

        self._broadcast("state", self._snapshot())
        return {
            "ok": True,
            "state": self._state,
            "concurrency": self._concurrency,
            "proxy_pool_size": len(self._proxy_pool),
            "max_per_ip": self._max_per_ip,
            "target_count": self._target_count,
        }

    def pause(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.RUNNING:
                return {"ok": False, "error": f"当前状态={self._state}，无法暂停"}
            self._pause_event.set()
            self._state = AutoLoopState.PAUSED
            self._last_message = "已请求暂停（当前执行中的账号跑完后生效）"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def resume(self) -> dict:
        with self._lock:
            if self._state != AutoLoopState.PAUSED:
                return {"ok": False, "error": f"当前状态={self._state}，无法恢复"}
            # 恢复时清空内存坏代理表，允许用户修复代理后继续使用
            self._pool_bad.clear()
            self._pause_event.clear()
            self._state = AutoLoopState.RUNNING
            self._last_message = "批量任务已恢复运行"
        self._broadcast("state", self._snapshot())
        return {"ok": True, "state": self._state}

    def stop(self) -> dict:
        with self._lock:
            if self._state == AutoLoopState.STOPPED:
                return {"ok": False, "error": "当前未在运行"}
            self._stop_event.set()
            self._pause_event.clear()
            self._last_message = "已请求停止（等待当前 worker 收尾）"
        self._broadcast("state", self._snapshot())
        return {"ok": True}

    def status(self) -> dict:
        return self._snapshot()

    def subscribe(self) -> queue.Queue:
        q: queue.Queue = queue.Queue(maxsize=100)
        with self._lock:
            self._subscribers.append(q)
        try:
            q.put_nowait({"kind": "state", "data": self._snapshot()})
        except queue.Full:
            pass
        return q

    def unsubscribe(self, q: queue.Queue):
        with self._lock:
            try:
                self._subscribers.remove(q)
            except ValueError:
                pass

    # ──────────────────────── Sticky 代理调度 ────────────────────────

    def _pick_proxy(self, *, current: Optional[str] = None, avoid: Optional[str] = None) -> str:
        """对齐 musepy/runner.py 的独占 sticky 代理选择逻辑。"""
        with self._lock:
            if not self._proxy_pool:
                return (self._options.get("proxy") or "").strip()

            if current and current not in self._pool_bad and current != avoid:
                self._proxy_claims.add(current)
                return current

            free_candidates = [
                p for p in self._proxy_pool
                if p not in self._pool_bad and p not in self._proxy_claims and p != avoid
            ]
            if free_candidates:
                chosen = random.choice(free_candidates)
                self._proxy_claims.add(chosen)
                return chosen

            # 没有完全空闲且未 avoid 的，退而求其次找非 bad 的未占用行
            fallback_free = [
                p for p in self._proxy_pool
                if p not in self._pool_bad and p not in self._proxy_claims
            ]
            if fallback_free:
                chosen = random.choice(fallback_free)
                self._proxy_claims.add(chosen)
                return chosen

            # 池内代理少于并发 worker 数时共享非 bad 代理
            non_bad = [p for p in self._proxy_pool if p not in self._pool_bad]
            pool = non_bad or list(self._proxy_pool)
            chosen = random.choice(pool)
            logger.info(f"[auto_loop] 代理池已满载，共享代理节点: {proxy_endpoint(chosen)}")
            self._proxy_claims.add(chosen)
            return chosen

    def _release_proxy(self, line: Optional[str]) -> None:
        if not line:
            return
        with self._lock:
            self._proxy_claims.discard(line)

    # ──────────────────────── 状态与通知 ────────────────────────

    def _snapshot(self) -> dict:
        with self._lock:
            stats = db.stats()
            inv = db.invite_stats()
            workers_info = [
                {
                    "id": wid,
                    "email": info.get("email", ""),
                    "run_id": info.get("run_id", ""),
                    "proxy": proxy_endpoint(info.get("proxy", "")) or "直连",
                    "uses": info.get("uses", 0),
                    "started_at": info.get("started_at", 0),
                }
                for wid, info in sorted(self._worker_status.items())
            ]
            return {
                "state": self._state,
                "started_at": self._started_at,
                "elapsed": (time.time() - self._started_at) if self._started_at else 0,
                "registered_ok": self._registered_ok,
                "registered_waitlist": self._registered_waitlist,
                "registered_pending": self._registered_pending,
                "registered_fail": self._registered_fail,
                "ip_switches": self._ip_switches,
                "max_per_ip": self._max_per_ip,
                "target_count": self._target_count,
                "remaining": (
                    max(0, self._target_count - self._registered_ok - self._registered_pending)
                    if self._target_count else None
                ),
                "concurrency": self._concurrency,
                "proxy_pool_size": len(self._proxy_pool),
                "stop_on_reject": self._stop_on_reject,
                "workers": workers_info,
                "last_message": self._last_message,
                "pool_stats": stats,
                "invite_stats": inv,
            }

    def _broadcast(self, kind: str, data):
        with self._lock:
            subs = list(self._subscribers)
        for q in subs:
            try:
                q.put_nowait({"kind": kind, "data": data})
            except queue.Full:
                pass

    def _trigger_pause_alert(self, reason: str):
        should_emit = False
        with self._lock:
            if self._state == AutoLoopState.RUNNING:
                self._pause_event.set()
                self._state = AutoLoopState.PAUSED
                self._last_message = reason
                should_emit = True
        if should_emit:
            logger.warning(f"[auto_loop] 触发停批保护暂停: {reason}")
            self._broadcast("circuit_break", {"reason": reason})
            self._broadcast("state", self._snapshot())

    def _check_target_reached(self) -> bool:
        with self._lock:
            if self._target_count and (
                self._registered_ok + self._registered_pending >= self._target_count
            ):
                if not self._stop_event.is_set():
                    self._stop_event.set()
                    self._last_message = (
                        f"🎯 已达成目标 {self._target_count} 个账号，自动停止 "
                        f"(已激活 {self._registered_ok} / 未激活保留 {self._registered_pending} "
                        f"/ 受限 {self._registered_waitlist} / 失败 {self._registered_fail})"
                    )
                    logger.info(self._last_message)
                return True
        return False

    # ──────────────────────── 主控与 Worker 循环 ────────────────────────

    def _manage_loop(self):
        try:
            workers = []
            for wid in range(self._concurrency):
                t = threading.Thread(
                    target=self._worker_loop,
                    args=(wid,),
                    daemon=True,
                    name=f"auto-loop-worker-{wid}",
                )
                t.start()
                workers.append(t)
                time.sleep(0.6)
            self._workers = workers
            for t in workers:
                t.join()
        except Exception as exc:
            logger.exception(f"manage_loop 异常: {exc}")
        finally:
            with self._lock:
                self._state = AutoLoopState.STOPPED
                self._worker_status.clear()
                self._proxy_claims.clear()
                self._last_message = (
                    f"已结束 (已激活 {self._registered_ok} / 未激活保留 {self._registered_pending} "
                    f"/ 地区受限 {self._registered_waitlist} "
                    f"/ 失败 {self._registered_fail} / 换IP {self._ip_switches} 次)"
                )
            self._broadcast("state", self._snapshot())

    def _worker_loop(self, worker_id: int):
        proxy_line: Optional[str] = None
        avoid_proxy: Optional[str] = None
        uses = 0
        retries = 0
        current_account: Optional[dict] = None
        idle_round = 0

        try:
            while True:
                if self._stop_event.is_set():
                    return

                if self._pause_event.is_set():
                    # 暂停期间先归还当前未消耗的邮箱与代理锁
                    if current_account is not None and current_account.get("pooled_claimed"):
                        db.release_unused(current_account["email"])
                        current_account = None
                        retries = 0
                    while self._pause_event.is_set() and not self._stop_event.is_set():
                        time.sleep(0.4)
                    if self._stop_event.is_set():
                        return

                mail_source = db.get_setting("mail_source", db.DEFAULT_MAIL_SOURCE)
                try:
                    pooled = get_provider_class(mail_source).pooled
                except MailProviderError as exc:
                    self._trigger_pause_alert(str(exc))
                    return

                # 领取新邮箱（若是 pre-send 重试则复用 current_account）
                if current_account is None:
                    with self._lock:
                        if self._target_count and (
                            self._registered_ok + self._registered_pending
                            + len(self._worker_status) >= self._target_count
                        ):
                            return

                    if pooled:
                        acc = db.claim_next(kind=mail_source)
                        if acc:
                            acc["pooled_claimed"] = True
                    else:
                        acc = {
                            "email": f"{mail_source}_{int(time.time()*1000)}_{worker_id}@placeholder.local",
                            "password": "",
                            "client_id": "",
                            "refresh_token": "",
                            "relay_url": "",
                            "kind": mail_source,
                            "pooled_claimed": False,
                        }

                    if not acc:
                        idle_round += 1
                        if idle_round == 1:
                            with self._lock:
                                self._last_message = f"worker-{worker_id}: 邮箱池已空，等待补充..."
                            self._broadcast("state", self._snapshot())
                        if idle_round >= 6:
                            logger.info(f"[worker-{worker_id}] 邮箱池无可用号，退出")
                            return
                        for _ in range(20):
                            if self._stop_event.is_set() or self._pause_event.is_set():
                                break
                            time.sleep(0.15)
                        continue

                    idle_round = 0
                    current_account = acc
                    retries = 0

                # 选择或复用 sticky 代理
                if proxy_line is None:
                    proxy_line = self._pick_proxy(current=None, avoid=avoid_proxy)
                    avoid_proxy = None

                run_options = dict(self._options)
                run_options["proxy"] = proxy_line or ""

                try:
                    run_id = registrar.start_registration(current_account, run_options)
                except Exception as exc:
                    logger.exception(f"[worker-{worker_id}] 启动注册异常: {exc}")
                    if pooled:
                        db.release_unused(current_account["email"])
                    current_account = None
                    time.sleep(1.5)
                    continue

                with self._lock:
                    self._worker_status[worker_id] = {
                        "email": current_account["email"],
                        "run_id": run_id,
                        "proxy": proxy_line or "",
                        "uses": uses,
                        "started_at": time.time(),
                    }
                self._broadcast("state", self._snapshot())
                self._broadcast("run_started", {
                    "worker_id": worker_id,
                    "email": current_account["email"],
                    "run_id": run_id,
                    "proxy": proxy_endpoint(proxy_line) or "直连",
                })

                run_row = self._wait_run_finish(run_id)
                with self._lock:
                    self._worker_status.pop(worker_id, None)

                st = run_row.get("status") or "failed"
                cat = run_row.get("error_category") or ""
                stage = run_row.get("stage") or ""
                err = run_row.get("error") or ""
                real_email = run_row.get("email") or current_account["email"]

                if st == "done":
                    with self._lock:
                        self._registered_ok += 1
                        self._last_message = (
                            f"已激活 {self._registered_ok} / 未激活保留 {self._registered_pending} "
                            f"/ 受限 {self._registered_waitlist} / 失败 {self._registered_fail}"
                        )
                    uses += 1
                    current_account = None
                    retries = 0

                    self._broadcast("run_finished", {
                        "worker_id": worker_id,
                        "email": real_email,
                        "run_id": run_id,
                        "ok": True,
                        "category": cat,
                    })

                    if cat == "pending_redemption" and self._stop_on_reject:
                        self._trigger_pause_alert(
                            f"账号 {real_email} 已激活保留，但邀请码未完成兑换 ({err})；已自动暂停以便补充邀请码"
                        )

                    if self._check_target_reached():
                        self._broadcast("state", self._snapshot())
                        return

                    # 达 max_per_ip 上限后释放当前代理，换新代理
                    if self._proxy_pool and uses >= self._max_per_ip and not self._stop_event.is_set():
                        self._release_proxy(proxy_line)
                        with self._lock:
                            self._ip_switches += 1
                        avoid_proxy = proxy_line
                        proxy_line = None
                        uses = 0

                elif cat == "pre_send_network" and retries < 3 and len(self._proxy_pool) > 1:
                    # 发码前网络失败：registrar 已把邮箱 release 回 available，这里重新 claim 同邮箱换代理重试
                    retries += 1
                    bad_px = proxy_line
                    self._release_proxy(bad_px)
                    with self._lock:
                        if bad_px:
                            self._pool_bad.add(bad_px)
                        self._ip_switches += 1
                        self._last_message = f"worker-{worker_id}: 发码前网络异常，换代理重试 ({retries}/3) -> {real_email}"
                    proxy_line = None
                    uses = 0
                    if pooled:
                        reclaimed = db.claim_account(current_account["email"])
                        if reclaimed:
                            reclaimed["pooled_claimed"] = True
                            current_account = reclaimed
                        else:
                            current_account = None
                    self._broadcast("state", self._snapshot())
                    continue

                elif cat == "pending_redemption":
                    # 账号已注册保留，只是年齢確認 (CCV) 未解除 / 邀请码未兑换。
                    # 邮箱已 mark_done，不能当失败处理，否则 target_count 永远到不了。
                    with self._lock:
                        self._registered_pending += 1
                        self._last_message = (
                            f"未激活保留 {self._registered_pending} / 已激活 {self._registered_ok} "
                            f"/ 受限 {self._registered_waitlist} / 失败 {self._registered_fail} "
                            f"(最近: {real_email})"
                        )
                    uses += 1
                    current_account = None
                    retries = 0
                    logger.info(f"[auto] {real_email} 已注册但未激活 ({stage})，保留等待人工解除")
                    self._broadcast("run_finished", {
                        "worker_id": worker_id,
                        "email": real_email,
                        "run_id": run_id,
                        "ok": False,
                        "category": cat,
                    })
                    if self._check_target_reached():
                        self._broadcast("state", self._snapshot())
                        return

                elif cat == "region_waitlist":
                    with self._lock:
                        self._registered_waitlist += 1
                    current_account = None
                    retries = 0
                    self._broadcast("run_finished", {
                        "worker_id": worker_id,
                        "email": real_email,
                        "run_id": run_id,
                        "ok": False,
                        "category": "region_waitlist",
                    })
                    if self._stop_on_reject:
                        self._trigger_pause_alert(
                            f"触发地区受限 waitlist（账号 {real_email} 已入库保留）！请切换日本 (JP) 节点后点「恢复」"
                        )

                elif cat == "server_rejected" and stage == "completion" and retries < 2:
                    # 服务端注册闸门临时拒绝（registrationBlocked）：邮箱已释放，重建整个会话重试
                    retries += 1
                    current_account = None
                    with self._lock:
                        self._last_message = (
                            f"worker-{worker_id}: 服务端暂缓注册，重建会话重试 "
                            f"({retries}/2) -> {real_email}"
                        )
                    if pooled:
                        reclaimed = db.claim_account(real_email)
                        if reclaimed:
                            reclaimed["pooled_claimed"] = True
                            current_account = reclaimed
                    self._broadcast("state", self._snapshot())
                    continue

                else:
                    # 若是 pre_send_network 但已耗尽重试，标记该号或暂停防死循环
                    if cat == "pre_send_network" and pooled and current_account:
                        if not self._stop_on_reject:
                            db.mark_failed(current_account["email"], f"[pre_send_network] 代理重试耗尽: {err}")

                    with self._lock:
                        self._registered_fail += 1
                        self._last_message = f"最近失败 [{stage}]: {err[:80]}"
                    current_account = None
                    retries = 0
                    self._broadcast("run_finished", {
                        "worker_id": worker_id,
                        "email": real_email,
                        "run_id": run_id,
                        "ok": False,
                        "category": cat,
                    })
                    if self._stop_on_reject:
                        self._trigger_pause_alert(
                            f"停批保护已触发 [{stage}/{cat}]: {err[:120]}（检查环境后可点「恢复」继续）"
                        )

                self._broadcast("state", self._snapshot())

                cool_down = float(self._options.get("cool_down_seconds") or 0)
                if cool_down > 0:
                    for _ in range(int(cool_down * 10)):
                        if self._stop_event.is_set() or self._pause_event.is_set():
                            break
                        time.sleep(0.1)
        finally:
            self._release_proxy(proxy_line)

    def _wait_run_finish(self, run_id: str, timeout: int = 900) -> dict:
        deadline = time.time() + timeout
        stop_deadline: Optional[float] = None
        while time.time() < deadline:
            row = db.get_run(run_id)
            if row and row.get("status") in ("done", "failed"):
                return row
            if self._stop_event.is_set():
                # 停止要求後も在途 run は止めずに走り切らせる。
                # 即 return すると run の分類が失われ、in_use のメールが取り残される。
                if stop_deadline is None:
                    stop_deadline = time.time() + 180
                if time.time() >= stop_deadline:
                    return row or {"status": "failed", "error": "stopped"}
            time.sleep(0.6)
        return {"status": "failed", "error": f"run timeout {timeout}s", "error_category": "timeout"}


CONTROLLER = AutoLoopController()
