#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
OpenDota 队友交集查询
给定玩家 A / B 的 SteamID，输出二人历史同局的所有比赛：比赛编号、时间、阵容、输赢。

用法:
    python teammate_intersection.py <steamID_A> <steamID_B> [选项]

选项:
    --years N     仅统计最近 N 年（默认 2）
    --since TS    起始时间（Unix 秒 或 YYYY-MM-DD）
    --until TS    结束时间
    --fast        不拉取每场阵容详情（快很多，只有编号/时间/输赢）
    --all         不限时间，拉取全部历史（较慢）

SteamID 支持三种写法，脚本自动识别:
    - SteamID64        76561198047011640
    - SteamID32        86745912
    - account_id       86745912
    - 个人主页链接      https://steamcommunity.com/id/xxx 或 /profiles/xxx

依赖: 仅标准库 (urllib)。无需 API key。
"""

import json
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

API = "https://api.opendota.com/api"
STEAM64_BASE = 76561197960265728  # SteamID64 与 account_id 的固定偏移

HERO_NAMES = {}  # hero_id -> 中/英文名，运行时从 /heroes 拉取

# ---------- HTTP ----------

def api_get(path, params=None, retries=4):
    """带重试的 GET，返回解析后的 JSON。"""
    url = API + path
    if params:
        url += "?" + urllib.parse.urlencode(params)
    last = None
    for i in range(retries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=40) as r:
                return json.loads(r.read().decode("utf-8"))
        except urllib.error.HTTPError as e:
            if e.code == 404:
                return {"error": "Not Found"}
            if e.code == 429:  # 限流，退避重试
                last = e
                time.sleep(3 * (i + 1))
                continue
            last = e
            time.sleep(2)
        except Exception as e:  # noqa: BLE001
            last = e
            time.sleep(2)
    raise RuntimeError(f"请求失败 {url}: {last}")


# ---------- 身份转换 ----------

def to_account_id(raw):
    """
    统一转换为 account_id(32位)。
    返回 (account_id, 说明字符串)
    """
    s = raw.strip()
    if "steamcommunity.com" in s:
        # 链接形式：/profiles/7656... 可直接算；/id/自定义 需走解析接口
        s = s.rstrip("/")
        tail = s.split("/")[-1]
        if s.split("/")[-2] == "profiles" and tail.isdigit():
            return int(tail) - STEAM64_BASE, f"链接解析 {tail}"
        # 自定义 ID 无法本地换算
        raise ValueError(
            f"无法从自定义主页链接换算: {s}\n"
            "  请改用 https://steamcommunity.com/profiles/<SteamID64> 形式的链接，"
            "或直接提供 SteamID64/account_id。"
        )
    if not s.isdigit():
        raise ValueError(f"无法识别的 SteamID: {raw}")
    n = int(s)
    if n > STEAM64_BASE:          # 视为 SteamID64
        return n - STEAM64_BASE, f"SteamID64 {n}"
    return n, f"account_id {n}"   # 视为 account_id


# ---------- 数据获取 ----------

def load_hero_names():
    """拉取英雄 id -> 名称映射，失败则返回空表（则显示 id）。"""
    try:
        data = api_get("/heroes")
        return {h["id"]: h.get("localized_name") or h.get("name") for h in data}
    except Exception:  # noqa: BLE001
        return {}


def get_player_matches(aid, limit=None, project_heroes=False,
                       date_min=None, date_max=None, max_pages=100):
    """
    拉取玩家公开对局（按时间倒序）。
    OpenDota 该接口单次上限 500 条，用 offset 分页，直到取完或越过 date_min。
    date_min / date_max 为 Unix 秒，服务端过滤。
    """
    out, offset = [], 0
    for _ in range(max_pages):
        params = {"limit": 500, "offset": offset}
        if project_heroes:
            params["project"] = "heroes"
        if date_min:
            params["date_min"] = int(date_min)
        if date_max:
            params["date_max"] = int(date_max)
        batch = api_get(f"/players/{aid}/matches", params)
        if isinstance(batch, dict) or not batch:
            break
        out.extend(batch)
        # 已翻到时间窗口下界，可提前结束
        if date_min and batch[-1].get("start_time", 0) < date_min:
            break
        if len(batch) < 500:
            break
        offset += 500
        if limit and len(out) >= limit:
            break
        time.sleep(0.4)
    # 服务端过滤偶有偏差，本地再过滤一次
    if date_min:
        out = [m for m in out if (m.get("start_time") or 0) >= date_min]
    if date_max:
        out = [m for m in out if (m.get("start_time") or 0) <= date_max]
    return out


def get_match_detail(match_id):
    """拉取单场完整详情（含 10 名玩家的 hero_id / account_id / 输赢）。"""
    return api_get(f"/matches/{match_id}")


# ---------- 核心逻辑 ----------

def intersect(aid_a, aid_b, detail=True, verbose=True,
              date_min=None, date_max=None):
    """
    返回 A、B 同局的比赛列表，并区分同队/敌对。
    每条包含: match_id, start_time, 是否同队, A 方阵容, B 方阵容, A/B 输赢, 英雄。
    """
    if verbose:
        print(f"[1/3] 拉取玩家 {aid_a} 的对局历史 ...")
    ma = get_player_matches(aid_a, date_min=date_min, date_max=date_max)
    print(f"      A 公开对局 {len(ma)} 场")

    if verbose:
        print(f"[2/3] 拉取玩家 {aid_b} 的对局历史 ...")
    mb = get_player_matches(aid_b, date_min=date_min, date_max=date_max)
    print(f"      B 公开对局 {len(mb)} 场")

    # 以 B 的 match_id 建索引，取交集
    idx_b = {m["match_id"]: m for m in mb}
    common = [m for m in ma if m["match_id"] in idx_b]
    if verbose:
        print(f"[3/3] 交集 {len(common)} 场")

    results = []
    for m in common:
        mid = m["match_id"]
        rec = {
            "match_id": mid,
            "start_time": m.get("start_time"),
            "time_str": time.strftime(
                "%Y-%m-%d %H:%M:%S", time.localtime(m.get("start_time") or 0)
            ),
            "a_hero_id": m.get("hero_id"),
            "b_hero_id": idx_b[mid].get("hero_id"),
            "a_player_slot": m.get("player_slot"),
            "b_player_slot": idx_b[mid].get("player_slot"),
            "radiant_win": m.get("radiant_win"),
            "duration": m.get("duration"),
            "game_mode": m.get("game_mode"),
            "lobby_type": m.get("lobby_type"),
        }
        # 同队判定：player_slot 同属天辉(<128)或夜魇(>=128)
        same_team = (rec["a_player_slot"] < 128) == (rec["b_player_slot"] < 128)
        rec["same_team"] = same_team
        # 输赢
        a_is_radiant = rec["a_player_slot"] < 128
        rec["a_win"] = (rec["radiant_win"] == a_is_radiant)
        rec["b_win"] = (rec["radiant_win"] == (rec["b_player_slot"] < 128))
        rec["a_team"] = "天辉" if a_is_radiant else "夜魇"
        rec["b_team"] = "天辉" if rec["b_player_slot"] < 128 else "夜魇"
        results.append(rec)

    results.sort(key=lambda r: r["start_time"] or 0, reverse=True)

    if detail and results:
        if verbose:
            print(f"      拉取每场阵容详情（{len(results)} 场，约 {len(results)} 次请求）...")
        for r in results:
            try:
                d = get_match_detail(r["match_id"])
                players = d.get("players") or []
                r["radiant_lineup"], r["dire_lineup"] = [], []
                for p in players:
                    hero = HERO_NAMES.get(p.get("hero_id"), str(p.get("hero_id")))
                    pinfo = {
                        "hero": hero,
                        "hero_id": p.get("hero_id"),
                        "account_id": p.get("account_id"),
                        "is_A": p.get("account_id") == aid_a,
                        "is_B": p.get("account_id") == aid_b,
                    }
                    (r["radiant_lineup"] if p.get("player_slot", 0) < 128
                     else r["dire_lineup"]).append(pinfo)
                time.sleep(0.3)
            except Exception as e:  # noqa: BLE001
                r["detail_error"] = str(e)
    return results


# ---------- 输出 ----------

def print_report(results, aid_a, aid_b):
    if not results:
        print("\n二人没有可查询到的同局记录。")
        print("可能原因：一方关闭了『公开比赛数据』、段位/模式差异导致无重叠、或对局未被 Valve 公开。")
        return

    same = [r for r in results if r["same_team"]]
    diff = [r for r in results if not r["same_team"]]
    print("\n" + "=" * 78)
    print(f"玩家 A(account_id={aid_a}) × 玩家 B(account_id={aid_b}) 同局记录")
    print("=" * 78)
    print(f"总计 {len(results)} 场｜同队 {len(same)} 场｜敌对 {len(diff)} 场")
    if same:
        w = sum(1 for r in same if r["a_win"])
        print(f"同队时 A 方胜 {w} 场，负 {len(same) - w} 场，胜率 {w / len(same) * 100:.1f}%")
    print("-" * 78)

    for r in results:
        tag = "同队" if r["same_team"] else "敌对"
        a_hero = HERO_NAMES.get(r["a_hero_id"], str(r["a_hero_id"]))
        b_hero = HERO_NAMES.get(r["b_hero_id"], str(r["b_hero_id"]))
        print(f"\n[{r['match_id']}] {r['time_str']}  {tag}  时长 {r['duration'] // 60}min")
        print(f"  A: {r['a_team']}  {a_hero}  -> {'胜' if r['a_win'] else '负'}")
        print(f"  B: {r['b_team']}  {b_hero}  -> {'胜' if r['b_win'] else '负'}")
        if "radiant_lineup" in r:
            rad = " / ".join(
                ("*" + p["hero"] + "*" if (p["is_A"] or p["is_B"]) else p["hero"])
                for p in r["radiant_lineup"]
            )
            dire = " / ".join(
                ("*" + p["hero"] + "*" if (p["is_A"] or p["is_B"]) else p["hero"])
                for p in r["dire_lineup"]
            )
            print(f"    天辉阵容: {rad}")
            print(f"    夜魇阵容: {dire}")


def export_json(results, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(results, f, ensure_ascii=False, indent=2)
    print(f"\n已导出 JSON: {path}")


def export_csv(results, path):
    import csv
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["match_id", "时间", "同队/敌对", "A队伍", "A英雄", "A结果",
                    "B队伍", "B英雄", "B结果", "天辉阵容", "夜魇阵容"])
        for r in results:
            w.writerow([
                r["match_id"], r["time_str"],
                "同队" if r["same_team"] else "敌对",
                r["a_team"], HERO_NAMES.get(r["a_hero_id"], r["a_hero_id"]),
                "胜" if r["a_win"] else "负",
                r["b_team"], HERO_NAMES.get(r["b_hero_id"], r["b_hero_id"]),
                "胜" if r["b_win"] else "负",
                " / ".join(p["hero"] for p in r.get("radiant_lineup", [])),
                " / ".join(p["hero"] for p in r.get("dire_lineup", [])),
            ])
    print(f"已导出 CSV: {path}")


def parse_time(v):
    """支持 Unix 秒或 YYYY-MM-DD / YYYY-MM-DD HH:MM。"""
    v = v.strip()
    if v.isdigit():
        return int(v)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            return int(time.mktime(time.strptime(v, fmt)))
        except ValueError:
            continue
    raise ValueError(f"无法解析时间: {v}")


def main():
    argv = sys.argv[1:]
    no_detail = "--fast" in argv
    all_time = "--all" in argv

    args = [a for a in argv if not a.startswith("-")]

    # 提取 --years / --since / --until 的值参数
    years, since, until = 2, None, None
    for i, a in enumerate(argv):
        if a == "--years" and i + 1 < len(argv):
            years = float(argv[i + 1]); args = [x for x in args if x != argv[i + 1]]
        elif a == "--since" and i + 1 < len(argv):
            since = parse_time(argv[i + 1]); args = [x for x in args if x != argv[i + 1]]
        elif a == "--until" and i + 1 < len(argv):
            until = parse_time(argv[i + 1]); args = [x for x in args if x != argv[i + 1]]

    if len(args) < 2:
        print(__doc__)
        print("示例: python teammate_intersection.py 76561198159512273 76561198099409470")
        sys.exit(1)

    now = int(time.time())
    date_min = None if all_time else (since or int(now - years * 365 * 86400))
    date_max = until

    global HERO_NAMES
    HERO_NAMES = load_hero_names()
    print(f"已加载英雄名称 {len(HERO_NAMES)} 条")
    if date_min:
        print(f"时间范围: {time.strftime('%Y-%m-%d', time.localtime(date_min))} 至今"
              + (f"（结束 {time.strftime('%Y-%m-%d', time.localtime(date_max))}）" if date_max else ""))
    else:
        print("时间范围: 全部历史")

    try:
        aid_a, desc_a = to_account_id(args[0])
        aid_b, desc_b = to_account_id(args[1])
    except ValueError as e:
        print(f"错误: {e}")
        sys.exit(1)

    print(f"玩家A: {desc_a} -> account_id={aid_a}")
    print(f"玩家B: {desc_b} -> account_id={aid_b}")

    for aid, desc in ((aid_a, desc_a), (aid_b, desc_b)):
        prof = api_get(f"/players/{aid}")
        if isinstance(prof, dict) and prof.get("error"):
            print(f"错误: {desc} 在 OpenDota 无数据（可能关闭了公开比赛数据）")
            sys.exit(1)
        p = prof.get("profile", {})
        print(f"  {desc}: {p.get('personaname')} | rank_tier={prof.get('rank_tier')}")

    results = intersect(aid_a, aid_b, detail=not no_detail,
                        date_min=date_min, date_max=date_max)
    print_report(results, aid_a, aid_b)
    export_json(results, "teammate_intersection.json")
    export_csv(results, "teammate_intersection.csv")


if __name__ == "__main__":
    main()
