"""WebUI 后端、邮箱 OTP 解析、邀请码裂变与导出全链路冒烟测试（使用临时数据库，零污染真实 webui.db）。"""

import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

try:  # cp932 コンソールでも日本語の print で落ちないように
    sys.stdout.reconfigure(errors="replace")
    sys.stderr.reconfigure(errors="replace")
except Exception:
    pass


def test_mail_provider_otp_formats():
    from mail_providers.icloud_relay import parse_relay_html

    # 1) muse-protocol 标准 OTP JSON 源
    feed_json = json.dumps([
        {"otp": "111111", "time": "2026-11-16T10:00:00Z"},
        {"otp": "882910", "time": "2026-11-16T10:05:00Z"},
    ])
    msgs = parse_relay_html(feed_json)
    assert len(msgs) == 2 and msgs[0]["otp"] == "882910" and msgs[0]["strict_utc"] is True, msgs

    # 2) 本地 4173 iCloud 工作台：空收件箱不得误走 fallback
    empty_4173 = json.dumps({"email": "test@icloud.com", "message": None})
    assert parse_relay_html(empty_4173) == []

    # 3) 本地 4173：若最新一封是旧 OpenAI 邮件，应严格忽略，防止共用邮箱串码
    openai_4173 = json.dumps({
        "email": "test@icloud.com",
        "message": {
            "id": "m-old",
            "subject": "Your ChatGPT code is 999999",
            "sender": "noreply@tm.openai.com",
            "code": "999999",
            "bodyText": "Your OpenAI verification code is 999999",
            "receivedAt": "2026-11-16T11:00:00.000Z",
        },
    })
    assert parse_relay_html(openai_4173) == []

    # 4) 本地 4173：收到 Muse / Meta 验证码邮件，准确提取
    muse_4173 = json.dumps({
        "email": "test@icloud.com",
        "message": {
            "id": "m-muse-1",
            "subject": "Muse 確認コード",
            "sender": "noreply_at_muse_ai_abc@icloud.com",
            "code": "654321",
            "bodyText": "Muse の認証コードは 654321 です。",
            "receivedAt": "2026-11-16T12:00:00.000Z",
        },
    })
    msgs_muse = parse_relay_html(muse_4173)
    assert len(msgs_muse) == 1 and msgs_muse[0]["otp"] == "654321", msgs_muse
    print("[PASS] mail_provider_otp_formats")


def test_db_and_fastapi_routes():
    with tempfile.TemporaryDirectory() as tmp:
        from webui import db
        orig_db = db.DB_PATH
        try:
            db.DB_PATH = Path(tmp) / "test_webui.db"
            db._invite_inflight.clear()
            db.init_db()

            from fastapi.testclient import TestClient
            from webui.app import app

            client = TestClient(app)

            # 1) 静态首页 + 健康检查
            r_root = client.get("/")
            assert r_root.status_code == 200 and "Muse Register" in r_root.text
            r_health = client.get("/api/health")
            assert r_health.status_code == 200 and r_health.json()["ok"] is True

            # 2) 导入邮箱（2段格式）
            r_imp = client.post("/api/import", json={
                "kind": "icloud_relay",
                "text": "u1@icloud.com----http://127.0.0.1:4173/openapi/mail/u1%40icloud.com/tok1/latest\n"
                        "u2@icloud.com----https://otp.example/u2",
            })
            assert r_imp.status_code == 200 and r_imp.json()["inserted"] == 2

            # 3) 导入种子邀请码 + 并发领用与用满逻辑
            r_inv = client.post("/api/invites/import", json={"text": "SEED-CODE-01", "max_uses": 2})
            assert r_inv.status_code == 200 and r_inv.json()["inserted"] == 1
            assert r_inv.json()["stats"]["remaining_uses"] == 2

            c1 = db.claim_invite_code()
            c2 = db.claim_invite_code()
            c3 = db.claim_invite_code()
            assert c1 == "SEED-CODE-01" and c2 == "SEED-CODE-01" and c3 is None
            db.finish_invite_code("SEED-CODE-01", True, "redeemed")
            db.finish_invite_code("SEED-CODE-01", True, "redeemed")
            st = db.invite_stats()
            assert st["exhausted"] == 1 and st["remaining_uses"] == 0, st

            # 4) 注册结果入库与导出格式校验
            db.save_registered({
                "email": "u1@icloud.com",
                "account_id": "frl-998877",
                "access_token": "frl_tok_xxx",
                "session_token": "hatch_sess_cookie_value_886chars",
                "status": "redeemed",
                "stage": "own_code",
                "own_code": "NEW-MUSE-CODE",
                "used_invite_code": "SEED-CODE-01",
                "ip": "203.0.113.88",
                "first_name": "ハルト",
                "last_name": "佐藤",
                "date_of_birth": "1994-05-12",
            })

            r_reg = client.get("/api/registered?filter=has_own_code")
            assert r_reg.status_code == 200 and r_reg.json()["total"] == 1
            item = r_reg.json()["items"][0]
            assert item["own_code"] == "NEW-MUSE-CODE" and item["st_len"] > 10

            r_exp = client.post("/api/registered/export", json={"format": "own_codes", "all": True})
            assert r_exp.status_code == 200 and r_exp.json()["text"] == "NEW-MUSE-CODE"

            r_exp_full = client.post("/api/registered/export", json={"format": "full_relay", "all": True})
            assert "u1@icloud.com----hatch_sess_cookie_value_886chars----NEW-MUSE-CODE----http://127.0.0.1:4173" in r_exp_full.json()["text"]

            r_exp_json = client.post("/api/registered/export", json={"format": "json_all", "all": True})
            assert r_exp_json.status_code == 200 and r_exp_json.json()["mode"] == "download"
            print("[PASS] db_and_fastapi_routes")
        finally:
            db.close_connections()
            db.DB_PATH = orig_db
            db._invite_inflight.clear()


