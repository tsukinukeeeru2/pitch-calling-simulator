"""守備配置(どの選手をどこに置くか)と、守備シフトの選択。

Defense は
  - 7 人の Fielder
  - position -> Fielder の割り当て(自由に入れ替え可能)
  - alignment(守備シフト)
を持つ。

expected_defensive_efficiency() は「参考値(目安)」であって最適解ではない。
プレイヤー自身が、打者の左右・傾向・配球と合わせて判断する。
"""

from fielders import (
    POSITION_REQUIREMENTS,
    POSITIONS,
    make_default_fielders,
    position_fit,
    weakest_fielder,
)

# 守備シフトの一覧(キー: 内部名 / 値: 表示名)
ALIGNMENTS = {
    "standard": "標準守備",
    "pull": "引っ張り警戒",
    "oppo": "逆方向警戒",
    "infield_in": "前進守備",
    "no_doubles": "長打警戒",
    "dp": "ゲッツー重視",
    "bunt": "バント警戒",
}

_CORNERS = ("1B", "3B")

_INFIELD = ("1B", "2B", "3B", "SS")


class Defense:
    def __init__(self, fielders=None, rng=None):
        # rng を渡さないと守備能力が乱数シードに乗らない(--seed で再現できない)ので、
        # generate_random_situation はゲームの rng を必ず渡すこと。
        self.fielders = fielders if fielders is not None else make_default_fielders(rng)
        self.alignment = "standard"
        # 最初はとりあえず POSITIONS 順に置く。プレイヤーが後で入れ替える。
        self.assignment = {pos: self.fielders[i] for i, pos in enumerate(POSITIONS)}

    def fielder_at(self, position):
        return self.assignment[position]

    def position_of(self, fielder):
        for pos, f in self.assignment.items():
            if f is fielder:
                return pos
        return None

    def swap(self, pos_a, pos_b):
        """2 つのポジションの選手を入れ替える。"""
        self.assignment[pos_a], self.assignment[pos_b] = (
            self.assignment[pos_b], self.assignment[pos_a])

    def set_alignment(self, key):
        if key not in ALIGNMENTS:
            raise ValueError(f"unknown alignment: {key}")
        self.alignment = key

    def describe_alignment(self):
        return ALIGNMENTS[self.alignment]

    def weakest(self):
        return weakest_fielder(self.fielders)

    def expected_defensive_efficiency(self):
        """0-100 の参考値。『来やすい場所ほど重く』適性を平均する。"""
        total_share = sum(POSITION_REQUIREMENTS[p]["ball_share"] for p in POSITIONS)
        score = sum(
            POSITION_REQUIREMENTS[p]["ball_share"] * position_fit(self.assignment[p], p)
            for p in POSITIONS
        )
        return score / total_share

    def alignment_out_adjust(self, position, batted_ball):
        """選んだシフトが この打球と噛み合っているかで ±(アウト確率の増減)。

        「シフトと配球がハマればアウト率が上がる／逆ならヒットが増える」を表す部分。
        """
        a = self.alignment
        direction = batted_ball.direction
        ball_type = batted_ball.ball_type
        infield = position in _INFIELD

        if a == "standard":
            return 0.0

        if a == "pull":                       # 引っ張り警戒
            if direction == "pull":
                return 0.08                   # 張ったところに来た
            if direction == "oppo":
                return -0.07                  # 逆を突かれて穴が空く
            return 0.0

        if a == "oppo":                       # 逆方向警戒(pull の鏡)
            if direction == "oppo":
                return 0.08
            if direction == "pull":
                return -0.07
            return 0.0

        if a == "infield_in":                 # 前進守備
            if infield and ball_type == "ground":
                return 0.10                   # 前に出て早く処理できる
            if ball_type == "line":
                return -0.08                  # 頭を越されると一気に抜ける
            return 0.0

        if a == "no_doubles":                 # 長打警戒(外野を深く)
            if not infield and ball_type in ("line", "fly"):
                return 0.06                   # 深い打球を前で処理
            if not infield and ball_type == "ground":
                return -0.06                  # 外野の前が大きく空く
            return 0.0

        if a == "dp":                         # ゲッツー重視(二遊間を中央寄せ)
            if position in ("SS", "2B") and ball_type == "ground":
                return 0.07
            if position in ("1B", "3B") and ball_type == "ground":
                return -0.05                  # 三遊間・一二塁間が広がる
            return 0.0

        if a == "bunt":                       # バント警戒(一塁・三塁が前に出る)
            if position in _CORNERS and ball_type == "ground":
                if batted_ball.hardness == "soft":
                    return 0.14                # 転がした/バントを素早く処理
                if batted_ball.hardness == "hard":
                    return -0.12               # 詰めすぎて強い打球に反応できない
                return -0.03                   # 普通の打球にも守備範囲が狭くなる
            return 0.0

        return 0.0


def make_default_defense(rng=None):
    return Defense(make_default_fielders(rng))
