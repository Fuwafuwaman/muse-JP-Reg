"""离线冒烟：musepy.client.parse_onboarding 纯函数（不联网、不执行页面脚本）。"""

import json
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from musepy.client import parse_onboarding


def _html(*push_chunks):
    scripts = []
    for chunk in push_chunks:
        scripts.append(
            "<script>self.__next_f.push(%s);</script>" % json.dumps([1, chunk])
        )
    noise = "<script>function notAFlightRecord(){return 0;}</script>"
    return "<html><body>" + noise + "".join(scripts) + "</body></html>"


def _row(row_id, payload):
    return row_id + ":" + json.dumps(payload) + "\n"


def case_normal():
    status = {"result": "success", "resolvedDestination": "main_app",
              "status": {"isGeoGated": False, "hasHatchAccess": True, "waitlistStatus": None}}
    disclosure = {"disclosureText": "次に進むことで同意します。", "disclosureVersion": "HATCH_ROW_v0"}
    doc = _html(_row("1a2b", status), _row("3c4d", disclosure))
    out = parse_onboarding(doc)
    assert out["state"] == "disclosure", out
    assert out["disclosure_text"] == disclosure["disclosureText"], out
    assert out["disclosure_version"] == "HATCH_ROW_v0", out
    assert out["resolved_destination"] == "main_app", out
    assert out["is_geo_gated"] is False and out["has_hatch_access"] is True, out


def case_waitlist():
    status = {"result": "success", "resolvedDestination": "waitlist",
              "status": {"isGeoGated": False, "hasHatchAccess": False, "waitlistStatus": "pending"}}
    doc = _html(_row("a1b2", status),
                _row("c3d4", {"disclosureText": "任意正文", "disclosureVersion": "HATCH_ROW_v0"}))
    out = parse_onboarding(doc)
    assert out["state"] == "waitlist", out
    assert out["resolved_destination"] == "waitlist", out
    assert out["waitlist_status"] == "pending", out


def case_geo_gated():
    status = {"result": "success", "resolvedDestination": "main_app",
              "status": {"isGeoGated": True, "hasHatchAccess": False, "waitlistStatus": None}}
    doc = _html(_row("aa11", status),
                _row("bb22", {"disclosureText": "任意正文", "disclosureVersion": "HATCH_ROW_v0"}))
    out = parse_onboarding(doc)
    assert out["state"] == "waitlist", out
    assert out["is_geo_gated"] is True, out


def case_conflict():
    doc = _html(
        _row("1a2b", {"disclosureText": "版本A的正文", "disclosureVersion": "HATCH_ROW_v0"}),
        _row("3c4d", {"disclosureText": "版本B的正文", "disclosureVersion": "HATCH_ROW_v0"}),
    )
    out = parse_onboarding(doc)
    assert out["state"] == "unresolved", out


def case_missing():
    doc = _html(_row("1a2b", {"unrelated": True}))
    out = parse_onboarding(doc)
    assert out["state"] == "unresolved", out


def case_unknown_version():
    doc = _html(_row("1a2b", {"disclosureText": "正文", "disclosureVersion": "HATCH_UNKNOWN_v9"}))
    out = parse_onboarding(doc)
    assert out["state"] == "unresolved", out
    assert out["reason"] == "disclosure_version_unsupported", out


def case_tolerant_activate_session():
    """accept-tos 済みのアカウントは動的披露文が取れない。tolerant なら失敗にしない。"""
    from musepy.client import MuseClient, MuseError

    class _Resp:
        status_code, headers = 200, {}

        def __init__(self, text):
            self.text = text

    class _Sess:
        def __init__(self, resp):
            self._resp = resp

        def post(self, *a, **k):
            return self._resp

        def get(self, *a, **k):
            return self._resp

    # status 行だけの payload（disclosureText が無い）→ parse_onboarding は unresolved
    status = {"result": "success", "resolvedDestination": "main_app",
              "status": {"isGeoGated": False, "hasHatchAccess": True}}
    doc = _html(_row("1a2b", status))

    def _client():
        cl = MuseClient(proxy="", lang="ja", timeout=10)
        cl.s = _Sess(_Resp(doc))
        cl.ccv_status = lambda: ""
        return cl

    out = _client().activate_session("tok", tolerant=True)
    assert out["state"] == "disclosure_missing", out
    assert not out.get("disclosure_text") and not out.get("disclosure_version"), out
    try:
        _client().activate_session("tok")
        raise AssertionError("strict のはずが例外が出ない")
    except MuseError as exc:
        assert "disclosure_context_unresolved" in str(exc), exc


def case_ccv_no_finalize():
    """finalize=False は /api/checkout/finalize を撃たずに ccv_status を返す。"""
    from musepy import ccv

    calls = []

    def fake_start_checkout(client, *a, **k):
        calls.append("start_checkout")
        return ({"upl_session_id": 1, "order_id": "o", "money": {"amount": 1.0, "currency": "USD"},
                 "common": {}, "container_mode": "LIVE", "otc": {}, "country_code": "JP",
                 "money_amount": 1.0}, {"mutation": {"success": True, "credentialId": "c1"}, "nextAction": None})

    def fake_post_json(client, path, payload):
        calls.append(path)
        return 200, {"checkoutStatus": "SUCCESS"}

    class _Client:
        def ccv_status(self):
            return "not_required"

    orig_start, orig_post = ccv.start_checkout, ccv._post_json
    ccv.start_checkout, ccv._post_json = fake_start_checkout, fake_post_json
    try:
        res = ccv.activate(_Client(), "4549240653688285", "09/30", "226", log=lambda *a: None, finalize=False)
        assert res["finalized"] is False, res
        assert res["ccv_status"] == "not_required", res
        assert "/api/checkout/finalize" not in calls, calls
        assert "finalize" not in " ".join(res["steps"]), res["steps"]

        calls.clear()
        res = ccv.activate(_Client(), "4549240653688285", "09/30", "226", log=lambda *a: None)
        assert res["finalized"] is True, res
        assert res["checkout_status"] == "SUCCESS", res
        assert "/api/checkout/finalize" in calls, calls
    finally:
        ccv.start_checkout, ccv._post_json = orig_start, orig_post


def main():
    tests = [("normal_disclosure", case_normal), ("waitlist", case_waitlist),
             ("geo_gated", case_geo_gated), ("disclosure_conflict", case_conflict),
             ("disclosure_missing", case_missing), ("unknown_version", case_unknown_version),
             ("tolerant_activate_session", case_tolerant_activate_session),
             ("ccv_no_finalize", case_ccv_no_finalize)]
    for name, fn in tests:
        fn()
        print(f"[PASS] {name}")
    print(f"冒烟通过 {len(tests)}/{len(tests)}")


if __name__ == "__main__":
    main()