def test_muse_flow_end_to_end_mock():
    from auth_flow import MuseFlow, MuseFlowError

    class DummyMail:
        display_name = "Dummy"
        def create_mailbox(self):
            return "test_muse@icloud.com"
        def wait_for_otp(self, email_addr, timeout=45, issued_after=None):
            return "612890"

    class MockMuseClient:
        def __init__(self, proxy=None, impersonate="chrome142", lang="ja", timeout=30):
            self.fp = impersonate
            self.lang = lang
            self.s = None
            self.tos_content_version = None
            self._waitlist = False

        def open_home(self): pass
        def restart(self, resume=False): return "csrf_mock_123"
        def send_otp(self, email): return {}
        def confirm_otp(self, code):
            assert code == "612890"
            self.tos_content_version = "ntm_dynamic_v4"
            return {"registration_state": "completion_required", "tos": {"content_version": "ntm_dynamic_v4"}}
        def register_completion(self, dob, first, last, **kw):
            return {"auth_meta_session": {"frl_access_token": "frl_at_mock", "frl_account_id": "uid_mock_88"}}
        def session_cookie(self, name="hatch_sess"):
            return "hatch_sess_mock_value"
        def activate_session(self, token):
            return {
                "state": "disclosure",
                "disclosure_text": "動的同意文",
                "disclosure_version": "HATCH_ROW_v0",
                "resolved_destination": "main_app",
                "source_url": "https://muse.ai/",
            }
        def accept_tos(self, onboarding=None): pass
        def wake_vm(self): return "11111111-2222-3333-4444-555555555555"
        def stateful_sharing(self): pass
        def is_live(self): return True
        def redeem(self, code): return True, "redeemed"
        def scratch_code(self): return "HARVESTED-CODE-99"
        def close(self): pass

    harvested = []
    early_saved = []
    with patch("auth_flow.MuseClient", MockMuseClient), patch("auth_flow.resolve_exit_ip", return_value="133.242.10.20"):
        flow = MuseFlow(
            proxy="jp.example:8000:u:p",
            redeem_mode="pool",
            on_early_save=lambda r: early_saved.append(r.account_id),
            claim_pool_code=lambda: "POOL-SEED-1",
            finish_pool_code=lambda c, ok, r: None,
            on_own_code=lambda own, em: harvested.append((own, em)),
        )
        res = flow.run_register(DummyMail())
        assert res.status == "redeemed"
        assert res.account_id == "uid_mock_88"
        assert res.own_code == "HARVESTED-CODE-99"
        assert res.ip == "133.242.10.20"
        assert early_saved == ["uid_mock_88"]
        assert harvested == [("HARVESTED-CODE-99", "test_muse@icloud.com")]
    print("[PASS] muse_flow_end_to_end_mock")


