"""
muse.ai 注册与邀请码兑换核心业务流（WebUI 引擎层）。

对接 musepy 协议客户端与 mail_providers 邮箱池，完整执行：
open_home → restart → send_otp → wait_otp → confirm_otp → register_completion
→ activate_session (Next Flight 动态披露 & 地区门判定) → accept_tos
→ wake_vm → stateful_sharing → is_live → (可选) redeem → scratch_code
"""
from __future__ import annotations

import logging
import random
import time
import uuid
from dataclasses import dataclass, field
from typing import Callable, Optional

from curl_cffi import requests as cr

from musepy import caa as _caa
from musepy import falco as _falco
from musepy.client import MuseClient, MuseError, normalize_proxy, random_dob
from musepy.runner import GIVEN_NAMES, PRE_SEND_STAGES, SURNAMES, TRANSPORT_MARKERS

logger = logging.getLogger("muse.flow")

# 在发出 OTP 邮件之前的阶段：此阶段发生网络错误零副作用，可安全释放邮箱并换代理重试
FLOW_PRE_SEND_STAGES = PRE_SEND_STAGES | {"ip_check"}

# 年齢確認(CCV)の解除手段
CCV_MODE_CARD = "card"                    # カード登録のみ
CCV_MODE_LINK = "link"                    # Instagram/Facebook 連携のみ
CCV_MODE_CARD_THEN_LINK = "card_then_link"  # カード優先、失敗したら連携にフォールバック
CCV_MODES = (CCV_MODE_CARD, CCV_MODE_LINK, CCV_MODE_CARD_THEN_LINK)

# カードが弾かれたときに「カード無しでは無理」を意味するコード（＝連携に落とす価値がある）
_CCV_CARD_REJECT_CODES = ("1383254", "2078022")


class MuseFlowError(RuntimeError):
    """带阶段与分类元信息的流程异常，供 registrar / auto_loop 精确调度。"""

    def __init__(
        self,
        message: str,
        *,
        stage: str = "initialized",
        category: str = "failed",
        registered: bool = False,
    ):
        super().__init__(message)
        self.stage = stage
        self.category = category
        self.registered = registered


def is_transport_exception(exc: BaseException) -> bool:
    if isinstance(exc, ConnectionError):
        return True
    text = str(exc).lower()
    return any(m in text for m in TRANSPORT_MARKERS) or any(
        k in text for k in ("ssl", "tls", "socks", "proxy", "dns", "name resolution", "curl: (6)", "curl: (97)")
    )


def resolve_exit_ip(proxy: Optional[str], impersonate: str = "chrome142", timeout: int = 25) -> Optional[str]:
    """通过 api.ipify.org 查询当前代理出口 IP。"""
    px = normalize_proxy(proxy)
    s = cr.Session(
        impersonate=impersonate,
        proxies={"http": px, "https": px} if px else {"http": "", "https": ""},
        timeout=timeout,
    )
    s.trust_env = False
    try:
        r = s.get("https://api.ipify.org")
        if r.status_code != 200:
            return None
        ip = (r.text or "").strip()
        return ip if (ip.count(".") == 3 or ":" in ip) and len(ip) <= 64 else None
    finally:
        try:
            s.close()
        except Exception:
            pass


@dataclass
class MuseRegisterResult:
    email: str = ""
    account_id: str = ""              # frl_account_id
    access_token: str = ""            # frl_access_token
    session_token: str = ""           # hatch_sess Cookie
    cookie_header: str = ""           # hatch_sess=<value>
    csrf_token: str = ""
    device_id: str = ""               # vm_id
    status: str = "pending"           # redeemed | activated | registered_pending_redemption | registered_waitlist | registered_unactivated
    stage: str = "initialized"
    registered: bool = False
    is_live: bool = False
    own_code: str = ""
    own_code_status: str = ""         # available | pending | skipped
    used_invite_code: str = ""
    redeem_reason: str = ""
    first_name: str = ""
    last_name: str = ""
    date_of_birth: str = ""
    resolved_destination: str = ""
    disclosure_version: str = ""
    ccv_status: str = ""              # not_required | failed | "" (年齢確認 CCV の状態)
    ccv_error: str = ""
    ip: str = ""
    last_error: str = ""
    extra: dict = field(default_factory=dict)

    def to_db_dict(self) -> dict:
        hatch_sess = self.session_token or ""
        return {
            "email": self.email,
            "password": "",
            "account_id": self.account_id,
            "access_token": self.access_token,
            "session_token": hatch_sess,
            "refresh_token": "",
            "id_token": "",
            "device_id": self.device_id,
            "csrf_token": self.csrf_token,
            "cookie_header": f"hatch_sess={hatch_sess}" if hatch_sess else "",
            "status": self.status,
            "stage": self.stage,
            "own_code": self.own_code,
            "used_invite_code": self.used_invite_code,
            "ip": self.ip,
            "registered": self.registered,
            "is_live": self.is_live,
            "own_code_status": self.own_code_status,
            "redeem_reason": self.redeem_reason,
            "first_name": self.first_name,
            "last_name": self.last_name,
            "date_of_birth": self.date_of_birth,
            "resolved_destination": self.resolved_destination,
            "disclosure_version": self.disclosure_version,
            "ccv_status": self.ccv_status,
            "ccv_error": self.ccv_error,
            "last_error": self.last_error,
        }


