"""打者を表す Batter クラスと、その「狙い球」推測ロジック。

情報の分け方:
  公開(スコアボードに出してよい): name / bats(左右) / OPS・AVG・OBP・SLG / 打者タイプ
  隠し(プレイヤーには見せない):
      power / contact / discipline / chase_rate / whiff_rate / aggression
      pull・center・oppo tendency / gb_tendency
      vs_fastball / vs_breaking / vs_offspeed(球種別の強さ)
      hot_course(得意) / weak_course(苦手) / weak_pitch
      guess_bias(狙い球のクセ) / two_strike_ability / pressure_tolerance / speed

judge.py は隠し情報で確率を動かすが、reaction.py と ui.py は公開情報しか触らない。
"""

import random
from dataclasses import dataclass, field

from pitch_data import all_pitch_keys, guess_class_of

HANDEDNESS = {"R": "右打", "L": "左打"}

# 打者タイプ(公開ラベル)。archetype とは別の、ざっくり表示用。
COARSE_TYPES = {"power": "強打者", "contact": "巧打者", "patient": "出塁型",
                "free_swinger": "フリースインガー", "average": "平均的", "weak": "弱打者"}


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


@dataclass
class Batter:
    """打者 1 人。データを保持するのが主目的なので @dataclass。

    フィールド名はコンストラクタ引数(lineup.py・CSV/JSON・テストが使う名前)と
    完全に一致させている。`ops` だけは obp+slg から自動計算するので
    `field(init=False)` にして __post_init__ で埋める。

    公開(スコアボードに出してよい): name / bats(左右) / OPS・AVG・OBP・SLG / 打者タイプ
    隠し(プレイヤーには見せない):
        power / contact / discipline / chase_rate / whiff_rate / aggression
        pull・gb_tendency(打球方向の傾向)
        vs_fastball / vs_breaking / vs_offspeed(球種別の強さ)
        hot_course(得意) / weak_course(苦手) / weak_pitch
        guess_bias(狙い球のクセ) / two_strike_ability / pressure_tolerance / speed

    judge.py は隠し情報で確率を動かすが、reaction.py と ui.py は公開情報しか触らない。
    """

    name: str = "打者"
    bats: str = "R"
    coarse_type: str = "average"
    avg: float = 0.255
    obp: float = 0.320
    slg: float = 0.410
    power: float = 0.5
    contact: float = 0.5
    discipline: float = 0.5
    chase_rate: float = 0.30
    whiff_rate: float = 0.24
    aggression: float = 0.5
    pull: float = 0.40             # 引っ張り傾向。打球方向は batted_ball.py が決める
    gb_tendency: float = 0.45
    vs_fastball: float = 0.5
    vs_breaking: float = 0.5
    vs_offspeed: float = 0.5
    hot_course: str = "mid_mid"     # 9 分割コースのキー(constants.COURSES)。
    weak_course: str = "mid_lo"     # データ側で必ず上書きされる想定だが、既定も有効な値にしておく
    weak_pitch: str = "slider"
    guess_bias: float = 0.55
    two_strike_ability: float = 0.5
    pressure_tolerance: float = 0.5
    speed: int = 50
    ops: float = field(init=False)

    def __post_init__(self):
        self.ops = round(self.obp + self.slg, 3)

    # ---------- 公開情報の見せ方 ----------
    def public_hand_name(self):
        return HANDEDNESS[self.bats]

    def type_label(self):
        return COARSE_TYPES.get(self.coarse_type, self.coarse_type)

    def public_line(self):
        return f"{self.bats} / OPS {self.ops:.3f}"

    def scouting_note(self):
        """打者タイプから読める程度のヒント。実際の隠し値とは一致しない。"""
        return {
            "power": "引っ張りの長打が怖い",
            "contact": "広角に当ててくる",
            "patient": "球を見てくる・簡単に振らない",
            "free_swinger": "早いカウントから振ってくる",
            "average": "目立った傾向は薄い",
            "weak": "強い打球は少ない",
        }.get(self.coarse_type, "傾向不明")

    def family_strength(self, family):
        return {"fastball": self.vs_fastball,
                "breaking": self.vs_breaking,
                "offspeed": self.vs_offspeed}[family]

    # ---------- 狙い球の推測(#8) ----------
    def predict_guess(self, history, state, pitcher):
        """この打者が今「何を待っているか」。プレイヤーには見せない。

        返り値:
            {"class": "fastball"|"offspeed", "class_strength": 0..1,
             "location": "inside"|"outside"|"middle"|"any", "loc_strength": 0..1}

        材料: カウント / 打者の性格(guess_bias, discipline) / 配球履歴 /
              同じ球の連続 / 投手の球種割合 / 前の球 / 打席をまたぐ記憶(打者一巡)。
        """
        lean_fastball = self.guess_bias   # 1.0 に近いほど速球待ち

        # 1. 配球履歴で見せられた球に山を張る
        recent = history.recent_class_counts(4)
        seen = recent["fastball"] + recent["offspeed"]
        if seen > 0:
            lean_fastball = 0.5 * lean_fastball + 0.5 * (recent["fastball"] / seen)
        else:
            # 履歴なし → 投手の持ち球の速球比率をうっすら反映
            reps = pitcher.repertoire
            fb = sum(1 for k in reps if guess_class_of(k) == "fastball")
            lean_fastball = 0.6 * lean_fastball + 0.4 * (fb / max(1, len(reps)))

        # 1.5 打席をまたぐ記憶(打者一巡): 前の打席で見せられた傾向を薄く覚えている
        memory = getattr(state, "batter_memory", None)
        mem = memory(self) if memory else None
        if mem:
            mem_total = mem["fastball"] + mem["offspeed"]
            if mem_total > 0:
                lean_fastball = 0.75 * lean_fastball + 0.25 * (mem["fastball"] / mem_total)

        # 2. カウント
        if state.balls > state.strikes:
            lean_fastball += 0.15          # 打者有利カウントは速球狙い
        if state.strikes == 2:
            lean_fastball = 0.5 + (lean_fastball - 0.5) * 0.4   # 絞りきれない

        # 3. 同じ球種を続けられている → その球に強く張る
        streak_class = None
        last = history.last()
        if history.same_type_streak() >= 2 and last is not None:
            streak_class = last["pitch_class"]
            if streak_class == "fastball":
                lean_fastball = min(1.0, lean_fastball + 0.20)
            else:
                lean_fastball = max(0.0, lean_fastball - 0.20)

        # 4. 前の球が速球ストライクなら、次は変化球を意識しやすい
        if last is not None and last["pitch_class"] == "fastball" and last["swung"] is False \
                and last.get("in_zone"):
            lean_fastball -= 0.08

        lean_fastball = _clamp(lean_fastball, 0.0, 1.0)
        if lean_fastball >= 0.5:
            guess_class = "fastball"
            class_strength = (lean_fastball - 0.5) * 2
        else:
            guess_class = "offspeed"
            class_strength = (0.5 - lean_fastball) * 2

        # 規律の低い打者ほど「決め打ち」しやすい → strength を持ち上げる
        class_strength = _clamp(class_strength * (1.2 - 0.4 * self.discipline), 0.0, 1.0)

        # 5. コースの狙い(打者有利カウントでは得意ゾーンに山を張る)
        if state.strikes == 2:
            location, loc_strength = "any", 0.0
        elif state.balls > state.strikes:
            location, loc_strength = self.hot_course, round(0.4 + 0.4 * class_strength, 2)
        else:
            location, loc_strength = "any", 0.15

        return {"class": guess_class, "class_strength": round(class_strength, 2),
                "location": location, "loc_strength": loc_strength}


