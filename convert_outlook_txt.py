"""把号商导出的 Outlook 文本转换成本工具的 4 段导入格式。

号商常见导出格式是冒号分隔，且 *顺序与工具相反*：

    email:password:refresh_token:client_id
    HoppesSowells37@outlook.com:tjpfxnu4626:M.C552_SN1.0...:9e5f94bc-e8a4-4e73-b8be-63364c29d753

工具 `outlook` provider 需要：

    email----password----client_id----refresh_token

本脚本按 client_id 是 GUID 这一点自动判断第 3/4 段谁是谁，因此
`email:password:client_id:refresh_token` 已正确顺序的行也能原样通过。
行分隔符统一为 LF，输出可直接整段贴进 WebUI「メール取込」。

用法：
    python convert_outlook_txt.py 输入.txt            # 输出 输入.4seg.txt
    python convert_outlook_txt.py 输入.txt -o 输出.txt
    python convert_outlook_txt.py 输入.txt -o -        # 打到 stdout
"""

from __future__ import annotations

import argparse
import re
import sys

SEP = "----"
# client_id 永远是 GUID；refresh_token 是 M.C… 之类的不透明串。
_GUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_EMAIL = re.compile(r"^[^@\s:]+@[^@\s:]+\.[^@\s:]+$")

DEFAULT_CLIENT_ID = "9e5f94bc-e8a4-4e73-b8be-63364c29d753"


def convert_line(line: str, line_no: int, client_id_default: str = "") -> str:
    """把一行转成 4 段格式，非法行抛 ValueError。"""
    line = line.strip()
    if SEP in line:
        parts = [p.strip() for p in line.split(SEP)]
        if len(parts) != 4:
            raise ValueError(f"第 {line_no} 行：{SEP} 分隔应为 4 段，实际 {len(parts)} 段")
        email, password, a, b = parts
    else:
        parts = [p.strip() for p in line.split(":")]
        if len(parts) != 4:
            raise ValueError(f"第 {line_no} 行：冒号分隔应为 4 段，实际 {len(parts)} 段")
        email, password, a, b = parts

    if not _EMAIL.match(email):
        raise ValueError(f"第 {line_no} 行：邮箱不合法 {email!r}")

    # 第 3/4 段里哪个是 GUID，哪个就是 client_id；两个都不是则用默认值补齐。
    if _GUID.match(a):
        client_id, refresh_token = a, b
    elif _GUID.match(b):
        client_id, refresh_token = b, a
    elif client_id_default:
        client_id, refresh_token = client_id_default, (a if len(a) >= len(b) else b)
    else:
        raise ValueError(f"第 {line_no} 行：找不到 client_id（GUID），也无法判断第 3/4 段")

    if len(refresh_token) < 20:
        raise ValueError(f"第 {line_no} 行：refresh_token 太短（{len(refresh_token)} 字符）")

    return SEP.join([email.lower(), password, client_id, refresh_token])


def convert_text(text: str, client_id_default: str = "") -> tuple[list[str], list[str]]:
    """返回 (转换后的行, 错误说明)。空行与 `#` 注释行被跳过。"""
    out: list[str] = []
    errors: list[str] = []
    for i, raw in enumerate(text.replace("\r\n", "\n").replace("\r", "\n").split("\n"), 1):
        if not raw.strip() or raw.lstrip().startswith("#"):
            continue
        try:
            out.append(convert_line(raw, i, client_id_default))
        except ValueError as e:
            errors.append(str(e))
    return out, errors


def main() -> int:
    ap = argparse.ArgumentParser(description="号商 Outlook 文本 → 工具 4 段导入格式")
    ap.add_argument("src", help="输入文件（utf-8，可带 BOM）")
    ap.add_argument("-o", "--out", default=None, help="输出文件；'-' 表示 stdout；默认 <输入>.4seg.txt")
    ap.add_argument(
        "--client-id",
        default="",
        help=f"第 3/4 段都没有 GUID 时补的 client_id（默认 {DEFAULT_CLIENT_ID}）",
    )
    args = ap.parse_args()

    with open(args.src, "rb") as f:
        text = f.read().decode("utf-8-sig")

    lines, errors = convert_text(text, args.client_id or DEFAULT_CLIENT_ID)
    body = "\n".join(lines) + ("\n" if lines else "")

    dst = args.out or (args.src + ".4seg.txt")
    if dst == "-":
        sys.stdout.write(body)
    else:
        with open(dst, "w", encoding="utf-8", newline="\n") as f:
            f.write(body)

    print(f"转换 {len(lines)} 行，错误 {len(errors)} 行", file=sys.stderr)
    for e in errors[:20]:
        print(f"  {e}", file=sys.stderr)
    if dst != "-":
        print(f"输出：{dst}", file=sys.stderr)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main())