class MuseFlow:
    """单账号端到端注册与兑换执行器。"""

    def __init__(
        self,
        *,
        proxy: Optional[str] = None,
        impersonate: str = "chrome142",
        lang: str = "ja",
        otp_timeout: int = 45,
        first_name: str = "",
        last_name: str = "",
        date_of_birth: str = "",
        send_caa: bool = True,
        send_falco: bool = True,
        resolve_ip: bool = True,
        redeem_mode: str = "pool",   # "none" | "single" | "pool"
        invite_code: str = "",
        completion_retries: int = 4,
        completion_retry_delay: float = 20.0,
        ccv_card: Optional[dict] = None,
        ccv_mode: str = CCV_MODE_CARD,
        ccv_link_account_type: str = "facebook",
        ccv_link_timeout: float = 600.0,
        on_link_url: Optional[Callable[[str, dict], None]] = None,
        on_phase: Optional[Callable[[str, dict], None]] = None,
        on_early_save: Optional[Callable[[MuseRegisterResult], None]] = None,
        claim_pool_code: Optional[Callable[[], Optional[str]]] = None,
        finish_pool_code: Optional[Callable[[str, bool, str], None]] = None,
        on_own_code: Optional[Callable[[str, str], None]] = None,
    ):
        self.proxy = (proxy or "").strip() or None
        self.impersonate = (impersonate or "chrome142").strip()
        self.lang = (lang or "ja").strip()
        self.otp_timeout = max(15, int(otp_timeout or 45))
        self.first_name = (first_name or "").strip() or random.choice(GIVEN_NAMES)
        self.last_name = (last_name or "").strip() or random.choice(SURNAMES)
        self.date_of_birth = (date_of_birth or "").strip() or random_dob()
        self.send_caa = bool(send_caa)
        self.send_falco = bool(send_falco)
        self.resolve_ip = bool(resolve_ip)
        self.redeem_mode = (redeem_mode or "none").strip().lower()
        self.invite_code = (invite_code or "").strip()
        self.completion_retries = max(1, int(completion_retries or 1))
        self.completion_retry_delay = max(0.0, float(completion_retry_delay or 0.0))
        self.ccv_card = dict(ccv_card or {})
        self.ccv_mode = (ccv_mode or CCV_MODE_CARD).strip().lower()
        self.ccv_link_account_type = (ccv_link_account_type or "facebook").strip().lower()
        self.ccv_link_timeout = max(30.0, float(ccv_link_timeout or 600.0))
        self.on_link_url = on_link_url

        self.on_phase = on_phase
        self.on_early_save = on_early_save
        self.claim_pool_code = claim_pool_code
        self.finish_pool_code = finish_pool_code
        self.on_own_code = on_own_code

        self.result = MuseRegisterResult(
            first_name=self.first_name,
            last_name=self.last_name,
            date_of_birth=self.date_of_birth,
        )

    def _phase(self, stage: str, msg: str = "") -> None:
        self.result.stage = stage
        if msg:
            logger.info(msg)
        if self.on_phase:
            try:
                self.on_phase(stage, {"email": self.result.email, "message": msg})
            except Exception:
                pass

    def _on_completion_retry(self, attempt: int, status: int, text: str) -> None:
        reason = next(
            (t for t in ("registrationBlocked", "registrationDisabled") if t in text),
            f"http{status}",
        )
        msg = (
            f"       ⏳ Muse 暂缓本次注册 ({reason})，{self.completion_retry_delay:.0f}s 后重试 "
            f"(第 {attempt}/{self.completion_retries} 次)"
        )
        self._phase("completion", msg)
        client = getattr(self, "_client", None)
        if not (client is not None and self.resolve_ip and self.proxy):
            return
        try:
            ip_now = client.s.get("https://api.ipify.org", timeout=10).text.strip()
            if ip_now and self.result.ip and ip_now != self.result.ip:
                logger.warning(f"       ⚠️ 出口 IP 发生漂移: {self.result.ip} -> {ip_now}")
        except Exception:
            pass

    def _activate_ccv(self, client: MuseClient) -> None:
        """年齢確認 (ccv_required) を解除する（カード登録 / アカウント連携）。

        Muse は登録直後のアカウントに年齢確認を要求し、未解除のままだと
        ``/api/hatch/invite`` と ``/api/hatch/invite-code/redeem`` が 403 になる。
        つまり解除できないと自分の招待コードを発行できず（＝増殖できない）、
        貯めた招待コードで他アカウントを承認することもできない。

        カードがデビットだと Muse は 1383254 で弾くため、Instagram/Facebook 連携で
        回避できるようにしてある（``ccv_mode``）。連携は承認 URL を人間がブラウザで
        開く必要があるため、``on_link_url`` で通知する。
        """
        status = ""
        try:
            status = client.ccv_status() or ""
        except Exception as exc:
            logger.warning(f"       ccv-status 取得失敗: {exc}")

        if status in ("", "not_required"):
            if status == "not_required":
                self.result.ccv_status = "not_required"
            return

        res = ensure_ccv(
            client,
            card=self.ccv_card,
            mode=self.ccv_mode,
            link_account_type=self.ccv_link_account_type,
            link_timeout=self.ccv_link_timeout,
            on_link_url=self.on_link_url,
            email=self.result.email,
            on_msg=lambda msg: self._phase("ccv", msg),
        )
        self.result.ccv_status = res["status"]
        self.result.ccv_error = res["error"]
        if res["status"] == "not_required":
            try:
                client.refresh_onboarding()
            except Exception:
                pass

    def _gate_ccv(self, client: MuseClient, onboarding: dict) -> dict:
        """onboarding の年齢確認ゲートを検出し、必要なら解除して最新 onboarding を返す。"""
        ccv_flag = str(onboarding.get("ccv_status") or "")
        if onboarding.get("verification_required") or ccv_flag not in ("", "not_required"):
            logger.info(f"       ℹ️ 年齢確認ゲートを検出 (state={onboarding.get('state')}, ccv={ccv_flag})")
            self._activate_ccv(client)
            if self.result.ccv_status == "not_required":
                try:
                    onboarding = client.refresh_onboarding()
                except Exception:
                    pass
        return onboarding

    def run_register(self, mail_provider) -> MuseRegisterResult:
        stage = "initialized"
        client: Optional[MuseClient] = None
        try:
            email = mail_provider.create_mailbox()
            self.result.email = email
            logger.info(f"[0/10] 分配邮箱: {email} (指纹={self.impersonate}, 语言={self.lang})")

            # ── 可选：出口 IP 检测（pre-send 阶段）──
            if self.resolve_ip:
                stage = "ip_check"
                self._phase(stage, "[1/10] 检测当前出口 IP...")
                try:
                    ip = resolve_exit_ip(self.proxy, impersonate=self.impersonate)
                except Exception as exc:
                    raise MuseFlowError(
                        f"出口 IP 检测失败（代理不可达）: {exc}",
                        stage=stage,
                        category="pre_send_network",
                    ) from exc
                if self.proxy and not ip:
                    raise MuseFlowError(
                        "代理出口 IP 解析为空，请检查代理连通性",
                        stage=stage,
                        category="pre_send_network",
                    )
                self.result.ip = ip or ""
                logger.info(f"       出口 IP = {ip or '直连/未知'}")

            client = MuseClient(proxy=self.proxy, impersonate=self.impersonate, lang=self.lang)
            self._client = client

            # ── [1] 打开首页拿初始 Cookie（pre-send 阶段）──
            stage = "bootstrap"
            self._phase(stage, "[2/10] 打开 muse.ai 首页获取初始 Cookie...")
            client.open_home()
            falco_sid = str(uuid.uuid4())
            if self.send_falco:
                _falco.send(client.s, client.fp, client.lang, falco_sid, [
                    _falco.perf("https://muse.ai/"),
                    _falco.interaction(
                        getattr(client, "home_url", "https://muse.ai/"),
                        "page_load", "app_root", "hatch_page_load",
                        meta={"surface": "app_load"}, locale=self.lang,
                    ),
                ])

            # ── [2] 重启注册会话拿 csrf_token（pre-send 阶段）──
            stage = "restart"
            self._phase(stage, "[3/10] 初始化注册流程 POST /api/auth/native/restart...")
            csrf = client.restart()
            self.result.csrf_token = csrf
            # 传入同一个 waterfall_id，保证 caa 埋点与 native 请求链路完全一致
            funnel = _caa.Caa(client.fp, client.lang, waterfall_id=getattr(client, "waterfall_id", None)) if self.send_caa else None
            if funnel:
                funnel.page_shown(client.s)
                time.sleep(random.uniform(1.2, 2.0))
                funnel.focus(client.s)
                time.sleep(random.uniform(0.4, 0.7))
                funnel.continue_clicked(client.s)
                funnel.registration_start(client.s)

            # ── [3] 发送邮箱验证码 ──
            stage = "send_otp"
            self._phase(stage, f"[4/10] 发送验证码邮件 -> {email}...")
            sent_at = time.time()
            client.send_otp(email)

            # ── [4] 轮询等待 6 位 OTP ──
            stage = "wait_otp"
            self._phase(stage, f"[5/10] 等待邮箱 6 位验证码 (超时 {self.otp_timeout}s)...")
            try:
                otp_code = mail_provider.wait_for_otp(
                    email, timeout=self.otp_timeout, issued_after=sent_at
                )
            except TimeoutError as exc:
                raise MuseFlowError(
                    f"OTP 等待超时 ({self.otp_timeout}s): {exc}",
                    stage=stage,
                    category="otp_timeout",
                ) from exc
            if not otp_code:
                raise MuseFlowError("未获取到有效 6 位验证码", stage=stage, category="otp_timeout")
            logger.info(f"       收到验证码: {otp_code}")

            if funnel:
                funnel.otp_typed(client.s)

            # ── [5] 校验验证码（动态提取 tos.content_version）──
            stage = "confirm_otp"
            self._phase(stage, "[6/10] 校验验证码 POST /api/auth/native/confirm-otp...")
            confirm_data = client.confirm_otp(otp_code)
            logger.info(
                f"       OTP 校验通过 (tos={client.tos_content_version or 'default'}, "
                f"state={confirm_data.get('registration_state')})"
            )

            if funnel:
                funnel.otp_success(client.s)
                funnel.birthday_start(client.s)
                time.sleep(random.uniform(2.5, 3.5))
                funnel.birthday_next(client.s)
            else:
                time.sleep(random.uniform(1.2, 2.0))

            # ── [6] 完成注册（first_name=名, last_name=姓, 满18岁生日）──
            stage = "completion"
            self._phase(
                stage,
                f"[7/10] 提交注册资料: {self.last_name} {self.first_name} ({self.date_of_birth})...",
            )
            try:
                comp_data = client.register_completion(
                    self.date_of_birth, self.first_name, self.last_name,
                    retries=self.completion_retries,
                    retry_delay=self.completion_retry_delay,
                    on_retry=self._on_completion_retry,
                )
            except MuseError as comp_exc:
                if "403" in str(comp_exc) and self.resolve_ip:
                    try:
                        ip_now = client.s.get("https://api.ipify.org", timeout=10).text.strip()
                        if ip_now and self.result.ip and ip_now != self.result.ip:
                            logger.warning(f"       ⚠️ 检测到会话中途出口 IP 发生漂移: {self.result.ip} -> {ip_now}")
                        else:
                            logger.info(f"       当前提交时出口 IP 仍为: {ip_now or '未知'}")
                    except Exception:
                        pass
                raise comp_exc
            auth_meta = comp_data.get("auth_meta_session") or {}
            token = auth_meta.get("frl_access_token") or ""
            uid = auth_meta.get("frl_account_id") or ""
            if not token:
                raise MuseFlowError(
                    "register-completion 未返回 frl_access_token",
                    stage=stage,
                    category="server_rejected",
                )

            self.result.registered = True
            self.result.account_id = str(uid)
            self.result.access_token = token
            self.result.session_token = client.session_cookie("hatch_sess") or ""
            self.result.status = "registered_unactivated"
            logger.info(
                f"       账号创建成功! account_id={uid}, "
                f"hatch_sess长度={len(self.result.session_token)}"
            )

            # 一旦创建成功立即早落盘，防止后续 waitlist 或网络中断丢失账号凭证
            if self.on_early_save:
                try:
                    self.on_early_save(self.result)
                except Exception as save_exc:
                    logger.warning(f"早落盘异常 (不影响主流程): {save_exc}")

            if funnel:
                funnel.create_success(client.s, uid)
                funnel.login_success(client.s, uid)

            if self.send_falco:
                _falco.send(client.s, client.fp, client.lang, falco_sid, [
                    _falco.perf("https://muse.ai/"),
                    _falco.interaction(
                        "https://muse.ai/", "page_load", "main_app", "page_load", locale=self.lang
                    ),
                ])

            # ── [7] 会话引导（auth.meta.com/hatch/session/bootstrap/）──
            stage = "session_bootstrap"
            self._phase(stage, "[8/10] 会话引导 session bootstrap & 解析动态披露...")
            onboarding = client.activate_session(token)
            self.result.session_token = client.session_cookie("hatch_sess") or self.result.session_token
            self.result.resolved_destination = onboarding.get("resolved_destination") or ""
            self.result.disclosure_version = onboarding.get("disclosure_version") or ""
            logger.info(
                f"       引导成功: destination={self.result.resolved_destination}, "
                f"disclosure={self.result.disclosure_version}"
            )

            # ── [8] 激活会话（单次调用 accept-tos，正文取服务端动态披露）──
            # 年齢確認 (ccv_required) のアカウントは accept-tos が 403 になるので先に解除する
            stage = "accept_tos"
            onboarding = self._gate_ccv(client, onboarding)
            self._phase(stage, "[9/10] 激活会话 POST /api/hatch/accept-tos...")
            client.accept_tos(onboarding)
            self.result.session_token = client.session_cookie("hatch_sess") or self.result.session_token

            # ── [9] 唤醒 VM + stateful-sharing + 权限校验 ──
            stage = "wake"
            self._phase(stage, "[9.5/10] 唤醒云端 VM (wake_trigger=mount) & 校验 Hatch 权限...")
            vm_id = client.wake_vm()
            if vm_id:
                self.result.device_id = vm_id
                logger.info(f"       VM 唤醒成功: vm_id={vm_id}")
            else:
                logger.warning("       未从页面提取到 vmId（若需兑换邀请码可能受影响）")

            stage = "sharing"
            client.stateful_sharing()

            stage = "invite"
            self.result.is_live = client.is_live()
            logger.info(f"       Hatch 存活校验: {'200 有效活号' if self.result.is_live else '401/403 未就绪'}")

            # ── [10] 邀请码兑换与提取本号邀请码 ──
            self._handle_redemption(client)
            self.result.session_token = client.session_cookie("hatch_sess") or self.result.session_token
            return self.result

        except MuseFlowError:
            raise
        except MuseError as exc:
            text = str(exc)
            self.result.stage = stage
            self.result.last_error = text
            if client is not None:
                self.result.session_token = client.session_cookie("hatch_sess") or self.result.session_token

            if "region_waitlist" in text:
                self.result.status = "registered_waitlist"
                raise MuseFlowError(
                    "地区受限 (region_waitlist)：账号已注册保留，但出口节点非免绑卡开放区（建议使用日本 JP 节点）",
                    stage=stage,
                    category="region_waitlist",
                    registered=self.result.registered,
                ) from exc

            if stage in FLOW_PRE_SEND_STAGES and is_transport_exception(exc):
                raise MuseFlowError(
                    text, stage=stage, category="pre_send_network", registered=False
                ) from exc

            cat = "server_rejected" if text.startswith(
                ("restart ", "send-otp ", "confirm-otp ", "register-completion ", "accept-tos ")
            ) else "protocol_error"
            raise MuseFlowError(
                text, stage=stage, category=cat, registered=self.result.registered
            ) from exc
        except Exception as exc:
            self.result.stage = stage
            self.result.last_error = str(exc)
            if client is not None:
                self.result.session_token = client.session_cookie("hatch_sess") or self.result.session_token

            if is_transport_exception(exc):
                cat = "pre_send_network" if stage in FLOW_PRE_SEND_STAGES else "post_send_network"
                raise MuseFlowError(
                    f"网络传输异常 ({stage}): {exc}",
                    stage=stage,
                    category=cat,
                    registered=self.result.registered,
                ) from exc

            raise MuseFlowError(
                f"{type(exc).__name__} ({stage}): {exc}",
                stage=stage,
                category="failed",
                registered=self.result.registered,
            ) from exc
        finally:
            if client is not None:
                client.close()

    def _handle_redemption(self, client: MuseClient) -> None:
        mode = self.redeem_mode
        if mode == "single" and not self.invite_code:
            mode = "none"

        if mode == "none":
            self.result.status = "activated" if self.result.is_live else "registered_pending_redemption"
            self.result.own_code_status = "skipped"
            self.result.redeem_reason = "skipped"
            logger.info("[10/10] 未开启邀请码兑换，已完成账号注册与会话激活")
            return

        if mode == "single":
            code = self.invite_code
            self._phase("redeem", f"[10/10] 兑换指定邀请码: {code}...")
            ok, reason = client.redeem(code)
            self.result.redeem_reason = reason
            if ok or reason == "already_redeemed":
                self.result.used_invite_code = code
                self.result.status = "redeemed"
                self._phase("own_code", "       兑换成功，正在提取本账号专属邀请码...")
                own = client.scratch_code() or ""
                self.result.own_code = own
                self.result.own_code_status = "available" if own else "pending"
                logger.info(f"       自有邀请码 = {own or '(暂未分配)'}")
                if own and self.on_own_code:
                    self.on_own_code(own, self.result.email)
            else:
                self.result.status = "registered_pending_redemption"
                self.result.last_error = f"redemption_unverified:{reason}"
                logger.warning(f"       邀请码 {code} 兑换未成功: {reason}（账号本身已激活保留）")
                if self.finish_pool_code:
                    self.finish_pool_code(code, False, reason)
            return

        # mode == "pool"：从邀请码池取码，若遇 used_up / invalid 自动标记并换下一个码重试（最多 5 次）
        if not self.claim_pool_code:
            self.result.status = "activated" if self.result.is_live else "registered_pending_redemption"
            self.result.redeem_reason = "pool_not_configured"
            return

        for attempt in range(1, 6):
            code = self.claim_pool_code()
            if not code:
                self.result.status = "registered_pending_redemption"
                self.result.redeem_reason = "redemption_code_unavailable"
                self.result.last_error = "redemption_code_unavailable"
                logger.warning("[10/10] 邀请码池已无可用邀请码，跳过兑换（账号已保留，可稍后在结果页补兑）")
                return

            self._phase("redeem", f"[10/10] 从邀请码池取码兑换 ({attempt}): {code}...")
            try:
                ok, reason = client.redeem(code)
            except Exception as exc:
                if self.finish_pool_code:
                    self.finish_pool_code(code, False, "network_error")
                raise exc

            self.result.redeem_reason = reason
            if ok:
                if self.finish_pool_code:
                    self.finish_pool_code(code, True, "redeemed")
                self.result.used_invite_code = code
                self.result.status = "redeemed"
                self._phase("own_code", "       兑换成功，正在提取本账号专属邀请码...")
                own = client.scratch_code() or ""
                self.result.own_code = own
                self.result.own_code_status = "available" if own else "pending"
                logger.info(f"       自有邀请码 = {own or '(暂未分配)'}")
                if own and self.on_own_code:
                    self.on_own_code(own, self.result.email)
                return

            if self.finish_pool_code:
                self.finish_pool_code(code, False, reason)

            if reason == "already_redeemed":
                self.result.status = "redeemed"
                own = client.scratch_code() or ""
                self.result.own_code = own
                self.result.own_code_status = "available" if own else "pending"
                return

            if reason in ("used_up", "invalid"):
                logger.warning(f"       池中邀请码 {code} 状态={reason}，自动标记淘汰并换下一个码...")
                continue

            self.result.status = "registered_pending_redemption"
            self.result.last_error = f"redemption_unverified:{reason}"
            logger.warning(f"       兑换未通过 ({reason})，账号已激活保留")
            return

        self.result.status = "registered_pending_redemption"
        self.result.last_error = "pool_codes_exhausted_retries"


