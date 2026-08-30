"""フェアになった打球の「中身」を作るモジュール。

打球を次の 4 つで表す:
    direction : "pull" / "center" / "oppo"  打者から見た方向
                (左右の実位置は fielding.py で打者の左右と合わせて決める)
    ball_type : "ground" / "line" / "fly"
    hardness  : "soft" / "medium" / "hard"
    distance  : "infield" / "shallow" / "deep"

物理シミュレーションではなく、読んで分かる確率モデル。
「打者の傾向 × 球種 × コース × タイミング」で各確率をずらしていくだけ。
"""

import random
from dataclasses import dataclass

from constants import zone_x, zone_y

DIRECTIONS = ["pull", "center", "oppo"]
BALL_TYPES = ["ground", "line", "fly"]
HARDNESS_LEVELS = ["soft", "medium", "hard"]

_DIRECTION_JP = {"pull": "引っ張り", "center": "センター方向", "oppo": "逆方向"}
_HARDNESS_JP = {"soft": "弱い", "medium": "そこそこの", "hard": "強い"}
_TYPE_JP = {"ground": "ゴロ", "line": "ライナー", "fly": "フライ"}


@dataclass
class BattedBall:
    """フェア打球 1 本のデータ。物理シミュレーションではなく確率モデルの出力。"""

    direction: str          # "pull" / "center" / "oppo"(打者から見た方向)
    ball_type: str          # "ground" / "line" / "fly"
    hardness: str           # "soft" / "medium" / "hard"
    hardness_score: float   # 0-1 の連続値(丸める前の強さ)
    distance: str           # "infield" / "shallow" / "deep"

    def describe(self):
        return f"{_DIRECTION_JP[self.direction]}への{_HARDNESS_JP[self.hardness]}{_TYPE_JP[self.ball_type]}"


def _weighted_pick(rng, pairs):
    """[(値, 重み), ...] から重み付き抽選。重みが負なら 0 に丸める。"""
    values = [v for v, _ in pairs]
    weights = [max(0.0, w) for _, w in pairs]
    total = sum(weights)
    if total <= 0:
        return rng.choice(values)
    r = rng.random() * total
    upto = 0.0
    for value, weight in zip(values, weights):
        upto += weight
        if r <= upto:
            return value
    return values[-1]


def generate_batted_ball(batter, pitch, course, timing, contact_bias=0.0, rng=None):
    """pitch は pitch_data.get_pitch() が返す辞書。

    contact_bias : judge.py が「隠し傾向・配球履歴・相性」から計算して渡す
                   "打ち頃度"。プラスほど強い打球になりやすい。
    """
    rng = rng or random

    # --- 1. 方向 (pull / center / oppo) ---
    p_pull = 0.30 + 0.40 * batter.pull     # 引っ張り屋ほど pull が増える
    p_oppo = 0.32 - 0.20 * batter.pull
    p_center = max(0.05, 1.0 - p_pull - p_oppo)
    if timing == "early":            # 前でさばく → 引っ張り
        p_pull += 0.16
        p_oppo -= 0.10
    elif timing == "late":           # 差し込まれる → 逆方向
        p_oppo += 0.16
        p_pull -= 0.10
    if zone_x(course) == "in":
        p_pull += 0.10
    elif zone_x(course) == "out":
        p_oppo += 0.10
    direction = _weighted_pick(rng, [("pull", p_pull), ("center", p_center), ("oppo", p_oppo)])

    # --- 2. 打球の種類 (ground / line / fly) ---
    gb = 0.5 * pitch["groundball_rate"] + 0.5 * batter.gb_tendency
    p_ground = gb
    p_fly = (1 - gb) * 0.55
    p_line = (1 - gb) * 0.45
    if timing == "on_time":
        p_line += 0.12
        p_ground -= 0.06
        p_fly -= 0.06
    else:                            # 打ち損じはゴロか力ないフライ
        p_ground += 0.08
        p_line -= 0.06
    if zone_y(course) == "lo":
        p_ground += 0.10
        p_fly -= 0.08
    elif zone_y(course) == "hi":
        p_fly += 0.10
        p_ground -= 0.08
    ball_type = _weighted_pick(rng, [("ground", p_ground), ("line", p_line), ("fly", p_fly)])

    # --- 3. 打球の強さ ---
    score = pitch["contact_quality"]
    if timing == "on_time":
        score += 0.18
    else:
        score -= 0.12
    if batter.coarse_type == "power":
        score += 0.10
    elif batter.coarse_type == "weak":
        score -= 0.05
    score += contact_bias
    score += rng.uniform(-0.08, 0.08)
    score = max(0.0, min(1.0, score))
    if score >= 0.62:
        hardness = "hard"
    elif score >= 0.38:
        hardness = "medium"
    else:
        hardness = "soft"

    # --- 4. 飛距離ゾーン ---
    if ball_type == "ground":
        distance = "infield" if rng.random() < 0.80 else "shallow"
    elif ball_type == "line":
        if rng.random() < 0.30:
            distance = "infield"
        else:
            distance = "deep" if hardness == "hard" else "shallow"
    else:  # fly
        if hardness == "hard":
            distance = "deep" if rng.random() < 0.70 else "shallow"
        elif hardness == "soft":
            distance = "infield" if rng.random() < 0.35 else "shallow"
        else:
            distance = "shallow" if rng.random() < 0.60 else "deep"

    return BattedBall(direction, ball_type, hardness, round(score, 3), distance)
