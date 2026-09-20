#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Dota2 同局查询 Web 服务（本地）

把 teammate_intersection.py 的领域逻辑包装成一个本地 HTTP 服务：
浏览器输入两位玩家的 ID，查询两人历史上是否出现在同一局，
结果以卡片展示（比赛编号 / 时间 / 同队或敌对 / 双方英雄 / 双方阵容 / 胜负），
并支持导出 CSV / JSON。玩家一律以其昵称指代，不用「A/B」。

启动（Windows，managed Python）:
    "C:/Users/15190/.workbuddy-ai/binaries/python/envs/default/Scripts/python.exe" \
        "D:/Code/D2D/web_server.py"

    默认监听 http://127.0.0.1:8765/ ，并自动打开浏览器。
    可用 --port 8766 --no-open 覆盖。

依赖: 仅标准库（http.server / urllib / threading / json / csv / io）。
     领域层复用 teammate_intersection.py，无需 API key。

说明:
- 仅绑定 127.0.0.1，局域网不可访问，无鉴权。
- OpenDota 无 key 时限流 60 次/分钟、3000 次/天，服务内置滑窗限流器（55 次/60秒）。
- 一次查询固定 4 次请求（双方资料 + 双方对局列表），耗时通常 5-10 秒。
- 只能查询 Valve 公开的比赛数据；玩家若关闭「公开比赛数据」则查不到。
"""

import collections
import csv
import io
import json
import os
import random
import signal
import string
import sys
import threading
import time
import traceback
import urllib.error
import urllib.parse
# ★ urllib.request 必须显式导入：单文件合并版本里若缺少它，
#   `import urllib` 仍会成功（parse/error 已导入），错误只在运行到网络请求时才暴露。
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------- 引导 import 领域层（用绝对路径，不依赖 cwd）----------

HERE = os.path.dirname(os.path.abspath(__file__))
if HERE not in sys.path:
    sys.path.insert(0, HERE)

import teammate_intersection as ti  # noqa: E402  （import 不会触发 main()）

VERSION = "1.4.0"
DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765

# ★ 服务启动标识：进程每次启动都会变。
#   前端据此判断「服务是否重启过」——重启则清空 localStorage 里记住的玩家 ID，
#   避免上一轮遗留的 ID 干扰新一次查询。仅刷新页面（服务未重启）时不会清空。
BOOT_ID = "%d%06d" % (int(time.time() * 1000), random.randint(0, 999999))

# 历史同局查询：每次向 OpenDota 拉取的最近对局条数
# 2000 场 ≈ 覆盖近 4 年（不同玩家活跃度差异大），单请求约 1.7s。
# 单请求即可覆盖绝大部分查询需求，无需分页。
DEFAULT_MATCH_LIMIT = 2000
MIN_MATCH_LIMIT = 20
MAX_MATCH_LIMIT = 2000

# SteamID64 与 account_id(32位) 的固定偏移，与领域层保持一致
STEAM64_BASE = ti.STEAM64_BASE

HERO_ICON = ("https://cdn.cloudflare.steamstatic.com/apps/dota2/"
             "images/dota_react/heroes/{slug}.png")

# 内嵌 1x1 透明 ICO（16 字节），省去额外文件与 favicon 404
FAVICON = bytes.fromhex(
    "00000100010001000000010020003000000016000000"
    "2800000001000000020000000100200000000000000000000000000000000000"
    "0000000000000000"
)

# 英雄映射（进程级缓存，启动时加载一次）
HERO_NAMES = {}   # hero_id -> localized_name
HERO_SLUGS = {}   # hero_id -> CDN slug

# 任务存储
JOBS = {}
JOBS_LOCK = threading.Lock()
RESULT_STORE = {}

# 进度阶段 → 百分比上限
STAGE_PERCENT = {
    "init": 2, "heroes": 5, "validate": 8,
    "fetch_a": 45, "fetch_b": 75, "intersect": 78,
    "live": 80, "scan": 10, "done": 100,
}

# 各 kind 的阶段文案百分比可不同（mate 流程无分页，range 更宽）
MATE_STAGES = {"fetch_a": 35, "fetch_b": 70, "intersect": 88}


# ---------- 限流 ----------

class RateLimiter:
    """滑窗限流：window 秒内最多 max_calls 次。锁外 sleep，避免占锁。"""

    def __init__(self, max_calls=55, window=60.0):
        self.max_calls = max_calls
        self.window = window
        self._calls = collections.deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > self.window:
                    self._calls.popleft()
                if len(self._calls) < self.max_calls:
                    self._calls.append(now)
                    return
                wait = self.window - (now - self._calls[0]) + 0.02
            time.sleep(min(wait, 2.0))


LIMITER = RateLimiter()


# ---------- OpenDota 配额观测 ----------
#
# ★ 实测：OpenDota **没有**专用的配额查询接口（/api/status、/api/usage、
#   /api/rate_limit 一律 404）。但它在**每一个**响应的响应头里都带配额字段，
#   连 404 / 500 响应也带：
#       X-Rate-Limit-Remaining-Minute: 58
#       X-Rate-Limit-Remaining-Day:    2509
#   因此这里的做法是：在唯一的出口 rate_limited_get 上旁路读取响应头，
#   把最新值存进模块级状态，供 /api/quota 端点与前端页脚展示。
#
# ⚠ 不可绕过的问题：ti.api_get 返回的是解析后的 JSON，**不暴露响应头**。
#   而领域层文件受「不改动 teammate_intersection.py」的约定约束。
#   故此处不改领域层，改为在本层复刻一次同语义的请求（带 header 读取），
#   仅在需要时使用（每次查询前的首请求 + /api/quota 主动探测）。

QUOTA_LOCK = threading.Lock()
QUOTA = {
    "remaining_minute": None,   # 本分钟剩余
    "remaining_day": None,      # 本日剩余
    "limit_minute": None,       # 本分钟上限（OpenDota 时常不发，留 None）
    "limit_day": None,
    "updated_at": None,         # 最近一次成功读到响应头的 unix 时间
    "source": None,             # 最后一次观测来自哪个 path，便于排错
}

# 实测无 key 时的免费档：60 次/分钟、3000 次/天。
# OpenDota 不主动返回上限字段，故用这两个常量在 UI 上算百分比；
# 若响应头里带了上限字段则以响应头为准（见 record_quota）。
FALLBACK_LIMIT_MINUTE = 60
FALLBACK_LIMIT_DAY = 3000


def _int_or_none(v):
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def record_quota(headers, source=""):
    """
    从 urllib 响应头中提取配额并写入模块级 QUOTA。

    只在拿得到数值时覆盖，避免某次不带头的响应把已有观测值抹成 None。
    """
    minute = _int_or_none(headers.get("X-Rate-Limit-Remaining-Minute"))
    day = _int_or_none(headers.get("X-Rate-Limit-Remaining-Day"))
    if minute is None and day is None:
        return
    with QUOTA_LOCK:
        if minute is not None:
            QUOTA["remaining_minute"] = minute
        if day is not None:
            QUOTA["remaining_day"] = day
        QUOTA["limit_minute"] = (_int_or_none(headers.get("X-Rate-Limit-Limit-Minute"))
                                 or QUOTA["limit_minute"] or FALLBACK_LIMIT_MINUTE)
        QUOTA["limit_day"] = (_int_or_none(headers.get("X-Rate-Limit-Limit-Day"))
                              or QUOTA["limit_day"] or FALLBACK_LIMIT_DAY)
        QUOTA["updated_at"] = time.time()
        QUOTA["source"] = source


def quota_snapshot():
    """返回配额快照（含用于展示的派生字段）。"""
    with QUOTA_LOCK:
        q = dict(QUOTA)
    day, day_lim = q.get("remaining_day"), q.get("limit_day")
    q["used_day"] = (day_lim - day) if (day is not None and day_lim) else None
    q["day_percent"] = (round(day / day_lim * 100, 1) if (day is not None and day_lim) else None)
    q["stale"] = (q["updated_at"] is None
                  or (time.time() - q["updated_at"]) > 300)
    return q


def probe_quota():
    """
    主动探测一次配额（发一个极轻量的请求，只为了读响应头）。

    用 /health：体积小、无副作用。★ 注意它同样**消耗 1 次配额**，
    这是换取准确读数的必要代价——OpenDota 没有任何免费读取配额的方式。
    """
    req = urllib.request.Request(
        ti.API + "/health", headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            record_quota(r.headers, source="/health")
        return True
    except urllib.error.HTTPError as e:
        # 即便 404/429，配额头依然在，照样能读
        record_quota(e.headers, source="/health(HTTPError)")
        return True
    except Exception:  # noqa: BLE001
        return False


def rate_limited_get(path, params=None, tries=3):
    """
    经限流的 OpenDota GET，并**旁路读取响应头中的配额**。

    ★ 实测 OpenDota 会偶发返回 500/502/503，且可能连续两三次。
      ti.api_get 内部对 5xx 只做固定 2s 间隔重试，连续抖动时仍会失败。
      此处在其外层再加一层**指数退避**（2s / 5s），显著降低瞬时抖动导致的失败率。
      注意：只对 5xx 重试；404 等语义错误立即抛出，不做无谓等待。

    ★ 配额观测不走 ti.api_get（它吞掉响应头），而是本层直接发一次请求。
      成功路径与异常路径都读头（HTTPError 也带头），保证读数不丢。
    """
    last = None
    for i in range(tries):
        LIMITER.acquire()
        try:
            return _get_with_quota(path, params)
        except RuntimeError as e:
            # 仅对服务端 5xx 抖动重试；404 等语义错误由 api_get 内部消化或直接抛出
            if "HTTP Error 5" not in str(e):
                raise
            last = e
            if i < tries - 1:
                time.sleep(2 if i == 0 else 5)
    raise last


def _get_with_quota(path, params=None):
    """
    与 ti.api_get 同语义的单次 GET，但会读取响应头写入配额。

    语义必须与领域层保持一致，否则会引入行为差异：
      404 → 返回 None（领域层把 404 视作「无数据」而非错误）
      5xx → 抛 RuntimeError，且文案含 "HTTP Error 5"（供上面的退避判定）
      其余 → 抛 RuntimeError
    """
    url = ti.API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            record_quota(r.headers, source=path)
            body = r.read().decode("utf-8")
        return json.loads(body) if body else None
    except urllib.error.HTTPError as e:
        # 异常响应同样带配额头，先记账再决定是否抛出
        record_quota(e.headers, source=path)
        if e.code == 404:
            return None
        raise RuntimeError(f"OpenDota {path} 返回 HTTP Error {e.code}") from e


# ---------- STRATZ（补全匿名位 / 扫描隐藏账号）----------
#
# ★ 为什么需要 STRATZ：
#   Valve 对「关闭公开比赛数据」的玩家有**两套割裂的发布策略**——
#     比赛维度：仍然记录其身份（在 /matches/{id} 里能看到他）
#     玩家维度：**不再发布该玩家的对局列表**（/players/{id}/matches 返回空）
#   而「两人历史同局」这个查询数学上要求**玩家维度索引存在**，
#   所以 OpenDota 在这类玩家上必然查不到（空集求交必为空）。
#
#   实测 STRATZ 的**比赛维度**数据完整：对同一场比赛，
#   OpenDota 返回 3 个 account_id=null 的匿名位，STRATZ 三个都有身份。
#   因此做法是「反向扫描」：以**公开的那一方**为基准拉其全部对局，
#   逐场用 STRATZ 拿该场 10 人身份，看目标账号是否在内。
#
#   实测成本（单账号约 5100 场）：52 次调用 / 60 秒，
#   因为单次请求可返回 100 场 × 每场 10 人（take 上限 100）。
#
# ⚠ 令牌：必须来自环境变量 STRATZ_TOKEN，不写入代码、不落盘。
#   未设置时 STRATZ 相关功能整体降级关闭，其余功能不受影响。

STRATZ_URL = "https://api.stratz.com/graphql"
STRATZ_TOKEN = os.environ.get("STRATZ_TOKEN") or ""
# 实测 take 上限为 100，超过会返回 errors: "You have surpassed the maximum take value of : 100"
STRATZ_TAKE_MAX = 100
# 深度扫描默认只扫最近 2000 场，与 OpenDota 常规查询的 limit 对齐（约覆盖 4 年）。
# 想扫全部历史可在请求里传 max_matches=0。
STRATZ_DEFAULT_MAX = 2000

# STRATZ 速率限制（实测免费档，且通过响应头持续更新）
STRATZ_LOCK = threading.Lock()
STRATZ_QUOTA = {
    "remaining_second": None, "limit_second": 8,
    "remaining_minute": None, "limit_minute": 150,
    "remaining_hour": None, "limit_hour": 1500,
    "remaining_day": None, "limit_day": 15000,
    "updated_at": None,
}


def stratz_enabled():
    return bool(STRATZ_TOKEN)


def _record_stratz_quota(headers):
    """记录 STRATZ 配额（同样通过响应头暴露）。"""
    def num(name):
        try:
            return int(str(headers.get(name)).strip())
        except (TypeError, ValueError):
            return None
    with STRATZ_LOCK:
        for key, hdr in (("remaining_second", "x-ratelimit-remaining-second"),
                         ("limit_second", "x-ratelimit-limit-second"),
                         ("remaining_minute", "x-ratelimit-remaining-minute"),
                         ("limit_minute", "x-ratelimit-limit-minute"),
                         ("remaining_hour", "x-ratelimit-remaining-hour"),
                         ("limit_hour", "x-ratelimit-limit-hour"),
                         ("remaining_day", "x-ratelimit-remaining-day"),
                         ("limit_day", "x-ratelimit-limit-day")):
            v = num(hdr)
            if v is not None:
                STRATZ_QUOTA[key] = v
        STRATZ_QUOTA["updated_at"] = time.time()


def stratz_snapshot():
    with STRATZ_LOCK:
        q = dict(STRATZ_QUOTA)
    q["enabled"] = stratz_enabled()
    q["stale"] = (q["updated_at"] is None
                  or (time.time() - q["updated_at"]) > 300)
    return q


class StratzThrottle:
    """
    STRATZ 每秒限速 8。用滑窗严格守住，避免 429。

    ⚠ 实测」每秒配额恢复很快（ratelimit-reset: 1），但连续突发会被拒，
      故这里留出余量，按每秒 6 次发。
    """

    def __init__(self, per_second=6):
        self.per_second = per_second
        self._calls = collections.deque()
        self._lock = threading.Lock()

    def acquire(self):
        while True:
            with self._lock:
                now = time.monotonic()
                while self._calls and now - self._calls[0] > 1.0:
                    self._calls.popleft()
                if len(self._calls) < self.per_second:
                    self._calls.append(now)
                    return
                wait = 1.0 - (now - self._calls[0]) + 0.01
            time.sleep(max(wait, 0.01))


STRATZ_THROTTLE = StratzThrottle()


class StratzError(RuntimeError):
    """STRATZ 调用失败（令牌无效 / 配额耗尽 / 网络问题）。"""


def stratz_query(query, variables=None, tries=3):
    """
    执行一次 STRATZ GraphQL 查询，返回 data 字典。

    STRATZ 的特点：HTTP 200 也可能带 errors（如 take 超限、令牌失效），
    所以必须检查 body 里的 errors 字段，不能只看状态码。
    """
    if not stratz_enabled():
        raise StratzError("未配置 STRATZ_TOKEN，无法使用补全功能。")

    payload = json.dumps({"query": query, "variables": variables or {}}).encode("utf-8")
    last_err = None
    for i in range(tries):
        STRATZ_THROTTLE.acquire()
        req = urllib.request.Request(STRATZ_URL, data=payload, headers={
            "Content-Type": "application/json",
            "User-Agent": "STRATZ_API",
            "Authorization": "Bearer " + STRATZ_TOKEN,
        })
        try:
            with urllib.request.urlopen(req, timeout=60) as r:
                _record_stratz_quota(r.headers)
                body = json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            _record_stratz_quota(e.headers)
            detail = ""
            try:
                detail = e.read().decode("utf-8", "replace")[:200]
            except Exception:  # noqa: BLE001
                pass
            if e.code in (429, 500, 502, 503, 504) and i < tries - 1:
                last_err = StratzError(f"STRATZ HTTP {e.code}")
                time.sleep(1.5 * (i + 1))
                continue
            if e.code == 401:
                raise StratzError("STRATZ 令牌无效或已过期，请更新 STRATZ_TOKEN。") from e
            raise StratzError(f"STRATZ HTTP {e.code} {detail}") from e
        except Exception as e:  # noqa: BLE001
            if i < tries - 1:
                last_err = StratzError(f"STRATZ 请求异常: {e}")
                time.sleep(1.5 * (i + 1))
                continue
            raise StratzError(f"STRATZ 请求异常: {e}") from e

        # HTTP 200 但业务错误
        errs = body.get("errors")
        if errs:
            msg = "; ".join(str(x.get("message") or x) for x in errs)
            raise StratzError(f"STRATZ 返回错误: {msg[:300]}")
        return body.get("data") or {}

    raise last_err or StratzError("STRATZ 调用失败")


STRATZ_PLAYER_MATCHES_Q = """
query P($id: Long!, $take: Int!, $skip: Int!) {
  player(steamAccountId: $id) {
    steamAccountId
    steamAccount { name }
    matchCount
    matches(request: { take: $take, skip: $skip }) {
      id
      startDateTime
      players {
        steamAccountId
        heroId
        isRadiant
        playerSlot
        steamAccount { name }
      }
    }
  }
}
"""


def stratz_player_meta(aid):
    """拿玩家的 matchCount 与昵称（用于估算扫描规模）。"""
    d = stratz_query(
        "query P($id: Long!){ player(steamAccountId: $id){"
        " steamAccountId matchCount steamAccount { name } } }",
        {"id": int(aid)})
    p = d.get("player") or {}
    acc = p.get("steamAccount") or {}
    return {
        "account_id": p.get("steamAccountId"),
        "match_count": p.get("matchCount") or 0,
        "personaname": acc.get("name"),
    }


def stratz_scan_matches(base_aid, target_aid, on_progress=None,
                        max_matches=None, start_skip=0):
    """
    以 base_aid 为基准扫描其全部对局，找出 target_aid 出现的场次。

    返回 (hits, meta)：
      hits —— [{match_id, start_time, same_team, target_slot, base_slot,
                target_hero, base_hero}]，按时间倒序
      meta —— {scanned, calls, truncated, target_found}

    ★ 关键：一次请求带 100 场 × 每场 10 人，所以 5000 场只需约 50 次调用。
      这是本方案成本可控的根本原因。
    """
    base_aid, target_aid = int(base_aid), int(target_aid)
    hits, skip, calls = [], start_skip, 0
    truncated = False

    while True:
        if max_matches is not None and skip >= max_matches:
            truncated = True
            break
        data = stratz_query(STRATZ_PLAYER_MATCHES_Q,
                            {"id": base_aid, "take": STRATZ_TAKE_MAX, "skip": skip})
        calls += 1
        player = data.get("player") or {}
        batch = player.get("matches") or []
        if not batch:
            break

        for m in batch:
            players = m.get("players") or []
            accs = {p.get("steamAccountId") for p in players}
            if target_aid not in accs:
                continue
            tgt = next(p for p in players if p.get("steamAccountId") == target_aid)
            bas = next(p for p in players if p.get("steamAccountId") == base_aid)
            t_rad, b_rad = bool(tgt.get("isRadiant")), bool(bas.get("isRadiant"))
            hits.append({
                "match_id": m.get("id"),
                "start_time": m.get("startDateTime"),
                "same_team": t_rad == b_rad,
                "target_slot": tgt.get("playerSlot"),
                "base_slot": bas.get("playerSlot"),
                "target_hero_id": tgt.get("heroId"),
                "base_hero_id": bas.get("heroId"),
                "target_team": "天辉" if t_rad else "夜魇",
                "base_team": "天辉" if b_rad else "夜魇",
                # ★ 10 人身份本来就在这次响应里，顺手带上不花额外请求。
                #   丢了就只能逐场重查（STRATZ 侧 1 次/场，OpenDota 侧更贵），
                #   所以这里必须保留，否则结果页无法显示阵容卡片。
                #   注意 playerSlot 语义与 OpenDota 的 player_slot 一致：
                #   0-4 天辉、128-132 夜魇（已实测对账）。
                "players": [{
                    "account_id": p.get("steamAccountId"),
                    "hero_id": p.get("heroId"),
                    "player_slot": p.get("playerSlot"),
                    "is_radiant": bool(p.get("isRadiant")),
                    # 昵称：实测带 name 只让单次响应 +60%（78.5→126.2 KB）、
                    # 耗时 +0.12 秒、配额消耗不变，扫 2000 场总计多约 2.4 秒，
                    # 换来 10 人卡片可显示昵称，值得。
                    "personaname": (p.get("steamAccount") or {}).get("name"),
                } for p in players],
            })

        skip += len(batch)
        if on_progress:
            on_progress(skip, len(hits), calls)

    # 时间倒序
    hits.sort(key=lambda r: r.get("start_time") or 0, reverse=True)
    return hits, {
        "scanned": skip,
        "calls": calls,
        "truncated": truncated,
        "target_found": len(hits),
    }


def stratz_match_players(match_id):
    """
    查单场比赛的全部 10 人身份（含 OpenDota 侧为匿名位的玩家）。

    返回 [{steamAccountId, heroId, isRadiant, playerSlot, name}]。
    """
    d = stratz_query(
        "query M($id: Long!){ match(id: $id){ id players {"
        " steamAccountId heroId isRadiant playerSlot"
        " steamAccount { name } } } }",
        {"id": int(match_id)})
    m = d.get("match") or {}
    out = []
    for p in (m.get("players") or []):
        acc = p.get("steamAccount") or {}
        out.append({
            "account_id": p.get("steamAccountId"),
            "hero_id": p.get("heroId"),
            "is_radiant": bool(p.get("isRadiant")),
            "player_slot": p.get("playerSlot"),
            "personaname": acc.get("name"),
        })
    return out


# ---------- 英雄数据 ----------

def load_heroes_once(verbose=True):
    """启动时加载一次英雄映射。失败则留空表，服务仍可启动（降级显示 hero_id）。"""
    global HERO_NAMES, HERO_SLUGS
    try:
        data = rate_limited_get("/heroes")
        if not isinstance(data, list):
            raise RuntimeError(f"意外的响应类型: {type(data).__name__}")
        names, slugs = {}, {}
        for h in data:
            hid = h.get("id")
            if hid is None:
                continue
            names[hid] = h.get("localized_name") or h.get("name") or str(hid)
            slugs[hid] = (h.get("name") or "").replace("npc_dota_hero_", "")
        HERO_NAMES, HERO_SLUGS = names, slugs
        ti.HERO_NAMES = HERO_NAMES  # 同步给领域层
        if verbose:
            print(f"[info] 已加载英雄名称 {len(names)} 条")
            q = quota_snapshot()
            if q.get("remaining_day") is not None:
                print(f"[info] OpenDota 配额剩余：今日 {q['remaining_day']}"
                      f"/{q.get('limit_day')}，本分钟 {q['remaining_minute']}"
                      f"/{q.get('limit_minute')}")
        return True
    except Exception as e:  # noqa: BLE001
        if verbose:
            print(f"[warn] 英雄表加载失败（将降级显示 hero_id）: {e}")
        return False


# ---------- 数据层 ----------

def fetch_matches(aid, date_min=None, date_max=None, max_pages=3):
    """
    拉取玩家公开对局（时间倒序）。

    ★ 关键：必须同时传 project=heroes 与 project=start_time（重复参数），
      才能一次拿到「10 人阵容」+「start_time」。
      若只传 project=heroes，start_time 会丢失，导致下面的提前退出判断
      恒为真（0 < date_min），翻一页就 break 静默截断。
      params 必须用「元组列表」而非 dict，urlencode 才会展开重复键。
    """
    out, offset = [], 0
    for _ in range(max_pages):
        params = [("limit", 500), ("offset", offset),
                  ("project", "heroes"), ("project", "start_time")]
        if date_min:
            params.append(("date_min", int(date_min)))
        if date_max:
            params.append(("date_max", int(date_max)))
        batch = rate_limited_get(f"/players/{aid}/matches", params)
        if not isinstance(batch, list) or not batch:
            break
        out.extend(batch)
        if len(batch) < 500:
            break
        if date_min and (batch[-1].get("start_time") or 0) < date_min:
            break
        offset += 500
        time.sleep(0.3)
    if date_min:
        out = [m for m in out if (m.get("start_time") or 0) >= date_min]
    if date_max:
        out = [m for m in out if (m.get("start_time") or 0) <= date_max]
    return out


def build_rec(m, aid_a, aid_b):
    """
    从列表记录（含 heroes 字典）直接构建单场结果。
    heroes 结构: {"0": {"account_id":..,"hero_id":..}, ..., "128": {...}}
    0-4 天辉，128-132 夜魇。
    """
    heroes = m.get("heroes") or {}
    st = m.get("start_time") or 0
    rec = {
        "match_id": m.get("match_id"),
        "start_time": st,
        "time_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(st)) if st else "",
        "radiant_win": m.get("radiant_win"),
        "duration": m.get("duration") or 0,
        "game_mode": m.get("game_mode"),
        "lobby_type": m.get("lobby_type"),
        "radiant_lineup": [],
        "dire_lineup": [],
        "a_player_slot": None, "b_player_slot": None,
        "a_hero_id": None, "b_hero_id": None,
        "same_team": None, "a_win": None, "b_win": None,
        "a_team": None, "b_team": None,
    }

    a_slot = b_slot = a_hid = b_hid = None
    for slot_s, v in heroes.items():
        try:
            slot = int(slot_s)
        except (TypeError, ValueError):
            continue
        hid = v.get("hero_id")
        acc = v.get("account_id")
        pinfo = {
            "hero": HERO_NAMES.get(hid, str(hid)),
            "hero_id": hid,
            "hero_slug": HERO_SLUGS.get(hid, ""),
            "account_id": acc,
            "is_A": acc is not None and acc == aid_a,
            "is_B": acc is not None and acc == aid_b,
        }
        (rec["radiant_lineup"] if slot < 128 else rec["dire_lineup"]).append(pinfo)
        if pinfo["is_A"]:
            a_slot, a_hid = slot, hid
        if pinfo["is_B"]:
            b_slot, b_hid = slot, hid

    rec["a_hero_id"], rec["b_hero_id"] = a_hid, b_hid
    rec["a_player_slot"], rec["b_player_slot"] = a_slot, b_slot

    # 阵容排序，天辉按 slot 升序、夜魇同理（渲染更稳定）
    if a_slot is None or b_slot is None:
        # 极老/未解析对局，缺 heroes 信息 → 降级为「未知」，不崩
        rec["detail_error"] = "该场未匹配到 A 或 B 的玩家记录"
        return rec

    rec["same_team"] = (a_slot < 128) == (b_slot < 128)
    rec["a_win"] = rec["radiant_win"] == (a_slot < 128)
    rec["b_win"] = rec["radiant_win"] == (b_slot < 128)
    rec["a_team"] = "天辉" if a_slot < 128 else "夜魇"
    rec["b_team"] = "天辉" if b_slot < 128 else "夜魇"
    return rec


def fetch_matches_bulk(aid, limit=2000):
    """
    单请求拉取玩家最近 N 场对局（实测 limit 可到 2000，约覆盖 4 年，耗时 ~1.7s）。
    用于「历史同局查询」：一次请求即可，无需分页，比 500/页 翻页快得多。

    同样必须带 project=heroes 与 project=start_time 两个重复参数。
    """
    params = [("limit", int(limit)),
              ("project", "heroes"), ("project", "start_time")]
    batch = rate_limited_get(f"/players/{aid}/matches", params)
    if not isinstance(batch, list):
        return []
    return batch


def intersect_one(aid_a, aid_b, limit=2000):
    """
    两位玩家的同局比对：拉双方各最近 limit 场，求 match_id 交集。
    返回 (results, info)。results 按时间倒序，含同队/敌对与输赢。
    """
    mine = fetch_matches_bulk(aid_a, limit)
    theirs = fetch_matches_bulk(aid_b, limit)
    idx_b = {m.get("match_id"): m for m in theirs}
    common = [m for m in mine if m.get("match_id") in idx_b]

    results = [build_rec(m, aid_a, aid_b) for m in common]
    results.sort(key=lambda r: r.get("start_time") or 0, reverse=True)

    info = {
        "a_total": len(mine),
        "b_total": len(theirs),
        "a_truncated": len(mine) >= limit,
        "b_truncated": len(theirs) >= limit,
        "limit": limit,
    }
    return results, info


def mark_current(results, live_match_id):
    """标记哪一场是当前正在进行的对局。"""
    for r in results:
        r["is_current"] = (live_match_id is not None
                           and str(r.get("match_id")) == str(live_match_id))
    return results


def summarize(results):
    """汇总统计。"""
    same = [r for r in results if r.get("same_team") is True]
    enemy = [r for r in results if r.get("same_team") is False]
    unknown = [r for r in results if r.get("same_team") is None]
    same_a_win = sum(1 for r in same if r.get("a_win"))
    enemy_a_win = sum(1 for r in enemy if r.get("a_win"))
    return {
        "total": len(results),
        "same": len(same),
        "enemy": len(enemy),
        "unknown": len(unknown),
        "same_a_win": same_a_win,
        "same_a_loss": len(same) - same_a_win,
        "same_a_winrate": round(same_a_win / len(same) * 100, 1) if same else None,
        "enemy_a_win": enemy_a_win,
        "enemy_a_loss": len(enemy) - enemy_a_win,
        "enemy_a_winrate": round(enemy_a_win / len(enemy) * 100, 1) if enemy else None,
    }


# ---------- 玩家资料 ----------

def fetch_profile(aid, label="玩家"):
    """
    拉取玩家公开资料并做「是否存在」校验。

    ★ 关键坑：OpenDota 对**越界/不存在的 account_id** 不会返回 {"error": ...}，
      而是返回一个字段全为 null 的「幽灵档案」，例如
        {"profile": {"account_id": 999999999999, "personaname": null,
                     "avatarfull": null, ...}, "rank_tier": null, ...}
      只判断 prof.get("error") 会漏掉这种情况，导致后续静默返回空结果，
      用户会误以为「我们没一起玩过」而不是「这个 ID 不存在」。
      故：既检查 error 键，也检查 personaname 与 avatar 是否同时为空。

    返回 (profile_dict, personaname, avatarfull, profileurl, rank_tier)。
    不满足时抛 ValueError。
    """
    aid = int(aid)
    prof = rate_limited_get(f"/players/{aid}")
    if not isinstance(prof, dict):
        raise ValueError(f"{label}（account_id {aid}）资料获取失败，请稍后重试。")
    if prof.get("error"):
        raise ValueError(
            f"{label}（account_id {aid}）在 OpenDota 无数据，"
            "可能该账号已关闭『公开比赛数据』，或 ID 不存在。"
        )
    p = prof.get("profile") or {}
    name = p.get("personaname")
    avatar = p.get("avatarfull")
    if not name and not avatar:
        # 幽灵档案：账号不存在 / 从未被 OpenDota 收录
        raise ValueError(
            f"{label}（account_id {aid}）在 OpenDota 查不到任何资料："
            "该 account_id 不存在，或其『公开比赛数据』从未开放。"
            "请核对 ID 是否正确。"
        )
    return prof, name, avatar, p.get("profileurl"), prof.get("rank_tier")


# ---------- 正在进行的对局 ----------

def steam64(account_id):
    """account_id(32位) → SteamID64。"""
    return account_id + STEAM64_BASE


def find_live_game(aid):
    """
    在 OpenDota /live 返回的对局中定位 aid 参与的那一场。

    ⚠ 重要限制：/live 只返回当前**最多 100 场**对局（实际偏向高分局），
      并非全量进行中对局。若玩家不在其中，只能返回「未找到」，
      这是接口能力边界，不是查询失败。OpenDota 也没有按玩家查直播对局的接口。
    """
    games = rate_limited_get("/live")
    if not isinstance(games, list):
        raise RuntimeError("OpenDota /live 返回了意外的数据结构")

    for g in games:
        for p in (g.get("players") or []):
            if p.get("account_id") == aid:
                return g, p
    return None, None


def build_live_game(g, aid):
    """把一场 live 对局整理成前端要好用的结构。"""
    players = []
    for p in (g.get("players") or []):
        acc = p.get("account_id")
        hid = p.get("hero_id")
        team = p.get("team")  # 0 = 天辉, 1 = 夜魇
        players.append({
            "account_id": acc,
            "steamid64": str(steam64(acc)) if acc else None,
            "steam_url": f"https://steamcommunity.com/profiles/{steam64(acc)}" if acc else None,
            "opendota_url": f"https://www.opendota.com/players/{acc}" if acc else None,
            "hero_id": hid,
            "hero": HERO_NAMES.get(hid, str(hid) if hid is not None else "未知"),
            "hero_slug": HERO_SLUGS.get(hid, ""),
            "team": team,
            "team_name": "天辉" if team == 0 else ("夜魇" if team == 1 else "未知"),
            "team_slot": p.get("team_slot"),
            "is_target": acc is not None and acc == aid,
        })
    radiant = [p for p in players if p["team"] == 0]
    dire = [p for p in players if p["team"] == 1]

    act = g.get("activate_time") or 0
    return {
        "match_id": g.get("match_id"),
        "opendota_url": f"https://www.opendota.com/matches/{g.get('match_id')}",
        "game_time": g.get("game_time") or 0,
        "game_time_str": time.strftime("%H:%M:%S", time.gmtime(g.get("game_time") or 0)),
        "activate_time": act,
        "started_str": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(act)) if act else "",
        "last_update_str": time.strftime("%Y-%m-%d %H:%M:%S",
                                         time.localtime(g.get("last_update_time") or 0))
                          if g.get("last_update_time") else "",
        "average_mmr": g.get("average_mmr"),
        "game_mode": g.get("game_mode"),
        "lobby_type": g.get("lobby_type"),
        "radiant_score": g.get("radiant_score"),
        "dire_score": g.get("dire_score"),
        "radiant_lead": g.get("radiant_lead"),
        "delay": g.get("delay"),
        "spectators": g.get("spectators"),
        "players": players,
        "radiant": radiant,
        "dire": dire,
    }


def fetch_live_by_account(aid):
    """拉取并返回 (live_game_dict 或 None, 元信息)。"""
    g, _p = find_live_game(aid)
    if not g:
        return None, {
            "live_total": 100,
            "reason": "该玩家当前不在 OpenDota 可查询的进行中对局列表中。",
        }
    return build_live_game(g, aid), None


# ---------- 任务层 ----------

def new_job_id():
    return "".join(random.choices(string.ascii_lowercase + string.digits, k=10))


def job_update(job_id, **kw):
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        if j:
            j.update(kw)
            j["last_access"] = time.time()


def job_get(job_id):
    """返回浅拷贝 —— 必须如此，否则序列化时 worker 并发写 key 会抛
    RuntimeError: dictionary changed size during iteration。"""
    with JOBS_LOCK:
        j = JOBS.get(job_id)
        return dict(j) if j else None


def set_stage(job_id, stage, text, percent=None):
    job_update(job_id,
               stage=stage,
               stage_text=text,
               percent=percent if percent is not None else STAGE_PERCENT.get(stage, 0))


def run_mate_job(job_id, req):
    """
    「历史同局查询」worker：查两位玩家是否曾在同一局出现。
    不限时间，各拉最近 limit 场（默认 2000，单请求），求 match_id 交集。
    """
    try:
        job_update(job_id, status="running")

        if not HERO_NAMES:
            set_stage(job_id, "heroes", "加载英雄名称表…")
            load_heroes_once(verbose=False)

        set_stage(job_id, "validate", "校验玩家信息…")
        aid_a, desc_a = ti.to_account_id(req.get("player_a", ""))
        aid_b, desc_b = ti.to_account_id(req.get("player_b", ""))
        if aid_a == aid_b:
            raise ValueError("两个 ID 是同一个账号。")

        players = {}
        for key, aid, desc in (("a", aid_a, desc_a), ("b", aid_b, desc_b)):
            who = "玩家 1" if key == "a" else "玩家 2"
            _prof, pname, pavatar, purl, prank = fetch_profile(aid, label=who)
            players[key] = {
                "account_id": aid, "desc": desc,
                "personaname": pname,
                "avatar": pavatar,
                "profileurl": purl,
                "rank_tier": prank,
                "steamid64": str(steam64(aid)),
            }

        limit = DEFAULT_MATCH_LIMIT
        try:
            limit = int(req.get("limit") or DEFAULT_MATCH_LIMIT)
        except (TypeError, ValueError):
            limit = DEFAULT_MATCH_LIMIT
        limit = max(MIN_MATCH_LIMIT, min(limit, MAX_MATCH_LIMIT))

        n1 = players["a"]["personaname"] or f"玩家 1（{aid_a}）"
        n2 = players["b"]["personaname"] or f"玩家 2（{aid_b}）"
        set_stage(job_id, "fetch_a", f"拉取 {n1} 最近对局…")
        mine = fetch_matches_bulk(aid_a, limit)
        job_update(job_id, a_count=len(mine))

        set_stage(job_id, "fetch_b", f"拉取 {n2} 最近对局…")
        theirs = fetch_matches_bulk(aid_b, limit)
        job_update(job_id, b_count=len(theirs))

        set_stage(job_id, "intersect", "比对双方同局记录…")
        idx_b = {m.get("match_id"): m for m in theirs}
        common = [m for m in mine if m.get("match_id") in idx_b]
        job_update(job_id, common=len(common))

        results = [build_rec(m, aid_a, aid_b) for m in common]
        results.sort(key=lambda r: r.get("start_time") or 0, reverse=True)

        # 尝试定位当前进行中的对局（/live 只覆盖 100 场，找不到也无妨）
        live_id = None
        try:
            g, _p = find_live_game(aid_a)
            if g:
                live_id = g.get("match_id")
        except Exception:  # noqa: BLE001
            pass
        results = mark_current(results, live_id)

        sm = summarize(results)
        with JOBS_LOCK:
            RESULT_STORE[job_id] = {
                "kind": "mate",
                "source": "opendota",
                "player_a": players["a"],
                "player_b": players["b"],
                "summary": sm,
                "results": results,
                "live_match_id": live_id,
                "info": {
                    "source": "opendota",
                    "source_label": "OpenDota",
                    "a_total": len(mine), "b_total": len(theirs),
                    "limit": limit,
                    "a_truncated": len(mine) >= limit,
                    "b_truncated": len(theirs) >= limit,
                    # ★ 关键提示：某一方对局列表为空时，"没有共同对局"这个结论
                    #   是**不可信**的 —— 该玩家的历史根本没被索引。
                    #   前端据此显示警示，避免用户误以为"确实没同局过"。
                    "a_opendota_empty": len(mine) == 0,
                    "b_opendota_empty": len(theirs) == 0,
                    "can_deep_scan": stratz_enabled(),
                },
            }
        set_stage(job_id, "done", "完成", 100)
        job_update(job_id, status="done", summary=sm)

    except ValueError as e:
        job_update(job_id, status="error", error=str(e), hint=None)
        print(f"[warn] mate job {job_id} 输入无效: {str(e).splitlines()[0]}")
    except Exception as e:  # noqa: BLE001
        msg = str(e) or e.__class__.__name__
        job_update(job_id, status="error", error=msg,
                   hint="OpenDota 可能暂时不稳定，请稍后重试。")
        print(f"[error] mate job {job_id} 失败: {msg}")
        traceback.print_exc()


def run_scan_job(job_id, req):
    """
    「深度扫描」worker：找出**关闭了公开比赛数据**的玩家与另一人的所有同局。

    ★ 为什么需要它：
      常规查询走 OpenDota 的「玩家 → 对局列表」求交集。若一方关闭了公开数据，
      其列表为空，交集必然为空 —— 无论两人是否真的同局过。这是**误导性的空结果**。
      本 worker 反向来做：以**公开的那一方**为基准拉其全部对局，
      逐场用 STRATZ 补全 10 人身份，直接看目标账号是否在场。

    请求参数：
      player_base   —— 已知公开、可拉取对局列表的一方（扫描基准）
      player_target —— 想确认是否同局的一方（可以是隐藏账号）
      max_matches   —— 可选，限制扫描场次（用于试探成本）
    """
    try:
        job_update(job_id, status="running")

        if not stratz_enabled():
            raise ValueError(
                "未配置 STRATZ_TOKEN，无法使用深度扫描。"
                "请在服务器上设置该环境变量后重启服务。")

        if not HERO_NAMES:
            set_stage(job_id, "heroes", "加载英雄名称表…")
            load_heroes_once(verbose=False)

        set_stage(job_id, "validate", "校验玩家信息…")
        aid_base, desc_base = ti.to_account_id(req.get("player_base", ""))
        aid_tgt, desc_tgt = ti.to_account_id(req.get("player_target", ""))
        if aid_base == aid_tgt:
            raise ValueError("两个 ID 是同一个账号。")

        # 基准方需要有公开对局列表；目标方允许查不到（正是本功能的意义）
        base_prof = {}
        try:
            _p, pname, pavatar, purl, prank = fetch_profile(aid_base, label="基准玩家")
            base_prof = {
                "account_id": aid_base, "desc": desc_base, "personaname": pname,
                "avatar": pavatar, "profileurl": purl, "rank_tier": prank,
                "steamid64": str(steam64(aid_base)),
            }
        except ValueError as e:
            raise ValueError(
                f"基准玩家（{aid_base}）在 OpenDota 无可用对局数据，无法作为扫描基准。"
                f"请把**公开数据可用**的一方填为基准玩家。原始信息：{e}") from e

        # 目标方：OpenDota 可能查不到，但仍尝试取昵称（取不到就用 STRATZ / ID 兜底）
        tgt_prof = {
            "account_id": aid_tgt, "desc": desc_tgt,
            "personaname": None, "avatar": None, "profileurl": None,
            "rank_tier": None, "steamid64": str(steam64(aid_tgt)),
        }
        try:
            _p, tname, tavatar, turl, trank = fetch_profile(aid_tgt, label="目标玩家")
            tgt_prof.update({"personaname": tname, "avatar": tavatar,
                             "profileurl": turl, "rank_tier": trank})
        except ValueError:
            pass

        # ★ 判定「目标方在 OpenDota 的**对局列表**是否为空」，必须单独查 /matches。
        #   不能复用 fetch_profile 的结果：它校验的是**资料**（personaname/avatar），
        #   而 Valve 的双轨发布策略下，关闭公开数据的玩家**资料仍在、对局列表为空**。
        #   例：某账号资料完整（有昵称、有头像），但 /matches 返回 []。
        #   这个标记驱动前端「空结果不可信」警示，判错会让用户重新掉进
        #   「没查到 = 没同局过」的坑，所以必须按对局列表的真实结果来判。
        tgt_odata_empty = False
        try:
            tgt_od_matches = rate_limited_get(
                f"/players/{aid_tgt}/matches", {"limit": 1})
            tgt_odata_empty = not tgt_od_matches
        except Exception:  # noqa: BLE001
            # 查询失败时不误报为「空」（宁可漏警示，不可假警示）
            tgt_odata_empty = False

        # 用 STRATZ 补昵称与总场次（即使 OpenDota 查不到也能拿到）
        set_stage(job_id, "validate", "读取 STRATZ 玩家信息…")
        try:
            meta = stratz_player_meta(aid_tgt)
            if not tgt_prof["personaname"] and meta.get("personaname"):
                tgt_prof["personaname"] = meta["personaname"]
        except StratzError:
            meta = {}
        try:
            base_meta = stratz_player_meta(aid_base)
        except StratzError:
            base_meta = {}
        total_base = base_meta.get("match_count") or 0

        n_base = base_prof["personaname"] or f"玩家（{aid_base}）"
        n_tgt = tgt_prof["personaname"] or f"玩家（{aid_tgt}）"

        # ★ 扫描场次上限：默认 2000，与 OpenDota 常规查询的覆盖范围对齐。
        #   不设上限会扫完全部历史（实测 5038 场 / 52 次调用 / 约 60 秒），
        #   虽然成本可控，但等待时间偏长，且与常规查询的覆盖口径不一致
        #   会让用户误以为两边结果可直接对比。
        #   传 max_matches 可覆盖；显式传 0 或负数表示「不限」。
        max_matches = STRATZ_DEFAULT_MAX
        try:
            if req.get("max_matches") is not None:
                v = int(req["max_matches"])
                max_matches = None if v <= 0 else max(100, v)
        except (TypeError, ValueError):
            max_matches = STRATZ_DEFAULT_MAX

        # 扫描：进度按「本次实际要扫的场次」映射到 10~95%。
        # ★ 分母要用 min(基准总场次, 上限)，否则设了 2000 上限但分母是 5038 时，
        #   进度条会卡在 40% 左右就跳到 97%，看起来像卡死了。
        plan_total = total_base
        if max_matches is not None:
            plan_total = min(total_base, max_matches) if total_base else max_matches
        job_update(job_id, b_count=plan_total)   # 让前端 counters 显示计划场次

        def on_progress(scanned, found, calls):
            if plan_total:
                pct = 10 + int(min(scanned / plan_total, 1.0) * 85)
            else:
                pct = min(95, 10 + calls)
            set_stage(job_id, "scan",
                      f"已扫描 {scanned} 场 · 命中 {found} 场 · 第 {calls} 次请求",
                      pct)
            job_update(job_id, a_count=scanned, common=found)

        set_stage(job_id, "scan", f"以 {n_base} 为基准扫描全部对局…", 10)
        hits, scan_meta = stratz_scan_matches(
            aid_base, aid_tgt, on_progress=on_progress, max_matches=max_matches)

        # 组装为与常规查询一致的记录结构，复用前端渲染
        set_stage(job_id, "intersect", "整理命中场次…", 97)
        results = []
        for h in hits:
            results.append({
                "match_id": h["match_id"],
                "start_time": h["start_time"],
                "time_str": time.strftime("%Y-%m-%d %H:%M:%S",
                                          time.localtime(h["start_time"] or 0)),
                "same_team": h["same_team"],
                "a_hero_id": h["base_hero_id"],
                "b_hero_id": h["target_hero_id"],
                "a_player_slot": h["base_slot"],
                "b_player_slot": h["target_slot"],
                "a_team": h["base_team"],
                "b_team": h["target_team"],
                # ★ 10 人阵容：数据来自扫描响应里本来就有的 players 列表，
                #   不额外发请求。结构与 OpenDota 侧（build_rec）保持一致，
                #   这样前端 lineupHtml / playerCardHtml 可直接复用。
                "radiant_lineup": _scan_lineup(h.get("players"), True, aid_base, aid_tgt),
                "dire_lineup": _scan_lineup(h.get("players"), False, aid_base, aid_tgt),
                # 胜负用 player_slot 推：<128 为天辉
                "radiant_win": None,
                "duration": None,
            })

        # 补胜负：用 STRATZ 的 isRadiant + radiantWin（批量拉这几场的详情）
        set_stage(job_id, "intersect", "补全胜负信息…", 98)
        for r in results:
            try:
                _fill_scan_win(r, r["match_id"])
            except Exception:  # noqa: BLE001
                pass

        same = [r for r in results if r["same_team"]]
        enemy = [r for r in results if not r["same_team"]]
        base_wins_same = sum(1 for r in same if r.get("a_win"))
        base_wins_enemy = sum(1 for r in enemy if r.get("a_win"))
        sm = {
            "total": len(results),
            "same": len(same),
            "enemy": len(enemy),
            "unknown": 0,
            "same_a_win": base_wins_same,
            "same_a_loss": len(same) - base_wins_same,
            "same_a_winrate": (round(base_wins_same / len(same) * 100, 1)
                               if same else None),
            "enemy_a_win": base_wins_enemy,
            "enemy_a_loss": len(enemy) - base_wins_enemy,
            "enemy_a_winrate": (round(base_wins_enemy / len(enemy) * 100, 1)
                                if enemy else None),
        }

        with JOBS_LOCK:
            RESULT_STORE[job_id] = {
                "kind": "scan",
                "source": "stratz",
                "player_a": base_prof,
                "player_b": tgt_prof,
                "summary": sm,
                "results": results,
                "live_match_id": None,
                "info": {
                    "source": "stratz",
                    "source_label": "STRATZ",
                    "scanned": scan_meta["scanned"],
                    "calls": scan_meta["calls"],
                    "truncated": scan_meta["truncated"],
                    "base_total": total_base,
                    # 本次扫描的场次上限（None 表示不限）
                    "max_matches": max_matches,
                    "plan_total": plan_total,
                    "stratz_quota": stratz_snapshot(),
                    # 目标方在 OpenDota 查不到 —— 这本身就是本次扫描的原因
                    "target_opendota_empty": tgt_odata_empty,
                },
            }
        set_stage(job_id, "done", "完成", 100)
        job_update(job_id, status="done", summary=sm)

    except ValueError as e:
        job_update(job_id, status="error", error=str(e), hint=None)
        print(f"[warn] scan job {job_id} 输入无效: {str(e).splitlines()[0]}")
    except StratzError as e:
        job_update(job_id, status="error", error=str(e),
                   hint="请检查 STRATZ_TOKEN 是否正确、是否超出配额。")
        print(f"[warn] scan job {job_id} STRATZ 错误: {e}")
    except Exception as e:  # noqa: BLE001
        msg = str(e) or e.__class__.__name__
        job_update(job_id, status="error", error=msg,
                   hint="请稍后重试，或检查网络与 STRATZ 配额。")
        print(f"[error] scan job {job_id} 失败: {msg}")
        traceback.print_exc()


def _scan_lineup(players, radiant, aid_base, aid_tgt):
    """
    把 STRATZ 扫描响应里的 players 列表切成天辉/夜魇阵容。

    输出结构与 OpenDota 侧 build_rec 生成的 lineup 元素保持一致
    （hero / hero_id / hero_slug / account_id / is_A / is_B），
    另附 personaname（STRATZ 独有，OpenDota 那侧没有昵称），
    这样前端 lineupHtml 与 playerCardHtml 都能直接复用。

    radiant=True 取天辉（player_slot < 128），False 取夜魇。
    ★ slot 语义已实测与 OpenDota 一致：0-4 天辉、128-132 夜魇。
    """
    out = []
    for p in (players or []):
        slot = p.get("player_slot")
        if slot is None:
            continue
        is_rad = slot < 128
        if is_rad != radiant:
            continue
        hid = p.get("hero_id")
        acc = p.get("account_id")
        out.append({
            "hero": HERO_NAMES.get(hid, str(hid)),
            "hero_id": hid,
            "hero_slug": HERO_SLUGS.get(hid, ""),
            "account_id": acc,
            "is_A": acc is not None and acc == aid_base,
            "is_B": acc is not None and acc == aid_tgt,
            "personaname": p.get("personaname"),
            # 仅用于排序，渲染层忽略
            "_slot": slot,
        })
    # ★ 按 slot 升序，保证渲染顺序稳定（与 OpenDota 侧一致）。
    #   不能用 hero_id 排序：他可能与 slot 无关，且为 None 时会乱序。
    out.sort(key=lambda x: x["_slot"])
    for x in out:
        x.pop("_slot", None)
    return out


def _fill_scan_win(rec, match_id):
    """给扫描结果补 radiant_win（STRATZ 的单场查询里带）。"""
    d = stratz_query(
        "query M($id: Long!){ match(id: $id){ id didRadiantWin } }",
        {"id": int(match_id)})
    m = d.get("match") or {}
    rw = m.get("didRadiantWin")
    rec["radiant_win"] = rw
    # 与领域层同语义：a_win = (radiant_win == a 在天辉)
    a_is_rad = (rec.get("a_player_slot") or 0) < 128
    b_is_rad = (rec.get("b_player_slot") or 0) < 128
    if rw is not None:
        rec["a_win"] = (rw == a_is_rad)
        rec["b_win"] = (rw == b_is_rad)
    else:
        rec["a_win"] = None
        rec["b_win"] = None


def cleanup_loop(stop_event, max_age=1800, interval=300):
    """daemon 线程：定期清理过期 job，防止内存无限增长。"""
    while not stop_event.wait(interval):
        now = time.time()
        with JOBS_LOCK:
            stale = [k for k, v in JOBS.items()
                     if now - v.get("last_access", v.get("created_at", now)) > max_age]
            for k in stale:
                JOBS.pop(k, None)
                RESULT_STORE.pop(k, None)


# ---------- 导出 ----------

CSV_HEADER_BASE = ["match_id", "时间", "同队/敌对"]


def csv_header(name_a, name_b):
    """表头用玩家昵称，不用 A/B。"""
    return (CSV_HEADER_BASE
            + [f"{name_a}所在队伍", f"{name_a}英雄", f"{name_a}结果",
               f"{name_b}所在队伍", f"{name_b}英雄", f"{name_b}结果",
               "天辉阵容", "夜魇阵容"])


def rel_text(r):
    if r.get("same_team") is True:
        return "同队"
    if r.get("same_team") is False:
        return "敌对"
    return "未知"


def res_text(v):
    if v is True:
        return "胜"
    if v is False:
        return "负"
    return "未知"


def render_csv(results, name_a="玩家1", name_b="玩家2"):
    """CSV 文本（不含 BOM，编码时再加）。"""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(csv_header(name_a, name_b))
    for r in results:
        w.writerow([
            r.get("match_id"), r.get("time_str"), rel_text(r),
            r.get("a_team") or "", HERO_NAMES.get(r.get("a_hero_id"), r.get("a_hero_id") or ""),
            res_text(r.get("a_win")),
            r.get("b_team") or "", HERO_NAMES.get(r.get("b_hero_id"), r.get("b_hero_id") or ""),
            res_text(r.get("b_win")),
            " / ".join(p["hero"] for p in r.get("radiant_lineup", [])),
            " / ".join(p["hero"] for p in r.get("dire_lineup", [])),
        ])
    return buf.getvalue()


def render_json(payload):
    return json.dumps(payload, ensure_ascii=False, indent=2)


# ---------- HTTP 层 ----------

class Handler(BaseHTTPRequestHandler):
    server_version = "Dota2SameMatchServer/" + VERSION
    protocol_version = "HTTP/1.1"

    # --- 响应辅助 ---

    def _send_bytes(self, body, status=200, ctype="application/json; charset=utf-8", extra=None):
        self.send_response(status)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _json(self, obj, status=200):
        self._send_bytes(json.dumps(obj, ensure_ascii=False).encode("utf-8"), status)

    def _err(self, status, msg, hint=None):
        self._json({"ok": False, "error": msg, "hint": hint}, status)

    def log_message(self, fmt, *args):
        """精简日志，避免 stderr 刷屏。"""
        sys.stderr.write(f"[{time.strftime('%H:%M:%S')}] {self.command} {self.path}\n")

    # --- 路由 ---

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        path = urllib.parse.urlparse(self.path).path
        parts = [p for p in path.split("/") if p]

        try:
            if not parts:
                return self.serve_index()
            if parts[0] == "api" and len(parts) >= 2:
                if parts[1] == "progress" and len(parts) == 3:
                    return self.handle_progress(parts[2])
                if parts[1] == "result" and len(parts) == 3:
                    return self.handle_result(parts[2])
                if parts[1] == "export" and len(parts) == 3:
                    return self.handle_export(parts[2])
                if parts[1] == "quota":
                    return self.handle_quota()
                if parts[1] == "stratz":
                    return self.handle_stratz()
            if parts[0] == "health":
                # 顺带带出最近观测到的配额，便于用脚本/监控盯额度
                q = quota_snapshot()
                sq = stratz_snapshot()
                return self._json({"ok": True, "heroes": len(HERO_NAMES),
                                   "jobs": len(JOBS), "version": VERSION,
                                   "quota": {
                                       "remaining_minute": q.get("remaining_minute"),
                                       "remaining_day": q.get("remaining_day"),
                                       "limit_day": q.get("limit_day"),
                                       "used_day": q.get("used_day"),
                                       "updated_at": q.get("updated_at"),
                                       "stale": q.get("stale"),
                                   },
                                   "stratz": {
                                       "enabled": sq.get("enabled"),
                                       "remaining_day": sq.get("remaining_day"),
                                       "limit_day": sq.get("limit_day"),
                                       "stale": sq.get("stale"),
                                   }})
            if parts[0] == "favicon.ico":
                # 内嵌 1x1 透明 ICO，避免浏览器控制台报 404
                return self._send_bytes(FAVICON, 200, "image/x-icon")
            return self._err(404, "接口不存在")
        except BrokenPipeError:
            pass
        except Exception as e:  # noqa: BLE001
            traceback.print_exc()
            try:
                self._err(500, f"服务端错误: {e}")
            except Exception:  # noqa: BLE001
                pass

    def do_POST(self):
        path = urllib.parse.urlparse(self.path).path.rstrip("/")
        if path not in ("/api/mate", "/api/scan"):
            return self._err(404, "接口不存在")
        try:
            length = int(self.headers.get("Content-Length") or 0)
            raw = self.rfile.read(length) if length else b"{}"
            req = json.loads(raw.decode("utf-8") or "{}")
        except Exception as e:  # noqa: BLE001
            return self._err(400, f"请求体解析失败: {e}")

        if path == "/api/scan":
            if not stratz_enabled():
                return self._err(
                    503, "深度扫描功能未启用：服务器未配置 STRATZ_TOKEN。",
                    "深度扫描用于查找「关闭了公开比赛数据」的玩家。"
                    "请在服务器上设置环境变量 STRATZ_TOKEN 后重启服务。")
            pbase = (req.get("player_base") or "").strip()
            ptgt = (req.get("player_target") or "").strip()
            if not pbase or not ptgt:
                return self._err(400, "基准玩家与目标玩家的 ID 都不能为空。")
            for label, v in (("基准玩家", pbase), ("目标玩家", ptgt)):
                if not v.isdigit() and "steamcommunity.com" not in v:
                    return self._err(400, f"无法识别的{label} SteamID: {v}",
                                     "请输入纯数字的 SteamID64 / account_id，"
                                     "或 https://steamcommunity.com/profiles/<SteamID64> 形式的链接。")
            return self._spawn(req, run_scan_job)

        pa = (req.get("player_a") or "").strip()
        pb = (req.get("player_b") or "").strip()
        if not pa or not pb:
            return self._err(400, "两位玩家的 ID 都不能为空。")
        for label, v in (("玩家 1", pa), ("玩家 2", pb)):
            if not v.isdigit() and "steamcommunity.com" not in v:
                return self._err(400, f"无法识别的{label} SteamID: {v}",
                                 "请输入纯数字的 SteamID64 / account_id，"
                                 "或 https://steamcommunity.com/profiles/<SteamID64> 形式的链接。")
        return self._spawn(req, run_mate_job)

    def _spawn(self, req, target):
        """建 job 并起 worker 线程，立即返回 job_id。"""
        job_id = new_job_id()
        now = time.time()
        with JOBS_LOCK:
            JOBS[job_id] = {
                "job_id": job_id, "status": "running", "stage": "init",
                "stage_text": "初始化中…", "percent": STAGE_PERCENT["init"],
                "a_count": 0, "b_count": 0, "common": 0, "found": None,
                "created_at": now, "last_access": now,
                "error": None, "hint": None,
            }
        threading.Thread(target=target, args=(job_id, req), daemon=True).start()
        self._json({"ok": True, "job_id": job_id}, 202)

    # --- 各端点实现 ---

    def serve_index(self):
        body = INDEX_HTML.replace("__BOOT_ID__", BOOT_ID).encode("utf-8")
        self._send_bytes(body, 200, "text/html; charset=utf-8")

    def handle_stratz(self):
        """
        GET /api/stratz —— STRATZ 可用性与配额。

        enabled=false 表示未配置 STRATZ_TOKEN，深度扫描功能不可用，
        其余功能（常规同局查询）不受影响。
        """
        snap = stratz_snapshot()
        snap["ok"] = True
        return self._json(snap)

    def handle_quota(self):
        """
        GET /api/quota            读缓存值（不发请求，**不消耗配额**）
        GET /api/quota?refresh=1  主动探测一次（**消耗 1 次配额**，但读数最新）

        前端默认走缓存，仅在用户点「刷新」时才 refresh=1。
        """
        qs = urllib.parse.parse_qs(urllib.parse.urlparse(self.path).query)
        if qs.get("refresh", ["0"])[0] in ("1", "true", "yes"):
            ok = probe_quota()
            if not ok and quota_snapshot()["updated_at"] is None:
                return self._err(503, "暂时读不到 OpenDota 配额",
                                 "可能是网络不通或 OpenDota 不可用。查询本身不受影响。")
        snap = quota_snapshot()
        snap["ok"] = True
        return self._json(snap)

    def handle_progress(self, job_id):
        j = job_get(job_id)
        if not j:
            return self._err(404, "任务不存在或已过期")
        j["ok"] = True
        j["elapsed"] = round(time.time() - j.get("created_at", time.time()), 1)
        j.pop("last_access", None)
        self._json(j)

    def handle_result(self, job_id):
        j = job_get(job_id)
        if not j:
            return self._err(404, "任务不存在或已过期")
        if j.get("status") == "error":
            return self._err(400, j.get("error") or "查询失败", j.get("hint"))
        if j.get("status") != "done":
            return self._err(409, "任务尚未完成")
        with JOBS_LOCK:
            payload = RESULT_STORE.get(job_id)
        if not payload:
            return self._err(410, "结果已被清理，请重新查询")
        out = {"ok": True}
        out.update(payload)
        return self._json(out)

    def handle_export(self, filename):
        # 形如 <job_id>.csv / <job_id>.json
        if "." not in filename:
            return self._err(404, "接口不存在")
        job_id, ext = filename.rsplit(".", 1)
        ext = ext.lower()
        if ext not in ("csv", "json"):
            return self._err(404, "仅支持导出 csv / json")
        with JOBS_LOCK:
            payload = RESULT_STORE.get(job_id)
        if not payload:
            return self._err(410, "结果不存在或已被清理，请重新查询")
        if not payload.get("results"):
            return self._err(400, "该结果没有可导出的记录。")

        pa_info = payload["player_a"]
        pb_info = payload["player_b"]
        aid_a = pa_info["account_id"]
        aid_b = pb_info["account_id"]
        prefix = "history_intersection"
        fname = f"{prefix}_{aid_a}_{aid_b}.{ext}"  # 刻意只用 ASCII

        if ext == "csv":
            # 表头用昵称；无昵称时回退到 account_id，保证列名可读
            name_a = pa_info.get("personaname") or f"玩家1({aid_a})"
            name_b = pb_info.get("personaname") or f"玩家2({aid_b})"
            body = render_csv(payload["results"], name_a, name_b).encode("utf-8-sig")  # BOM 供 Excel
            ctype = "text/csv; charset=utf-8"
        else:
            body = render_json({k: v for k, v in payload.items()}).encode("utf-8")
            ctype = "application/json; charset=utf-8"

        self._send_bytes(body, 200, ctype,
                         {"Content-Disposition": f'attachment; filename="{fname}"'})


# ---------- 前端 ----------

INDEX_HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Dota2 同局查询</title>
<style>
:root{
  --bg:#0f1116; --card:#171a21; --line:#262b36; --fg:#e6e9ef; --muted:#8b93a7;
  --accent:#5b8def; --accent2:#7c5cff; --same:#2ea043; --enemy:#d9534f;
  --win:#3fb950; --lose:#f85149; --input:#0c0e13;
}
@media (prefers-color-scheme: light){
  :root{ --bg:#f6f7f9; --card:#fff; --line:#e3e6ec; --fg:#1f2328; --muted:#57606a; --input:#fff; }
}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--fg);
     font:14px/1.6 "Segoe UI","Microsoft YaHei",system-ui,-apple-system,sans-serif}
header{padding:28px 16px 8px;max-width:1100px;margin:0 auto}
h1{margin:0 0 4px;font-size:22px;font-weight:650}
.sub{margin:0;color:var(--muted);font-size:13px}
main{max-width:1100px;margin:12px auto 60px;padding:0 16px}
.card{background:var(--card);border:1px solid var(--line);border-radius:10px;
      padding:16px;margin-bottom:14px}
label{display:block;font-size:13px;color:var(--muted);margin-bottom:5px}
input[type=text],input[type=date]{font:inherit;color:var(--fg);background:var(--input);
      border:1px solid var(--line);border-radius:6px;padding:8px 10px;width:100%}
input:focus{outline:none;border-color:var(--accent)}
.grid-2{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-bottom:12px}
.grid-3{display:grid;grid-template-columns:1fr 1fr 1fr;gap:12px}
@media(max-width:720px){.grid-2,.grid-3{grid-template-columns:1fr}}
.chk{display:flex;align-items:center;gap:7px;color:var(--fg);font-size:13px;margin:0}
.chk input{width:auto}
.row{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-top:14px;flex-wrap:wrap}
button{font:inherit;color:var(--fg);background:var(--input);border:1px solid var(--line);
       border-radius:6px;padding:8px 16px;cursor:pointer}
button:hover:not(:disabled){border-color:var(--accent)}
button:disabled{opacity:.5;cursor:not-allowed}
#submit-btn{background:var(--accent);border-color:var(--accent);color:#fff;padding:9px 22px;font-weight:600}
#submit-btn:hover:not(:disabled){filter:brightness(1.1)}
.hint{color:var(--muted);font-size:12px;margin:10px 0 0}
/* 模式切换标签 */
/* 数据源选择器 */
.src-row{display:flex;align-items:flex-start;gap:12px;margin-bottom:10px}
.src-label{font-size:12px;color:var(--muted);padding-top:10px;white-space:nowrap}
.tabs{display:flex;gap:8px;flex:1;flex-wrap:wrap}
.tab{display:flex;flex-direction:column;align-items:flex-start;gap:2px;
     background:var(--input);border:1px solid var(--line);color:var(--muted);
     padding:8px 16px;border-radius:8px;font-size:13px;text-align:left;
     transition:border-color .15s,background .15s}
.tab .tab-name{font-weight:600;font-size:13.5px}
.tab .tab-cost{font-size:11px;opacity:.8;white-space:nowrap}
.tab:hover:not(:disabled){border-color:var(--accent);color:var(--fg)}
.tab.active{background:rgba(91,141,239,.13);border-color:var(--accent);color:var(--fg)}
.tab.active .tab-name{color:var(--accent)}
.tab:disabled{opacity:.4;cursor:not-allowed;background:transparent}
.src-badge{display:inline-block;margin-left:10px;padding:2px 8px;border-radius:5px;
           font-size:11px;font-weight:500;vertical-align:middle;
           background:rgba(139,147,167,.18);color:var(--muted)}
/* 提示条 */
.notice{border-radius:8px;padding:12px 14px;font-size:13px;line-height:1.65;
        border:1px solid var(--line);background:var(--input)}
.notice.warn{border-color:#d0a53c;background:rgba(208,165,60,.12)}
.notice.info{border-color:var(--accent);background:rgba(91,141,239,.10)}
.notice.bad{border-color:var(--enemy);background:rgba(217,83,79,.10)}
.notice strong{display:block;margin-bottom:4px}
.notice .act{margin-top:10px;display:flex;gap:8px;flex-wrap:wrap}
.notice button{font-size:12.5px;padding:6px 13px}
.progress-head{display:flex;justify-content:space-between;font-size:13px;margin-bottom:8px}
#progress-percent{color:var(--accent);font-variant-numeric:tabular-nums;font-weight:600}
.bar{height:8px;background:var(--input);border-radius:4px;overflow:hidden}
.bar-fill{height:100%;width:0;border-radius:4px;
          background:linear-gradient(90deg,var(--accent),var(--accent2));
          transition:width .35s ease}
.counters{display:flex;gap:16px;flex-wrap:wrap;margin-top:10px;
          font-size:12px;color:var(--muted);font-variant-numeric:tabular-nums}
#error-card{border-color:var(--enemy)}
#error-card strong{color:var(--enemy)}
#error-msg{margin:8px 0 0}
.result-head{display:flex;justify-content:space-between;align-items:center;
             gap:12px;flex-wrap:wrap;margin-bottom:14px}
.summary{display:flex;gap:18px;flex-wrap:wrap;font-size:13px}
.summary b{font-size:17px;font-variant-numeric:tabular-nums}
.summary .k{color:var(--muted);font-size:12px;display:block}
.actions{display:flex;gap:8px}
.match{border:1px solid var(--line);border-left:4px solid var(--muted);
       border-radius:8px;padding:12px 14px;margin-bottom:12px;background:var(--input)}
@media (prefers-color-scheme: light){.match{background:#fbfbfd}}
.match.same{border-left-color:var(--same)}
.match.enemy{border-left-color:var(--enemy)}
.match-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;font-size:13px}
.tag{padding:2px 9px;border-radius:20px;font-size:12px;font-weight:600;
     background:rgba(139,147,167,.16);color:var(--muted)}
.match.same .tag{background:rgba(46,160,67,.16);color:var(--same)}
.match.enemy .tag{background:rgba(217,83,79,.16);color:var(--enemy)}
.mid{color:var(--accent);text-decoration:none;font-weight:600}
.mid:hover{text-decoration:underline}
.time,.dur{color:var(--muted)}
.players{margin-top:10px;display:flex;flex-direction:column;gap:5px}
.prow{display:flex;align-items:center;gap:9px;padding:6px 10px;border-radius:6px;
      font-size:13px;background:rgba(139,147,167,.07)}
.prow.is-A{background:rgba(91,141,239,.13)}
.prow.is-B{background:rgba(124,92,255,.13)}
.who{font-weight:600;min-width:118px}
.badge{font-size:10px;font-weight:700;padding:1px 5px;border-radius:3px;
       background:var(--accent);color:#fff}
.badge.b{background:var(--accent2)}
.side{color:var(--muted);font-size:12px}
.res{margin-left:auto;font-weight:700}
.res.win{color:var(--win)} .res.lose{color:var(--lose)} .res.na{color:var(--muted)}
.hero-icon{width:30px;height:30px;border-radius:4px;object-fit:cover;flex:none;
           background:rgba(139,147,167,.15)}
.lineups{display:grid;grid-template-columns:1fr 1fr;gap:12px;margin-top:12px;
         padding-top:11px;border-top:1px solid var(--line)}
@media(max-width:720px){.lineups{grid-template-columns:1fr}}
.lineup h4{margin:0 0 7px;font-size:12px;color:var(--muted);font-weight:600;letter-spacing:.4px}
.lineup.radiant h4{color:#6fbf73} .lineup.dire h4{color:#e0796f}
.lineup ul{list-style:none;margin:0;padding:0;display:flex;flex-wrap:wrap;gap:6px}
.lineup li{display:flex;align-items:center;gap:5px;padding:3px 9px 3px 4px;
           border:1px solid var(--line);border-radius:6px;font-size:12.5px}
.lineup li.is-A{border-color:var(--accent);background:rgba(91,141,239,.12)}
.lineup li.is-B{border-color:var(--accent2);background:rgba(124,92,255,.12)}
.lineup .hero-icon{width:22px;height:22px}
.empty{text-align:center;padding:34px 16px;color:var(--muted)}
.empty b{display:block;color:var(--fg);font-size:15px;margin-bottom:10px}
.empty ul{display:inline-block;text-align:left;margin:0;padding-left:20px;font-size:13px}
/* 模式切换标签 */
.tabs{display:flex;gap:6px;margin-bottom:16px;border-bottom:1px solid var(--line);padding-bottom:0}
.tab{background:transparent;border:none;border-bottom:2px solid transparent;border-radius:0;
     padding:8px 14px;color:var(--muted);font-weight:600;cursor:pointer}
.tab:hover{color:var(--fg)}
.tab.active{color:var(--accent);border-bottom-color:var(--accent)}
/* 数据源选择器：两张互斥大卡。选中态 = 白底 + 蓝色边框（用户指定，简洁为主）；
   未选中 = 略暗表面 + 灰边框。用独立 class 以免被上面的 .tab 通用样式覆盖。 */
.src-tabs{display:flex;gap:14px;margin-bottom:16px}
.src-tab{position:relative;flex:1;display:flex;flex-direction:column;gap:5px;
  /* 未选中用略暗的表面，让选中的「白底」能跳出来（同一主题下形成对比） */
  background:var(--bg);border:2px solid var(--line);border-radius:14px;
  padding:16px 20px;cursor:pointer;text-align:left;color:var(--muted);
  opacity:.72;
  transition:border-color .15s,background .15s,box-shadow .15s,transform .06s,opacity .15s}
/* 鼠标悬停时先提亮，但远不及选中态，强化「选中才是主」的层级感。
   ★ 必须排除 .active：`:hover:not(:disabled)` 的特异性(0,3,0)高于 `.active`(0,2,0)，
     不排除的话鼠标停在选中卡上时，灰边框会盖掉蓝边框。 */
.src-tab:not(.active):hover:not(:disabled){opacity:.92}
.src-tab .tab-name{font-weight:800;font-size:18px;letter-spacing:.4px}
.src-tab .tab-cost{font-size:12.5px;opacity:.85;white-space:nowrap}
/* 悬停用中性色提亮，不引入蓝色（同样排除选中态，保证蓝边框不被覆盖） */
.src-tab:not(.active):hover:not(:disabled){border-color:var(--muted);color:var(--fg)}
.src-tab:active:not(:disabled){transform:translateY(1px)}
/* 选中态（用户指定）：白底 + 蓝色边框，不加填充/发光/角标，保持简洁。
   var(--card) 在浅色主题下即纯白；深色主题下取卡片色，同样清晰。 */
.src-tab.active{color:var(--fg);opacity:1;
  background:var(--card);border-color:var(--accent)}
.src-tab:disabled{opacity:.4;cursor:not-allowed;background:var(--input);transform:none}
/* 直播对局 */
.live-head{display:flex;align-items:center;gap:12px;flex-wrap:wrap;margin-bottom:6px}
.live-badge{display:inline-flex;align-items:center;gap:6px;padding:3px 10px;border-radius:20px;
            background:rgba(217,83,79,.16);color:var(--enemy);font-size:12px;font-weight:700}
.dot{width:7px;height:7px;border-radius:50%;background:var(--enemy);
     animation:pulse 1.4s ease-in-out infinite}
@keyframes pulse{0%,100%{opacity:1}50%{opacity:.25}}
.live-stats{display:flex;gap:20px;flex-wrap:wrap;font-size:13px;margin:10px 0 4px}
.live-stats .k{color:var(--muted);font-size:12px;display:block}
.live-stats b{font-size:16px;font-variant-numeric:tabular-nums}
.scoreline{display:flex;align-items:center;gap:14px;margin:14px 0;font-size:22px;font-weight:700}
.scoreline .r{color:#6fbf73} .scoreline .d{color:#e0796f}
.scoreline .lbl{font-size:12px;color:var(--muted);font-weight:400}
.lead{font-size:12px;color:var(--muted);font-weight:400;margin-left:auto}
.team-block{margin-top:16px}
.team-block h4{margin:0 0 9px;font-size:12px;font-weight:600;letter-spacing:.4px;
               display:flex;align-items:center;gap:8px}
.team-block.radiant h4{color:#6fbf73} .team-block.dire h4{color:#e0796f}
.plist{display:grid;grid-template-columns:1fr 1fr;gap:8px}
@media(max-width:760px){.plist{grid-template-columns:1fr}}
.pcard{display:flex;align-items:center;gap:10px;padding:8px 11px;border-radius:7px;
       border:1px solid var(--line);background:var(--input);font-size:13px}
@media (prefers-color-scheme: light){.pcard{background:#fbfbfd}}
.pcard.is-target{border-color:var(--accent);background:rgba(91,141,239,.13)}
.pcard .hero-icon{width:34px;height:34px}
.pmeta{min-width:0;flex:1}
.pname{font-weight:600;white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.psid{font-size:11.5px;color:var(--muted);font-family:ui-monospace,Consolas,monospace;
      white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.prow2{display:flex;align-items:center;gap:6px;flex-wrap:wrap;margin-top:1px}
.hlabel{font-size:11.5px;color:var(--muted)}
.pcard a{color:var(--accent);text-decoration:none;font-size:11.5px}
.pcard a:hover{text-decoration:underline}
.hidden{display:none !important}
/* 结论横幅 */
.verdict{display:flex;align-items:center;gap:14px;padding:14px 16px;border-radius:9px;
         margin-bottom:16px;border:1px solid var(--line);background:var(--input)}
@media (prefers-color-scheme: light){.verdict{background:#fbfbfd}}
.verdict.yes{border-color:var(--same);background:rgba(46,160,67,.10)}
.verdict.no{border-color:var(--enemy);background:rgba(217,83,79,.10)}
.verdict .vicon{font-size:26px;line-height:1;flex:none}
.verdict .vmain{flex:1;min-width:0}
.verdict .vtitle{font-size:17px;font-weight:700;margin-bottom:3px}
.verdict.yes .vtitle{color:var(--same)} .verdict.no .vtitle{color:var(--enemy)}
.verdict .vsub{font-size:12.5px;color:var(--muted)}
.verdict .vmeta{font-size:12px;color:var(--muted);text-align:right;white-space:nowrap}
.cur-badge{display:inline-flex;align-items:center;gap:5px;padding:2px 9px;border-radius:20px;
           background:rgba(91,141,239,.16);color:var(--accent);font-size:11.5px;font-weight:700}
.match.is-current{border-color:var(--accent);box-shadow:0 0 0 1px var(--accent) inset}
/* 玩家标识 */
.pid-line{font-size:12px;color:var(--muted);margin-bottom:2px}
.pid-line code{font-family:ui-monospace,Consolas,monospace}
footer{max-width:1100px;margin:0 auto;padding:0 16px 40px;color:var(--muted);font-size:12px}
.foot-row{display:flex;align-items:center;justify-content:space-between;gap:12px;flex-wrap:wrap}
.quota{display:inline-flex;align-items:center;gap:8px;font-family:ui-monospace,Consolas,monospace;
       font-size:11.5px;white-space:nowrap}
.quota.low{color:#e0796f}
/* 数据源名（OpenDota / STRATZ）用弱化色，与后面的读数区分开，
   避免整行等权重导致读不出"哪个值属于哪个源"。 */
.quota #quota-label{color:var(--muted)}
.quota button{font-size:11px;padding:2px 9px;border-radius:5px}
/* 结果卡片：每个数据源各占一张卡片。
   ★ 两个源的结果**同屏共存**，各自往自己的节点里写，谁也不碰谁的 DOM，
   从结构上杜绝「查了 B 就把 A 冲掉」。切换数据源只决定下一次查询打哪个源，
   不会动任何已经渲染出来的结果。 */
.src-hint{margin:0;font-size:12.5px;color:var(--muted);line-height:1.65}
.src-hint b{color:var(--fg)}
.result-card{transition:border-color .15s}
/* 当前选中的数据源：只加一圈高亮边框，不做任何内容替换 */
.result-card.is-active{border-color:var(--accent)}
.rb-id{display:flex;align-items:center;gap:9px;flex-wrap:wrap}
.rb-badge{display:inline-block;padding:2px 9px;border-radius:5px;font-size:11.5px;
  font-weight:700;letter-spacing:.2px;white-space:nowrap}
.rb-badge.b-mate{background:rgba(91,141,239,.16);color:var(--accent)}
.rb-badge.b-scan{background:rgba(124,92,255,.16);color:var(--accent2)}
.rb-desc{font-size:12px;color:var(--muted)}
/* 「查询中…」小标：只标在当前源卡片上，另一张卡片完全不受影响 */
.rb-state{font-size:11.5px;color:var(--accent);white-space:nowrap}
.result-card .notice{margin:0 0 14px}
code{background:rgba(139,147,167,.16);padding:1px 5px;border-radius:4px;font-size:12px}
</style>
</head>
<body>
<header>
  <h1>Dota2 同局查询</h1>
  <p class="sub">查询两位玩家历史同局的比赛编号、时间、双方阵容与胜负</p>
</header>
<main>
  <section class="card" id="form-card">
    <div class="src-row">
      <span class="src-label">数据源</span>
      <div class="tabs src-tabs" id="mode-tabs">
        <button type="button" class="tab src-tab active" data-mode="mate" id="tab-mate" title="OpenDota：常规交集，速度快，但查不到关闭公开数据的账号">
          <span class="tab-name">OpenDota</span>
          <span class="tab-cost">快 · 约 30 秒</span>
        </button>
        <button type="button" class="tab src-tab" data-mode="scan" id="tab-scan" title="STRATZ：逐场补全身份，能查关闭公开数据的账号，耗时约 60 秒">
          <span class="tab-name">STRATZ</span>
          <span class="tab-cost">全 · 约 60 秒</span>
        </button>
      </div>
    </div>
    <form id="query-form">
      <div class="grid-2">
        <div><label for="player_a" id="label-a">玩家 1 ID</label>
          <input type="text" id="player_a" placeholder="steam id" autocomplete="off" required></div>
        <div id="wrap-b"><label for="player_b" id="label-b">玩家 2 ID</label>
          <input type="text" id="player_b" placeholder="steam id" autocomplete="off" required></div>
      </div>
      <div class="row">
        <p class="hint" style="margin:0" id="form-hint">不限时间，各拉双方最近 2000 场求交集，约 30 秒。</p>
        <button type="submit" id="submit-btn">开始查询</button>
      </div>
    </form>
  </section>

  <section class="card" id="progress-card" hidden>
    <div class="progress-head">
      <span id="progress-text">准备中…</span><span id="progress-percent">0%</span>
    </div>
    <div class="bar"><div class="bar-fill" id="progress-bar"></div></div>
    <div class="counters" id="progress-counters"></div>
  </section>

  <section class="card" id="error-card" hidden>
    <strong id="error-title">查询失败</strong>
    <p id="error-msg"></p>
    <p class="hint" id="error-hint" hidden></p>
  </section>

  <!-- 顶部提示：只说明「当前数据源有没有结果」，不承载任何结果内容。
       两个数据源的结果各自在下面各占一张卡片，同屏共存、互不覆盖。 -->
  <section class="card" id="src-hint-card" hidden>
    <p class="src-hint" id="src-hint"></p>
  </section>

  <!-- OpenDota（常规交集）结果卡片 -->
  <section class="card result-card" id="result-mate" data-src="mate" hidden>
    <div class="result-head">
      <div class="rb-id">
        <span class="rb-badge b-mate">OpenDota</span>
        <span class="rb-desc" id="desc-mate">常规交集 · 约 30 秒</span>
        <span class="rb-state" id="state-mate" hidden>查询中…</span>
      </div>
      <div class="actions" id="export-mate">
        <button type="button" id="btn-csv-mate">导出 CSV</button>
        <button type="button" id="btn-json-mate">导出 JSON</button>
      </div>
    </div>
    <div class="notice" id="notice-mate" hidden></div>
    <div class="summary" id="summary-mate"></div>
    <div id="matches-mate"></div>
  </section>

  <!-- STRATZ（深度扫描）结果卡片：与上面那张完全独立 -->
  <section class="card result-card" id="result-scan" data-src="scan" hidden>
    <div class="result-head">
      <div class="rb-id">
        <span class="rb-badge b-scan">STRATZ</span>
        <span class="rb-desc" id="desc-scan">逐场补全身份 · 约 60 秒</span>
        <span class="rb-state" id="state-scan" hidden>查询中…</span>
      </div>
      <div class="actions" id="export-scan">
        <button type="button" id="btn-csv-scan">导出 CSV</button>
        <button type="button" id="btn-json-scan">导出 JSON</button>
      </div>
    </div>
    <div class="notice" id="notice-scan" hidden></div>
    <div class="summary" id="summary-scan"></div>
    <div id="matches-scan"></div>
  </section>
</main>
<footer>
  <div class="foot-row">
    <span id="src-credit">
      数据来自 <a href="https://docs.opendota.com/" target="_blank" rel="noopener" style="color:var(--accent)">OpenDota API</a>
      · 仅可查询 Valve 公开的比赛数据
    </span>
    <span class="quota" id="quota">
      <span id="quota-label">OpenDota 配额</span>
      <span id="quota-text">读取中…</span>
      <button type="button" id="quota-refresh" title="主动查询会消耗 1 次 OpenDota 配额">刷新</button>
    </span>
  </div>
</footer>

<script>
(function(){
  "use strict";
  var $ = function(id){ return document.getElementById(id); };
  var ICON = "https://cdn.cloudflare.steamstatic.com/apps/dota2/images/dota_react/heroes/";

  var form = $("query-form"), submitBtn = $("submit-btn");
  var progressCard = $("progress-card"), errorCard = $("error-card");
  var pollTimer = null, currentJob = null;

  // 当前模式：mate = 常规查询（OpenDota 交集）
  //           scan = 深度扫描（STRATZ 逐场补全身份，用于隐藏账号）
  var MODE = "mate";
  // 服务端是否配置了 STRATZ_TOKEN（决定深度扫描是否可用）
  var STRATZ_OK = false;
  // 服务启动标识（服务端进程注入，每次重启都会变）。
  // 前端跟 localStorage 里记的对比：不一致说明服务重启过，清空记住的玩家 ID。
  var BOOT_ID = "__BOOT_ID__";

  // 当前查询双方的昵称。放在模块作用域，供 heroCell / lineupHtml /
  // playerCardHtml 等子函数引用——这些函数在 renderMate 之外，
  // 拿不到那里的局部变量。查询前为 null，渲染时由 renderMate 赋值。
  var NAME_A = null, NAME_B = null;
  function nameA(){ return NAME_A || "玩家 A"; }
  function nameB(){ return NAME_B || "玩家 B"; }

  // ---- 两个数据源各自的结果卡片 ----
  // ★ 核心诉求（用户明确要求）：不同数据源查出来的数据，在 UI 上不能相互覆盖。
  //   做法：OpenDota 与 STRATZ 各占一张独立的卡片，各自只往自己的节点里写，
  //   谁也不碰谁的 DOM —— 从结构上杜绝覆盖，而不是靠「切回来再贴一遍」。
  //   切换数据源只决定「下一次查询打哪个源」，已渲染的结果一律不动。
  var SRC_NAME = { mate: "OpenDota", scan: "STRATZ" };
  var SRC_ACTION = { mate: "开始查询", scan: "开始深度扫描" };

  var CARDS = {
    mate: {
      root: $("result-mate"), summary: $("summary-mate"), matches: $("matches-mate"),
      export: $("export-mate"), csv: $("btn-csv-mate"), json: $("btn-json-mate"),
      notice: $("notice-mate"), state: $("state-mate"),
    },
    scan: {
      root: $("result-scan"), summary: $("summary-scan"), matches: $("matches-scan"),
      export: $("export-scan"), csv: $("btn-csv-scan"), json: $("btn-json-scan"),
      notice: $("notice-scan"), state: $("state-scan"),
    },
  };

  // 每个源各记一份自己的状态：
  //   key —— 这份结果属于哪两位玩家（输入框换人后旧结果不再适用）
  //   job —— 该次查询的后端 job_id，导出 CSV/JSON 时用
  // ★ 关键：job 分源存放。以前共用一个 currentJob，被后来的查询一改，
  //   先前那份结果的导出按钮就指到错的 job 上去了。
  var SRC = { mate: { key: null, job: null }, scan: { key: null, job: null } };

  // 玩家对指纹：把两个 ID 归一化后拼起来，用于判断结果是否还适用于当前输入
  function pairKey(a, b){
    return String(a || "").trim() + "|" + String(b || "").trim();
  }

  function esc(s){
    return String(s == null ? "" : s).replace(/[&<>"']/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c];
    });
  }

  // ---- 提示条（按数据源分开，挂在各自卡片内部）----
  // 提示是「这份结果的附带说明」（如：某方对局列表为空、结论不可信），
  // 因此必须跟着结果走 —— 放在各自卡片里，就不会出现「STRATZ 的警示
  // 挂在 OpenDota 的结果上」这种串台。
  function showNotice(src, kind, title, body, actions){
    var card = CARDS[src];
    if (!card) return;
    var html = "<strong>" + esc(title) + "</strong>" + body;
    if (actions && actions.length){
      html += '<div class="act">' + actions.map(function(a, i){
        return '<button type="button" data-act="' + i + '">' + esc(a.label) + "</button>";
      }).join("") + "</div>";
    }
    card.notice.innerHTML = html;
    card.notice.className = "notice " + (kind || "info");
    card.notice.hidden = false;
    if (actions && actions.length){
      // 逐个绑定，避免 innerHTML 里的 onclick 被 CSP 拦掉
      var btns = card.notice.querySelectorAll("button[data-act]");
      for (var i = 0; i < btns.length; i++){
        (function(btn){
          var a = actions[parseInt(btn.getAttribute("data-act"), 10)];
          btn.onclick = function(){ a.run(); };
        })(btns[i]);
      }
    }
  }
  function hideNotice(src){
    var card = CARDS[src];
    if (!card) return;
    card.notice.hidden = true;
    card.notice.innerHTML = "";
  }

  function showProgress(j){
    progressCard.hidden = false;
    errorCard.hidden = true;
    var p = Math.max(0, Math.min(100, j.percent || 0));
    $("progress-text").textContent = j.stage_text || "查询中…";
    $("progress-percent").textContent = p + "%";
    $("progress-bar").style.width = p + "%";
    var bits = [];
    if (j.elapsed != null) bits.push("已用时 " + j.elapsed + "s");
    if (j.a_count) bits.push(nameA() + " 对局 " + j.a_count);
    if (j.b_count) bits.push(nameB() + " 对局 " + j.b_count);
    if (j.common)  bits.push("同局 " + j.common);
    $("progress-counters").textContent = bits.join("　·　");
  }

  function showError(msg, hint){
    progressCard.hidden = true;
    errorCard.hidden = false;
    // ★ 不隐藏任何结果卡片：查询失败只是这次没拿到新数据，
    //   已经查出来的结果（本源的旧结果 / 另一个源的结果）都该留着。
    setCardBusy(MODE, false);
    $("error-msg").textContent = msg || "未知错误";
    var h = $("error-hint");
    if (hint){ h.textContent = hint; h.hidden = false; } else { h.hidden = true; }
    submitBtn.disabled = false;
  }

  // ---- 结果卡片的写入口 ----
  // 每个数据源只写自己那张卡片，两个源之间没有任何共享节点 —— 这是
  // 「不相互覆盖」的结构性保证。
  function fillCard(src, bodyHtml, summaryHtml, key, job){
    var card = CARDS[src];
    card.summary.innerHTML = summaryHtml;
    card.matches.innerHTML = bodyHtml;
    card.root.hidden = false;
    card.export.classList.remove("hidden");
    SRC[src].key = key;
    SRC[src].job = job;
    setCardBusy(src, false);
  }

  function clearCard(src){
    var card = CARDS[src];
    card.root.hidden = true;
    card.summary.innerHTML = "";
    card.matches.innerHTML = "";
    card.export.classList.add("hidden");
    hideNotice(src);
    setCardBusy(src, false);
    SRC[src].key = null;
    SRC[src].job = null;
  }

  // 「查询中…」标记：只加在当前源的卡片标题栏上。
  // ★ 刻意保留卡片里的旧内容不删 —— 万一这次查询失败，上次的结果还在。
  function setCardBusy(src, busy){
    var card = CARDS[src];
    if (card.state) card.state.hidden = !busy;
  }

  // 把卡片滚进视野（新结果出来了，让用户直接看到它）
  function scrollToCard(src){
    var el = CARDS[src].root;
    if (el && typeof el.scrollIntoView === "function"){
      try { el.scrollIntoView({ behavior: "smooth", block: "start" }); } catch (e) {}
    }
  }

  // 布局刷新：只显示「当前数据源」那张结果卡片，另一个源的卡片隐藏。
  // ★ 两个源的结果各自留在自己的卡片里（DOM 不删、状态不丢），
  //   只是不在屏幕上同时出现 —— 这样既满足「选中谁只看谁」，
  //   也满足「互不覆盖 / 切回去原样还在」。清空的唯一时机是
  //   重新搜索（见 submit）或刷新页面（整页重载）。
  function refreshLayout(){
    for (var s in CARDS){
      if (!CARDS.hasOwnProperty(s)){
        continue;
      }
      var active = (s === MODE);
      CARDS[s].root.classList.toggle("is-active", active);
      // 显示规则：只有「当前数据源」且「有查过结果」的卡片才显示。
      // 空卡片（从没查过 / 被清空）一律不显示，避免首屏冒出一张空白卡片。
      // 非当前数据源的卡片：隐藏但保留内容（DOM 不删、key/job 不丢），
      // 切回去原样还在 —— 既满足「选中谁只看谁」，也满足「互不覆盖」。
      var hasContent = (SRC[s].key != null);
      CARDS[s].root.hidden = !(active && hasContent);
    }
    updateSrcHint();
  }

  function updateSrcHint(){
    var box = $("src-hint-card"), el = $("src-hint");
    // 当前卡片没有结果（hidden）时才提示去查询；另一张卡片此时本就
    // 不显示，所以不再写「已保留在下方」之类的话（会误导，因为看不到）。
    if (CARDS[MODE].root.hidden){
      el.innerHTML = "当前数据源 <b>" + SRC_NAME[MODE] + "</b> 还没有结果，"
                   + "点「" + SRC_ACTION[MODE] + "」查询。";
      box.hidden = false;
    } else {
      box.hidden = true;
      el.innerHTML = "";
    }
  }

  // 导出按钮：按源各绑一次，点击时才去读该源自己的 job_id。
  // ★ 不能用共享变量 —— 那样后来查询会把先前结果的导出目标改掉。
  (function bindExports(){
    var list = ["mate", "scan"];
    for (var i = 0; i < list.length; i++){
      (function(s){
        CARDS[s].csv.onclick = function(){
          if (SRC[s].job) window.location.href = "/api/export/" + SRC[s].job + ".csv";
        };
        CARDS[s].json.onclick = function(){
          if (SRC[s].job) window.location.href = "/api/export/" + SRC[s].job + ".json";
        };
      })(list[i]);
    }
  })();

  // ---- 历史同局查询结果 ----
  function renderMate(d){
    progressCard.hidden = true;
    errorCard.hidden = true;
    submitBtn.disabled = false;

    var s = d.summary || {};
    var pa = (d.player_a && d.player_a.personaname) || "玩家 1";
    var pb = (d.player_b && d.player_b.personaname) || "玩家 2";
    NAME_A = pa; NAME_B = pb;   // 供 heroCell / lineupHtml / playerCardHtml 使用
    var me = d.player_a || {}, mate = d.player_b || {};
    var info = d.info || {};

    // 结论横幅
    // 玩家一律用昵称指代（pa / pb），不用「A/B」或「我方/队友」，
    // 因为读的人未必分得清哪个是哪个。
    var vHtml;
    if (s.total > 0){
      var sub = '其中同队 ' + (s.same||0) + ' 场，敌对 ' + (s.enemy||0) + ' 场';
      if (s.same){
        sub += '　·　同队时 ' + esc(pa) + ' 胜率 ' + s.same_a_winrate + '%'
             + '（' + s.same_a_win + ' 胜 ' + s.same_a_loss + ' 负）';
      }
      if (s.enemy){
        sub += '　·　敌对时 ' + esc(pa) + ' 胜率 ' + s.enemy_a_winrate + '%'
             + '（' + s.enemy_a_win + ' 胜 ' + s.enemy_a_loss + ' 负）';
      }
      vHtml = '<div class="verdict yes"><span class="vicon">✓</span><div class="vmain">'
        + '<div class="vtitle">曾经同局过 ' + s.total + ' 场'
        + '<span class="src-badge">数据源 OpenDota</span></div>'
        + '<div class="vsub">' + sub + '</div></div>'
        + '<div class="vmeta">' + esc(pa) + '<br>×<br>' + esc(pb) + '</div></div>';
    } else {
      vHtml = '<div class="verdict no"><span class="vicon">✕</span><div class="vmain">'
        + '<div class="vtitle">在可查询范围内没有找到共同对局'
        + '<span class="src-badge">数据源 OpenDota</span></div>'
        + '<div class="vsub">已比对 ' + esc(pa) + ' 与 ' + esc(pb) + ' 各最近 '
        + (info.limit||2000) + ' 场对局</div>'
        + '</div><div class="vmeta">' + esc(pa) + '<br>×<br>' + esc(pb) + '</div></div>';
    }

    // 身份信息行（含 SteamID64）
    var idLine = '<div class="pid-line" style="margin-bottom:14px">'
      + esc(pa) + ' <code>' + esc(me.steamid64||'') + '</code>'
      + '（account_id ' + esc(me.account_id) + '）'
      + '　·　' + esc(pb) + ' <code>' + esc(mate.steamid64||'') + '</code>'
      + '（account_id ' + esc(mate.account_id) + '）'
      + '</div>';

    // 汇总数字（用昵称代替「我方/队友」）
    var items = [["共同对局", s.total||0], ["同队", s.same||0], ["敌对", s.enemy||0]];
    if (s.same) items.push(["同队时 " + pa + " 胜率", s.same_a_winrate + "%"]);
    if (s.enemy) items.push(["敌对时 " + pa + " 胜率", s.enemy_a_winrate + "%"]);
    items.push([pa + " 样本", info.a_total||0]);
    items.push([pb + " 样本", info.b_total||0]);
    var smHtml = items.map(function(it){
      return '<div><span class="k">' + esc(it[0]) + '</span><b>' + esc(it[1]) + '</b></div>';
    }).join("");

    var trunc = "";
    if (info.a_truncated || info.b_truncated){
      trunc = '<p class="hint">注：'
        + (info.a_truncated ? esc(pa) : '')
        + (info.a_truncated && info.b_truncated ? ' 与 ' : '')
        + (info.b_truncated ? esc(pb) : '')
        + ' 已取满 ' + (info.limit || 2000) + ' 场，更早的交集可能未覆盖。</p>';
    }

    var bodyHtml = vHtml + idLine
      + (d.results && d.results.length ? d.results.map(matchHtml).join("") : "")
      + trunc;

    // ★ 只写 mate 自己那张卡片（OpenDota）。STRATZ 卡片的 DOM 一个字节都不碰，
    //   两张卡片同屏共存 —— 这正是「不同数据源的结果不相互覆盖」的落点。
    fillCard("mate", bodyHtml, smHtml,
             pairKey($("player_a").value, $("player_b").value), currentJob);
    refreshLayout();
    scrollToCard("mate");

    // ★ 若某一方的 OpenDota 对局列表为空，那么「没有共同对局」这个结论
    //   是不可信的 —— 该玩家的历史根本没被索引。必须显式提示，
    //   否则用户会把「查不到」误读成「确实没同局过」。
    maybeWarnEmpty(pa, pb, info, d);
  }

  // 两方中谁在 OpenDota 无对局数据？返回 [名, id, 是否为空] 列表
  function emptySides(pa, pb, info, d){
    var out = [];
    if (info.a_opendota_empty) out.push([pa, (d.player_a||{}).account_id]);
    if (info.b_opendota_empty) out.push([pb, (d.player_b||{}).account_id]);
    return out;
  }

  // 提示写进 mate 自己的卡片里（OpenDota 的结论说明跟着 OpenDota 的结果走）
  function maybeWarnEmpty(pa, pb, info, d){
    var empty = emptySides(pa, pb, info, d);
    if (!empty.length){
      // ★ 必须主动隐藏，不能「什么都不做」：
      //   用户先查一个隐藏账号（弹了警示），再查两个正常账号，
      //   若这里不清理，警示条会残留，让人误以为本次结果也不可信。
      hideNotice("mate");
      return;
    }
    var names = empty.map(function(e){ return esc(e[0]); }).join("、");
    // ★ 标题已写明「结果不完整：xx 的对局数据不可用」，正文不再重复玩家名，
    //   否则同一句话在标题和正文里出现两遍，读起来很啰嗦。
    var body = "<span>因此本次结果<b>无法说明</b>二人是否同局过。</span>"
      + '<div style="margin-top:8px;color:var(--muted);font-size:12.5px">'
      + "常见原因：对方在 Dota2 客户端关闭了「公开比赛数据」。"
      + "此时 Valve 不再发布其对局列表（但比赛内仍能看到他），"
      + "任何按 account_id 求交集的查询都查不到。</div>";
    var actions = [];
    if (STRATZ_OK){
      actions.push({
        label: "改用 STRATZ 数据源重查（推荐）",
        run: function(){
          switchMode("scan");
          // STRATZ 扫描需要「公开的一方」作基准，故把非空的那方填为基准
          var baseIsA = !info.a_opendota_empty;
          $("player_a").value = baseIsA ? (d.player_a.account_id) : (d.player_b.account_id);
          $("player_b").value = baseIsA ? (d.player_b.account_id) : (d.player_a.account_id);
          hideNotice("mate");
          form.requestSubmit ? form.requestSubmit() : form.dispatchEvent(new Event("submit"));
        }
      });
    }
    showNotice("mate", "warn", "⚠ 结果不完整：" + names + " 的对局数据不可用", body, actions);
  }

  function heroIcon(slug, name){
    if (!slug) return "";
    return '<img class="hero-icon" src="' + ICON + esc(slug) + '.png" alt="' +
           esc(name) + '" loading="lazy" onerror="this.remove()">';
  }

  // ---- 阵容玩家卡片 ----
  function playerCardHtml(p){
    var sid = p.steamid64 || "";
    var name = p.account_id ? ("account_id " + p.account_id) : "无账号（匿名/机器人）";
    return '<div class="pcard' + (p.is_target ? " is-target" : "") + '">'
      + heroIcon(p.hero_slug, p.hero)
      + '<div class="pmeta">'
      +   '<div class="prow2"><span class="pname">' + esc(p.hero) + '</span>'
      +     (p.is_target ? '<span class="badge">目标玩家</span>' : '') + '</div>'
      +   '<div class="psid">' + esc(sid || "—") + '</div>'
      +   '<div class="prow2">'
      +     (p.steam_url ? '<a href="' + esc(p.steam_url) + '" target="_blank" rel="noopener">Steam 主页</a>' : '')
      +     (p.opendota_url ? '<a href="' + esc(p.opendota_url) + '" target="_blank" rel="noopener">OpenDota</a>' : '')
      +   '</div>'
      + '</div></div>';
  }

  function teamBlockHtml(title, cls, list){
    return '<div class="team-block ' + cls + '"><h4>' + title + '（' + list.length + ' 人）</h4>'
      + '<div class="plist">' + list.map(playerCardHtml).join("") + '</div></div>';
  }


  function relClass(r){
    if (r.same_team === true) return "same";
    if (r.same_team === false) return "enemy";
    return "";
  }
  function relText(r){
    if (r.same_team === true) return "同队";
    if (r.same_team === false) return "敌对";
    return "未知";
  }
  function resCell(v){
    if (v === true)  return '<span class="res win">胜</span>';
    if (v === false) return '<span class="res lose">负</span>';
    return '<span class="res na">未知</span>';
  }
  function heroCell(r, key, side){
    var hid = r[key + "_hero_id"];
    var slug = "";
    var name = hid == null ? "—" : String(hid);
    var pools = [r.radiant_lineup || [], r.dire_lineup || []];
    for (var i = 0; i < pools.length; i++){
      for (var k = 0; k < pools[i].length; k++){
        var p = pools[i][k];
        if (p["is_" + key.toUpperCase()] || (hid != null && p.hero_id === hid)) {
          slug = p.hero_slug || ""; name = p.hero || name;
        }
      }
    }
    var isA = key === "a";
    var who = isA ? nameA() : nameB();
    return '<span class="who">' + esc(who) + " · " + esc(name) + '</span>'
         + '<span class="badge' + (isA ? "" : " b") + '">' + esc(who) + '</span>'
         + heroIcon(slug, name)
         + '<span class="side">' + esc(side || "") + '</span>'
         + resCell(r[key + "_win"]);
  }

  function lineupHtml(title, cls, list){
    var lis = (list || []).map(function(p){
      var who = p.is_A ? nameA() : p.is_B ? nameB() : "";
      var badge = who ? '<b style="color:var(--accent)">' + esc(who) + '</b>' : "";
      var cls2 = p.is_A ? " is-A" : p.is_B ? " is-B" : "";
      return '<li class="' + cls2.trim() + '">' + heroIcon(p.hero_slug, p.hero) +
             '<span>' + esc(p.hero) + '</span>' + badge + '</li>';
    }).join("");
    return '<div class="lineup ' + cls + '"><h4>' + title + '</h4><ul>' + lis + '</ul></div>';
  }

  function matchHtml(r){
    var mins = Math.round((r.duration || 0) / 60);
    var cur = r.is_current
      ? '<span class="cur-badge"><span class="dot"></span>当前对局</span>' : '';
    return '<article class="match ' + relClass(r) + (r.is_current ? ' is-current' : '') + '">'
      + '<div class="match-head">'
      +   '<span class="tag">' + relText(r) + '</span>'
      +   cur
      +   '<a class="mid" href="https://www.opendota.com/matches/' + esc(r.match_id) +
            '" target="_blank" rel="noopener">' + esc(r.match_id) + '</a>'
      +   '<span class="time">' + esc(r.time_str) + '</span>'
      +   '<span class="dur">' + mins + ' 分钟</span>'
      + '</div>'
      + '<div class="players">'
      +   '<div class="prow is-A">' + heroCell(r, "a", r.a_team) + '</div>'
      +   '<div class="prow is-B">' + heroCell(r, "b", r.b_team) + '</div>'
      + '</div>'
      + '<div class="lineups">'
      +   lineupHtml("天辉", "radiant", r.radiant_lineup)
      +   lineupHtml("夜魇", "dire", r.dire_lineup)
      + '</div>'
      + '</article>';
  }

  function poll(){
    if (!currentJob) return;
    fetch("/api/progress/" + currentJob)
      .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
      .then(function(res){
        if (!res.ok){ stopPoll(); showError(res.j.error || "任务不存在或已过期"); return; }
        var j = res.j;
        showProgress(j);
        if (j.status === "error"){ stopPoll(); showError(j.error, j.hint); loadQuota(false); return; }
        if (j.status === "done"){
          stopPoll();
          fetch("/api/result/" + currentJob)
            .then(function(r){ return r.json(); })
            .then(function(d){
              if (!d.ok){ showError(d.error, d.hint); return; }
              // 深度扫描与常规查询的记录结构一致，但结论文案与提示不同
              if (d.kind === "scan") renderScan(d); else renderMate(d);
            })
            .catch(function(e){ showError("获取结果失败: " + e.message); });
          // 本次查询已消耗若干次配额，服务端读数已刷新，同步到页脚
          loadQuota(false);
        }
      })
      .catch(function(e){ stopPoll(); showError("进度查询失败: " + e.message); });
  }

  // ---- 深度扫描结果 ----
  // 与常规查询共用 matchHtml 渲染，差别在结论横幅与说明文案：
  // 扫描是以「基准玩家的全部对局」为范围，命中数即两人同局数。
  function renderScan(d){
    progressCard.hidden = true;
    errorCard.hidden = true;
    submitBtn.disabled = false;

    var s = d.summary || {};
    var info = d.info || {};
    var pa = (d.player_a && d.player_a.personaname) || "基准玩家";
    var pb = (d.player_b && d.player_b.personaname) || "目标玩家";
    NAME_A = pa; NAME_B = pb;
    var me = d.player_a || {}, mate = d.player_b || {};

    var vHtml;
    if (s.total > 0){
      var sub = "其中同队 " + (s.same||0) + " 场，敌对 " + (s.enemy||0) + " 场";
      if (s.same){
        sub += "　·　同队时 " + esc(pa) + " 胜率 " + s.same_a_winrate + "%"
             + "（" + s.same_a_win + " 胜 " + s.same_a_loss + " 负）";
      }
      if (s.enemy){
        sub += "　·　敌对时 " + esc(pa) + " 胜率 " + s.enemy_a_winrate + "%"
             + "（" + s.enemy_a_win + " 胜 " + s.enemy_a_loss + " 负）";
      }
      vHtml = '<div class="verdict yes"><span class="vicon">✓</span><div class="vmain">'
        + '<div class="vtitle">曾经同局过 ' + s.total + ' 场'
        + '<span class="src-badge">数据源 STRATZ</span></div>'
        + '<div class="vsub">' + sub + '</div></div>'
        + '<div class="vmeta">' + esc(pa) + '<br>×<br>' + esc(pb) + '</div></div>';
    } else {
      vHtml = '<div class="verdict no"><span class="vicon">✕</span><div class="vmain">'
        + '<div class="vtitle">没有找到共同对局'
        + '<span class="src-badge">数据源 STRATZ</span></div>'
        + '<div class="vsub">已扫描 ' + esc(pa) + ' 的 ' + (info.scanned||0) + ' 场对局，'
        + '逐场补全 10 人身份比对</div>'
        + '</div><div class="vmeta">' + esc(pa) + '<br>×<br>' + esc(pb) + '</div></div>';
    }

    var idLine = '<div class="pid-line" style="margin-bottom:14px">'
      + esc(pa) + ' <code>' + esc(me.steamid64||'') + '</code>'
      + '（account_id ' + esc(me.account_id) + '）'
      + '　·　' + esc(pb) + ' <code>' + esc(mate.steamid64||'') + '</code>'
      + '（account_id ' + esc(mate.account_id) + '）'
      + '</div>';

    var items = [
      ["共同对局", s.total||0], ["同队", s.same||0], ["敌对", s.enemy||0],
      ["扫描场次", info.scanned||0], ["STRATZ 调用", info.calls||0],
    ];
    if (s.same) items.push(["同队时 " + pa + " 胜率", s.same_a_winrate + "%"]);
    if (s.enemy) items.push(["敌对时 " + pa + " 胜率", s.enemy_a_winrate + "%"]);
    items.push([pa + " 总场次", info.base_total||0]);
    var smHtml = items.map(function(it){
      return '<div><span class="k">' + esc(it[0]) + '</span><b>' + esc(it[1]) + '</b></div>';
    }).join("");

    var trunc = "";
    if (info.truncated){
      // 明确告知「还有多少历史没覆盖」，否则用户会以为这是全部结果。
      var more = (info.base_total || 0) - (info.scanned || 0);
      trunc = '<p class="hint">注：本次只扫描了最近 ' + (info.scanned || 0) + ' 场'
            + (more > 0 ? '，该玩家还有约 ' + more + ' 场更早的对局未覆盖' : '')
            + '。如需覆盖全部历史，可在查询参数中传 max_matches=0。</p>';
    }

    var bodyHtml = vHtml + idLine
      + (d.results && d.results.length ? d.results.map(matchHtml).join("") : "")
      + trunc;

    // ★ 与 renderMate 同理，但只写 scan 自己那张卡片。
    //   两张卡片各自独立，同屏共存 —— 查 STRATZ 不会动 OpenDota 那份结果，
    //   反过来也一样。
    fillCard("scan", bodyHtml, smHtml,
             pairKey($("player_a").value, $("player_b").value), currentJob);
    refreshLayout();
    scrollToCard("scan");

    // 扫描成功找到同局 —— 说明该玩家虽在 OpenDota 无索引，
    // 但确实与基准玩家同局过。给出解释，避免用户困惑于两处结果不一致。
    // ★ 这条提示跟着 STRATZ 的结果走，只写进 scan 卡片，不碰 OpenDota 那张。
    if (info.target_opendota_empty){
      var body = "<span><b>" + esc(pb) + "</b> 在 OpenDota 没有公开对局记录，"
        + "常规查询查不到他 —— 但本次深度扫描从比赛维度补全了他的身份。</span>"
        + '<div style="margin-top:8px;color:var(--muted);font-size:12.5px">'
        + "深度扫描以 " + esc(pa) + " 的全部对局为范围，逐场读取该场 10 人的真实身份，"
        + "因此能发现常规查询漏掉的同局。</div>";
      showNotice("scan", s.total > 0 ? "info" : "warn",
                 s.total > 0 ? "✓ 已通过深度扫描找到同局记录" : "扫描完成",
                 body, []);
    } else {
      hideNotice("scan");
    }
  }

  function stopPoll(){ if (pollTimer){ clearInterval(pollTimer); pollTimer = null; } }

  // ---- 记住上次输入的双方 ID ----
  // 用 localStorage 持久化，刷新页面/重开浏览器后自动回填。
  // 读不到（首次访问 / 隐私模式禁用存储）时保留 HTML 里的默认值。
  var LS_A = "d2d.player_a", LS_B = "d2d.player_b", LS_MODE = "d2d.mode";
  var LS_BOOT = "d2d.boot_id";

  function lsGet(k){
    try { return window.localStorage.getItem(k); } catch (e) { return null; }
  }
  function lsSet(k, v){
    try { window.localStorage.setItem(k, v); } catch (e) { /* 存不了就算了 */ }
  }

  function restoreInputs(){
    // ★ 服务重启过（或首次访问）：BOOT_ID 对不上，清空记住的双方 ID。
    //   仅刷新页面（服务未重启）时 BOOT_ID 不变，仍会正常回填。
    if (lsGet(LS_BOOT) !== BOOT_ID){
      lsSet(LS_A, ""); lsSet(LS_B, "");
      lsSet(LS_BOOT, BOOT_ID);
      $("player_a").value = "";
      $("player_b").value = "";
      return;
    }
    var a = lsGet(LS_A), b = lsGet(LS_B);
    // 只在有历史值且非空时才覆盖，避免把默认值清掉
    if (a) $("player_a").value = a;
    if (b) $("player_b").value = b;
  }

  restoreInputs();

  // ---- OpenDota 剩余配额展示 ----
  // 读数来自服务端：它在每次网络请求时旁路抓取响应头
  // （X-Rate-Limit-Remaining-Day / -Minute），所以页脚的值会随查询自动更新。
  // 点「刷新」会主动探测一次——★ 代价是消耗 1 次配额，故按钮上标了提示。
  var quotaBox = $("quota"), quotaText = $("quota-text"), quotaBtn = $("quota-refresh");
  var quotaLabel = $("quota-label");

  // ★ 页脚两处内容都随数据源变化，不能写死：
  //   1) 归属说明（数据来自哪个 API）
  //   2) 配额读数（OpenDota 与 STRATZ 是两套独立的配额体系）
  //   代价是 STRATZ 模式下点「刷新」会真的消耗 1 次 STRATZ 调用（无轻量探测接口，
  //   用一次玩家查询代替），故按钮 title 也要跟着换。
  function renderQuota(q){
    var isScan = (MODE === "scan");
    if (!q || q.remaining_day == null){
      quotaText.textContent = (isScan
        ? "暂无数据（扫描一次后自动更新）"
        : "暂无数据（查询一次后自动更新）");
      quotaBox.classList.remove("low");
      return;
    }
    var s = "今日剩余 " + q.remaining_day;
    if (q.limit_day) s += " / " + q.limit_day;
    if (!isScan && q.remaining_minute != null){
      // STRATZ 侧有更细的秒/分/时/日四档，但页脚空间有限，
      // 只展示日剩余即可，其余留待 /api/stratz 排错时查。
      s += "　本分钟剩余 " + q.remaining_minute;
      if (q.limit_minute) s += " / " + q.limit_minute;
    }
    quotaText.textContent = s;
    // 日剩余低于 10% 时标红，提醒用户配额将尽
    quotaBox.classList.toggle("low",
      q.day_percent != null && q.day_percent < 10);
  }

  function loadQuota(refresh){
    var isScan = (MODE === "scan");
    var url = (isScan ? "/api/stratz" : "/api/quota")
            + (!isScan && refresh ? "?refresh=1" : "");
    return fetch(url).then(function(r){ return r.json(); })
      .then(function(q){ if (q && q.ok) renderQuota(q); })
      .catch(function(){ /* 读不到配额不影响主功能，静默忽略 */ });
  }

  quotaBtn.addEventListener("click", function(){
    quotaBtn.disabled = true;
    loadQuota(true).then(function(){ quotaBtn.disabled = false; });
  });

  // 页脚归属说明 + 配额标签：按当前数据源切换
  // 注意要与页脚里原本的 <a href> 链接目标一致，否则点进去会到错站点。
  var CREDIT = {
    mate: '数据来自 <a href="https://docs.opendota.com/" target="_blank"'
        + ' rel="noopener" style="color:var(--accent)">OpenDota API</a>'
        + ' · 仅可查询 Valve 公开的比赛数据',
    scan: '数据来自 <a href="https://stratz.com/" target="_blank"'
        + ' rel="noopener" style="color:var(--accent)">STRATZ API</a>'
        + ' · 仅可查询 Valve 公开的比赛数据'
  };

  function refreshCredit(){
    var isScan = (MODE === "scan");
    $("src-credit").innerHTML = CREDIT[isScan ? "scan" : "mate"];
    quotaLabel.textContent = isScan ? "STRATZ 配额" : "OpenDota 配额";
    quotaBtn.title = isScan
      ? "主动探测会消耗 1 次 STRATZ 调用（无轻量探测接口）"
      : "主动查询会消耗 1 次 OpenDota 配额";
  }

  // ★ 这里不预先 loadQuota()：启动时 switchMode() 一定会被调用一次
  //   （由 /api/stratz 探测回调触发），而 switchMode 内部已经会
  //   refreshCredit() + loadQuota()。预加载只会多发一次无谓请求。

  // ---- 模式切换 ----
  // mate：常规查询（OpenDota 交集，快，但查不到隐藏账号）
  // scan：深度扫描（STRATZ 逐场补全身份，能查隐藏账号，慢且耗 STRATZ 配额）
  function switchMode(m){
    MODE = m;
    var tabs = $("mode-tabs").querySelectorAll(".tab");
    for (var i = 0; i < tabs.length; i++){
      tabs[i].classList.toggle("active", tabs[i].getAttribute("data-mode") === m);
    }
    if (m === "scan"){
      // ★ 与 OpenDota 模式保持一致的输入标签，降低切换时的认知负担。
      //   （扫描在内部仍以「玩家 1 为基准、玩家 2 为目标」运行。）
      $("label-a").textContent = "玩家 1 ID";
      $("label-b").textContent = "玩家 2 ID";
      $("form-hint").textContent =
        "以玩家 1 最近 2000 场（与 OpenDota 覆盖范围一致）为范围，"
        + "逐场补全双方在场的 10 人身份，找出玩家 2 的所有同局。"
        + "约 100 场/请求，2000 场约 60 秒。";
      submitBtn.textContent = "开始深度扫描";
    } else {
      $("label-a").textContent = "玩家 1 ID";
      $("label-b").textContent = "玩家 2 ID";
      $("form-hint").textContent =
        "不限时间，各拉双方最近 2000 场求交集，约 30 秒。";
      submitBtn.textContent = "开始查询";
    }
    // ★ 页脚两处（归属说明 + 配额读数）依赖 MODE，必须在 MODE 赋值之后刷新。
    //   顺序也有讲究：先 refreshCredit 定好标签，再 loadQuota 重新取数，
    //   否则会先按旧标签渲染一次，闪一下才对。
    refreshCredit();
    loadQuota(false);
    lsSet(LS_MODE, m);
    // ※ 这里**不**清空、也不隐藏任何结果。两个数据源的结果各占一张卡片、
    //   同屏共存，切标签只是决定「下一次查询打哪个源」，对已渲染的内容
    //   零影响（只做高亮 + 更新顶部提示，见 refreshLayout）。
    refreshLayout();
  }

  var tabsEl = $("mode-tabs");
  tabsEl.addEventListener("click", function(ev){
    // ★ 不能只看 ev.target：标签内部有 <span class="tab-name"> 等子元素，
    //   用户点到的往往是 span，而 data-mode 挂在 button 上。
    //   用 closest 向上找最近的 .tab 祖先，才能拿到 data-mode。
    //   （老版本标签是纯文本 button，直接读 ev.target 就行，改结构后必须这样取。）
    var t = ev.target;
    var btn = t && t.closest ? t.closest(".tab") : null;
    if (!btn){ return; }
    if (btn.disabled){ return; }
    var m = btn.getAttribute("data-mode");
    if (m){ switchMode(m); }
  });

  // 启动时探测 STRATZ 可用性，据此决定「深度扫描」标签是否可点
  fetch("/api/stratz").then(function(r){ return r.json(); }).then(function(s){
    STRATZ_OK = !!(s && s.enabled);
    var tab = $("tab-scan");
    if (!STRATZ_OK){
      tab.disabled = true;
      tab.title = "服务器未配置 STRATZ_TOKEN，STRATZ 数据源不可用";
    }
    // 恢复上次选择的数据源。★ 必须在 STRATZ 探测之后：
    // 若上次选的是 STRATZ 而本次服务端未配令牌，要退回 OpenDota，
    // 否则会停在一个被禁用、点了没反应的标签上。
    var last = lsGet(LS_MODE);
    if (last === "scan" && !STRATZ_OK) last = "mate";
    if (last === "scan" || last === "mate") switchMode(last);
    else switchMode("mate");
  }).catch(function(){
    // 探测失败就当作不可用，不影响常规查询
    switchMode(lsGet(LS_MODE) === "scan" && STRATZ_OK ? "scan" : "mate");
  });

  form.addEventListener("submit", function(ev){
    ev.preventDefault();
    stopPoll();
    var pa = $("player_a").value.trim();
    var pb = $("player_b").value.trim();

    if (!pa || !pb){
      showError(MODE === "scan" ? "基准玩家与目标玩家的 ID 都不能为空。"
                                : "两位玩家的 ID 都不能为空。");
      return;
    }

    // 提交即记住，这样即使查询失败下次也不用重打
    lsSet(LS_A, pa);
    lsSet(LS_B, pb);
    submitBtn.disabled = true;
    errorCard.hidden = true;
    // 本次是重查「当前数据源」：只清它自己的旧提示（旧结果先留着，
    // 万一这次失败，上次的结果还在，不会白查一次）。
    hideNotice(MODE);
    // ★ 另一个数据源的卡片一律不动 —— 那正是要留着做对比的。
    //   唯一的例外：输入框里的人换了，那份结果就不再适用于当前查询
    //   （它属于上一组玩家），此时才清掉它；同一组玩家则原样保留。
    var key = pairKey(pa, pb);
    for (var s in SRC){
      if (SRC.hasOwnProperty(s) && s !== MODE && SRC[s].key && SRC[s].key !== key){
        clearCard(s);
      }
    }
    setCardBusy(MODE, true);
    // 查询进行中昵称尚不可知，先用输入的 ID 占位，
    // 返回结果后再换成真实昵称（renderMate 里覆盖）。
    NAME_A = pa; NAME_B = pb;
    showProgress({percent:0, stage_text:"正在提交…"});

    var body, url;
    if (MODE === "scan"){
      url = "/api/scan";
      body = {player_base: pa, player_target: pb};
    } else {
      url = "/api/mate";
      body = {player_a: pa, player_b: pb};
    }

    fetch(url, {
      method: "POST",
      headers: {"Content-Type": "application/json"},
      body: JSON.stringify(body)
    })
    .then(function(r){ return r.json().then(function(j){ return {ok:r.ok, j:j}; }); })
    .then(function(res){
      if (!res.ok || !res.j.job_id){ stopPoll(); showError(res.j.error || "提交失败", res.j.hint); return; }
      currentJob = res.j.job_id;
      poll();
      pollTimer = setInterval(poll, 800);
    })
    .catch(function(e){ stopPoll(); showError("提交失败: " + e.message); });
  });

  if (location.protocol !== "http:" && location.protocol !== "https:"){
    showError("请通过 http://127.0.0.1:8765/ 访问本页，不要直接打开 HTML 文件。");
  }
})();
</script>
</body>
</html>
"""


