#!/usr/bin/env python3
"""一键将本地 iCloud 服务 (../icloud) 下生成的 iCloud 隐藏邮箱导入 muse-reg 号池。

功能：
1. 读取 ../icloud/data/workbench.db 中的 unused 邮箱（支持通过环境变量 ICLOUD_DB_PATH 覆盖）
2. 为邮箱生成公开访问 token（写入 address_public_access）
3. 拼装为 muse-reg 支持的 2 段格式：email----http://127.0.0.1:4173/openapi/mail/<email>/<token>/latest
4. 导出到 icloud_accounts.txt 并一键写入 webui.db 号池
"""

import hashlib
import os
import secrets
import sqlite3
import sys
import urllib.parse
from pathlib import Path

MUSE_DIR = Path(__file__).resolve().parent
ICLOUD_DB = Path(os.environ.get("ICLOUD_DB_PATH") or (MUSE_DIR.parent / "icloud" / "data" / "workbench.db"))
TXT_OUTPUT = MUSE_DIR / "icloud_accounts.txt"


def generate_icloud_lines(limit: int = 0) -> list[str]:
    if not ICLOUD_DB.exists():
        raise FileNotFoundError(f"未找到本地 iCloud 数据库: {ICLOUD_DB}")

    con = sqlite3.connect(str(ICLOUD_DB), timeout=15)
    cur = con.cursor()

    # 仅领取尚未分配公开访问密钥 (p.token_hash IS NULL) 的全新 unused 邮箱，
    # 绝不覆盖 GPT-reg 已导出的 103 个邮箱的 token_hash，确保两套系统取件密钥互不干扰。
    query = """
    SELECT h.id, h.email, p.token_hash
    FROM hidden_addresses h
    LEFT JOIN address_public_access p ON p.address_id = h.id
    WHERE h.local_state = 'unused' AND p.token_hash IS NULL
    ORDER BY h.created_at ASC
    """
    if limit > 0:
        query += f" LIMIT {int(limit)}"

    cur.execute(query)
    rows = cur.fetchall()

    lines = []
    for addr_id, email, _thash in rows:
        token = secrets.token_urlsafe(32)
        new_hash = hashlib.sha256(token.encode("utf-8")).hexdigest()

        cur.execute(
            """
            INSERT INTO address_public_access (address_id, token_hash, created_at)
            VALUES (?, ?, datetime('now'))
            ON CONFLICT(address_id) DO UPDATE SET
                token_hash = excluded.token_hash,
                rotated_at = excluded.created_at
            """,
            (addr_id, new_hash),
        )

        encoded_email = urllib.parse.quote(email)
        relay_url = f"http://127.0.0.1:4173/openapi/mail/{encoded_email}/{token}/latest"
        lines.append(f"{email}----{relay_url}")

    con.commit()
    con.close()
    return lines


def sync_icloud_to_muse(limit: int = 0, save_txt: bool = True) -> dict:
    """供 WebUI 或脚本调用：同步本地 4173 unused 邮箱进入 muse-reg 号池。"""
    lines = generate_icloud_lines(limit=limit)
    if not lines:
        return {"found": 0, "parsed": 0, "inserted": 0, "updated": 0, "skipped": 0}
    if save_txt:
        with open(TXT_OUTPUT, "w", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    if str(MUSE_DIR) not in sys.path:
        sys.path.insert(0, str(MUSE_DIR))
    from webui import db

    res = db.import_accounts("\n".join(lines), kind="icloud_relay")
    return {"found": len(lines), **res}


def main():
    import argparse

    parser = argparse.ArgumentParser(description="导入本地 iCloud 邮箱到 muse-reg")
    parser.add_argument("--limit", type=int, default=0, help="限制导入数量（默认全部）")
    parser.add_argument("--save-only", action="store_true", help="只保存到 txt，不写入 webui.db")
    args = parser.parse_args()

    lines = generate_icloud_lines(args.limit)
    if not lines:
        print("[!] 没有可用的 unused 邮箱")
        return

    with open(TXT_OUTPUT, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(f"[+] 已导出 {len(lines)} 个账号到: {TXT_OUTPUT}")

    if not args.save_only:
        if str(MUSE_DIR) not in sys.path:
            sys.path.insert(0, str(MUSE_DIR))
        from webui import db

        res = db.import_accounts("\n".join(lines), kind="icloud_relay")
        print(f"[+] 成功同步到 muse-reg 号池: {res}")


if __name__ == "__main__":
    main()
