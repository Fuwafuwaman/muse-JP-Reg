"""
muse.ai 年齢確認(CCV) のカード方式アクティベーション（純 HTTP・ブラウザ不要）

`/access/verification` に閉じ込められた `ccv_status=ccv_required` アカウントを、
Meta Pay(ECP) のカード登録チェックアウトで解除する。手順は Next.js の
チャンクから復元した実際のフロントエンド実装（`HatchOnboardingVerificationRoute`
の CCV 画面）そのままで、以下 5 ステップを順に叩く。

    POST /api/checkout/init                 注文の作成(uplSessionId はクライアント生成)
    POST /api/ptt/encryption-key            trustChain(サーバ公開鍵証明書)取得
    POST /api/checkout/credential           operation=CREATE でカード登録
    POST /api/checkout/card-processing-info 3DS 判定のポーリング
    POST /api/checkout/finalize             CREATE_CONTAINER の PTT で確定

PTT(platform trust token) は `base64url(JSON({payload, signatures}))`。
カード登録の payload は秘密情報を AES-GCM で包む必要があるためサーバ公開鍵との
ECDH-ES を実装している。確定処理の payload は秘密情報を含まないので平文のまま
`signatures=[]` でよい（署名鍵はクライアント側で使われない）。
"""

import base64
import hashlib
import json
import logging
import os
import time
import uuid

from cryptography import x509
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from .client import BASE, HEADERS_JSON, HEADERS_JSON as _JSON_HEADERS  # noqa: F401

logger = logging.getLogger("muse.ccv")

# フロントエンドの定数（チャンク module 538529）と一致させること
PAYMENT_PRODUCT_ID = "766358168544755"
RECEIVER_ID = "352501280694489"
APP_ID = "2805739562851096"
DEVICE_ID = "device_id"
# PTT の data 側だけ大文字。フロントエンドは API には "device_id"、
# カード PTT の data.device_id には "DEVICE_ID" を渡す（チャンクの実装どおり）
CARD_DEVICE_ID = "DEVICE_ID"

PTT_OP_ADD_CARD = "ADD_CARD"
PTT_OP_CREATE_CONTAINER = "CREATE_CONTAINER"

# data.credit_card / data.csc に入る「秘密情報は secretPayload を見よ」マーカー。
# フロントエンドの `let a="$e2ee"` そのもの。秘密情報を送る側（CREATE）で使い、
# 秘密情報を送らない側（EDIT）では空文字にする（チャンクの実装どおり）。
E2EE_MARKER = "$e2ee"

DEFAULT_AMOUNT = "1.00"
DEFAULT_CURRENCY = "USD"

VERIFICATION_URL = BASE + "/access/verification"

_POLL_INTERVAL = 1.5
_POLL_TIMEOUT = 30.0


class CcvError(RuntimeError):
    pass


# --------------------------------------------------------------------------- #
# base64url / JWE-ES ヘルパー
# --------------------------------------------------------------------------- #