def check_account_live(
    cred: dict,
    *,
    proxy: Optional[str] = None,
    impersonate: str = "chrome142",
    lang: str = "ja",
    fetch_own_code: bool = True,
) -> dict:
    """用已存 hatch_sess 检测账号存活/Hatch 权限，并可顺带取回 own_code。"""
    sess_val = (cred.get("session_token") or "").strip()
    if not sess_val:
        return {"status": "no_session", "label": "无会话Cookie"}

    client = MuseClient(proxy=proxy, impersonate=impersonate, lang=lang, timeout=45)
    try:
        client.set_session_cookie(sess_val)
        resp = client.invite()
        if resp.status_code == 200:
            data = client._json(resp)
            own = (
                data.get("code")
                or data.get("inviteCode")
                or (cred.get("own_code") or "").strip()
            )
            if not own and fetch_own_code and cred.get("status") == "redeemed":
                try:
                    own = client.scratch_code() or ""
                except Exception:
                    own = ""
            return {
                "status": "live",
                "label": "活号 (200)",
                "own_code": own or "",
            }
        if resp.status_code == 401:
            return {"status": "token_invalid", "label": "会话失效 (401)"}
        if resp.status_code == 403:
            return {"status": "forbidden", "label": "无Hatch权限 (403)"}
        return {"status": "error", "label": f"HTTP {resp.status_code}"}
    finally:
        client.close()


