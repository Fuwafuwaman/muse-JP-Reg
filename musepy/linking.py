"""
muse.ai 年齢確認(CCV) のアカウント連携方式（Instagram / Facebook）

`ccv_status=ccv_required` のアカウントはカード登録以外に、Meta の
Instagram / Facebook アカウントを連携させても解除できる。カードが
デビットだと弾かれる（1383254）ため、**カード無しの迂回路**として重要。

フロントエンド（チャンク `2b8hzcj947omd.js` の useCcvAccountLinking）は 3 手順:

    GET  /api/hatch/age-confirmation/linking-options
         → {"linkable_account_types": ["instagram","facebook"]}
    GET  /api/hatch/age-confirmation/linking-web-auth?account_type=<t>
         → {"url": <Meta の承認URL>, "attempt_id": "hatchagelink:<uuid>.<token>",
            "expires_at": <epoch秒>}
         （再承認時は &retry_attempt_id=<前回の attempt_id> を付ける）
    POST /api/hatch/age-confirmation/linking-web-auth-completion {"attempt_id": ...}
         → {"next_action": "pending" | "continue" | "blocked" | "use_credit_card"
                           | "retry_auth" | "restart_linking"}

`pending` の間はポーリングを続ける。`url` は人間がブラウザで開いて Meta に
ログイン・承認する必要がある（この手順だけは純 HTTP では代替できない）。
`continue` が出れば連携成立＝年齢確認ゲートが外れる。
"""

import contextlib
import json
import logging
import random
import threading
import time

from .client import BASE, HEADERS_GET, HEADERS_JSON

logger = logging.getLogger("muse.linking")

OPTIONS_PATH = "/api/hatch/age-confirmation/linking-options"
START_PATH = "/api/hatch/age-confirmation/linking-web-auth"
COMPLETE_PATH = "/api/hatch/age-confirmation/linking-web-auth-completion"

LINK_ACCOUNT_TYPES = ("facebook", "instagram")
DEFAULT_ACCOUNT_TYPE = "facebook"

# サーバが返しうる next_action の全量（フロントエンドの定義と一致させること）
NEXT_ACTIONS = ("pending", "continue", "blocked", "use_credit_card",
                "retry_auth", "restart_linking")

# 続行不能＝ポーリングをやめてよい next_action
TERMINAL_ACTIONS = frozenset(a for a in NEXT_ACTIONS if a != "pending")

# 連携は成立した（ゲートが外れた）next_action
SUCCESS_ACTIONS = frozenset({"continue"})

# 人間の承認やり直しが要る next_action
RETRY_ACTIONS = frozenset({"retry_auth", "restart_linking"})

ACTION_HINTS = {
    "pending": "Meta 側の承認待ち",
    "continue": "連携成立。年齢確認ゲートが解除された",
    "blocked": "連携は通ったが Muse 側でブロックされた",
    "use_credit_card": "連携は通ったが年齢確認には使えず、クレジットカードが必要",
    "retry_auth": "承認をやり直す必要がある（retry_attempt_id を付けて再開）",
    "restart_linking": "最初から連携をやり直す必要がある",
}

_POLL_INTERVAL = 5.0
_POLL_TIMEOUT = 600.0
# complete() は「Meta 側がまだ解決していない」間、HTTP 429
# {"error":"Failed to resolve account linking"} を返す（＝実質 pending の合図。
# 1 本だけ・0.3 req/s でも 429 になるので、レート制限ではない）。よって 429 は
# 失敗扱いせず、間隔どおり淡々とポーリングする。429 以外の連続失敗だけを粘る。
_POLL_JITTER = 0.25         # ポーリング間隔のゆらぎ（±25%）
_MAX_ERR_BACKOFF = 15.0     # 429 以外が続いたときの待ち時間の上限（秒）
# 429 以外の失敗が errs を超え、かつ連続 _COMPLETE_ERROR_GRACE 秒を超えたら諦める。
_MAX_COMPLETE_ERRORS = 10
_COMPLETE_ERROR_GRACE = 60.0

# 送信間隔と同時実行の上限（送信の山崩し。429 の主因ではないが安全側に倒す）。
_MIN_REQUEST_GAP = 0.3      # 送信の最小間隔（秒）＝最大 3.3 req/s
_MAX_INFLIGHT = 6           # 同時に飛ばす上限
_slot_lock = threading.Lock()
_slot_sem = threading.Semaphore(_MAX_INFLIGHT)
_next_slot = 0.0