def _b64u(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode("ascii")


def _b64u_str(text: str) -> str:
    return _b64u(text.encode("utf-8"))


def _u32(value: int) -> bytes:
    return value.to_bytes(4, "big")


def _len_prefixed(data: bytes) -> bytes:
    return _u32(len(data)) + data


def _json_bytes(obj) -> bytes:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def _pem_public_key(der: bytes) -> str:
    body = base64.b64encode(der).decode("ascii")
    lines = [body[i:i + 64] for i in range(0, len(body), 64)]
    return "-----BEGIN PUBLIC KEY-----\n" + "\n".join(lines) + "\n-----END PUBLIC KEY-----\n"


def _encryption_key(shared_secret: bytes, apu: str) -> bytes:
    """JWE ECDH-ES + A256GCM の Concat KDF（RFC 7518 §4.6）を SHA-256 で代用した形。"""
    other_info = b"".join([
        _u32(1),
        shared_secret,
        _len_prefixed(b"A256GCM"),
        _len_prefixed(b""),
        _len_prefixed(apu.encode("utf-8")),
        _u32(256),
    ])
    return hashlib.sha256(other_info).digest()


def _encrypt_payload(payload_b64u: str, secret: dict, server_public_key) -> str:
    """`payload_b64u` を第 1 セグメント兼 AAD とする JWE 風文字列を返す。

    フロントエンドの実装（`h()`/`m()`）では KDF の PartyUInfo に使う
    `"fp:"+b64u(sha256(サーバ SPKI))` を **apv** に、
    apu は空文字列に割り当てているのでそのとおりに組む。
    """
    ephemeral = ec.generate_private_key(ec.SECP256R1())
    shared = ephemeral.exchange(ec.ECDH(), server_public_key)
    server_spki = server_public_key.public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    fingerprint = "fp:" + _b64u(hashlib.sha256(server_spki).digest())
    key = _encryption_key(shared, fingerprint)

    ephemeral_spki = ephemeral.public_key().public_bytes(
        serialization.Encoding.DER, serialization.PublicFormat.SubjectPublicKeyInfo
    )
    header = {
        "alg": "ECDH-ES",
        "apu": _b64u_str(""),
        "apv": _b64u_str(fingerprint),
        "enc": "A256GCM",
        "epk": {"crv": "P-256", "kty": "EC", "pem": _pem_public_key(ephemeral_spki)},
    }
    header_b64u = _b64u_str(json.dumps(header, separators=(",", ":")))
    aad = (header_b64u + "." + payload_b64u).encode("ascii")
    iv = os.urandom(12)
    sealed = AESGCM(key).encrypt(iv, _json_bytes(secret), aad)
    body, tag = sealed[:-16], sealed[-16:]
    return ".".join([payload_b64u, header_b64u, "", _b64u(iv), _b64u(body), _b64u(tag)])


def build_ptt(data: dict, op: str, secret: dict = None, server_public_key=None) -> str:
    payload = _b64u_str(json.dumps(
        {"data": data, "nonce": str(uuid.uuid4()), "op": op, "ver": 1},
        separators=(",", ":"),
    ))
    if secret and server_public_key is not None:
        payload = _encrypt_payload(payload, secret, server_public_key)
    return _b64u_str(json.dumps({"payload": payload, "signatures": []}, separators=(",", ":")))


def server_public_key_from_cert(cert_b64: str):
    der = base64.b64decode(cert_b64)
    cert = x509.load_der_x509_certificate(der)
    return cert.public_key()


def normalize_expiry(expiry: str) -> tuple:
    digits = "".join(ch for ch in str(expiry) if ch.isdigit())
    if len(digits) == 3:
        digits = "0" + digits
    if len(digits) != 4:
        raise CcvError(f"有効期限の形式が不正です: {expiry!r}（MM/YY か MMYY）")
    return digits[:2], "20" + digits[2:]


# --------------------------------------------------------------------------- #
# HTTP
# --------------------------------------------------------------------------- #

def _post_json(client, path, body, referer=VERIFICATION_URL):
    headers = client._h(HEADERS_JSON, {"referer": referer})
    r = client.s.post(
        BASE + path,
        impersonate=client.fp,
        headers=headers,
        data=json.dumps(body, separators=(",", ":"), ensure_ascii=False),
    )
    try:
        return r.status_code, (r.json() or {})
    except Exception:
        return r.status_code, {"_raw": r.text[:200]}


# サーバが返す既知のエラーコード → 日本語の意味（運用者向けのヒント）
ERROR_HINTS = {
    "1383254": ("クレジットカードが必要。デビットカードは年齢確認に使えない"
                "（サーバは「VisaまたはMastercardのクレジットカードを使用してください。"
                "デビットカードは利用できません」と明言する）"),
    "2078022": ("この支払い方法はセキュリティのため一時的に停止されています"
                "（カード BIN 単位のブロック。時間を置くと解除されることがある）"),
    "2078011": ("汎用エラー（カード登録の write 段で拒否）。otc を送るとサーバが理由を"
                "伏せるため、外して撃ち直すと 1383254 / 2078022 が出る"),
    "1353114": "このカードは使用できません（平文パスで送った場合に出る）",
    "2992007": "カード情報を確認してください（復号は成功・Luhn か不一致）",
    "2078132": "汎用の一時エラー（PTT 形式不正 / 有効期限の年が 2 桁 など）",
    "2078135": "PTT の署名フィールド不正",
    "2078136": ("otc の不整合。otcSessionId だけ送って otcType を送っていない、または "
                "encryption-key で束ねた otc と credential で宣言した otc が食い違っている"),
}

# otc（otcSessionId/otcType）付きだと理由が伏される汎用コード。
GENERIC_WRITE_CODES = frozenset({"2078011", "2078132"})


def describe_failure(payload: dict) -> str:
    """サーバのエラーペイロードを 1 行の診断文字列にする。"""
    err = payload.get("error")
    if not isinstance(err, dict):
        err = ((payload.get("mutation") or {}).get("error")
               or (payload.get("checkout") or {}).get("error"))
    if not isinstance(err, dict):
        return json.dumps(payload, ensure_ascii=False)[:300]
    code = str(err.get("code") or "")
    parts = [f"code={code}" if code else ""]
    if err.get("failureStep"):
        parts.append(f"step={err['failureStep']}")
    msg = err.get("message") or err.get("description") or err.get("title") or ""
    if msg:
        parts.append(str(msg))
    if code in ERROR_HINTS:
        parts.append(f"→ {ERROR_HINTS[code]}")
    return " ".join(p for p in parts if p)


def checkout_init(client, upl_session_id, amount=DEFAULT_AMOUNT, currency=DEFAULT_CURRENCY):
    return _post_json(client, "/api/checkout/init", {
        "amount": amount,
        "currency": currency,
        "sessionId": upl_session_id,
        "productId": PAYMENT_PRODUCT_ID,
        "receiverId": RECEIVER_ID,
        "productData": [],
    })


def create_card_credential(client, common, card_ptt, card_number, expiry_month,
                           expiry_year, zip_code, country_code, otc=None):
    """カード 1 枚を credential CREATE で登録する。

    `otc` はフロントエンドも送る（otcSessionId/otcType）が、付けるとサーバは失敗理由を
    汎用コード 2078011 に丸める。otc=None で撃つと 1383254（クレジットカード必須）や
    2078022（BIN 単位の一時停止）のような具体的な理由が返る（実測）。
    """
    body = {
        **common,
        "operation": "CREATE",
        "platformTrustToken": card_ptt,
        "cardFirst6": str(card_number)[:6],
        "cardLast4": str(card_number)[-4:],
        "expiryMonth": expiry_month,
        "expiryYear": expiry_year,
        "zip": zip_code,
        "countryCode": country_code,
    }
    if otc:
        body.update(otc)
    status, cred = _post_json(client, "/api/checkout/credential", body)
    if status != 200:
        raise CcvError(f"checkout/credential {status}: {str(cred)[:200]}")
    return cred


def default_country(init):
    """billing country の既定値（init の postal policy に従う）。"""
    policy = (init.get("paymentMethod") or {}).get("countryPostalPolicy") or {}
    return policy.get("defaultCountryCode") or "JP"


def start_checkout(client, card_number, expiry_month, expiry_year, cvv, zip_code,
                   country_code, amount, currency, use_otc=True):
    """init → encryption-key → カード PTT → credential まで 1 回通す。

    戻り値 (info, cred)。cred が None のときは info["error"] に理由が入る。
    info には upl_session_id / common / order_id / container_mode / money /
    country_code / otc が入り、成功時に後続の finalize でそのまま使える。

    `use_otc=False` は「otc を一切送らない」完全なパス。フロントエンドは常に送るので
    本線ではないが、送ると失敗理由が汎用コードに丸められるため診断に使う。
    """
    upl = "upl_%d_%s" % (int(time.time() * 1000), uuid.uuid4())
    otc = {"otcSessionId": str(uuid.uuid4()), "otcType": "card"} if use_otc else {}
    info = {"upl_session_id": upl, "otc": otc}

    status, init = checkout_init(client, upl, amount=amount, currency=currency)
    money = init.get("currencyAmount") or {}
    if status != 200 or not init.get("orderId") or not money.get("amount"):
        info["error"] = f"checkout/init {status}: {str(init)[:200]}"
        return info, None
    country_code = country_code or default_country(init)
    info.update({
        "order_id": init["orderId"],
        "money": money,
        "container_mode": init.get("paymentContainerMode"),
        "country_code": country_code,
        "common": {
            "uplSessionId": upl,
            "ecpCheckoutSessionId": init.get("ecpCheckoutSessionId"),
            "orderId": init["orderId"],
            "amount": money["amount"],
            "currencyCode": money.get("currency") or DEFAULT_CURRENCY,
            "paymentContainerMode": init.get("paymentContainerMode"),
            "productId": PAYMENT_PRODUCT_ID,
            "receiverId": RECEIVER_ID,
        },
    })

    status, keys = _post_json(client, "/api/ptt/encryption-key", {
        "deviceId": DEVICE_ID,
        "paymentType": "ECP",
        "uplSessionId": upl,
        "productId": PAYMENT_PRODUCT_ID,
        **otc,
    })
    if status != 200 or not (keys.get("trustChain") or []):
        info["error"] = f"ptt/encryption-key {status}: {str(keys)[:200]}"
        return info, None

    # 秘密情報(PAN/CVV)は secretPayload 側に置き、data 側は $e2ee マーカーにする
    ptt = build_ptt({
        "app_id": APP_ID,
        "auth_ticket_type": "PAN",
        "cardholder_name": "",
        "credit_card": E2EE_MARKER,
        "csc": E2EE_MARKER,
        "device_id": CARD_DEVICE_ID,
        "expiry_month": expiry_month,
        "expiry_year": expiry_year,
    }, PTT_OP_ADD_CARD, secret={"csc": cvv, "credit_card": card_number},
       server_public_key=server_public_key_from_cert(keys["trustChain"][0]))
    cred = create_card_credential(client, info["common"], ptt, card_number, expiry_month,
                                  expiry_year, zip_code, country_code, otc)
    return info, cred


# --------------------------------------------------------------------------- #
# 本体
# --------------------------------------------------------------------------- #

def activate(client, card_number, expiry, cvv, zip_code="1000001", country_code=None,
             amount=None, currency=None, log=None):
    """カード 1 枚で年齢確認を通す。戻り値は結果をまとめた dict。

    `client` は muse.ai にログイン済み（`hatch_sess` Cookie 保持）の MuseClient。
    """
    say = log if log else (lambda msg, *args: logger.info(msg, *args))

    card_number = "".join(ch for ch in str(card_number) if ch.isdigit())
    if not card_number:
        raise CcvError("カード番号が空です")
    expiry_month, expiry_year = normalize_expiry(expiry)
    cvv = str(cvv).strip()
    zip_code = str(zip_code).strip()

    result = {"steps": []}

    amount = amount or DEFAULT_AMOUNT
    currency = currency or DEFAULT_CURRENCY

    # --- 1〜3. init / サーバ公開鍵 / カード登録 --------------------------
    info, cred = start_checkout(client, card_number, expiry_month, expiry_year, cvv,
                                zip_code, country_code, amount, currency, use_otc=True)
    if cred is None:
        raise CcvError(str(info.get("error")))
    upl_session_id = info["upl_session_id"]
    result["upl_session_id"] = upl_session_id
    result["steps"].extend(["init", "encryption-key"])
    say("ccv: init OK order=%s %s%s", info["order_id"],
        info["money"].get("amount"), info["money"].get("currency"))
    say("ccv: encryption-key OK")

    mutation = cred.get("mutation") or {}
    if not mutation.get("success"):
        code = str((mutation.get("error") or {}).get("code") or "")
        if code in GENERIC_WRITE_CODES:
            # otc を送るとサーバが失敗理由を汎用コードに丸める。otc を一切送らない
            # 試行を挟むと 1383254（クレジットカード必須）/ 2078022（BIN 一時停止）が出る。
            say("ccv: credential は %s（汎用）→ otc なしで理由を取り直す", code)
            info2, cred2 = start_checkout(
                client, card_number, expiry_month, expiry_year, cvv, zip_code,
                country_code, amount, currency, use_otc=False)
            if cred2 is not None and (cred2.get("mutation") or {}).get("success"):
                info, cred, mutation = info2, cred2, cred2["mutation"]
                upl_session_id = info["upl_session_id"]
                result["upl_session_id"] = upl_session_id
                say("ccv: otc なしで credential OK")
            elif cred2 is not None:
                raise CcvError(
                    "checkout/credential 拒否: %s / otc なしだと %s"
                    % (describe_failure(cred), describe_failure(cred2)))
        if not mutation.get("success"):
            raise CcvError(f"checkout/credential 拒否: {describe_failure(cred)}")

    common = info["common"]
    order_id = info["order_id"]
    money = info["money"]
    container_mode = info["container_mode"]
    otc = info["otc"]
    country_code = info["country_code"]

    credential_id = mutation.get("credentialId")
    if not credential_id:
        raise CcvError(f"checkout/credential に credentialId なし: {str(cred)[:200]}")
    result["credential_id"] = credential_id
    result["steps"].append("credential")
    say("ccv: credential OK id=%s next=%s", credential_id, (cred.get("nextAction") or {}).get("type"))

    # --- 4. 3DS 判定（返ってこなければ素通し） ---------------------------
    next_action = (cred.get("nextAction") or {}).get("type")
    if next_action == "THREE_DS_CHALLENGE":
        deadline = time.time() + _POLL_TIMEOUT
        state = None
        while time.time() < deadline:
            status, info = _post_json(client, "/api/checkout/card-processing-info", {
                "credentialId": credential_id,
                "uplSessionId": upl_session_id,
            })
            state = info.get("cardActionState") if status == 200 else f"http-{status}"
            if state in ("SUCCESS", "FAILURE"):
                break
            time.sleep(_POLL_INTERVAL)
        result["card_action_state"] = state
        result["steps"].append(f"card-processing-info:{state}")
        say("ccv: card-processing-info → %s", state)
        if state == "FAILURE":
            raise CcvError("カード登録後の処理に失敗しました (cardActionState=FAILURE)")

    # --- 5. 確定 ---------------------------------------------------------
    container_ptt = build_ptt({
        "receiver_id": RECEIVER_ID,
        "context_id": order_id,
        "container_mode": container_mode,
        "components": [{
            "amount": float(money["amount"]),
            "cred_id": credential_id,
            "currency": money.get("currency") or DEFAULT_CURRENCY,
        }],
        "security_origin": BASE,
    }, PTT_OP_CREATE_CONTAINER, secret=None)
    status, final = _post_json(client, "/api/checkout/finalize", {
        **common,
        "selectedCredentialId": credential_id,
        "credentialType": "CREDIT_CARD",
        "countryCode": country_code,
        "payButtonClickId": str(uuid.uuid4()),
        "paymentRequestId": str(uuid.uuid4()),
        "platformTrustToken": container_ptt,
        **otc,
    })
    checkout_status = final.get("checkoutStatus")
    result["checkout_status"] = checkout_status
    result["steps"].append(f"finalize:{checkout_status}")
    say("ccv: finalize → %s", checkout_status)
    if status != 200 or checkout_status not in ("SUCCESS", "PENDING"):
        raise CcvError(f"checkout/finalize {status}/{checkout_status}: {describe_failure(final)}")

    result["ccv_status"] = client.ccv_status()
    say("ccv: ccv-status → %s", result["ccv_status"])
    return result