def ensure_ccv(
    client: MuseClient,
    *,
    card: Optional[dict] = None,
    mode: str = CCV_MODE_CARD,
    link_account_type: str = "facebook",
    link_timeout: float = 600.0,
    on_link_url: Optional[Callable[[str, dict], None]] = None,
    email: str = "",
    on_msg: Optional[Callable[[str], None]] = None,
) -> dict:
    """CCV が未解除なら解除する（カード登録 → 必要なら連携にフォールバック）。

    戻り値 ``{"status": 解除後の ccv_status, "error": 理由, "action": link の next_action}``。
    ``status`` が ``not_required`` なら解除成功。
    """
    from musepy import ccv as ccv_mod
    from musepy import linking as linking_mod

    mode = (mode or CCV_MODE_CARD).strip().lower()
    if mode not in CCV_MODES:
        mode = CCV_MODE_CARD

    def say(msg):
        logger.info("       " + msg)
        if on_msg:
            try:
                on_msg("       " + msg)
            except Exception:
                pass

    out = {"status": "", "error": "", "action": None}
    try:
        status = client.ccv_status() or ""
    except Exception as exc:
        logger.warning(f"       ccv-status 取得失敗: {exc}")
        out["status"] = ""
        return out
    if status in ("", "not_required"):
        out["status"] = status
        return out

    card_ok = bool(card and card.get("card_number"))

    def try_card():
        if not card_ok:
            logger.warning(f"       ⚠️ 年齢確認が必要 (ccv_status={status}) ですがカードが未設定です")
            return "カード未設定"
        say(f"💳 年齢確認 (ccv_status={status}) をカード登録で解除します...")
        try:
            res = ccv_mod.activate(
                client,
                card_number=str(card.get("card_number") or "").replace(" ", "").replace("-", ""),
                expiry=str(card.get("expiry") or ""),
                cvv=str(card.get("cvv") or ""),
                zip_code=str(card.get("zip") or "1000001"),
                country_code=str(card.get("country") or "") or None,
                log=lambda msg, *a: logger.info("       " + (msg % a if a else msg)),
            )
        except Exception as exc:
            logger.warning(f"       ❌ カードでの解除に失敗: {exc}")
            return str(exc)
        logger.info(f"       ✅ 年齢確認(CCV)をカードで解除しました credential={res.get('credential_id')}")
        return ""

    def try_link():
        kind = (link_account_type or "facebook").strip().lower()
        say(f"🔗 年齢確認 (ccv_status={status}) を {kind} 連携で解除します...")
        try:
            info = linking_mod.start(client, kind)
        except Exception as exc:
            logger.warning(f"       ❌ 連携の開始に失敗: {exc}")
            return None, str(exc)
        if info["url"] is None:
            logger.info("       ✅ 連携済みだったためスキップしました")
            return "continue", ""
        logger.info(f"       🔗 承認 URL: {info['url']}")
        if on_link_url:
            try:
                on_link_url(info["url"], {"email": email, **info})
            except Exception:
                pass
        say(f"👉 ブラウザで承認してください（{kind} にログイン → 許可）。最大 {link_timeout:.0f} 秒待ちます")
        action = linking_mod.wait(
            client, info["attempt_id"], timeout=link_timeout,
            log=lambda msg, *a: logger.info("       " + (msg % a if a else msg)),
        )
        return action, ""

    error = ""
    if mode == CCV_MODE_LINK:
        action, error = try_link()
        if action in linking_mod.SUCCESS_ACTIONS:
            out["status"] = "not_required"
            return out
        out["status"] = "pending" if action in ("pending", "stopped") else "failed"
        out["action"] = action
        out["error"] = error or f"link:{action} {linking_mod.describe(action)}"
        _link_failure_hint(action, linking_mod)
        return out

    error = try_card()
    if not error:
        out["status"] = "not_required"
        return out
    if mode != CCV_MODE_CARD_THEN_LINK:
        out["status"] = "failed"
        out["error"] = error
        return out

    say("↩️ カードで解除できないため連携方式にフォールバックします")
    if any(c in error for c in _CCV_CARD_REJECT_CODES):
        logger.warning("       （カードが拒否された理由: クレジットカード必須 / BIN 単位の一時停止）")
    action, error = try_link()
    if action in linking_mod.SUCCESS_ACTIONS:
        out["status"] = "not_required"
        return out
    out["status"] = "pending" if action in ("pending", "stopped") else "failed"
    out["action"] = action
    out["error"] = error or f"card:{error} / link:{action}"
    _link_failure_hint(action, linking_mod)
    return out


