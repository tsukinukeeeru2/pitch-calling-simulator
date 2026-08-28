"""野手(投手・捕手を除く 7 人)と、ポジション適性の計算。

ポイント:
  - 各野手は range / hands / arm / reaction / speed の 5 能力を持つ(20-80 目安)。
  - ポジションごとに「必要な能力の重み」と「難しさ」が違う。
  - position_fit(選手, ポジション) は「その選手をそのポジションに置いたときの
    実質守備力」を返す。同じ選手でも SS と 1B では値が変わる ―― ここが肝。

『一番下手な 1 人をどこに隠すか』が意思決定になるのは、
  1. 難しいポジション(SS など)は能力差が結果に響きやすい(difficulty_gain)
  2. 打球が来やすいポジションほど守備全体への影響が大きい(ball_share)
の 2 つが効くから。
"""

import random
from dataclasses import dataclass

INFIELD_POSITIONS = ["3B", "SS", "2B", "1B"]
OUTFIELD_POSITIONS = ["LF", "CF", "RF"]
POSITIONS = INFIELD_POSITIONS + OUTFIELD_POSITIONS

ATTRS = ["range", "hands", "arm", "reaction", "speed"]


@dataclass
class Fielder:
    """野手 1 人。5 能力を保持するだけのデータクラス。"""

    name: str
    range: float      # 守備範囲(一歩目からの到達力)
    hands: float      # 捕球の確実さ(glove)
    arm: float        # 送球の強さ・正確さ
    reaction: float   # 打球への反応の速さ
    speed: float      # 足の速さ

    def overall(self):
        """5 能力の単純平均。『総合的にうまいか』のざっくり指標。"""
        return sum(getattr(self, a) for a in ATTRS) / len(ATTRS)


# weights は合計 1.0。difficulty_gain>1 は「能力差が結果に響きやすい」。
# ball_share は「フェア打球のうち だいたい何割がここに来るか」(EDE 計算用の重み)。
POSITION_REQUIREMENTS = {
    "SS": {"weights": {"range": 0.34, "hands": 0.20, "arm": 0.24, "reaction": 0.16, "speed": 0.06},
           "difficulty_gain": 1.35, "ball_share": 0.17},
    "2B": {"weights": {"range": 0.30, "hands": 0.24, "arm": 0.12, "reaction": 0.24, "speed": 0.10},
           "difficulty_gain": 1.15, "ball_share": 0.15},
    "3B": {"weights": {"range": 0.20, "hands": 0.24, "arm": 0.34, "reaction": 0.20, "speed": 0.02},
           "difficulty_gain": 1.10, "ball_share": 0.13},
    "1B": {"weights": {"range": 0.12, "hands": 0.46, "arm": 0.10, "reaction": 0.20, "speed": 0.12},
           "difficulty_gain": 0.75, "ball_share": 0.12},
    "CF": {"weights": {"range": 0.40, "hands": 0.14, "arm": 0.14, "reaction": 0.12, "speed": 0.20},
           "difficulty_gain": 1.20, "ball_share": 0.13},
    "LF": {"weights": {"range": 0.30, "hands": 0.16, "arm": 0.14, "reaction": 0.12, "speed": 0.28},
           "difficulty_gain": 0.85, "ball_share": 0.09},
    "RF": {"weights": {"range": 0.28, "hands": 0.16, "arm": 0.26, "reaction": 0.12, "speed": 0.18},
           "difficulty_gain": 0.90, "ball_share": 0.09},
}


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


def position_fit(fielder, position):
    """その選手をそのポジションに置いたときの実質守備力(1-99)。

      1. そのポジションが求める能力の重み付き合計 (raw) を出す
      2. 50 を基準に、difficulty_gain で「平均からのズレ」を拡大/縮小する
         → 難しいポジションでは、下手さも上手さも増幅される
    """
    req = POSITION_REQUIREMENTS[position]
    raw = sum(getattr(fielder, attr) * w for attr, w in req["weights"].items())
    fit = 50 + (raw - 50) * req["difficulty_gain"]
    return _clamp(fit, 1.0, 99.0)


def _roll(rng, mean, spread):
    return _clamp(rng.gauss(mean, spread), 20, 80)


def make_default_fielders(rng=None):
    """守備能力をランダムに決めた 7 人を作る。

    そのうち 1 人だけ、明確に守備能力が低い『穴』の選手にする。
    誰が穴かは能力表を見れば分かる ―― 『どこに置くか』がプレイヤーの判断。
    """
    rng = rng or random
    names = ["A", "B", "C", "D", "E", "F", "G"]
    weak_index = rng.randrange(len(names))
    fielders = []
    for i, name in enumerate(names):
        if i == weak_index:
            mean, spread = 34, 4      # 明確に下手な 1 人
        else:
            mean, spread = 58, 7      # 平均的な 6 人
        fielders.append(Fielder(
            name,
            _roll(rng, mean, spread), _roll(rng, mean, spread),
            _roll(rng, mean, spread), _roll(rng, mean, spread), _roll(rng, mean, spread),
        ))
    return fielders


def weakest_fielder(fielders):
    """総合能力が最も低い選手を返す(画面の ▼ 表示用)。"""
    return min(fielders, key=lambda f: f.overall())
