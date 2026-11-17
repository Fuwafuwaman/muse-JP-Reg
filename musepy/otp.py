"""OTP 验证码轮询。

select_otp 为纯函数；wait_otp 轮询 OTP 源 URL，只取 send 开始时间之后的
新 6 位验证码（幂等：同一封邮件重试不会把旧码再当新码用）。
"""

import json
import re
import time
from datetime import datetime, timezone

UTC = timezone.utc


def parse_iso8601(value):
    """解析为带时区的 UTC 时间；naive 输入视为未知，返回 None。"""
    try:
        if isinstance(value, datetime):
            parsed = value
        elif isinstance(value, str):
            parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
        else:
            return None
        if parsed.tzinfo is None or parsed.utcoffset() is None:
            return None
        return parsed.astimezone(UTC)
    except (TypeError, ValueError, OverflowError):
        return None


def select_otp(records, after):
    """取严格晚于 after 的最新 6 位数字验证码；最新一秒出现多个不同码视为歧义。

    records: [{"otp": "123456", "time": "<ISO8601>"}, ...]
    after:   本次 send_otp 开始时间（ISO8601 字符串或 datetime）。
    """
    boundary = parse_iso8601(after)
    if boundary is None or not isinstance(records, list):
        return None
    candidates = []
    for item in records:
        if not isinstance(item, dict):
            continue
        code = item.get("otp")
        if not isinstance(code, str) or not re.fullmatch(r"[0-9]{6}", code):
            continue
        timestamp = parse_iso8601(item.get("time"))
        if timestamp is not None and timestamp > boundary:
            candidates.append((timestamp, code))
    if not candidates:
        return None
    newest = max(timestamp for timestamp, _ in candidates)
    codes = {code for timestamp, code in candidates if timestamp == newest}
    return next(iter(codes)) if len(codes) == 1 else None


def wait_otp(url, after, *, timeout=45, poll_interval=0.6, request_timeout=15,
             session_factory=None):
    """轮询 OTP 源 URL 直到出现新验证码。"""
    if session_factory is not None:
        session = session_factory()
    else:
        from curl_cffi import requests as cr
        session = cr.Session()
        session.trust_env = False
    end = time.monotonic() + timeout
    try:
        while time.monotonic() < end:
            response = session.get(url, timeout=request_timeout, headers={"accept": "*/*"})
            if response.status_code != 200:
                return None
            try:
                records = json.loads(response.text)
            except (TypeError, ValueError):
                return None
            otp = select_otp(records, after)
            if otp:
                return otp
            time.sleep(poll_interval)
        return None
    finally:
        try:
            session.close()
        except Exception:
            pass
