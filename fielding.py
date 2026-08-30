"""フェア打球を「どの野手が」「さばけるか(アウト/ヒット)」に変換するモジュール。

アウト確率 =  base_out_probability             打球の種類・飛距離で決まる素の値
            + defender_skill_adjustment       その野手の総合力(50 が平均)
            + position_fit_adjustment         配置が合っているか × ポジション難度
            + defensive_alignment_adjustment  シフトが打球と噛み合ったか
            + batted_ball_adjustment          強い打球ほどアウトになりにくい

どの行がアウト率をどれだけ動かしたかは、返り値の breakdown で確認できる。
"""

import random

from fielders import position_fit

# 打球の種類 × 飛距離 ごとの「素の」アウト確率。
BASE_OUT_PROB = {
    ("ground", "infield"): 0.76,
    ("ground", "shallow"): 0.35,   # 内野の間を抜けかけたゴロ
    ("line", "infield"): 0.42,     # 鋭いライナー(正面なら捕れる)
    ("line", "shallow"): 0.30,
    ("line", "deep"): 0.40,
    ("fly", "infield"): 0.92,      # 内野フライ
    ("fly", "shallow"): 0.78,
    ("fly", "deep"): 0.74,
}

# 打球の強さによる補正
HARDNESS_ADJUST = {"soft": +0.10, "medium": -0.02, "hard": -0.13}

# エラー(処理できたはずの打球を落球・悪送球で出塁させてしまう)基準確率
ERROR_BASE = 0.045


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def _hit_extent(bb, batter, rng):
    """安打になったとき、単打 / 二塁打 / 三塁打 / 本塁打 のどれかを決める。

    打球の強さ・種類・飛距離・方向 と、打者の power / speed で確率を動かす。
    """
    power = getattr(batter, "power", 0.5)
    speed = getattr(batter, "speed", 50)
    t, d, h = bb.ball_type, bb.distance, bb.hardness

    # 本塁打: 強い打球が深く飛んだフライ/ライナーのみ
    if h == "hard" and d == "deep" and t in ("fly", "line"):
        hr_p = 0.10 + 0.30 * power
        if bb.direction == "pull":
            hr_p += 0.08
        elif bb.direction == "oppo":
            hr_p -= 0.05
        if t == "line":
            hr_p *= 0.5                      # ライナーは本塁打になりにくい
        if rng.random() < max(0.0, hr_p):
            return "本塁打"

    # 三塁打: 深いギャップへの打球 + 俊足
    if d == "deep" and t in ("line", "fly"):
        tri_p = 0.05 + (speed - 50) / 100 * 0.25
        if bb.direction == "center":
            tri_p += 0.04
        if rng.random() < max(0.0, tri_p):
            return "三塁打"

    # 二塁打: 深い打球 / 浅いライナー / 三塁線・一塁線を破る強いゴロ
    if d == "deep":
        dbl_p = 0.55
    elif t == "line" and d == "shallow":
        dbl_p = 0.15
    elif t == "ground" and h == "hard" and bb.direction == "pull":
        dbl_p = 0.20
    else:
        dbl_p = 0.0
    if rng.random() < dbl_p:
        return "二塁打"

    return "単打"


def _field_side(direction, bats):
    """打者相対の方向(pull/center/oppo)を、実際の左右(left/center/right)に直す。

    右打者の引っ張り = レフト方向 / 左打者の引っ張り = ライト方向。
    ここが「右打者か左打者かで、弱い野手を隠す場所が変わる」の中心。
    """
    if direction == "center":
        return "center"
    pull_is_left = (bats == "R")
    if direction == "pull":
        return "left" if pull_is_left else "right"
    return "right" if pull_is_left else "left"   # oppo


def _error_chance(overall, fit, hardness):
    """アウトになるはずだった打球を、エラーで出塁させてしまう確率。

    総合力・ポジション適性が高いほど下がり、強い打球ほど上がる。
    """
    chance = ERROR_BASE
    chance -= (overall - 50) / 100 * 0.10
    chance -= (fit - overall) / 100 * 0.04
    if hardness == "hard":
        chance += 0.02
    elif hardness == "soft":
        chance -= 0.015
    return _clamp(chance, 0.01, 0.15)


def responsible_position(batted_ball, bats, rng=None):
    """その打球をまず処理する野手のポジションを返す。"""
    rng = rng or random
    side = _field_side(batted_ball.direction, bats)

    reaches_outfield = (
        batted_ball.ball_type == "fly"
        or (batted_ball.ball_type == "line" and batted_ball.distance != "infield")
    )
    if reaches_outfield:
        return {"left": "LF", "center": "CF", "right": "RF"}[side]

    # 内野ゴロ / 内野ライナー / ポップフライ
    if side == "center":
        return rng.choice(["SS", "2B"])          # 二遊間
    if side == "left":
        return "3B" if rng.random() < 0.4 else "SS"
    return "1B" if rng.random() < 0.4 else "2B"   # right


def resolve_batted_ball(batted_ball, defense, batter, rng=None):
    """打球 + 守備配置 + 打者 から、アウト/ヒットを判定する。"""
    rng = rng or random

    position = responsible_position(batted_ball, batter.bats, rng)
    fielder = defense.fielder_at(position)

    base = BASE_OUT_PROB.get((batted_ball.ball_type, batted_ball.distance), 0.45)

    overall = fielder.overall()
    fit = position_fit(fielder, position)

    # 総合力そのもの(平均 50 からのズレ)
    defender_skill_adjustment = _clamp((overall - 50) / 100, -0.15, 0.15) * 0.7
    # 「配置が合っているか」= fit と overall の差。難しいポジションほど差が開く。
    position_fit_adjustment = _clamp((fit - overall) / 100, -0.25, 0.25) * 1.0
    # シフトが打球と噛み合っているか
    defensive_alignment_adjustment = defense.alignment_out_adjust(position, batted_ball)
    # 打球が強いほどアウトにしにくい
    batted_ball_adjustment = HARDNESS_ADJUST[batted_ball.hardness]

    # 足の速い打者は内野ゴロを内野安打にすることがある
    speed_adjustment = 0.0
    if batted_ball.ball_type == "ground" and position in ("1B", "2B", "3B", "SS"):
        speed_adjustment = -(getattr(batter, "speed", 50) - 50) / 100 * 0.14

    out_p = (base
             + defender_skill_adjustment
             + position_fit_adjustment
             + defensive_alignment_adjustment
             + batted_ball_adjustment
             + speed_adjustment)
    out_p = _clamp(out_p, 0.03, 0.97)

    error_p = _error_chance(overall, fit, batted_ball.hardness)
    is_out = rng.random() < out_p
    is_error = is_out and rng.random() < error_p
    if is_error:
        is_out = False
        batter_result = "エラー"
    elif is_out:
        batter_result = "アウト"
    else:
        batter_result = _hit_extent(batted_ball, batter, rng)

    return {
        "result": batter_result,               # "アウト" / "エラー" / "単打" / "二塁打" / "三塁打" / "本塁打"
        "batter_out": is_out,
        "error": is_error,
        "air_out": is_out and batted_ball.ball_type in ("fly", "line"),
        "position": position,
        "fielder": fielder.name,
        "out_probability": round(out_p, 3),
        "breakdown": {
            "base": round(base, 3),
            "defender_skill": round(defender_skill_adjustment, 3),
            "position_fit": round(position_fit_adjustment, 3),
            "alignment": round(defensive_alignment_adjustment, 3),
            "batted_ball": round(batted_ball_adjustment, 3),
            "runner_speed": round(speed_adjustment, 3),
            "error_chance": round(error_p, 3),
        },
    }
