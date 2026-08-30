"""ゲームで使う「変わらないデータ」をまとめたファイル。

選択肢(コース・打者タイプ)と、判定に使う補正値をここに集める。
数値のバランス調整をしたいときは、まずこのファイルを見る。

分野ごとに大きいデータは別ファイルに分けてある:
  - 球種のデータ            : pitch_data.py
  - ポジション適性のデータ  : fielders.py
  - 守備シフトのデータ      : defense.py
  - アウト確率の素の値      : fielding.py
"""

# コースの一覧(3x3 の 9 分割)。
#   キーは "<横>_<縦>"。 横: in/mid/out   縦: hi/mid/lo
COURSES = {
    "in_hi": "内角高め", "mid_hi": "高め", "out_hi": "外角高め",
    "in_mid": "内角", "mid_mid": "真ん中", "out_mid": "外角",
    "in_lo": "内角低め", "mid_lo": "低め", "out_lo": "外角低め",
}
CENTER_COURSE = "mid_mid"

# 画面での短縮表記
COURSE_SHORT = {
    "in_hi": "内高", "mid_hi": "高", "out_hi": "外高",
    "in_mid": "内", "mid_mid": "中", "out_mid": "外",
    "in_lo": "内低", "mid_lo": "低", "out_lo": "外低",
}


def zone_x(course):
    """横位置 "in" / "mid" / "out"。"""
    return course.split("_")[0]


def zone_y(course):
    """縦位置 "hi" / "mid" / "lo"。"""
    return course.split("_")[1]


def is_corner(course):
    """隅(横も縦も中央でない)か。"""
    return zone_x(course) != "mid" and zone_y(course) != "mid"


def zone_overlap(a, b):
    """2 つのコースの近さ。 同じ=1.0 / 同じ列か行=0.5 / それ以外=0.0。

    「外角低めが苦手」な打者は「外角」や「低め」でもいくらか差し込める、を表す。
    """
    if a == b:
        return 1.0
    if zone_x(a) == zone_x(b) or zone_y(a) == zone_y(b):
        return 0.5
    return 0.0


# コースの「隣」。投球がすっぽ抜けた/浮いたとき、どこへズレるか(pitcher.py)。
# 隅を狙って外すと、たいてい真ん中寄りに「甘く入る」。
COURSE_NEIGHBORS = {
    "in_hi": ["mid_hi", "in_mid", "mid_mid"],
    "mid_hi": ["mid_mid", "in_hi", "out_hi"],
    "out_hi": ["mid_hi", "out_mid", "mid_mid"],
    "in_mid": ["mid_mid", "in_hi", "in_lo"],
    "mid_mid": ["in_mid", "out_mid", "mid_hi", "mid_lo"],
    "out_mid": ["mid_mid", "out_hi", "out_lo"],
    "in_lo": ["mid_lo", "in_mid", "mid_mid"],
    "mid_lo": ["mid_mid", "in_lo", "out_lo"],
    "out_lo": ["mid_lo", "out_mid", "mid_mid"],
}

# コースごとの補正
#   strike_rate  : そのセルを狙ったとき、ストライクゾーンに収まる確率(隅ほど低い)
#   contact_bonus: そこに来た球の打ちやすさ(真ん中が最も甘い、隅ほどマイナス)
COURSE_PROFILE = {
    "in_hi": {"strike_rate": 0.42, "contact_bonus": -0.02},
    "mid_hi": {"strike_rate": 0.60, "contact_bonus": 0.05},
    "out_hi": {"strike_rate": 0.40, "contact_bonus": -0.08},
    "in_mid": {"strike_rate": 0.60, "contact_bonus": 0.02},
    "mid_mid": {"strike_rate": 0.82, "contact_bonus": 0.22},
    "out_mid": {"strike_rate": 0.55, "contact_bonus": -0.08},
    "in_lo": {"strike_rate": 0.40, "contact_bonus": -0.06},
    "mid_lo": {"strike_rate": 0.56, "contact_bonus": -0.05},
    "out_lo": {"strike_rate": 0.38, "contact_bonus": -0.12},
}

# --- 「反応」機能で使う定数(reaction.py) ---

# 反応(reaction.py)の見せ方を決める確率
REACTION_MISLEAD_RATE = 0.15   # 真実と逆の印象を与える確率
REACTION_REVEAL_RATE = 0.60    # (ミスリードでないとき)素直な反応を見せる確率
