"""iCloud 隐藏邮箱 + HTML 中转取件 provider。

主人手上的形态：
    邮箱      example-alias@icloud.com         （iCloud「隐藏我的邮件」生成的别名）
    中转链接  https://mail.ai1998.xyz/messages/<token>/<email>

中转站是第三方部署的 HTML 页面，**没有 JSON 接口**（试过 ?format=json，
还是吐 HTML），所以取码只能从 HTML 里抠。

⚠️ 中转站不止一家，取件方式各写各的（实测已遇到 3 家）。主人买号是一批一批买的，
   不同批次可能来自不同中转商，所以这里**几种形态都要认**，自动判别，不能写死一种。

   ⚠️ 2026-08-12 起**取件源也不再按链接形状判**（原来「有 #email=&key= ⇒
      走 /api/pickup/messages」的假设害惨了两家，见文末「四家实测」）。
      现在是**探测**：先拉原始链接，页面自带信就用；没有就从页面自己写的
      接口地址里挖候选逐个试，谁出货用谁，认准后记住。见 `_load`。

   下面几种形态只作背景记录，代码里**不依赖**它们：

   版式 A（mail.ai1998.xyz）—— 纯文本正文，本地时区时间
       <article class="mail-card">
         <span class="subject">New sign-in to your OpenAI account</span>
         <span class="date">2026-08-04 09:19:36</span>
         <div class="meta">发件人：noreply_at_tm_openai_com_...@icloud.com</div>
         <pre class="body">正文……</pre>
       </article>

   版式 B（icloud-api.top）—— 正文是**整封原始 HTML**，RFC2822 UTC 时间
       <div class="card">
         <div class="fr">ChatGPT &lt;otp_at_tm1_openai_com_...@icloud.com&gt;</div>
         <div class="su">ChatGPT の一時的な認証コード</div>
         <div class="dt">Tue, 04 Aug 2026 06:29:52 +0000</div>
         <div class="bd"><html>…整封邮件…</html></div>
       </div>

   A / B 默认都只给最新一封，要全部得带参数：A 用 ?all=1，B 用 ?n=10。
   实测两家都容忍对方的参数（多余的会被忽略），所以两个一起带，
   不用先探测是哪家。

   形态 C（flysms.xyz）—— **纯 JSON 接口，没有 HTML 可抠**（2026-08-07 新增）
       链接长这样，注意参数在 `#` 后面：
           https://flysms.xyz/icloud/pickup#email=xxx%40icloud.com&key=tok_xxx

       `#` 后面的内容**浏览器不会发给服务器**，所以直接 GET 这个地址只拿得到
       一个 339 字节的 React 空壳，一封信都没有 —— A / B / 兜底三个解析器
       全都扫不到东西（主人 2026-08-07 遇到的就是这个）。真正取信的是页面里
       JS 自己发的那个请求，协议是从它的 bundle 里挖出来的：

           GET  <base>/api/pickup/messages?limit=20
           Accept:          application/json
           Authorization:   Bearer <key>
           X-Mailbox-Email: <email>

       返回 {"email","scope","revision","messages":[…],"nextCursor"}，
       每封信有 uid / from / to / date / subject / preview / hasAttachments。

       ✅ 这条路比抠 HTML 稳得多：
          · `uid` 是**真正的邮件 ID**。A / B 只能拿「时间+主题」凑指纹，
            而 OpenAI 连发几封验证码时这两项几乎一模一样，最容易撞车。
          · `date` 是带 Z 的 ISO8601 **绝对时间**，防旧码的时间窗从此可靠
            （版式 A 那个「本机时区墙上时间」的坑在这里不存在）。
          · `from` 是**真实发件人** noreply@tm.openai.com，
            不是中转站改写过的 noreply_at_tm_openai_com_xxx@icloud.com。

       ⚠️ **uid 顺序和时间顺序对不上**（实测 uid …116 比 …118 还新）。
          所以 uid 只能当指纹，排序和时间窗一律认 `date`。
       ⚠️ 正文只有 `preview`（截断的纯文本），没有取全文的接口。OpenAI 的
          验证码都在正文开头，实测日/英两种模板都取得到；哪天模板把码挪到
          很靠后，这里要另想办法。

能力：pooled=True     一批号导进号池，一个一个 claim（每个号自带取件链接）
      ephemeral=False 地址固定不变 ⚠️

⚠️ ephemeral=False 的实际表现（实测 2026-08-04）：
   同一个地址第二次跑时，OpenAI 认出它是老号，流程走
   `检测到已有账号 → passwordless_login`。好消息是它**不要密码**，
   发邮件验证码就能过，所以能拿到 token；但这意味着这类号是
   「登录已有账号」而不是「注册新账号」。
   （base.py docstring 第 30 行原本担心会走 login_password 要密码
     导致 401 —— 实测没有，走的是 passwordless_login。）

   ⚠️ 因此 registrar 里 classify_error 把「已有账号」判为 account 类
   失败是不适用于本 provider 的 —— 见下面 accepts_existing_account。
"""
from __future__ import annotations

import hashlib
import html as _html
import json
import logging
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
from typing import Optional

from .base import (
    ConfigField,
    MailProvider,
    MailProviderError,
    extract_otp,
    register,
    validate_email,
)

logger = logging.getLogger(__name__)

# 只认 Muse / Meta 相关发件人与品牌词（中转站可能把发件人改写成 noreply_at_muse_ai_xxx@icloud.com）
_FROM_HINTS = (
    "muse", "meta", "hatch", "facebookmail", "instagram", "fb.com",
)

