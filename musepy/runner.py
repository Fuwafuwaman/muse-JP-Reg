"""多账号批量调度（命令行 runner，语义对齐 duwqijlk/muse-protocol）。"""

import concurrent.futures as cf
import json
import os
import random
import threading
from datetime import datetime, timezone

from . import caa as _caa
from . import otp as _otp
from .client import MuseClient, MuseError, random_dob

PRE_SEND_STAGES = frozenset({"initialized", "bootstrap", "restart"})
AUTH_STAGES = PRE_SEND_STAGES | {"send_otp", "wait_otp", "confirm_otp", "completion"}

TRANSPORT_MARKERS = (
    "curl: (35)", "curl: (28)", "curl: (7)", "curl: (56)", "curl: (52)",
    "timed out", "connectionerror", "connection reset", "connection refused",
)

# 常用日本姓名池（姓 / 名），register_completion 语义：first_name=名、last_name=姓
SURNAMES = ["佐藤", "鈴木", "高橋", "田中", "伊藤", "渡辺", "山本", "中村", "小林", "加藤",
            "吉田", "山田", "佐々木", "山口", "松本", "井上", "木村", "林", "斎藤", "清水",
            "山崎", "森", "池田", "橋本", "阿部", "石川", "前田", "藤田", "小川", "岡田",
            "後藤", "長谷川", "石井", "村上", "近藤", "坂本", "遠藤", "青木", "藤井", "西村"]
GIVEN_NAMES = ["ヨウ", "タクミ", "ユウキ", "ハルト", "ソウタ", "リク", "カイト", "ミナト", "ハル", "アオイ",
               "ヒナタ", "ツバサ", "レン", "コウ", "シュン", "ナオキ", "ケイタ", "ダイキ", "リョウ", "ユウト",
               "サクラ", "ユイ", "メイ", "アカリ", "ヒカリ", "ノゾミ", "ミサキ", "カナ", "リナ", "ユナ",
               "ソラ", "カイ", "トワ", "イツキ", "アサヒ", "マコト", "タケル", "ジン", "カズマ", "ユウマ"]


def _lines(path):
    try:
        with open(path, encoding="utf-8") as stream:
            return [line.strip() for line in stream if line.strip()]
    except OSError:
        return []


def mkproxy(line):
    """host:port:user:pass → http://user:pass@host:port；host:port → http://host:port；
    同时兼容已带协议前缀（http:// / socks5://）的代理行。
    """
    raw = (line or "").strip()
    if "://" in raw:
        return raw
    parts = raw.split(":")
    if len(parts) == 4:
        host, port, user, password = parts
        return f"http://{user}:{password}@{host}:{port}"
    if len(parts) == 2:
        host, port = parts
        return f"http://{host}:{port}"
    raise ValueError("proxy_line_format")


def proxy_endpoint(line):
    """仅取 host:port，用于日志（不打印凭据）。"""
    raw = (line or "").strip()
    if "://" in raw:
        rest = raw.split("://", 1)[1]
        if "@" in rest:
            rest = rest.rsplit("@", 1)[1]
        return rest
    parts = raw.split(":")
    return f"{parts[0]}:{parts[1]}" if len(parts) >= 2 else None


def _classify_error(exc):
    """传输层失败归 transport_error；其余归 failed。不做任何 IP 封禁推断。"""
    if isinstance(exc, (TimeoutError, ConnectionError)):
        return "transport_error"
    text = str(exc).lower()
    if any(marker in text for marker in TRANSPORT_MARKERS):
        return "transport_error"
    return "failed"


def utc_stamp():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds").replace("+00:00", "Z")


