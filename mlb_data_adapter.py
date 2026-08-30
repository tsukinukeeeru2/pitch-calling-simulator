"""実データ(MLB Stats API / Baseball Savant 風のデータ)を Game Engine の
Batter に変換する Adapter。

    MLB Stats API / Savant(生データ、どんな形でもよい)
              ↓
    mlb_data_adapter.py（このファイル）
              ↓
    lineup._BATTER_FIELDS と同じキーの dict(records)
              ↓
    Lineup.from_records(records) → Batter オブジェクト → Game Engine

Game Engine 側(judge.py 以下)は Batter オブジェクトの形しか知らない。
外部データの形式(API のレスポンス形式)が変わっても、直すのはこの
ファイルだけでよい ―― という「置き換え可能にする」ための境界線。

対応できる範囲(重要な割り切り):
    avg / obp / slg / bats のような公開成績は生データからそのまま使える。
    しかし discipline・aggression・weak_pitch・guess_bias・
    two_strike_ability・pressure_tolerance のような「このゲーム独自の
    隠しパラメータ」は、一般に公開されている統計だけでは決まらない。
    そこで、公開統計から合理的に近い値を "推定" するか、
    決まらない部分は選手名から決定的に(=同じ選手なら毎回同じ値に)
    振る。乱数で山勘を作っているだけで、実際の傾向とは一致しない。

hot_course / weak_course(得意/苦手コース)について:
    生データ(raw)に "zone_profile"（9 分割コースごとの成績、数値が高いほど
    得意）を含めれば、_hot_weak_from_zone_profile() が実データから
    hot_course/weak_course を計算する ―― Baseball Savant がプレイヤーページで
    公開している「ゾーン別成績」をこのゲームの 9 分割コース表記
    （constants.COURSES のキー）に人手で置き換えて zone_profile に渡せば、
    本物の得意/苦手コースになる。zone_profile を渡さなければ、従来どおり
    選手名からの決定的な「山勘」にフォールバックする(既存の SAMPLE_LINEUP
    (架空選手)と同じ扱い)。
    この Adapter は Baseball Savant のゾーン別データを自動取得はしない
    ―― Savant の zone 番号(1-14, 捕手視点)は打者の左右で「内角/外角」の
    意味が変わるため、このゲーム側の「常に打者視点」という表記へ正しく
    変換できているかは実データでの検証が必要で、そこまでは踏み込んでいない。
    未検証の変換を「実データ取得」として実装するのは避け、今回は
    「渡された zone_profile を正しく使う」受け口とテストだけを用意した。
"""

import json
import random
import urllib.error
import urllib.request

from constants import COURSES
from lineup import Lineup
from pitch_data import all_pitch_keys

_SAMPLE_FIXTURE = "mlb_data/sample_lineup_raw.json"

# MLB Stats API(公開・無料・API キー不要): https://statsapi.mlb.com/
# チームID の例: 119 = Los Angeles Dodgers, 147 = New York Yankees など。
_MLB_STATS_API_TIMEOUT = 6.0


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _pct(raw, key, default):
    """0-100 のパーセンテージ表記でも 0-1 の割合表記でも受け取れるようにする。"""
    v = raw.get(key)
    if v is None:
        return default
    return v / 100.0 if v > 1.0 else v


def _seeded_pick(name, key, choices):
    """選手名 + key から決定的に1つ選ぶ(同じ選手なら毎回同じ値になる)。

    苦手コース等、公開統計だけでは決まらない値のための「山勘」。
    本物のホット/コールドゾーンではない(README に明記)。
    """
    return random.Random(f"{name}:{key}").choice(choices)


