"""Meta リンク承認 (年齢確認ゲート) を CDP 経由で操作する補助ツール.

なぜ必要か:
    Muse の年齢確認ゲート (`ccv_status=ccv_required`) は、Muse が発行する
    `linking-web-auth` URL を「実ブラウザで人間が開いて Meta ログインを踏む」ことで
    解除される。WebUI の /link ページは URL を出すだけで、あとは人間の操作だった。
    ここでは Edge を CDP (--remote-debugging-port) で繋ぎ、DOM を直接読んで
    クリックするので、AX ツリーが取れない環境でも操作できる。

前提:
    * Edge を次のように起動しておく (プロファイルは既存のもののコピーを使う)。
      生の `...\\Microsoft\\Edge\\User Data` を --user-data-dir に渡すと
      Edge は DevTools を有効化しない ("requires a non-default data directory")。

        msedge.exe --user-data-dir=C:\\edgecdp \\
                   --profile-directory="Profile 9" \\
                   --remote-debugging-port=9222 --no-first-run

    * WebUI が 127.0.0.1:8766 で動いていること。

使い方:
    python tools/link_cdp.py probe <email>   # URL を再発行して開き、ページ内容を表示
    python tools/link_cdp.py dump            # 現在のページ内容だけを表示
    python tools/link_cdp.py goto <url>      # 現在のページで URL へ移動
    python tools/link_cdp.py click <テキスト> # テキスト一致の要素をクリック
    python tools/link_cdp.py fb [メール] [パスワード]  # FB ログインフォームを埋めて送信
    python tools/link_cdp.py fill <セレクタ> <値>   # 入力欄に値を入れる
    python tools/link_cdp.py keys <キー>     # 現在フォーカス中の要素にキー送出
    python tools/link_cdp.py reload          # リロード

結果は %TEMP%\\link_cdp.json に UTF-8 で書き出し、コンソールにも出す
(コンソールは cp932 で化けることがあるためファイルを見るのが確実)。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import time
import urllib.request

API = "http://127.0.0.1:8766"
CDP = "http://127.0.0.1:9222"
OUT = os.path.join(tempfile.gettempdir(), "link_cdp.json")

# Edge に保存されている Meta 側アカウント (Profile 9 から取り出したもの)。
# 1 個の FB アカウントを 20 アカウント分の承認で使い回す。
FB_EMAIL = "nikonikovide0@yahoo.co.jp"
FB_PASSWORD = 'gb86?E"y@T(o'

MAX_ITEMS = 60


def _api(path: str, payload: dict | None = None):
    if payload is None:
        req = urllib.request.Request(API + path)
    else:
        req = urllib.request.Request(
            API + path,
            data=json.dumps(payload).encode("utf-8"),
            headers={"Content-Type": "application/json"},
        )
    with urllib.request.urlopen(req, timeout=30) as fh:
        return json.load(fh)


def _page(ctx):
    pages = [p for p in ctx.pages if p.url != "about:blank" or len(ctx.pages) == 1]
    if not pages:
        return ctx.new_page()
    # アクティブに近いのは末尾側。まずどれか 1 枚を掴む。
    return pages[-1]


def snapshot(page) -> dict:
    info: dict = {"url": page.url, "title": page.title()}
    try:
        body = page.inner_text("body")
        info["body"] = body[:4000]
    except Exception as exc:  # noqa: BLE001
        info["body_error"] = repr(exc)

    def collect(sel: str, key: str):
        items = []
        for el in page.query_selector_all(sel)[:MAX_ITEMS]:
            try:
                if not el.is_visible():
                    continue
            except Exception:  # noqa: BLE001
                continue
            items.append(
                {
                    "tag": el.evaluate("e => e.tagName.toLowerCase()"),
                    "text": (el.inner_text() or "").strip()[:80],
                    "name": el.get_attribute("name"),
                    "type": el.get_attribute("type"),
                    "id": el.get_attribute("id"),
                    "aria": el.get_attribute("aria-label"),
                    "ph": el.get_attribute("placeholder"),
                    "value": (el.get_attribute("value") or "")[:40],
                    "href": (el.get_attribute("href") or "")[:120],
                }
            )
        info[key] = items

    collect("button, [role=button], input[type=submit], a[role=button]", "buttons")
    collect("input, textarea", "fields")
    collect("a[href]", "links")
    info["frames"] = [
        {"url": fr.url[:120]} for fr in page.frames if fr is not page.main_frame
    ]
    return info


def _body(page) -> str:
    try:
        return page.inner_text("body")
    except Exception:  # noqa: BLE001
        return ""


def _first(page, *candidates):
    """最初にクリックできた候補を返す。"""
    for cand in candidates:
        try:
            loc = cand()
            loc.first.click(timeout=4000)
            return "ok"
        except Exception:  # noqa: BLE001
            continue
    return "miss"


def hop(page) -> str:
    """Meta リンクの現在の画面から 1 手だけ進める。実行した操作名を返す。"""
    body = _body(page)

    # 1. Meta のエラーダイアログ。正解は「Facebookにログイン」で再ログイン
    #    （「キャンセル」を押すとトークンを失って行き止まりになる）。
    if "もう一度Facebookアカウントにログイン" in body:
        return "retry-fb:" + _first(page, lambda: page.get_by_role(
            "button", name="Facebookにログイン", exact=False))

    # 2. Muse の fxauth が返す「〜としてログイン」確認。
    if "としてログイン" in body:
        return "login-as:" + _first(page, lambda: page.get_by_role(
            "button", name="としてログイン", exact=False))

    # 3. パスワードだけのフォーム。ここは click では送信されず Enter が確実
    #    （AYMH はピッカーを DOM に残したまま重ねるので、ピッカー判定より先に見る）。
    passbox = page.locator('input[name="pass"]:visible')
    if passbox.count() and page.locator('input[name="email"]:visible').count() == 0:
        passbox.first.fill(FB_PASSWORD)
        passbox.first.press("Enter")
        return "pass-enter:ok"

    # 4. メール＋パスワードの素のフォーム。
    if page.locator('input[name="email"]:visible').count():
        page.fill('input[name="email"]', FB_EMAIL)
        page.locator('input[name="pass"]:visible').first.fill(FB_PASSWORD)
        page.locator('input[name="pass"]:visible').first.press("Enter")
        return "fb-form:ok"

    # 5. auth.meta.com の入口。
    if "ログインまたはアカウントを作成" in body or "Facebookでログイン" in body:
        return "meta-entry:" + _first(page, lambda: page.get_by_role(
            "button", name="Facebookでログイン", exact=False),
            lambda: page.get_by_text("Facebookでログイン", exact=False))

    # 6. accountscenter の「アカウントは追加済みです」ダイアログ。
    if "アカウントは追加済み" in body:
        return "already:" + _first(page, lambda: page.get_by_role(
            "button", name="他のアカウントを追加する", exact=False),
            lambda: page.get_by_text("他のアカウントを追加する", exact=False))

    # 6.5 Instagram の fxcal 同意画面。ここの「次へ」は実クリック / .click() /
    #     force=True のどれでも不発で、focus して Enter だけが通る
    #     （onClick は存在するのに動かない＝SSR ハイドレートの取りこぼし）。
    if "アクセスする必要があります" in body:
        btn = page.locator('div[role="button"]', has_text="次へ").first
        btn.evaluate("e => e.focus()")
        page.keyboard.press("Enter")
        return "ig-consent-enter:ok"

    # 7. AYMH のプロフィールピッカー。ここの「次へ」は href も form も無い
    #    div[role=button] で実マウス/JS/Enter のどれでも動かない。押すべきは
    #    「別のプロフィールを使用」で、これを押すと本物のログインフォームに
    #    差し替わり <form action> が next= を保持したままになる (次の hop で
    #    上の 4 番が拾う)。「次へ」を押すと next= を失ってピッカーがループする。
    if "別のプロフィールを使用" in body:
        return "alt-profile:" + _first(
            page,
            lambda: page.get_by_text("別のプロフィールを使用", exact=True),
            lambda: page.get_by_text("別のプロフィールを使用", exact=False),
        )

    return "none"


def _state(email: str) -> str:
    try:
        data = _api("/api/ccv/link/sessions")
    except Exception as exc:  # noqa: BLE001
        return "api-error:" + repr(exc)
    live = [s for s in data.get("sessions", [])
            if s.get("email") == email and not s.get("expired")]
    return live[0]["state"] if live else "no-session"


def chain(page, email: str, email_is_live: bool, max_hops: int = 14) -> dict:
    """現在のページから Meta リンクを最後まで踏む。"""
    trace = []
    for i in range(max_hops):
        st = _state(email)
        trace.append({"hop": i, "state": st, "url": page.url[:200]})
        if st in ("continue",):
            break
        if st == "no-session" and i:
            break
        action = hop(page)
        trace.append({"hop": i, "action": action})
        page.wait_for_timeout(5000 if action.endswith(":ok") else 3000)
        if action == "none":
            # accountscenter で token を失った行き止まりは開き直す。
            if "accountscenter.meta.com" in page.url and "token=" not in page.url \
                    and email_is_live:
                try:
                    page.goto(_live_url(email), wait_until="domcontentloaded", timeout=60000)
                    page.wait_for_timeout(4000)
                    trace.append({"hop": i, "action": "reopen"})
                    continue
                except Exception as exc:  # noqa: BLE001
                    trace.append({"hop": i, "reopen_error": repr(exc)})
            if i > 2:
                break
    trace.append({"final_state": _state(email), "final_url": page.url[:200]})
    return {"trace": trace}


def _live_url(email: str) -> str:
    data = _api("/api/ccv/link/sessions")
    live = [s for s in data.get("sessions", [])
            if s.get("email") == email and not s.get("expired")]
    if not live:
        raise RuntimeError("live なセッションが無い: " + email)
    return live[0]["url"]


def main() -> int:
    mode = sys.argv[1] if len(sys.argv) > 1 else "dump"
    arg1 = sys.argv[2] if len(sys.argv) > 2 else None
    arg2 = sys.argv[3] if len(sys.argv) > 3 else None

    from playwright.sync_api import sync_playwright

    result: dict = {"mode": mode, "ts": time.strftime("%Y-%m-%d %H:%M:%S")}

    if mode == "probe":
        if not arg1:
            print("probe にはメールアドレスが必要", file=sys.stderr)
            return 2
        started = _api("/api/ccv/link/start", {"email": arg1, "timeout": 600})
        result["start_response"] = started
        url = started.get("url") or (started.get("session") or {}).get("url")
        result["minted_url"] = url

    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp(CDP)
        ctx = browser.contexts[0]
        page = _page(ctx)
        page.set_default_timeout(30000)

        if mode == "chain":
            email = arg1 or FB_EMAIL
            try:
                started = _api("/api/ccv/link/start", {"email": email, "timeout": 600})
                result["start_response"] = started
                url = started.get("url") or (started.get("session") or {}).get("url")
            except Exception:  # noqa: BLE001
                url = _live_url(email)
            if not url:
                url = _live_url(email)
            result["link_url"] = url
            page.goto(url, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
            result["chain"] = chain(page, email, True)
        elif mode == "probe" and result.get("minted_url"):
            page.goto(result["minted_url"], wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(5000)
        elif mode == "goto" and arg1:
            page.goto(arg1, wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        elif mode == "reload":
            page.reload(wait_until="domcontentloaded", timeout=60000)
            page.wait_for_timeout(4000)
        elif mode == "click" and arg1:
            clicked = None
            for attempt in (
                lambda: page.get_by_role("button", name=arg1, exact=False).first.click(),
                lambda: page.get_by_role("link", name=arg1, exact=False).first.click(),
                lambda: page.get_by_text(arg1, exact=False).first.click(),
                lambda: page.locator(arg1).first.click(),
            ):
                try:
                    attempt()
                    clicked = "ok"
                    break
                except Exception as exc:  # noqa: BLE001
                    clicked = repr(exc)
            result["click"] = clicked
            page.wait_for_timeout(4000)
        elif mode == "fill" and arg1 and arg2 is not None:
            page.fill(arg1, arg2)
            page.wait_for_timeout(1500)
        elif mode == "keys" and arg1:
            page.keyboard.press(arg1)
            page.wait_for_timeout(3000)
        elif mode == "fb":
            email = arg1 or FB_EMAIL
            password = arg2 or FB_PASSWORD
            steps = []
            for label, fn in (
                ("email", lambda: page.fill('input[name="email"]', email)),
                ("pass", lambda: page.fill('input[name="pass"]', password)),
            ):
                try:
                    fn()
                    steps.append(label + ":ok")
                except Exception as exc:  # noqa: BLE001
                    steps.append(label + ":" + repr(exc))
            try:
                page.get_by_role("button", name="ログイン").first.click()
                steps.append("submit:ok")
            except Exception as exc:  # noqa: BLE001
                steps.append("submit:" + repr(exc))
            result["steps"] = steps
            page.wait_for_timeout(9000)

        result["snapshot"] = snapshot(page)
        result["open_pages"] = [{"url": p.url[:150], "title": p.title()[:80]} for p in ctx.pages]

    text = json.dumps(result, ensure_ascii=False, indent=2)
    with open(OUT, "w", encoding="utf-8") as fh:
        fh.write(text)
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:  # noqa: BLE001
        pass
    print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
