"""投手と「実投球(Pitch Execution)」。

捕手が「アウトローにスライダー」と要求しても、必ずそこへ行くわけではない。

    Pitch Call(要求)  →  execute_pitch()  →  Actual Pitch(実際)

投手の能力:
    control  : すっぽ抜け・浮きの少なさ(大きい程ミスが減る)
    command  : 狙った一点への精度(隅を狙うほど効く)
    velocity : 球速
    stuff    : 球そのものの威力(空振り・打ち損じを増やす)
    quality  : 球種ごとの出来(0〜100)

『良い配球でも失投すれば打たれる / 悪い配球でも凄い球なら抑える』を作る部分。
"""

import random
from dataclasses import dataclass, field

from constants import CENTER_COURSE, COURSE_NEIGHBORS, is_corner
from pitch_data import DEFAULT_PITCHER

# 実投球の結果
#   actual_course : 実際に来たコース
#   quality       : 0.0〜1.0。球の出来(高い程 打ちにくい)
#   missed        : 狙ったコースを外したか
#   miss_kind     : "leak"(甘く入った) / "spray"(散らばった) / None


@dataclass
class Pitcher:
    """投手 1 人。能力値の保持が主目的だが、execute_pitch()/fatigue() という
    振る舞いも持つので普通の @dataclass(frozen にはしない、pitches_thrown が
    投球のたびに増える可変フィールドのため)。
    """

    name: str = "投手"
    throws: str = "R"
    control: float = 55
    command: float = 55
    velocity: float = 55
    stuff: float = 55
    repertoire: list = None
    pitch_quality: dict = None
    pitches_thrown: int = field(default=0, init=False)   # 球数(#D 疲労)

    def __post_init__(self):
        self.repertoire = list(self.repertoire or DEFAULT_PITCHER["repertoire"])
        # 球種ごとの出来。指定がなければ stuff を土台に少しばらす。
        self.pitch_quality = dict(self.pitch_quality or {})
        for key in self.repertoire:
            self.pitch_quality.setdefault(key, self.stuff)

    # ---- 疲労(#D) ----
    def fatigue(self):
        """球数がかさむと 0.0〜0.12 で増える疲労値。16 球あたりから効いてくる。"""
        return min(0.12, max(0.0, self.pitches_thrown - 16) * 0.006)

    def fatigue_level(self):
        f = self.fatigue()
        return 2 if f >= 0.08 else (1 if f >= 0.03 else 0)

    def execute_pitch(self, pitch_type, target_course, intent, rng=None, tempo=0.0):
        """tempo は捕手の返球リズム(qte.catcher_return_ball)の出来。-0.25〜0.25。
        良いテンポほど投手が乗り、失投が少し減って球威が少し上がる。0.0 で無影響。"""
        rng = rng or random
        self.pitches_thrown += 1
        tired = self.fatigue()

        demands_precision = intent in ("chase", "freeze", "weak_contact")
        aiming_corner = is_corner(target_course)
        aiming_edge = target_course != CENTER_COURSE

        # 1. コースを外す確率
        miss_p = 0.32 - self.control / 400 - self.command / 400
        if demands_precision and aiming_edge:
            miss_p += 0.08
        if aiming_corner:
            miss_p += 0.06          # 隅(横も縦も端)はさらに難しい
        if intent == "waste":
            miss_p -= 0.12          # わざと大きく外す球は「狙い通り」外れやすい
        miss_p += tired             # 疲れるとコントロールが乱れる
        miss_p -= tempo * 0.10      # 捕手の返球テンポが良いと失投が少し減る
        miss_p = max(0.05, min(0.65, miss_p))
        missed = rng.random() < miss_p

        if not missed:
            actual_course = target_course
            miss_kind = None
        elif aiming_edge:
            # 端を狙って外す → 6 割は真ん中に甘く入る(leak)、残りは隣へ散る
            if rng.random() < 0.60:
                actual_course, miss_kind = CENTER_COURSE, "leak"
            else:
                actual_course, miss_kind = rng.choice(COURSE_NEIGHBORS[target_course]), "spray"
        else:
            actual_course, miss_kind = rng.choice(COURSE_NEIGHBORS[CENTER_COURSE]), "spray"

        # 2. 球の出来(quality)
        quality = self.pitch_quality.get(pitch_type, self.stuff) / 100.0
        quality += (self.velocity - 50) / 500.0
        quality += rng.uniform(-0.14, 0.14)
        if missed:
            quality -= 0.07        # 失投は出来も落ちがち(でも必ずではない)
        quality -= tired * 0.5     # 疲れると球威も落ちる
        quality += tempo * 0.10    # テンポが整うと球威も少し上がる
        quality = max(0.03, min(0.99, quality))

        return {
            "pitch_type": pitch_type,
            "target_course": target_course,
            "actual_course": actual_course,
            "quality": quality,
            "missed": missed,
            "miss_kind": miss_kind,
        }


def build_sample_pitcher(rng=None, name="リリーフ"):
    """終盤に出てくるリリーフ投手を 1 人作る。"""
    rng = rng or random
    throws = "R" if rng.random() < 0.7 else "L"
    repertoire = rng.choice([
        ["four_seam", "slider", "changeup"],
        ["four_seam", "sinker", "slider", "curveball"],
        ["four_seam", "cutter", "sweeper", "splitter"],
        ["sinker", "cutter", "slider", "changeup"],
    ])
    return Pitcher(
        name=name, throws=throws,
        control=rng.randint(45, 72), command=rng.randint(42, 70),
        velocity=rng.randint(45, 78), stuff=rng.randint(45, 75),
        repertoire=repertoire,
        pitch_quality={k: rng.randint(40, 78) for k in repertoire},
    )