def _hot_weak_from_zone_profile(zone_profile):
    """9 分割コースごとの成績(数値が高いほど打者が得意)から hot/weak を決める。

    zone_profile は {course_key: 数値} の dict(COURSES の 9 キーの一部でよい)。
    Baseball Savant のような実データから作れる想定 ―― キーはこのゲームの
    9 分割コース表記(constants.COURSES)に合わせて渡すこと(打者の左右で
    「内角/外角」の意味が変わる点はすでにこのゲーム側の表記に揃えてある)。
    有効な値が 2 つ未満なら None を返し、呼び出し側は「山勘」にフォールバックする。
    """
    valid = {c: v for c, v in (zone_profile or {}).items() if c in COURSES and v is not None}
    if len(valid) < 2:
        return None
    hot_course = max(valid, key=valid.get)
    weak_course = min(valid, key=valid.get)
    if hot_course == weak_course:
        return None
    return hot_course, weak_course


def normalize_batter_record(raw):
    """MLB 風の生データ 1 人分(dict)を、Batter が受け取れる形に変換する。

    raw に必須なのは name / bats / avg / obp / slg だけ。
    whiff_pct・chase_pct・pull_pct・k_pct・bb_pct・gb_pct・speed_pct は
    あれば使う(Savant のリーダーボードに近い列名)。無ければリーグ平均
    相当のデフォルトにフォールバックする。
    """
    name = raw["name"]
    bats = raw["bats"]
    avg = float(raw["avg"])
    obp = float(raw["obp"])
    slg = float(raw["slg"])

    whiff = _pct(raw, "whiff_pct", 0.24)
    chase = _pct(raw, "chase_pct", 0.30)
    pull = _pct(raw, "pull_pct", 0.40)
    k_rate = _pct(raw, "k_pct", 0.22)
    bb_rate = _pct(raw, "bb_pct", 0.08)
    gb = _pct(raw, "gb_pct", 0.45)
    speed = raw.get("speed_pct", 50)

    power = _clamp((slg - 0.320) / 0.420, 0.15, 0.95)
    contact = _clamp(1.0 - (whiff - 0.10) / 0.35, 0.15, 0.92)
    discipline = _clamp(0.5 + (bb_rate - 0.08) * 3.0 - (chase - 0.28) * 1.5, 0.15, 0.90)
    aggression = _clamp(0.5 + (chase - 0.28) * 1.2 - (bb_rate - 0.08) * 1.5, 0.20, 0.85)
    two_strike_ability = _clamp(0.6 - (k_rate - 0.22) * 1.5, 0.20, 0.85)

    if slg >= 0.520 and power >= 0.55:
        coarse_type = "power"
    elif obp - slg >= 0.05:
        coarse_type = "patient"
    elif chase >= 0.34 and bb_rate <= 0.06:
        coarse_type = "free_swinger"
    elif contact >= 0.65 and power < 0.55:
        coarse_type = "contact"
    else:
        coarse_type = "average"

    zone_pick = _hot_weak_from_zone_profile(raw.get("zone_profile"))
    if zone_pick is not None:
        hot_course, weak_course = zone_pick
    else:
        hot_course = _seeded_pick(name, "hot", list(COURSES))
        weak_course = _seeded_pick(name, "weak", list(COURSES))
        # 山勘の 2 回の抽選がたまたま同じコースを引くと「得意＝苦手」になって
        # しまう。その場合だけ、残りのコースから決定的に引き直す。
        if weak_course == hot_course:
            weak_course = _seeded_pick(name, "weak_alt",
                                      [c for c in COURSES if c != hot_course])

    return {
        "name": name, "bats": bats, "coarse_type": coarse_type,
        "avg": round(avg, 3), "obp": round(obp, 3), "slg": round(slg, 3),
        "power": round(power, 2), "contact": round(contact, 2),
        "discipline": round(discipline, 2), "chase_rate": round(_clamp(chase, 0.10, 0.50), 2),
        "whiff_rate": round(_clamp(whiff, 0.06, 0.45), 2), "aggression": round(aggression, 2),
        "pull": round(_clamp(pull, 0.20, 0.75), 2), "gb_tendency": round(_clamp(gb, 0.25, 0.65), 2),
        # 球種別成績(vs_fastball 等)はこの簡易データには含まれないので中立値
        "vs_fastball": 0.5, "vs_breaking": 0.5, "vs_offspeed": 0.5,
        "hot_course": hot_course,
        "weak_course": weak_course,
        "weak_pitch": _seeded_pick(name, "weak_pitch", all_pitch_keys()),
        "guess_bias": round(_clamp(0.5 + (chase - 0.28) * 0.6, 0.30, 0.80), 2),
        "two_strike_ability": round(two_strike_ability, 2),
        "pressure_tolerance": 0.5,
        "speed": int(_clamp(speed, 20, 90)),
    }