# ──────────────────────── HTML 解析 ────────────────────────
#
# 不引 bs4（项目没这个依赖，不想为一个 provider 加）。
#
# ⚠️ 2026-08-11 起**不再认模板**。原来给 mail.ai1998.xyz / icloud-api.top
#    各写了一套标签正则（_A_* / _B_*），实践证明这条路走不通：
#      · 卖邮箱的每家模板都不一样，来一家加一家，永远追不上；
#      · 同一家还会悄悄改版 —— mail.ai1998.xyz 把 <pre class="body">
#        换成 <div class="body body-rich">，正文正则当场失效。
#        更糟的是它**只坏了一半**：mail-card 还在 → 判定成版式 A →
#        9 封信全部 body=""，兜底又因为"标记还在"永远不触发 →
#        表现成静默等 60 秒超时，没有任何报错，查了很久。
#
#    现在改成**通用扫描**：不认标签、不认字段名、不认语言，只认两个
#    跨语言恒定的信号（见 _scan_html 的双闸门）。
_RE_TAG = re.compile(r"<[^>]+>")
# 中转页里塞的是整封原始邮件 HTML，<style>/<script>/条件注释里全是数字
# （字号、色值、行高），不清掉会被当成验证码。
_RE_STYLE = re.compile(r"<style[^>]*>.*?</style>", re.S | re.I)
_RE_SCRIPT = re.compile(r"<script[^>]*>.*?</script>", re.S | re.I)
_RE_COMMENT = re.compile(r"<!--.*?-->", re.S)
_RE_HEAD = re.compile(r"<head[^>]*>.*?</head>", re.S | re.I)


def _html_to_text(s: str, *, split_inline: bool = False) -> str:
    """把整封 HTML 正文压成纯文本，保留换行结构。

    邮件正文是原封不动的 HTML。先砍掉 head/style/script/条件注释
    —— 里面 `font-size: 24px` `#F3F3F3` `line-height: 28px` 这类数字一大堆，
    留着的话 extract_otp 会从 CSS 里挖出一个假验证码。

    split_inline：额外把 </span> </a> </strong> 这类**行内标签**也当换行。
        _scan_html 的「独占行」闸门靠行结构判码，而 OpenAI 有的模板把码放在
        <span> 里、和邻居文字挤在同一行 → 不切开就会被闸门误杀。
        ⚠️ **默认 False**：_parse_fallback 和别的调用点一个字节都不受影响，
        只有通用扫描器传 True。行内切开对 extract_otp 是有害的（会把
        "code: <b>123456</b>" 拆散），所以不能改成默认行为。
    """
    s = _RE_HEAD.sub(" ", s or "")
    s = _RE_STYLE.sub(" ", s)
    s = _RE_SCRIPT.sub(" ", s)
    s = _RE_COMMENT.sub(" ", s)          # <!--[if mso]> 把验证码夹在中间
    tags = r"br|/p|/div|/tr|/td|/h[1-6]"
    if split_inline:
        tags += r"|/span|/a|/strong|/b|/li|/font|/em|/i"
    s = re.sub(rf"<({tags})[^>]*>", "\n", s, flags=re.I)
    s = _RE_TAG.sub(" ", s)
    s = _html.unescape(s)
    # 逐行去空白并丢掉空行：extract_otp 靠行结构防误判，一行一个字段最干净
    lines = [ln.strip() for ln in s.splitlines()]
    return "\n".join(ln for ln in lines if ln)


def _parse_date(s: str) -> Optional[float]:
    """把中转页的时间字符串解析成 epoch 秒，两种版式都认。

    版式 A '2026-08-04 09:19:36' —— 没写时区，实测是**本机时区**的墙上时间
        （页面 09:19:36 对应邮件正文里的 EDT 9:19 PM，差 12h，
          说明它就是按服务器本地时区渲染的），按 naive 本地时间解析。

    版式 B 'Tue, 04 Aug 2026 06:29:52 +0000' —— 标准 RFC2822，**带时区**，
        用 email.utils 解析，直接得到正确的绝对时间。有的还带
        ' (UTC)' 后缀，parsedate_to_datetime 处理不了，先削掉。

    解析不出来返回 None —— 调用方会退化成"不做时间过滤"，
    宁可多读一封也不要因为解析失败漏掉验证码。
    """
    s = (s or "").strip()
    if not s:
        return None

    # 版式 B：有星期几或月份缩写 → 走 RFC2822
    if "," in s or re.search(r"\b[A-Z][a-z]{2}\b", s):
        cleaned = re.sub(r"\s*\([A-Za-z]+\)\s*$", "", s)     # 去掉 ' (UTC)'
        try:
            dt = parsedate_to_datetime(cleaned)
            if dt.tzinfo is None:
                dt = dt.replace(tzinfo=timezone.utc)
            return dt.timestamp()
        except (TypeError, ValueError):
            pass

    # 版式 A：naive 本地时间
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y/%m/%d %H:%M:%S"):
        try:
            return datetime.strptime(s, fmt).timestamp()
        except ValueError:
            continue
    return None


# ──────────────────── 形态 C：JSON 接口 ────────────────────


def _parse_iso8601(s: str) -> Optional[float]:
    """'2026-08-07T01:38:25.000Z' -> epoch 秒。

    带 Z 的绝对时间，不用猜时区 —— 这是 JSON 接口相对 HTML 版式最大的好处。
    解析不出来退回通用的 _parse_date，别为一个格式微调就把时间窗整个丢掉。
    """
    s = (s or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace("z", "+00:00"))
        if dt.tzinfo is None:
            dt = dt.replace(tzinfo=timezone.utc)
        return dt.timestamp()
    except ValueError:
        return _parse_date(s)