# --------- 素早い簡易生成(テストやクイック確認用) ---------
def create_random_batter(rng=None):
    rng = rng or random
    coarse = rng.choice(["power", "contact", "average"])
    bats = "R" if rng.random() < 0.6 else "L"
    obp = round(rng.uniform(0.290, 0.370), 3)
    slg = round(rng.uniform(0.360, 0.520), 3)
    return Batter(
        name="打者", bats=bats, coarse_type=coarse,
        avg=round(rng.uniform(0.230, 0.300), 3), obp=obp, slg=slg,
        power=rng.uniform(0.3, 0.8), contact=rng.uniform(0.3, 0.8),
        discipline=rng.uniform(0.3, 0.7), chase_rate=rng.uniform(0.20, 0.40),
        whiff_rate=rng.uniform(0.15, 0.32), aggression=rng.uniform(0.4, 0.7),
        pull=round(_clamp(rng.gauss(0.45, 0.1), 0.15, 0.85), 2),
        gb_tendency=round(_clamp(rng.gauss(0.45, 0.08), 0.2, 0.7), 2),
        vs_fastball=rng.uniform(0.35, 0.65), vs_breaking=rng.uniform(0.35, 0.65),
        vs_offspeed=rng.uniform(0.35, 0.65),
        hot_course=rng.choice(list_courses()), weak_course=rng.choice(list_courses()),
        weak_pitch=rng.choice(all_pitch_keys()),
        guess_bias=round(rng.uniform(0.35, 0.75), 2),
        two_strike_ability=rng.uniform(0.3, 0.7), pressure_tolerance=rng.uniform(0.3, 0.7),
        speed=rng.randint(35, 70),
    )


def list_courses():
    from constants import COURSES
    return list(COURSES)


def random_course(rng):
    return rng.choice(list_courses())