def load_fixture_lineup(path=_SAMPLE_FIXTURE):
    """同梱のサンプル生データ(mlb_data/sample_lineup_raw.json)を読み、
    Lineup を作る。ネット接続なしでも常に動く。
    """
    with open(path, encoding="utf-8") as fp:
        data = json.load(fp)
    records = [normalize_batter_record(b) for b in data["batters"]]
    return Lineup.from_records(records)


def fetch_live_roster_raw(team_id, season):
    """MLB Stats API から打者成績を取得する(標準ライブラリの urllib のみ)。

    成功すれば normalize_batter_record() が読める生データのリストを返す。
    ネットワークが使えない・API 形式が変わった等で失敗したら、原因を
    包んだ例外(RuntimeError)を投げる ―― 呼び出し側(build_demo_lineup)
    がこれを捕まえてサンプルデータにフォールバックする。
    """
    url = (f"https://statsapi.mlb.com/api/v1/teams/{team_id}/roster"
          f"?rosterType=active&season={season}")
    try:
        with urllib.request.urlopen(url, timeout=_MLB_STATS_API_TIMEOUT) as resp:
            roster = json.loads(resp.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError) as err:
        raise RuntimeError(f"MLB Stats API(roster)に接続できませんでした: {err}") from err

    raw_batters = []
    for entry in roster.get("roster", []):
        pos = entry.get("position", {}).get("abbreviation")
        if pos in ("P", "TWP"):
            continue
        person = entry["person"]
        stat_url = (f"https://statsapi.mlb.com/api/v1/people/{person['id']}/stats"
                   f"?stats=season&season={season}&group=hitting")
        try:
            with urllib.request.urlopen(stat_url, timeout=_MLB_STATS_API_TIMEOUT) as resp:
                stat_data = json.loads(resp.read().decode("utf-8"))
            splits = stat_data["stats"][0]["splits"]
            if not splits:
                continue
            s = splits[0]["stat"]
            bats = person.get("batSide", {}).get("code", "R")
            if bats not in ("R", "L"):
                bats = "R"   # このゲームはスイッチヒッター("S")非対応。簡略化してRとして扱う
            raw_batters.append({
                "name": person["fullName"], "bats": bats,
                "avg": float(s["avg"]), "obp": float(s["obp"]), "slg": float(s["slg"]),
            })
        except (urllib.error.URLError, TimeoutError, OSError, json.JSONDecodeError,
                KeyError, IndexError, ValueError):
            continue   # この選手だけスキップ(API全体は失敗にしない)
        if len(raw_batters) >= 9:
            break

    if len(raw_batters) < 9:
        raise RuntimeError(f"打者データが{len(raw_batters)}人しか取れませんでした(9人必要)")
    return raw_batters[:9]


def build_demo_lineup(prefer_live=False, team_id=147, season=2024):
    """MLB DATA DEMO 用の Lineup を1つ作る。

    prefer_live=True なら MLB Stats API から実際に取りにいく。失敗したら
    (ネットワーク不通・API 形式変更・9人揃わない、など理由は問わず)
    同梱のサンプルデータへ自動でフォールバックする ―― Web/CLI 本体は
    このデモが失敗しても絶対に落ちない。
    """
    if prefer_live:
        try:
            raw = fetch_live_roster_raw(team_id, season)
            records = [normalize_batter_record(b) for b in raw]
            return Lineup.from_records(records), "live"
        except Exception as err:   # 理由を問わずサンプルへフォールバック
            print(f"（MLB Stats API を使えなかったのでサンプルデータを使います: {err}）")
    return load_fixture_lineup(), "fixture"