@contextlib.contextmanager
def _slot():
    """送信枠を確保してから本体を実行する（終わったら枠を返す）。"""
    global _next_slot
    _slot_sem.acquire()
    try:
        with _slot_lock:
            now = time.time()
            send_at = max(now, _next_slot)
            _next_slot = send_at + _MIN_REQUEST_GAP
        delay = send_at - time.time()
        if delay > 0:
            time.sleep(delay)
        yield
    finally:
        _slot_sem.release()


class LinkingError(RuntimeError):
    def __init__(self, msg, status=None):
        super().__init__(msg)
        self.status = status

    @property
    def throttled(self):
        """429（＝そのうち通る）かどうか。"""
        return self.status == 429 or " 429:" in str(self)


def _get_json(client, path):
    with _slot():
        r = client.s.get(BASE + path, impersonate=client.fp,
                         headers=client._h(HEADERS_GET))
    try:
        return r.status_code, (r.json() or {})
    except Exception:
        return r.status_code, {"_raw": r.text[:200]}


def _post_json(client, path, body):
    with _slot():
        r = client.s.post(BASE + path, impersonate=client.fp,
                          headers=client._h(HEADERS_JSON), data=json.dumps(body))
    try:
        return r.status_code, (r.json() or {})
    except Exception:
        return r.status_code, {"_raw": r.text[:200]}


def linking_options(client):
    """連携できるアカウント種別の一覧を返す。"""
    status, data = _get_json(client, OPTIONS_PATH)
    if status != 200:
        raise LinkingError(f"linking-options {status}: {str(data)[:200]}")
    types = [t for t in (data.get("linkable_account_types") or [])
             if t in LINK_ACCOUNT_TYPES]
    return types


def describe(action):
    """next_action を 1 行の日本語にする。"""
    return ACTION_HINTS.get(action, f"未知の next_action={action}")


def start(client, account_type=DEFAULT_ACCOUNT_TYPE, retry_attempt_id=None):
    """連携を開始し ``{"url","attempt_id","expires_at"}`` を返す。

    ``url`` は人間がブラウザで開く必要がある（Meta のログイン/承認画面）。
    """
    account_type = (account_type or DEFAULT_ACCOUNT_TYPE).strip().lower()
    if account_type not in LINK_ACCOUNT_TYPES:
        raise LinkingError(
            f"account_type={account_type!r} は非対応（{'/'.join(LINK_ACCOUNT_TYPES)} のみ）")
    path = f"{START_PATH}?account_type={account_type}"
    if retry_attempt_id:
        path += "&retry_attempt_id=" + retry_attempt_id
    status, data = _get_json(client, path)
    if status != 200:
        raise LinkingError(f"linking-web-auth {status}: {str(data)[:200]}")
    url = data.get("url")
    if not url:
        # url=null は「すでに連携済み/対象外」の意味（フロントは連携成功扱いにする）
        return {"url": None, "attempt_id": None, "expires_at": None,
                "account_type": account_type}
    attempt_id = data.get("attempt_id")
    if not attempt_id:
        raise LinkingError(f"attempt_id が返らない: {str(data)[:200]}")
    return {"url": url, "attempt_id": attempt_id,
            "expires_at": data.get("expires_at"), "account_type": account_type}


def complete(client, attempt_id):
    """1 回だけ完了チェックして next_action を返す。"""
    status, data = _post_json(client, COMPLETE_PATH, {"attempt_id": attempt_id})
    if status != 200:
        raise LinkingError(f"linking-web-auth-completion {status}: {str(data)[:200]}",
                           status=status)
    action = data.get("next_action")
    if action not in NEXT_ACTIONS:
        raise LinkingError(f"未知の next_action={action!r}: {str(data)[:200]}")
    return action