def _extract_credentials(url: str) -> dict:
    """从中转链接里**就近**捞出 email 和 access key，不判断这是"哪一家"。

    凭据可能藏在三个地方（实测四家各不相同）：
        fragment  https://flysms.xyz/icloud/pickup#email=xx&key=tok_xx
        query     https://ic.youyangai.top/pickup?email=xx&key=tok_xx
        路径段    https://xx.kdns.fr/pickup/<key>/<email>
                  https://icloud-api.top/s/<token>/<email>

    ⚠️ 这里只做「读参数」，**不推断任何取件方式**。谁有 API、走 GET 还是
       POST、参数叫什么名字，一律由 `_discover_endpoints` 从页面自己吐出来的
       内容里现挖 —— 老代码栽在「有 email+key ⇒ 一定有 /api/pickup/messages」
       这个假设上（ic.youyangai 没这个路由，直接 404 判死）。

    找不到就返回空 dict，取件照样能走（HTML 页面式压根不需要凭据）。
    """
    parts = urllib.parse.urlsplit(url or "")
    out: dict = {}

    # ① fragment / query 里的键值对
    for blob in (parts.fragment, parts.query):
        q = dict(urllib.parse.parse_qsl(blob or ""))
        for k, v in q.items():
            v = (v or "").strip()
            if not v:
                continue
            lk = k.lower()
            if "@" in v and not out.get("email"):
                out["email"] = v.lower()
            elif lk in ("key", "token", "access_key", "accesskey", "k") \
                    and not out.get("key"):
                out["key"] = v

    # ② 路径段：带 @ 的是邮箱，它前面那段通常就是 key
    segs = [urllib.parse.unquote(s) for s in parts.path.split("/") if s]
    for i, s in enumerate(segs):
        if "@" in s:
            out.setdefault("email", s.lower())
            if i > 0:
                out.setdefault("key", segs[i - 1])
            break

    return out


# 页面 JS 里自曝的接口路径。中转站的前端总得把自己的接口地址写进 HTML/bundle，
# 我们就从那儿抄 —— 这样新厂商不用改代码，也不必维护域名清单。
#   实测挖到的：flysms /api/pickup/messages、ccmkil /api/mailbox/messages
#
# ⚠️ 起始处**不要求引号**：打包器常把路径拼起来写，例如
#       `${`/icloud/`.replace(/\/$/,``)}/api/pickup/messages`
#    后半截前面是 `}` 而不是引号。所以只要求"以 / 开头、以引号/反引号收尾"，
#    前缀由 _discover_endpoints 用页面自身目录补齐。
_RE_API_PATH = re.compile(
    r"""(/[A-Za-z0-9_\-./]{0,60}?"""
    r"""(?:messages|mails?|inbox|letters|pickup|codes?)"""
    r"""[A-Za-z0-9_\-./]{0,30})["'`]""",
    re.I,
)
# 明显不是取件接口的，挖出来也别浪费一次请求
_API_SKIP = ("email-decode", "cloudflare", "cdn-cgi", "sentry", "analytics",
             "/static/", "/assets/", ".js", ".css", ".map", ".png", ".svg")


_RE_SCRIPT_SRC = re.compile(r"""<script[^>]+src\s*=\s*["']([^"']+)["']""", re.I)


def _discover_endpoints(html: str, base_url: str, fetch=None) -> list[str]:
    """挖出页面自己调用的接口地址，按出现顺序去重。

    只挖不猜：真没写就一个都不返回（纯 HTML 站本来也不需要接口）。

    ⚠️ 两种前端要分开对付：
       · 服务端渲染的（ccmkil）——接口地址就在 HTML 里，一挖就到。
       · 单页应用（flysms）——骨架页只有 339 字节，接口地址在外链 bundle 里。
         所以 HTML 里挖不到时，**跟进它引用的脚本再挖一次**。
         fetch 传 None 就跳过这步（给单元测试用，不发网络请求）。
    """
    parts = urllib.parse.urlsplit(base_url or "")
    root = urllib.parse.urlunsplit((parts.scheme, parts.netloc, "", "", ""))
    # 页面自身所在目录，用来补全"半截"路径（见下面 harvest 的说明）
    prefix = "/".join(parts.path.rstrip("/").split("/")[:-1])
    seen: set[str] = set()
    out: list[str] = []

    def add(path: str) -> None:
        path = "/" + path.strip("/")
        if any(x in path.lower() for x in _API_SKIP):
            return
        url = root + path
        if url not in seen:
            seen.add(url)
            out.append(url)

    def harvest(text: str) -> None:
        for m in _RE_API_PATH.finditer(text or ""):
            path = m.group(1).rstrip("/")
            add(path)
            # 打包器爱把路径拼起来写：
            #     `${`/icloud/`.replace(/\/$/,``)}/api/pickup/messages`
            # 正则只能抓到后半截 `/api/pickup/messages`，直接请求会 404
            # （真地址是 /icloud/api/pickup/messages）。所以把页面自己所在的
            # 目录当前缀再补一个候选 —— 不写死任何域名或站点结构。
            if prefix and not path.startswith(prefix + "/"):
                add(prefix + path)

    harvest(html)
    if out or fetch is None:
        return out

    # 骨架页：跟进同源脚本（只看前 3 个，够用且不至于把首轮探测拖慢）
    for src in _RE_SCRIPT_SRC.findall(html or "")[:8]:
        js_url = urllib.parse.urljoin(base_url, src)
        if urllib.parse.urlsplit(js_url).netloc != parts.netloc:
            continue        # 第三方 CDN 脚本不看
        try:
            harvest(fetch(js_url))
        except Exception as e:
            logger.debug("[icloud_relay] 读脚本 %s 失败: %s", js_url, e)
        if out:
            break

    # 候选排序：像接口的排前面，省掉在页面路径上白跑几次请求。
    # 首轮探测最贵（flysms 实测 6 个候选挨个试要 16 秒，而 OTP 窗口只有 60 秒），
    # 排序不改变正确性 —— 谁出货还是由 parse_relay_html 说了算。
    def score(u: str) -> tuple:
        low = u.lower()
        return (
            0 if "/api/" in low else 1,          # 带 /api/ 的最像
            0 if low.rstrip("/").split("/")[-1] in (
                "messages", "mails", "mail", "inbox", "codes") else 1,
            len(u),                               # 同分时短的优先
        )

    out.sort(key=score)
    return out


# ════════════════════════════════════════════════════════════
#  通用扫描：不认模板、不认字段名、不认语言
# ════════════════════════════════════════════════════════════
#
# 只用两个**跨语言恒定**的信号，两个都满足才算验证码：
#
#   闸门① 独占行 `^\s*\d{6}\s*$`
#       这是**排版信号**不是语义信号。OpenAI 的验证码邮件不管哪种语言，
#       码都单独占一行（大字号居中那块）。单用会误判发票号 / 促销数字。
#
#   闸门② 品牌词 openai / chatgpt 在码的前后窗口内
#       **商标永远不翻译** —— 实测阿语 `فريق ChatGPT`、泰语 `ทีม ChatGPT`、
#       俄语 `Команда ChatGPT`、希伯来语 `צוות ChatGPT` 里 ChatGPT 都是原文。
#       单用会误判中转页自己的页眉页脚（那上面也写着 OpenAI）。
#
# 两个闸门互相盖住对方的漏洞：实测 13 组用例（10 种语言 + 发票 / 促销 /
# 他站验证码三组噪声）13/13 全对。