def _link_failure_hint(action: Optional[str], linking_mod) -> None:
    if action == "use_credit_card":
        logger.warning("       👉 この Meta アカウントでは年齢確認に使えません。クレジットカードが必要です")
    elif action in ("pending", "stopped"):
        logger.warning("       👉 承認待ちのままです。URL をもう一度開いて承認してください")
    elif action == "timeout":
        logger.warning("       👉 承認が完了しませんでした。URL をもう一度開いて承認してください")


def _ensure_ccv(client: MuseClient, ccv_card: Optional[dict],
                ccv_mode: str = CCV_MODE_CARD,
                link_account_type: str = "facebook",
                link_timeout: float = 600.0,
                on_link_url: Optional[Callable[[str, dict], None]] = None,
                email: str = "") -> str:
    """CCV 未解除なら解除する。戻り値は解除後の ccv_status。"""
    res = ensure_ccv(
        client, card=ccv_card, mode=ccv_mode,
        link_account_type=link_account_type, link_timeout=link_timeout,
        on_link_url=on_link_url, email=email,
    )
    if res["status"] == "not_required":
        try:
            return client.ccv_status() or ""
        except Exception:
            return "not_required"
    return res["status"] or "failed"


def redeem_registered_account(
    cred: dict,
    invite_code: str,
    *,
    proxy: Optional[str] = None,
    impersonate: str = "chrome142",
    lang: str = "ja",
    ccv_card: Optional[dict] = None,
    ccv_mode: str = CCV_MODE_CARD,
    ccv_link_account_type: str = "facebook",
    ccv_link_timeout: float = 600.0,
    on_link_url: Optional[Callable[[str, dict], None]] = None,
) -> dict:
    """对已注册（含未激活或已激活）的账号补跑会话引导 + accept_tos + wake_vm + redeem + scratch_code。"""
    sess_val = (cred.get("session_token") or "").strip()
    access_token = (cred.get("access_token") or "").strip()
    if not sess_val and not access_token:
        return {"ok": False, "reason": "no_session_cookie"}
    code = (invite_code or "").strip()
    if not code:
        return {"ok": False, "reason": "empty_invite_code"}

    client = MuseClient(proxy=proxy, impersonate=impersonate, lang=lang, timeout=60)
    try:
        if sess_val:
            client.set_session_cookie(sess_val)
        cur_status = (cred.get("status") or "").strip()
        if access_token and (cur_status in ("registered_unactivated", "registered_waitlist", "") or not sess_val):
            onboarding = client.activate_session(access_token)
            # 年齢確認ゲートが残っていると accept-tos / redeem が 403 になるので解除する
            if onboarding.get("verification_required") or str(onboarding.get("ccv_status") or "") not in ("", "not_required"):
                _ensure_ccv(client, ccv_card, ccv_mode, ccv_link_account_type,
                            ccv_link_timeout, on_link_url,
                            email=(cred.get("email") or ""))
                try:
                    onboarding = client.refresh_onboarding()
                except Exception:
                    pass
            client.accept_tos(onboarding)
        else:
            # アクティベーション済みでも CCV が未解除なら 403 のままなので解除しておく
            _ensure_ccv(client, ccv_card, ccv_mode, ccv_link_account_type,
                        ccv_link_timeout, on_link_url,
                        email=(cred.get("email") or ""))
        # 协议要求：兑换前必须调 wake_vm，否则有效码也会误报 invalid
        vm_id = client.wake_vm()
        client.stateful_sharing()
        ok, reason = client.redeem(code)
        own = ""
        if ok or reason == "already_redeemed":
            own = client.scratch_code() or ""
        new_sess = client.session_cookie("hatch_sess") or sess_val
        return {
            "ok": bool(ok or reason == "already_redeemed"),
            "reason": reason,
            "own_code": own,
            "vm_id": vm_id or "",
            "session_token": new_sess,
        }
    finally:
        client.close()