def wait(client, attempt_id, timeout=_POLL_TIMEOUT, interval=_POLL_INTERVAL,
         log=None, should_stop=None):
    """``pending`` が続く限り完了チェックを繰り返し、最終の next_action を返す。

    タイムアウトしたら ``"pending"``、``should_stop()`` が真になったら ``"stopped"``。
    """
    deadline = time.time() + max(1.0, float(timeout or _POLL_TIMEOUT))
    last = None
    errs = 0
    err_since = None
    backoff = max(1.0, interval)
    while time.time() < deadline:
        if should_stop and should_stop():
            return "stopped"
        try:
            action = complete(client, attempt_id)
            errs = 0
            err_since = None
            backoff = max(1.0, interval)
        except LinkingError as exc:
            if exc.throttled:
                # 「まだ解決していない」だけ。失敗回数に数えず、間隔どおり続ける。
                if log:
                    log("link: まだ未解決（429）、待機中")
                time.sleep(max(1.0, interval * random.uniform(1 - _POLL_JITTER,
                                                             1 + _POLL_JITTER)))
                continue
            errs += 1
            if err_since is None:
                err_since = time.time()
            hard = exc.status is not None and 400 <= exc.status < 500
            fatal = (errs > _MAX_COMPLETE_ERRORS
                     and (time.time() - err_since) > _COMPLETE_ERROR_GRACE)
            if log:
                log("link: 完了チェック失敗（%d 回目 / 連続 %.0fs、継続）: %s",
                    errs, time.time() - err_since, str(exc)[:120])
            if hard or fatal:
                raise
            backoff = min(_MAX_ERR_BACKOFF, backoff * 1.7)
            time.sleep(backoff * random.uniform(0.7, 1.3))
            continue
        if action != last:
            if log:
                log("link: %s（%s）", action, describe(action))
            last = action
        if action != "pending":
            return action
        time.sleep(max(0.5, interval * random.uniform(1 - _POLL_JITTER,
                                                     1 + _POLL_JITTER)))
    return "pending"


def activate(client, account_type=DEFAULT_ACCOUNT_TYPE, timeout=_POLL_TIMEOUT,
             interval=_POLL_INTERVAL, log=None, should_stop=None,
             on_url=None, retry_attempt_id=None):
    """連携で年齢確認を解除する。``on_url(url, info)`` で承認 URL を通知する。

    戻り値は ``{"next_action": ..., "attempt_id": ..., "ok": bool}``。
    ``continue`` 以外は ``ok=False``（理由は ``next_action``）。
    """
    info = start(client, account_type, retry_attempt_id=retry_attempt_id)
    if info["url"] is None:
        if log:
            log("link: url が null（連携済み扱い）")
        return {"next_action": "continue", "attempt_id": None, "ok": True,
                "account_type": info["account_type"]}
    if on_url:
        on_url(info["url"], info)
    if log:
        log("link: 承認 URL をブラウザで開いてください（%s にログインして承認）",
            info["account_type"])
    action = wait(client, info["attempt_id"], timeout=timeout, interval=interval,
                  log=log, should_stop=should_stop)
    return {"next_action": action, "attempt_id": info["attempt_id"],
            "ok": action in SUCCESS_ACTIONS, "account_type": info["account_type"]}


def _main(argv):
    """`python -m musepy.linking <email> [facebook|instagram] [timeout秒]`"""
    import sys

    from .client import MuseClient

    if len(argv) < 2:
        print(__doc__.strip().splitlines()[0])
        print("使い方: python -m musepy.linking <email> [facebook|instagram] [timeout秒]")
        return 2
    email = argv[1]
    account_type = argv[2] if len(argv) > 2 else DEFAULT_ACCOUNT_TYPE
    timeout = float(argv[3]) if len(argv) > 3 else _POLL_TIMEOUT

    import webui.db as db

    row = db.get_registered(email)
    if not row:
        print(f"registered に {email} がいない")
        return 1
    proxies = json.loads(db.get_setting("proxy_pool") or "[]")
    client = MuseClient(proxy=(proxies or [None])[0], impersonate="chrome142", lang="ja")
    for kv in (row["cookie_header"] or "").split(";"):
        if "=" in kv:
            k, v = kv.strip().split("=", 1)
            client.s.cookies.set(k, v, domain="muse.ai")
    try:
        before = client.ccv_status()
        print(f"ccv-status(before) = {before}")
        if before == "not_required":
            print("すでに解除済み")
            return 0
        print("linkable =", linking_options(client))
        res = activate(client, account_type, timeout=timeout,
                       log=lambda m, *a: print("   " + (m % a if a else m), flush=True),
                       on_url=lambda url, info: print("\n>>> " + url + "\n", flush=True))
        print("result =", res)
        print("ccv-status(after) =", client.ccv_status())
        return 0 if res["ok"] else 1
    finally:
        client.close()


if __name__ == "__main__":
    import sys

    raise SystemExit(_main(sys.argv))