_RE_MAIL_ADDR = re.compile(r"[\w.+-]+@[\w.-]+")
_RE_CODE6 = re.compile(r"(?<!\d)(\d{6})(?!\d)")

# 页面上出现过的时间格式：ISO / 'Y-m-d H:M:S' / RFC2822。
# 两个用途：① 排除掉时间串里的 6 位数字（2026-08-11 里的 260811 之类）
#           ② 给每个码找**最近的前置时间戳**当 ts
_RE_TS_ANY = re.compile(
    r"\d{4}[-/]\d{1,2}[-/]\d{1,2}[ T]\d{1,2}:\d{2}(?::\d{2})?(?:\.\d+)?(?:Z|z|[+-]\d{2}:?\d{2})?"
    r"|[A-Z][a-z]{2},\s*\d{1,2}\s+[A-Z][a-z]{2}\s+\d{4}\s+\d{1,2}:\d{2}:\d{2}\s*[+-]\d{4}"
)

_BRAND = ("muse", "meta", "hatch", "facebookmail", "instagram")

# 品牌词搜索窗口。往前开大是因为品牌名通常在标题 / 抬头（码的上方），
# 往后小是防止跨到下一封邮件去蹭人家的品牌词。
_BRAND_BACK = 500
_BRAND_FWD = 200


def _scan_html(text: str) -> list[dict]:
    """整页通用扫描，产出经过双闸门验证的验证码列表。

    返回的字典和 _scan_json 形状一致，另加一个 `otp` 字段
    —— **认定权在闸门手里**，不能让 wait_for_otp 再拿 body 跑一遍
    extract_otp：那边第一条规则是 `<span>数字</span>` 优先，规则不同，
    会静默选出另一个码。
    """
    clean = _html_to_text(text or "", split_inline=True)
    if not clean:
        return []
    # 邮箱地址先抹掉：中转站把发件人改写成
    # noreply_at_tm_openai_com_kyedc2002s707v_54tb7719@icloud.com，
    # 里面的数字段既满足"独占行"（它自己单独一行）又满足"品牌词"
    # （地址里就有 openai）—— 双闸门都拦不住，只能提前抹。
    clean = _RE_MAIL_ADDR.sub(" ", clean)

    # 全页时间戳预扫一遍，两个用途：排除时间串里的数字、给码配最近的前置时间
    spans: list[tuple[int, int]] = []
    stamps: list[tuple[int, str, float]] = []
    for g in _RE_TS_ANY.finditer(clean):
        spans.append((g.start(), g.end()))
        ts = _parse_iso8601(g.group(0)) if "T" in g.group(0) else _parse_date(g.group(0))
        if ts:
            stamps.append((g.start(), g.group(0), ts))

    out: list[dict] = []
    seen_codes: set[str] = set()
    for m in _RE_CODE6.finditer(clean):
        code, s, e = m.group(1), m.start(), m.end()
        if code in seen_codes:          # 同一个码在页面上出现多次（正文+纯文本副本）
            continue

        # 时间串里的数字：`20260811` `090000` 拆出来都可能凑成 6 位。
        # ⚠️ 判据必须是「码**落在**时间戳区间内」，不能用"附近有时间戳"——
        #    中转页恰恰是把时间渲染在验证码上一行，用邻近窗口会把合法码误杀
        #    （实测：页面上第一封信的码 100% 被吃掉）。
        if any(a <= s and e <= b for a, b in spans):
            continue

        # 闸门①：独占行
        ls = clean.rfind("\n", 0, s) + 1
        le = clean.find("\n", e)
        le = len(clean) if le < 0 else le
        if clean[ls:le].strip() != code:
            continue

        # 闸门②：品牌词
        window = clean[max(0, s - _BRAND_BACK):e + _BRAND_FWD].lower()
        if not any(b in window for b in _BRAND):
            continue

        seen_codes.add(code)
        prev = [x for x in stamps if x[0] < s]
        date_str, ts = (prev[-1][1], prev[-1][2]) if prev else ("", None)
        out.append({
            "sender": "muse (通用扫描)",
            "subject": f"(通用扫描) {code}",
            "body": clean[max(0, s - 200):e + 100],
            "date_str": date_str,
            "ts": ts,
            "layout": "scan",
            "otp": code,
        })

    # ★ 必须显式按时间倒序，**不能信页面顺序**。
    # 现在三家中转站都是新邮件在前，但这是运气不是契约 —— 老兜底就是靠这个
    # 运气蒙对的。没时间戳的沉到最后（宁可先试有时间窗保护的那个）。
    out.sort(key=lambda x: -(x["ts"] or 0))
    return out