# ---------- 入口 ----------

def bind_server(host, port, handler, tries=10):
    """端口被占用时顺延重试。"""
    for p in range(port, port + tries):
        try:
            return ThreadingHTTPServer((host, p), handler), p
        except OSError as e:
            print(f"[warn] 端口 {p} 不可用（{e}），尝试 {p+1} …")
    raise SystemExit(f"端口 {port}-{port + tries - 1} 全部不可用，请用 --port 指定其他端口。")


def _env_int(name, default):
    """从环境变量读整数，非法则回退默认值。"""
    raw = os.environ.get(name)
    if raw is None or str(raw).strip() == "":
        return default
    try:
        return int(str(raw).strip())
    except ValueError:
        print(f"[warn] 环境变量 {name}={raw!r} 不是整数，回退为 {default}")
        return default


def main():
    argv = sys.argv[1:]
    # 优先级：命令行参数 > 环境变量 > 内置默认值（环境变量便于容器配置）
    host = os.environ.get("D2D_HOST", DEFAULT_HOST)
    port = _env_int("D2D_PORT", DEFAULT_PORT)
    do_open = True
    for i, a in enumerate(argv):
        if a == "--host" and i + 1 < len(argv):
            host = argv[i + 1]
        elif a == "--port" and i + 1 < len(argv):
            port = int(argv[i + 1])
        elif a == "--no-open":
            do_open = False
        elif a == "--open":
            do_open = True
        elif a in ("-h", "--help"):
            print(__doc__)
            return

    print(f"Dota2 同局查询 Web 服务 v{VERSION}")
    print(f"领域层: {os.path.basename(ti.__file__)}")
    load_heroes_once()

    httpd, actual_port = bind_server(host, port, Handler)
    url = f"http://{host}:{actual_port}/"
    print(f"[info] 服务已启动: {url}")
    if host != "127.0.0.1":
        print("[warn] 当前监听非本机地址且无任何鉴权，任何人都可访问并消耗 OpenDota 配额。")
    print("[info] Ctrl+C 停止服务")

    if do_open:
        try:
            threading.Timer(0.8, lambda: webbrowser.open(url)).start()
        except Exception:  # noqa: BLE001  无 GUI 环境（容器/服务器）忽略
            pass

    stop_event = threading.Event()
    threading.Thread(target=cleanup_loop, args=(stop_event,), daemon=True).start()

    # 容器里 docker stop 发的是 SIGTERM，需转为优雅关闭
    def _on_term(_signum, _frame):
        print("\n[info] 收到终止信号，正在停止服务…")
        threading.Thread(target=httpd.shutdown, daemon=True).start()

    for sig_name in ("SIGTERM", "SIGINT"):
        sig = getattr(signal, sig_name, None)
        if sig is not None:
            try:
                signal.signal(sig, _on_term)
            except (ValueError, OSError):
                pass  # 非主线程等场景忽略

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n[info] 正在停止服务…")
    finally:
        stop_event.set()
        httpd.server_close()
        print("[info] 已停止")


if __name__ == "__main__":
    main()
