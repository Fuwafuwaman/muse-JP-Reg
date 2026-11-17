"""Muse 注册结果批量导出格式注册表。"""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Callable, Optional


@dataclass(frozen=True)
class ExportFormat:
    id: str
    label: str
    filename: str
    mode: str = "text"                            # "text" | "download"
    mime: str = "text/plain; charset=utf-8"
    render: Optional[Callable[[dict], str]] = None
    render_all: Optional[Callable[[list], bytes]] = None
    note: str = ""


def _s(row: dict, key: str) -> str:
    v = row.get(key)
    if v is None:
        return ""
    return str(v).strip()


def _render_json_all(rows: list) -> bytes:
    payload = []
    for r in rows or []:
        extra = r.get("extra") or {}
        payload.append({
            "email": _s(r, "email"),
            "status": _s(r, "status"),
            "stage": _s(r, "stage"),
            "account_id": _s(r, "account_id"),
            "own_code": _s(r, "own_code"),
            "used_invite_code": _s(r, "used_invite_code"),
            "hatch_sess": _s(r, "session_token"),
            "frl_access_token": _s(r, "access_token"),
            "vm_id": _s(r, "device_id"),
            "ip": _s(r, "ip"),
            "relay_url": _s(r, "relay_url"),
            "first_name": extra.get("first_name", ""),
            "last_name": extra.get("last_name", ""),
            "date_of_birth": extra.get("date_of_birth", ""),
        })
    return json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8")


FORMATS: list[ExportFormat] = [
    ExportFormat(
        id="email_sess",
        label="邮箱----hatch_sess",
        filename="muse_email_sess.txt",
        render=lambda r: f'{_s(r, "email")}----{_s(r, "session_token")}',
        note="Muse 核心登录 Cookie 凭证",
    ),
    ExportFormat(
        id="hatch_sess",
        label="仅 hatch_sess",
        filename="hatch_sess.txt",
        render=lambda r: _s(r, "session_token"),
    ),
    ExportFormat(
        id="email_own_code",
        label="邮箱----自有邀请码",
        filename="muse_email_invites.txt",
        render=lambda r: f'{_s(r, "email")}----{_s(r, "own_code")}',
    ),
    ExportFormat(
        id="own_codes",
        label="仅自有邀请码（可直接导入邀请码池）",
        filename="muse_invite_codes.txt",
        render=lambda r: _s(r, "own_code"),
        note="每个邀请码最多可供 30 个新账号兑换",
    ),
    ExportFormat(
        id="full_line",
        label="邮箱----AccountID----hatch_sess----自有邀请码",
        filename="muse_full.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "account_id")}----'
            f'{_s(r, "session_token")}----{_s(r, "own_code")}'
        ),
    ),
    ExportFormat(
        id="full_relay",
        label="邮箱----hatch_sess----自有邀请码----取件URL",
        filename="muse_full_relay.txt",
        render=lambda r: (
            f'{_s(r, "email")}----{_s(r, "session_token")}----'
            f'{_s(r, "own_code")}----{_s(r, "relay_url")}'
        ),
        note="含收件链接，请妥善保管",
    ),
    ExportFormat(
        id="json_all",
        label="完整 JSON 文件 (.json 下载)",
        filename="muse_accounts.json",
        mode="download",
        mime="application/json; charset=utf-8",
        render_all=_render_json_all,
        note="包含完整字段结构",
    ),
]

_BY_ID = {f.id: f for f in FORMATS}


def list_formats() -> list[dict]:
    return [
        {
            "id": f.id,
            "label": f.label,
            "filename": f.filename,
            "mode": f.mode,
            "mime": f.mime,
            "note": f.note,
        }
        for f in FORMATS
    ]


def get_format(fmt_id: str) -> Optional[ExportFormat]:
    return _BY_ID.get((fmt_id or "").strip())


def render_text(rows: list, fmt: "ExportFormat | str") -> str:
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None or not f.render:
        raise KeyError(f"未知文本导出格式: {fmt}")
    lines = []
    for r in rows or []:
        try:
            line = f.render(r)
            if f.id == "own_codes" and not line.strip():
                continue
            lines.append(line)
        except Exception:
            lines.append("")
    return "\n".join(lines)


def render_bytes(rows: list, fmt: "ExportFormat | str") -> bytes:
    f = get_format(fmt) if isinstance(fmt, str) else fmt
    if f is None or not f.render_all:
        raise KeyError(f"未知文件导出格式: {fmt}")
    return f.render_all(rows or [])