class BatchRunner:
    """多账号批量调度器。一个账号 = 一个新 MuseClient 实例 + 一条当前 sticky 代理行。"""

    def __init__(self, config, *, client_factory=None, otp_waiter=None, ip_resolver=None):
        self.cfg = config
        self.workers = int(config.get("workers", 5) or 5)
        self.max_per_ip = int(config.get("max_per_ip", 6) or 6)
        self.lang = config.get("lang", "ja")
        self.fp = config.get("impersonate", "chrome142")
        self.otp_file = config.get("otp_file")
        self.proxy_file = config.get("proxy_file")
        self.codes_file = config.get("codes_file")
        self.output_dir = config.get("output_dir", "output")
        self.tried_file = config.get("tried_file")
        self.send_caa = bool(config.get("send_caa", False))
        self.resolve_ip = bool(config.get("resolve_ip", True))
        self.client_factory = client_factory or self._default_client_factory
        self.otp_waiter = otp_waiter or self._default_otp_waiter
        self.ip_resolver = ip_resolver or self._default_ip_resolver
        self.ROWS, self.QUEUE, self.PX, self.CODES = {}, [], [], []
        self._lk = threading.Lock()
        self._codes_lk = threading.Lock()
        self._storage_lk = threading.Lock()
        self._stop = threading.Event()
        self._stop_reason = None
        self._proxy_claims, self._pool_bad, self._ipcache, self._tried = set(), set(), {}, set()
        self._qi = [0]
        self._stats = {key: 0 for key in ("started", "ok", "pending", "server_rejected",
                                          "transport_error", "failed", "ip_switches")}

    def _default_client_factory(self, proxy_url):
        return MuseClient(proxy=proxy_url, impersonate=self.fp, lang=self.lang)

    @staticmethod
    def _default_otp_waiter(email, url, after):
        return _otp.wait_otp(url, after)

    @staticmethod
    def _default_ip_resolver(proxy_url):
        from curl_cffi import requests as cr
        from .client import normalize_proxy
        px = normalize_proxy(proxy_url)
        s = cr.Session(impersonate="chrome142", proxies={"http": px, "https": px} if px else None, timeout=12)
        s.trust_env = False
        try:
            r = s.get("https://api.ipify.org")
            ip = r.text.strip()
            return ip if ip.count(".") == 3 else None
        finally:
            s.close()

    def init(self, count=30):
        count = int(count)
        if count < 1:
            raise ValueError("count_must_be_positive")
        rows = {}
        for line in _lines(self.otp_file or ""):
            if "----" not in line:
                continue
            email, url = line.split("----", 1)
            email = email.strip()
            if not email or any(ch in email for ch in "/\\:\x00"):
                continue
            rows[email] = url.strip()
        tried = (set(_lines(self.tried_file))
                 if self.tried_file and os.path.exists(self.tried_file) else set())
        proxies = _lines(self.proxy_file or "")
        if not proxies:
            raise ValueError("proxy_pool_empty")
        with self._lk:
            self.ROWS = rows
            self.QUEUE = [email for email in rows if email not in tried][:count]
            self.PX = proxies
            self.CODES = _lines(self.codes_file) if self.codes_file else []
            self._tried = set(tried)
            self._proxy_claims.clear()
            self._pool_bad.clear()
            self._ipcache.clear()
            self._qi[0] = 0
            for key in self._stats:
                self._stats[key] = 0
            self._stop.clear()
            self._stop_reason = None
        os.makedirs(self.output_dir, exist_ok=True)
        return len(self.QUEUE)

    def next_job(self):
        with self._lk:
            if self._stop.is_set() or self._qi[0] >= len(self.QUEUE):
                return None
            email = self.QUEUE[self._qi[0]]
            self._qi[0] += 1
            self._stats["started"] += 1
            return email

    def request_stop(self, reason):
        with self._lk:
            if not self._stop.is_set():
                self._stop_reason = reason
            self._stop.set()

    def summary(self):
        with self._lk:
            return {**self._stats,
                    "queued": len(self.QUEUE),
                    "remaining": len(self.QUEUE) - self._stats["started"],
                    "stopped": self._stop.is_set(),
                    "stop_reason": self._stop_reason}

    def _resolve_ip(self, line):
        if not self.resolve_ip:
            return None, True
        with self._lk:
            if line in self._ipcache:
                cached = self._ipcache[line]
                return cached, cached is not None
        try:
            ip = self.ip_resolver(mkproxy(line))
            if ip is not None and not (isinstance(ip, str) and ip.count(".") == 3):
                ip = None
        except Exception:
            ip = None
        with self._lk:
            self._ipcache[line] = ip
        return ip, ip is not None

    def _pick_proxy(self, *, current=None, avoid=None):
        for _ in range(400):
            line = random.choice(self.PX)
            if line in self._pool_bad or line == avoid:
                continue
            if line != current:
                with self._lk:
                    if line in self._proxy_claims:
                        continue
            ip, ok = self._resolve_ip(line)
            if not ok:
                with self._lk:
                    self._pool_bad.add(line)
                continue
            with self._lk:
                self._proxy_claims.add(line)
            return line, ip
        if current is not None and current not in self._pool_bad:
            with self._lk:
                self._proxy_claims.add(current)
            return current, self._ipcache.get(current)
        for _ in range(len(self.PX)):
            with self._lk:
                free = [item for item in self.PX
                        if item not in self._pool_bad and item not in self._proxy_claims
                        and item != avoid]
                if not free:
                    break
                line = random.choice(free)
                self._proxy_claims.add(line)
            ip, ok = self._resolve_ip(line)
            if ok:
                return line, ip
            with self._lk:
                self._proxy_claims.discard(line)
        if current is not None:
            with self._lk:
                self._proxy_claims.add(current)
            return current, self._ipcache.get(current)
        with self._lk:
            shared = list(self._proxy_claims) or list(self.PX)
        line = random.choice(shared)
        print(f"[i] proxy pool exhausted; sharing {proxy_endpoint(line)}", flush=True)
        with self._lk:
            self._proxy_claims.add(line)
        return line, self._ipcache.get(line)

    def _release(self, line):
        if not line:
            return
        with self._lk:
            self._proxy_claims.discard(line)

    def _save_record(self, rec):
        path = os.path.join(self.output_dir, rec["email"] + ".json")
        with self._storage_lk:
            previous = {}
            try:
                with open(path, encoding="utf-8") as stream:
                    previous = json.load(stream)
            except (OSError, ValueError):
                previous = {}
            merged = {**previous, **rec}
            for key in ("registered", "own_code"):
                if previous.get(key) and not rec.get(key):
                    merged[key] = previous[key]
            if previous.get("status") == "redeemed":
                merged["status"] = "redeemed"
            merged["updated_at"] = utc_stamp()
            with open(path, "w", encoding="utf-8") as stream:
                json.dump(merged, stream, ensure_ascii=False, indent=2)

    def _stamp_tried(self, email):
        with self._storage_lk:
            self._tried.add(email)
            if self.tried_file:
                with open(self.tried_file, "a", encoding="utf-8") as stream:
                    stream.write(email + "\n")

    def _take_code(self):
        with self._codes_lk:
            if self.CODES:
                return self.CODES.pop(0)
        return None

    def _finish(self, email, rec, outcome, reason, stage, *, status=None):
        rec.update(stage=stage, last_error=reason)
        if status is None:
            if rec.get("registered") and outcome == "ok":
                status = "redeemed"
            elif rec.get("registered"):
                status = "registered_pending_redemption"
            else:
                status = outcome
        rec["status"] = status
        self._save_record(rec)
        stop = (outcome in ("server_rejected", "pending", "failed")
                or (outcome == "transport_error" and stage not in PRE_SEND_STAGES))
        if stop:
            self.request_stop("authentication_paused" if stage in AUTH_STAGES
                              else "workflow_paused")
        return outcome, {"stage": stage, "status": status,
                         "registered": bool(rec.get("registered")), "reason": reason}

    def register_one(self, email, proxy_line):
        rec = {"email": email, "status": "pending", "stage": "initialized",
               "registered": False, "own_code": None, "own_code_status": None,
               "reveal_status": "not_attempted", "is_revealed": None,
               "ip": self._ipcache.get(proxy_line), "last_error": None}
        stage = "initialized"
        client = None
        try:
            if not self.ROWS.get(email):
                return self._finish(email, rec, "failed", "otp_source_missing", stage)
            client = self.client_factory(mkproxy(proxy_line))
            caa = _caa.Caa(client.fp, client.lang) if self.send_caa else None
            stage = "bootstrap"
            client.open_home()
            if caa:
                caa.page_shown(client.s)
                caa.focus(client.s)
                caa.continue_clicked(client.s)
            stage = "restart"
            client.restart()
            if caa:
                caa.registration_start(client.s)
            stage = "send_otp"
            client.send_otp(email)
            sent_at = utc_stamp()
            if caa:
                caa.otp_typed(client.s)
            stage = "wait_otp"
            otp_code = self.otp_waiter(email, self.ROWS.get(email), sent_at)
            if not otp_code:
                return self._finish(email, rec, "pending", "otp_wait_timeout", stage)
            if caa:
                caa.otp_success(client.s)
                caa.birthday_start(client.s)
            stage = "confirm_otp"
            client.confirm_otp(otp_code)
            stage = "completion"
            data = client.register_completion(random_dob(),
                                              random.choice(GIVEN_NAMES),
                                              random.choice(SURNAMES))
            auth = data.get("auth_meta_session") or {}
            token, uid = auth.get("frl_access_token"), auth.get("frl_account_id")
            if not token:
                return self._finish(email, rec, "pending", "completion_result_unknown", stage)
            rec["registered"] = True
            if caa:
                caa.create_success(client.s, uid)
                caa.login_success(client.s, uid)
            stage = "session_bootstrap"
            onboarding = client.activate_session(token)
            stage = "accept_tos"
            client.accept_tos(onboarding)
            stage = "wake"
            if client.wake_vm() is None:
                return self._finish(email, rec, "pending", "vm_unresolved", stage)
            stage = "sharing"
            client.stateful_sharing()
            stage = "invite"
            if not client.is_live():
                return self._finish(email, rec, "pending", "invite_result_unverified", stage)
            code = self._take_code()
            if not code:
                return self._finish(email, rec, "pending", "redemption_code_unavailable", stage)
            stage = "redeem"
            ok, reason = client.redeem(code)
            if not ok:
                return self._finish(email, rec, "server_rejected",
                                    f"redemption_unverified:{reason}", stage)
            stage = "own_code"
            own = client.scratch_code()
            if own:
                rec["own_code"] = own
                rec["own_code_status"] = "available"
                return self._finish(email, rec, "ok", "redemption_verified", stage)
            rec["own_code_status"] = "pending"
            return self._finish(email, rec, "ok", "own_code_pending", stage)
        except MuseError as exc:
            text = str(exc)
            if "region_waitlist" in text:
                return self._finish(email, rec, "pending", "region_waitlist", stage,
                                    status="registered_waitlist")
            if text.startswith(("restart ", "send-otp ", "confirm-otp ",
                                "register-completion ", "accept-tos ")):
                return self._finish(email, rec, "server_rejected", text, stage)
            return self._finish(email, rec, "pending", text, stage)
        except Exception as exc:
            return self._finish(email, rec, _classify_error(exc), type(exc).__name__, stage)
        finally:
            if client is not None:
                try:
                    client.close()
                except Exception:
                    pass

    def _worker(self, wid):
        proxy_line, avoid = None, None
        uses, retries = 0, 0
        email = None
        while True:
            if email is None:
                email = self.next_job()
                if email is None:
                    self._release(proxy_line)
                    return
                retries = 0
            if proxy_line is None:
                proxy_line, _ = self._pick_proxy(current=None, avoid=avoid)
                avoid = None
            outcome, detail = self.register_one(email, proxy_line)
            message = json.dumps(detail, ensure_ascii=False, sort_keys=True)
            print(f"  [W{wid}] {outcome} {message}", flush=True)
            with self._lk:
                self._stats[outcome if outcome in self._stats else "failed"] += 1
            if outcome == "ok":
                self._stamp_tried(email)
                uses += 1
                email, retries = None, 0
                if uses >= self.max_per_ip and not self._stop.is_set():
                    self._release(proxy_line)
                    with self._lk:
                        self._stats["ip_switches"] += 1
                    avoid = proxy_line
                    proxy_line, uses = None, 0
            elif (outcome == "transport_error" and retries < 3
                    and detail.get("stage") in PRE_SEND_STAGES):
                retries += 1
                self._release(proxy_line)
                with self._lk:
                    self._pool_bad.add(proxy_line)
                proxy_line = None
                with self._lk:
                    self._stats["ip_switches"] += 1
                print(f"  [W{wid}] proxy_retry#{retries} {email}", flush=True)
            else:
                self._stamp_tried(email)
                email, retries = None, 0
            if self._stop.is_set():
                self._release(proxy_line)
                return

    def run(self):
        workers = max(1, self.workers)
        with cf.ThreadPoolExecutor(max_workers=workers) as executor:
            list(executor.map(self._worker, range(workers)))