def test_ccv_link_auto_rearm_margin():
    """auto_rearm_tick の margin 上書き（寿命が近い分だけ／強制全件）。"""
    import time
    from types import SimpleNamespace

    from webui import ccv_link as cl

    mgr = cl.LinkManager()
    mgr.auto_rearm = True
    done = []
    mgr.rearm = lambda email, **kw: (done.append(email), {"ok": True})[1]

    now = time.time()

    def sess(email, ttl, state="pending", external=False):
        return SimpleNamespace(
            id=email, email=email, state=state, external=external,
            expires_at=(now + ttl) if ttl else None,
            to_dict=lambda: {"done": False},
        )

    mgr._sessions = {
        "a": sess("long@x", 600.0),            # まだ余裕あり
        "b": sess("soon@x", 60.0),             # 既定閾値(150秒)未満
        "c": sess("err@x", 600.0, state="error"),
        "d": sess("ext@x", 10.0, external=True),  # フロー側が自走中なので触らない
    }

    # 既定の margin: 寿命が近いものと error だけが出し直し対象
    assert sorted(mgr.auto_rearm_tick()) == ["err@x", "soon@x"], done

    # 大きな margin: 直前の 2 件はクールダウンで外れ、余裕のある 1 件が対象になる
    done.clear()
    assert mgr.auto_rearm_tick(margin=10 ** 9) == ["long@x"], done

    # クールダウンを無視すれば全件が対象（external は常に対象外）
    done.clear()
    mgr._last_rearm.clear()
    assert sorted(mgr.auto_rearm_tick(margin=10 ** 9)) == \
        ["err@x", "long@x", "soon@x"], done

    # 自動出し直しが OFF なら何もしない
    mgr.auto_rearm = False
    assert mgr.auto_rearm_tick(margin=10 ** 9) == []
    print("[PASS] ccv_link_auto_rearm_margin")


def test_ccv_link_targets_skip_redeemed():
    """pending_accounts は交換済み(redeemed)と解除済み(not_required)を対象から外す。"""
    from webui import ccv_link as cl

    rows = [
        {"email": "a@x", "status": "registered_unactivated", "extra": {"ccv_status": "pending"}},
        {"email": "b@x", "status": "redeemed", "extra": {"ccv_status": "pending"}},   # 交換＝解除済み
        {"email": "c@x", "status": "registered_unactivated", "extra": {"ccv_status": "not_required"}},
        {"email": "d@x", "status": "registered_unactivated", "extra": {}},
    ]
    orig = cl.db.list_registered_full
    cl.db.list_registered_full = lambda limit=5000: rows
    try:
        got = [r["email"] for r in cl.pending_accounts()]
        assert got == ["a@x", "d@x"], got
    finally:
        cl.db.list_registered_full = orig
    print("[PASS] ccv_link_targets_skip_redeemed")


def test_ccv_link_pick_client_403_fallback():
    """proxy が 403 でも direct 成功ならそちら、全滅なら最後の 1 件を返す。"""
    from webui import ccv_link as cl

    class _Cli:
        def __init__(self, tag, forbidden):
            self.tag = tag
            self.forbidden = forbidden
            self.closed = False

        def close(self):
            self.closed = True

    tags_forbidden = {"p1"}
    made = []
    orig = (cl._default_proxies, cl._build_client, cl.linking.linking_options)

    def build(email, proxy):
        tag = proxy or "direct"
        cli = _Cli(tag, tag in tags_forbidden)
        made.append(cli)
        return cli

    def opts(client, on_forbidden="empty"):
        if client.forbidden and on_forbidden == "raise":
            raise cl.linking.LinkingForbidden("403")
        return [] if client.forbidden else ["facebook"]

    cl._default_proxies = lambda: ["p1", None]
    cl._build_client = build
    cl.linking.linking_options = opts
    try:
        chosen = cl._pick_client("a@x")
        assert chosen.tag == "direct", chosen.tag
        assert made[0].closed, "403 だった proxy のクライアントは閉じる"

        # proxy も direct も 403 → 例外にせず最後のクライアントを返す（error にしない）
        tags_forbidden.add("direct")
        made.clear()
        cl._build_client = build
        chosen = cl._pick_client("a@x")
        assert chosen.tag == "direct", chosen.tag
        assert not chosen.closed, "返したクライアントは閉じない"
    finally:
        (cl._default_proxies, cl._build_client,
         cl.linking.linking_options) = orig
    print("[PASS] ccv_link_pick_client_403_fallback")


def main():
    test_mail_provider_otp_formats()
    test_db_and_fastapi_routes()
    test_muse_flow_end_to_end_mock()
    test_ccv_link_auto_rearm_margin()
    test_ccv_link_targets_skip_redeemed()
    test_ccv_link_pick_client_403_fallback()
    print("WebUI 全链路冒烟通过 6/6")


if __name__ == "__main__":
    main()