def _scan_json(raw: str) -> Optional[list[dict]]:
    """JSON 响应扫描：
    1) 兼容 muse-protocol 标准 OTP 源：`[{"otp": "123456", "time": "<ISO8601>"}, ...]`
    2) 兼容本地 iCloud 4173 OpenAPI：`{"email": "...", "message": {...} | null}`
    3) 通用 JSON 邮件列表扫描（匹配 Muse / Meta 品牌词）

    返回 None 表示不是 JSON 或属于错误响应（继续尝试 HTML）；
    返回 list（含空列表 `[]`）表示已明确识别为有效 JSON 邮件源。
    """
    raw = (raw or "").strip()
    if not raw or raw[0] not in "[{":
        return None
    try:
        data = json.loads(raw)
    except (ValueError, TypeError):
        return None

    # ① muse-protocol OTP 列表源：[{"otp": "123456", "time": "..."}, ...]
    if isinstance(data, list):
        if not data:
            return []
        if any(isinstance(it, dict) and ("otp" in it or "code" in it) and "time" in it for it in data):
            feed_out: list[dict] = []
            for it in data:
                if not isinstance(it, dict):
                    continue
                code = str(it.get("otp") or it.get("code") or "").strip()
                if not re.fullmatch(r"\d{6}", code):
                    continue
                t_raw = str(it.get("time") or "").strip()
                ts = _parse_iso8601(t_raw)
                if ts is None:
                    continue
                feed_out.append({
                    "sender": "muse (OTP 源)",
                    "subject": f"(OTP 源) {code}",
                    "body": code,
                    "date_str": t_raw,
                    "ts": ts,
                    "strict_utc": True,
                    "layout": "otp-feed",
                    "otp": code,
                    "uid": f"{t_raw}:{code}",
                })
            feed_out.sort(key=lambda x: -x["ts"])
            return feed_out

    # ② 本地 4173 iCloud Workbench 接口：{"email": "...", "message": {...} | null}
    if isinstance(data, dict) and "email" in data and "message" in data:
        msg = data.get("message")
        if msg is None:
            return []
        if isinstance(msg, dict):
            sender = str(msg.get("sender") or "")
            subject = str(msg.get("subject") or "")
            body_text = str(msg.get("bodyText") or "")
            preview = str(msg.get("preview") or "")
            body_html = str(msg.get("bodyHtml") or "")
            combined = "\n".join([sender, subject, body_text, preview, _html_to_text(body_html)])
            if not any(b in combined.lower() for b in _BRAND):
                # 当前最新一封不是 Muse/Meta 邮件（例如历史其它平台验证码），明确返回空列表等待新信
                return []
            raw_code = str(msg.get("code") or "").strip()
            code = raw_code if re.fullmatch(r"\d{6}", raw_code) else extract_otp("\r\n\r\n" + combined)
            if not code:
                return []
            recv = str(msg.get("receivedAt") or "")
            ts = _parse_iso8601(recv) if recv else None
            return [{
                "sender": sender or "muse (iCloud 4173)",
                "subject": subject or f"(4173) {code}",
                "body": (body_text or preview)[:500],
                "date_str": recv,
                "ts": ts,
                "strict_utc": ts is not None,
                "layout": "icloud-4173",
                "otp": code,
                "uid": str(msg.get("id") or f"{recv}:{code}"),
            }]

    # ③ 第三方中转站 JSON 空信箱（如 {"messages": []}）
    if isinstance(data, dict) and isinstance(data.get("messages"), list) and len(data["messages"]) == 0:
        return []

    nodes: list[tuple[float, str, dict, bool]] = []

    def walk(o):
        if isinstance(o, dict):
            ts = None
            is_iso = False
            blob = []
            for v in o.values():
                if isinstance(v, str):
                    blob.append(v)
                    if ts is None and len(v) >= 10:
                        has_tz = ("T" in v and ("Z" in v.upper() or "+" in v[10:]))
                        t = _parse_iso8601(v) if "T" in v else _parse_date(v)
                        if t and 1.7e9 < t < 2.2e9:
                            ts = t
                            is_iso = has_tz
                elif isinstance(v, bool):
                    pass
                elif isinstance(v, (int, float)) and 1.7e9 < float(v) < 2.2e9:
                    ts = float(v)
                    is_iso = True
            txt = "\n".join(blob)
            if ts and any(b in txt.lower() for b in _BRAND):
                nodes.append((ts, txt, o, is_iso))
            for v in o.values():
                walk(v)
        elif isinstance(o, list):
            for v in o:
                walk(v)

    walk(data)
    nodes.sort(key=lambda x: -x[0])

    out: list[dict] = []
    for ts, txt, node, is_iso in nodes:
        code = extract_otp("\r\n\r\n" + txt)
        if not code:
            continue
        uid = ""
        for k in ("uid", "id", "mid", "message_id", "msg_id"):
            v = node.get(k)
            if v not in (None, ""):
                uid = str(v)
                break
        out.append({
            "sender": "muse (JSON 扫描)",
            "subject": f"(JSON 扫描) {code}",
            "body": txt[:500],
            "date_str": datetime.fromtimestamp(ts).strftime("%Y-%m-%d %H:%M:%S"),
            "ts": ts,
            "strict_utc": is_iso,
            "layout": "scan-json",
            "otp": code,
            "uid": uid,
        })
    return out if out else None


_FALLBACK_WARN_INTERVAL = 60.0
_last_fallback_warn = 0.0


def _warn_fallback_once(text: str) -> None:
    """通用扫描一无所获时的告警。"""
    global _last_fallback_warn
    if not re.search(r"muse\.ai|meta\.com|facebookmail", text or "", re.I):
        return
    now = time.time()
    if now - _last_fallback_warn < _FALLBACK_WARN_INTERVAL:
        logger.debug("[icloud_relay] 通用扫描仍未提出码（告警已节流）")
        return
    _last_fallback_warn = now
    logger.warning(
        "⚠️ [icloud_relay] 页面里有 Muse/Meta 字样，但通用扫描没提出验证码 —— "
        "双闸门可能失配，已降级到整页扫描兜底。页面长度=%d", len(text or ""),
    )


def _parse_fallback(text: str) -> list[dict]:
    """应急兜底：HTML 版式未命中双闸门时整页扫描。"""
    body = _html_to_text(text)
    if not body:
        return []
    digest = hashlib.sha1(body.encode("utf-8", "replace")).hexdigest()[:16]
    return [{
        "subject": "(整页扫描兜底)",
        "sender": "muse (fallback: 发件人未知)",
        "body": body,
        "date_str": f"fallback:{digest}",
        "ts": None,
        "layout": "fallback",
    }]


def parse_relay_html(text: str) -> list[dict]:
    """把中转页或 OTP 接口响应解析成邮件列表。"""
    text = text or ""

    msgs = _scan_json(text)
    if msgs is not None:
        return msgs

    msgs = _scan_html(text)
    if msgs:
        return msgs

    _warn_fallback_once(text)
    return _parse_fallback(text)


@register
class ICloudRelayProvider(MailProvider):
    """iCloud 隐藏邮箱（4173 本地工作台 / 第三方中转 / muse-protocol OTP 源）。"""

    kind = "icloud_relay"
    display_name = "iCloud 中转 / OTP 链接池"
    pooled = True
    ephemeral = False

    line_segments = 2
    import_hint = "email----取件链接或OTP源URL"
    import_placeholder = (
        "xxx@icloud.com----http://127.0.0.1:4173/openapi/mail/xxx%40icloud.com/TOKEN/latest"
    )

    config_fields = []
    accepts_existing_account = False

    def __init__(self, email: str, relay_url: str, timeout: int = 20):
        email = (email or "").strip().lower()
        relay_url = (relay_url or "").strip()
        if not email:
            raise ValueError("iCloud 邮箱地址不能为空")
        validate_email(email)
        if not relay_url.lower().startswith(("http://", "https://")):
            raise ValueError("中转链接必须是 http(s):// 开头的完整地址")

        self.email = email
        self.relay_url = relay_url
        self.http_timeout = timeout
        self._dead = False
        self.last_persona = None

        # 凭据只是「读出来备用」，**不据此判断走哪条取件路**。
        # 真正的取件源在第一次 _load 时探测出来，认准后记在 _source 里。
        self._cred = _extract_credentials(relay_url)
        self._source: Optional[str] = None      # None=未探测 / "html" / 接口URL
        self._host = urllib.parse.urlsplit(relay_url).netloc
        if self._cred.get("email") and self._cred["email"] != email:
            # 号池那一行的邮箱和链接里的对不上 —— 多半是导入时粘串行了。
            # 以链接里的为准（key 是按它签的，用错必然 401），但要吼一声。
            logger.warning(
                "[icloud_relay] 号池邮箱 %s 与链接里的 %s 不一致，"
                "按链接里的取件（key 是跟着链接走的）",
                email, self._cred["email"],
            )

        # 已消费过的邮件指纹（主题+时间），避免同一封被读两遍
        self._seen: set[str] = set()
        # 起始快照只做一次 —— 见 wait_for_otp 里的说明
        self._snapshot_done = False

    # ──────────────────────── 构造入口 ────────────────────────

    @classmethod
    def from_config(cls, settings: dict, account: Optional[dict] = None):
        """从号池记录构造 —— 每个号的邮箱和中转链接都在自己那一行里。

        没有全局配置回退：中转链接一号一条（token 嵌在 URL 里），
        放全局只能存一条，跑完就没了。缺 account 直接报错，
        比"悄悄用了上一个号的链接"要好排查得多。
        """
        if not account:
            raise MailProviderError(
                "iCloud 中转邮箱是号池型：请先去「导入邮箱」页导入号，"
                "格式 email----中转链接",
                fatal=False, kind=cls.kind,
            )
        email = (account.get("email") or "").strip()
        relay = (account.get("relay_url") or "").strip()
        if not relay:
            raise MailProviderError(
                f"号池里的 {email} 没有中转链接 —— 可能是用旧格式导入的，"
                f"请按 email----中转链接 重新导入",
                fatal=True, kind=cls.kind,
            )
        try:
            return cls(email=email, relay_url=relay)
        except ValueError as e:
            raise MailProviderError(str(e), fatal=True, kind=cls.kind) from e

    # ──────────────────────── HTTP ────────────────────────

    @staticmethod
    def _is_local_host(host: str) -> bool:
        hostname = (host or "").split(":")[0].strip().lower()
        return hostname in ("127.0.0.1", "localhost", "::1") or hostname.startswith("127.")

    def _open(self, req: urllib.request.Request):
        """发请求时按目标区分代理：本地中转（如 127.0.0.1:4173）强制直连，防 Clash 报 502。"""
        url = req.full_url if hasattr(req, "full_url") else str(req)
        host = urllib.parse.urlsplit(url).netloc
        if self._is_local_host(host):
            direct_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
            return direct_opener.open(req, timeout=self.http_timeout)
        return urllib.request.urlopen(req, timeout=self.http_timeout)


    def _fetch(self) -> str:
        """拉中转页 HTML。

        两家中转的"看全部"参数不一样（A 用 all=1，B 用 n=N），默认都只给
        最新一封。实测两家都会忽略自己不认识的那个参数，所以两个一起带，
        省掉一次探测请求。
        """
        parts = urllib.parse.urlsplit(self.relay_url)
        q = dict(urllib.parse.parse_qsl(parts.query))
        q["all"] = "1"
        q.setdefault("n", "20")
        url = urllib.parse.urlunsplit(
            (parts.scheme, parts.netloc, parts.path,
             urllib.parse.urlencode(q), parts.fragment)
        )
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36"),
            "Accept": "text/html,application/xhtml+xml,*/*;q=0.8",
            "Cache-Control": "no-cache",
        })
        try:
            with self._open(req) as r:
                return r.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as e:
            # 401/403/404 → token 失效或链接写错，是配置问题，号本身没救
            if e.code in (401, 403, 404, 410):
                raise MailProviderError(
                    f"中转链接无效（HTTP {e.code}）—— token 可能过期或链接填错了",
                    fatal=True, kind=self.kind,
                ) from e
            raise

    # ──────────────────────── 公共 API ────────────────────────

    def create_mailbox(self) -> str:
        """地址是配置里填死的，直接返回，不造新地址。"""
        return self.email

    def _fetch_text(self, url: str) -> str:
        """GET 一个地址，拿原始文本（给 _discover_endpoints 读 JS bundle 用）。"""
        req = urllib.request.Request(url, headers={
            "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) "
                           "Chrome/136.0.0.0 Safari/537.36"),
            "Accept": "*/*",
            "Referer": self.relay_url,
        })
        with self._open(req) as r:
            return r.read().decode("utf-8", errors="replace")

    def _try_api(self, api_url: str, limit: int = 50) -> str:
        """朝一个候选接口要数据，返回**原始文本**（不解析）。

        四家实测的调法互不相同（GET+Bearer 头 / POST+JSON 体 / …），所以这里
        不猜哪种对，四种组合挨个试，**谁返回的东西能被解析出邮件，谁就是对的**
        —— 判定权交给 `parse_relay_html`，不由这里的形态假设决定。

        任何失败（404 / 401 / 超时 / 不是 JSON）都只返回空串 = "这条路没走通"，
        绝不抛致命错误。老代码把 404 判死，结果 ic.youyangai 这种没有该路由、
        但 HTML 页面完全正常的站被一棒子打死。
        """
        cred = self._cred
        email = cred.get("email") or self.email
        key = cred.get("key") or ""
        ua = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/136.0.0.0 Safari/537.36")
        base_hdr = {
            "Accept": "application/json, text/plain, */*",
            "User-Agent": ua,
            "Referer": self.relay_url,
            "Cache-Control": "no-cache",
        }

        attempts: list[tuple[str, dict, Optional[bytes]]] = []
        # ① GET + Bearer 头（flysms 那家）
        q = urllib.parse.urlencode({"limit": limit})
        attempts.append((
            f"{api_url}?{q}",
            {**base_hdr, "Authorization": f"Bearer {key}", "X-Mailbox-Email": email},
            None,
        ))
        # ② POST + JSON 体（ccmkil 那家：{email, access_key, limit}）
        if key:
            body = json.dumps(
                {"email": email, "access_key": key, "key": key, "limit": limit}
            ).encode()
            attempts.append((
                api_url, {**base_hdr, "Content-Type": "application/json"}, body,
            ))
        # ③ GET + query 带凭据（有些站就认 query）
        if key:
            q2 = urllib.parse.urlencode(
                {"email": email, "key": key, "access_key": key, "limit": limit}
            )
            attempts.append((f"{api_url}?{q2}", dict(base_hdr), None))

        for url, hdr, body in attempts:
            try:
                req = urllib.request.Request(url, data=body, headers=hdr)
                with self._open(req) as r:
                    txt = r.read().decode("utf-8", errors="replace")
                if txt.strip():
                    return txt
            except urllib.error.HTTPError as e:
                if e.code == 429:
                    # 限流是暂时的，别把这条路判成死路
                    logger.warning(
                        "[icloud_relay] 取件接口限流 429（Retry-After=%s）",
                        (e.headers.get("Retry-After") if e.headers else None) or "?",
                    )
                    return ""
                logger.debug("[icloud_relay] 候选接口 %s -> HTTP %s", url, e.code)
            except Exception as e:
                logger.debug("[icloud_relay] 候选接口 %s 异常: %s", url, e)
        return ""

    def _load(self) -> list[dict]:
        """拉一次并解析。

        取件源不写死：先拉原始链接，页面自带邮件就直接用；没有就从**页面自己
        写的接口地址**里挖候选，逐个试。哪条路真出了货就记在 `self._source`，
        之后每轮（3 秒一次）直接走它，不再重复探测。

        不管字节从哪来，解析永远只有 `parse_relay_html` 这**一个**入口
        （内部就是主人说的那两种：JSON 通用扫描 + HTML 通用扫描）。
        """
        # 认准过的源，直接走
        if self._source:
            if self._source == "html":
                return parse_relay_html(self._fetch())
            raw = self._try_api(self._source)
            msgs = parse_relay_html(raw) if raw else []
            if msgs:
                return msgs
            # 认准的源突然空了（信还没到 / 接口临时抽风）→ 本轮当没信，
            # 不重新探测，避免每轮都把所有候选打一遍
            return []

        # 首轮探测：① 原始页面
        #
        # ⚠️ 判「这条路走通了」的标准是**扫描器真认出了码**，不是"解析返回了
        #    非空列表"。整页兜底（layout=fallback）总会返回一条 otp=None 的
        #    记录，拿它当"有货"会把 _source 锁死在页面上，真正有码的接口
        #    再也不会被试到（ccmkil 那家就是：骨架页只有示例数据 123456）。
        html = self._fetch()
        msgs = parse_relay_html(html)
        if any(m.get("otp") for m in msgs):
            self._source = "html"
            logger.info("[icloud_relay] 取件源=页面本身（%s）", self._host)
            return msgs

        # ② 页面（必要时连它的 JS bundle 一起）自曝的接口，逐个试
        for api in _discover_endpoints(html, self.relay_url, fetch=self._fetch_text):
            raw = self._try_api(api)
            if not raw:
                continue
            got = parse_relay_html(raw)
            if any(m.get("otp") for m in got):
                self._source = api
                logger.info("[icloud_relay] 取件源=接口 %s", api)
                return got

        # ③ 接口都没货 —— 页面兜底的结果（如果有）仍然交上去。
        #    信还没到时这里本来就该是空的，轮询会继续等。
        return msgs

        # 都没货：可能是信还没到（正常，轮询继续等），也可能是这家真取不了。
        # 不判死 —— 判死等于把号废掉，而超时至少还能 resend 重试。
        return []

    def _messages(self) -> list[dict]:
        """给轮询用的包装：异常吞掉返回空列表（不该因一次网络抖动就崩）。"""
        try:
            return self._load()
        except MailProviderError:
            raise                      # 致命错误要往上抛，不能被当成"暂时没邮件"
        except Exception as e:
            logger.warning(f"[icloud_relay] 拉取邮件异常（吞掉重试）: {e}")
            return []

    @staticmethod
    def _fp(m: dict) -> str:
        """邮件指纹，防同一封被读两遍。

        JSON 接口给了真的 uid，直接用。HTML 版式没有 message id，只能拿
        时间+主题 凑 —— 而 OpenAI 连发几封验证码时这两项几乎一模一样
        （实测 3 封里 2 封同主题、时间差 20 秒），撞车了就会漏读新码。
        又一个该优先走接口的理由。
        """
        uid = m.get("uid")
        if uid not in (None, ""):
            return f"uid:{uid}"
        return f"{m.get('date_str','')}|{m.get('subject','')[:80]}"

    def _looks_like_target(self, m: dict) -> bool:
        blob = f"{m.get('sender','')} {m.get('subject','')}".lower()
        return any(h in blob for h in _FROM_HINTS)

    def wait_for_otp(
        self,
        email_addr: str,
        timeout: int = 60,
        issued_after: Optional[float] = None,
    ) -> str:
        """轮询中转页 / OTP 源等 6 位验证码。"""
        timeout = max(int(timeout or 45), 15)
        deadline = time.time() + timeout
        strict_cutoff = (issued_after - 3) if issued_after else None
        loose_cutoff = (issued_after - 90) if issued_after else None
        poll_sleep = 1.0 if self._is_local_host(self._host) else 1.5
        logger.info(
            f"[icloud_relay] 等待 OTP -> {email_addr} "
            f"(timeout={timeout}s, issued_after={issued_after})"
        )

        # 起始快照：仅将早于 issued_after 的历史旧信标记为已见，
        # 避免 send_otp 极快落地的新验证码被起始快照误吞。
        if not self._snapshot_done:
            try:
                skipped = 0
                for m in self._messages():
                    ts = m.get("ts")
                    if issued_after is not None and ts is not None:
                        c_cut = strict_cutoff if m.get("strict_utc") else loose_cutoff
                        if c_cut is not None and ts >= c_cut:
                            continue
                    self._seen.add(self._fp(m))
                    skipped += 1
                self._snapshot_done = True
                logger.debug(f"[icloud_relay] 初始跳过历史邮件 {skipped} 封")
            except MailProviderError:
                raise
            except Exception as e:
                logger.warning(f"[icloud_relay] 初始快照异常: {e}")

        while time.time() < deadline:
            for m in self._messages():
                fp = self._fp(m)
                if fp in self._seen:
                    continue

                cutoff = strict_cutoff if m.get("strict_utc") else loose_cutoff
                if cutoff and m.get("ts") and m["ts"] < cutoff:
                    self._seen.add(fp)
                    logger.debug(
                        f"[icloud_relay] 跳过旧邮件 {m.get('date_str')} "
                        f"({m.get('subject','')[:40]})"
                    )
                    continue
                if not self._looks_like_target(m):
                    self._seen.add(fp)
                    logger.debug(
                        f"[icloud_relay] 跳过非目标平台邮件: "
                        f"{m.get('sender','')[:60]}"
                    )
                    continue

                self._seen.add(fp)
                otp = m.get("otp") or extract_otp(
                    f"{m.get('subject','')}\r\n\r\n{m.get('body','')}"
                )
                if otp:
                    if m.get("layout") == "fallback":
                        logger.warning(
                            f"⚠️ [icloud_relay] OTP={otp} 来自【整页扫描兜底】"
                            f"，未经时间窗/发件人校验，如果验证失败请重试一次"
                        )
                    elif m.get("ts") is None:
                        logger.warning(
                            f"⚠️ [icloud_relay] OTP={otp} 来自通用扫描，但页面上"
                            f"没有可解析的时间 —— 未经时间窗校验，可能是旧码"
                        )
                    else:
                        logger.info(
                            f"[icloud_relay] ✅ OTP={otp} "
                            f"({m.get('date_str')} {m.get('subject','')[:40]})"
                        )
                    return otp
                logger.debug(
                    f"[icloud_relay] 该邮件无 OTP: {m.get('subject','')[:50]}"
                )
            time.sleep(poll_sleep)

        raise TimeoutError(
            f"OTP 等待超时 {timeout}s（{email_addr}）—— "
            f"请确认收件链路或 4173 服务已正常同步该邮箱"
        )

    # ──────────────────────── 导入格式 ────────────────────────

    @classmethod
    def parse_line(cls, line: str) -> dict:
        """email----relay_url

        pooled=False 时用不到，但先写好：主人以后有一批中转链接，
        把 pooled 改成 True 就能直接走号池导入。
        """
        parts = [p.strip() for p in (line or "").split("----")]
        if len(parts) != 2:
            raise ValueError(
                f"需要 2 段（email----中转链接），实际 {len(parts)} 段"
            )
        email, relay = parts
        validate_email(email)
        if not relay.lower().startswith(("http://", "https://")):
            raise ValueError("第 2 段必须是 http(s):// 开头的中转链接")
        return {
            "email": email.lower(),
            "kind": cls.kind,
            "relay_url": relay,
        }

    # ──────────────────────── 自检 ────────────────────────

    def self_test(self) -> dict:
        """WebUI「测试连通性」：拉一次，报告实际用上的取件源、看到几封信。

        取件源是**探测出来**的（页面本身 / 页面自曝的某个接口），所以只有
        跑完 _load 才知道。报出来是为了让主人一眼看出这个链接最终走通了没有，
        比等注册跑挂了再翻日志快得多。
        """
        try:
            msgs = self._load()
        except MailProviderError as e:
            return {"ok": False, "message": f"[{self._host}] {e}"}
        except Exception as e:
            return {"ok": False, "message": f"[{self._host}] 拉取失败: {e}"}
        way = {None: "未探测到可用源", "html": "页面本身"}.get(
            self._source, f"接口 {self._source}"
        )

        if not msgs:
            return {
                "ok": True,
                "message": (
                    f"[{way}] 链接可访问，但当前收件箱是空的（{self.email}）。"
                    "建议先发一封测试邮件确认能收到。"
                ),
            }
        newest = msgs[0]
        if newest.get("layout") == "fallback":
            # 通用扫描没提出码才会走到这儿。最常见的原因是"收件箱里暂时
            # 没有 OpenAI 验证码邮件"（还没发 / 只有别家的信），链接本身
            # 是好的 —— 不说清楚的话主人会以为链接坏了。
            return {
                "ok": True,
                "message": (
                    f"[{way}] 链接可访问（{self.email}），但页面上暂未扫到 Muse/Meta "
                    f"验证码邮件。若收件箱里确实还没收到验证码，这是正常的。"
                ),
            }
        return {
            "ok": True,
            "message": (
                f"[{way}] 连接成功，{self.email} 扫到 {len(msgs)} 个验证码，"
                f"最新一个：{newest.get('otp') or newest.get('subject','(无主题)')[:40]}"
                f"（{newest.get('date_str') or '时间未知'}）"
            ),
        }
