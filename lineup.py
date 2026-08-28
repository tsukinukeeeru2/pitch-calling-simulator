"""相手打線(9 人)と打順の管理。

- SAMPLE_LINEUP : 特徴が明確に違う架空の 9 人(打順 1〜9 番)の設計値
- build_sample_lineup() : そこから Batter を 9 人作る(archetype 内で少しランダム化)
- Lineup : 9 人 + 現在の打順位置。advance() で 1 番→2 番→…→9 番→1 番 と回る
- Lineup.from_records() / load_lineup_file() : 将来 CSV / JSON / API のデータに差し替える口

将来 侍ジャパンや MLB チームの実データを使うときは、選手 1 人を
下の dict と同じキーの dict にして from_records() に渡せばよい。
"""

import csv
import json
import random

from batter import Batter

# 各 archetype の「中心値」。build_sample_lineup がこの周辺で乱数を振る。
# spot: 打順 / coarse_type: 表示ラベル用
SAMPLE_LINEUP = [
    {"spot": 1, "name": "1 リードオフ", "coarse_type": "patient", "bats": "L",
     "obp": 0.375, "slg": 0.390, "power": 0.30, "contact": 0.70, "discipline": 0.78,
     "chase_rate": 0.20, "whiff_rate": 0.16, "aggression": 0.40,
     "pull": 0.40, "gb": 0.55, "vs_fb": 0.60, "vs_br": 0.50, "vs_off": 0.52,
     "hot": "mid_lo", "weak": "in_mid", "guess_bias": 0.62, "two_strike": 0.65,
     "pressure": 0.60, "speed": 72},
    {"spot": 2, "name": "2 コンタクト", "coarse_type": "contact", "bats": "R",
     "obp": 0.340, "slg": 0.400, "power": 0.32, "contact": 0.80, "discipline": 0.62,
     "chase_rate": 0.26, "whiff_rate": 0.13, "aggression": 0.55,
     "pull": 0.45, "gb": 0.50, "vs_fb": 0.58, "vs_br": 0.56, "vs_off": 0.50,
     "hot": "out_mid", "weak": "mid_hi", "guess_bias": 0.58, "two_strike": 0.70,
     "pressure": 0.55, "speed": 58},
    {"spot": 3, "name": "3 総合型", "coarse_type": "power", "bats": "L",
     "obp": 0.375, "slg": 0.520, "power": 0.72, "contact": 0.68, "discipline": 0.66,
     "chase_rate": 0.24, "whiff_rate": 0.20, "aggression": 0.55,
     "pull": 0.55, "gb": 0.42, "vs_fb": 0.66, "vs_br": 0.60, "vs_off": 0.58,
     "hot": "mid_mid", "weak": "in_mid", "guess_bias": 0.55, "two_strike": 0.62,
     "pressure": 0.62, "speed": 52},
    {"spot": 4, "name": "4 強打者", "coarse_type": "power", "bats": "R",
     "obp": 0.360, "slg": 0.560, "power": 0.85, "contact": 0.58, "discipline": 0.55,
     "chase_rate": 0.28, "whiff_rate": 0.27, "aggression": 0.60,
     "pull": 0.66, "gb": 0.38, "vs_fb": 0.66, "vs_br": 0.54, "vs_off": 0.50,
     "hot": "in_mid", "weak": "out_mid", "guess_bias": 0.60, "two_strike": 0.52,
     "pressure": 0.58, "speed": 42},
    {"spot": 5, "name": "5 長打型", "coarse_type": "power", "bats": "R",
     "obp": 0.320, "slg": 0.540, "power": 0.88, "contact": 0.46, "discipline": 0.44,
     "chase_rate": 0.32, "whiff_rate": 0.33, "aggression": 0.66,
     "pull": 0.72, "gb": 0.33, "vs_fb": 0.60, "vs_br": 0.44, "vs_off": 0.42,
     "hot": "mid_mid", "weak": "mid_lo", "guess_bias": 0.66, "two_strike": 0.40,
     "pressure": 0.50, "speed": 40},
    {"spot": 6, "name": "6 フリースインガー", "coarse_type": "free_swinger", "bats": "L",
     "obp": 0.300, "slg": 0.430, "power": 0.55, "contact": 0.50, "discipline": 0.30,
     "chase_rate": 0.40, "whiff_rate": 0.30, "aggression": 0.80,
     "pull": 0.58, "gb": 0.45, "vs_fb": 0.52, "vs_br": 0.42, "vs_off": 0.40,
     "hot": "mid_hi", "weak": "out_mid", "guess_bias": 0.70, "two_strike": 0.38,
     "pressure": 0.45, "speed": 50},
    {"spot": 7, "name": "7 平均的", "coarse_type": "average", "bats": "R",
     "obp": 0.320, "slg": 0.400, "power": 0.50, "contact": 0.55, "discipline": 0.50,
     "chase_rate": 0.30, "whiff_rate": 0.24, "aggression": 0.55,
     "pull": 0.48, "gb": 0.47, "vs_fb": 0.50, "vs_br": 0.50, "vs_off": 0.50,
     "hot": "mid_mid", "weak": "mid_lo", "guess_bias": 0.55, "two_strike": 0.50,
     "pressure": 0.50, "speed": 52},
    {"spot": 8, "name": "8 選球眼型", "coarse_type": "patient", "bats": "L",
     "obp": 0.340, "slg": 0.330, "power": 0.25, "contact": 0.58, "discipline": 0.80,
     "chase_rate": 0.18, "whiff_rate": 0.20, "aggression": 0.35,
     "pull": 0.42, "gb": 0.52, "vs_fb": 0.50, "vs_br": 0.48, "vs_off": 0.46,
     "hot": "mid_lo", "weak": "mid_hi", "guess_bias": 0.60, "two_strike": 0.62,
     "pressure": 0.55, "speed": 55},
    {"spot": 9, "name": "9 俊足弱打", "coarse_type": "weak", "bats": "R",
     "obp": 0.290, "slg": 0.320, "power": 0.22, "contact": 0.48, "discipline": 0.44,
     "chase_rate": 0.34, "whiff_rate": 0.28, "aggression": 0.55,
     "pull": 0.46, "gb": 0.60, "vs_fb": 0.44, "vs_br": 0.42, "vs_off": 0.42,
     "hot": "mid_mid", "weak": "out_mid", "guess_bias": 0.58, "two_strike": 0.45,
     "pressure": 0.45, "speed": 80},
]


def _jitter(rng, value, amount, lo=0.0, hi=1.0):
    return round(max(lo, min(hi, value + rng.uniform(-amount, amount))), 3)


def _batter_from_spec(spec, rng):
    avg = _jitter(rng, spec["obp"] - 0.060, 0.010, 0.20, 0.34)
    return Batter(
        name=spec["name"], bats=spec["bats"], coarse_type=spec["coarse_type"],
        avg=avg, obp=_jitter(rng, spec["obp"], 0.012, 0.26, 0.42),
        slg=_jitter(rng, spec["slg"], 0.020, 0.28, 0.62),
        power=_jitter(rng, spec["power"], 0.06), contact=_jitter(rng, spec["contact"], 0.06),
        discipline=_jitter(rng, spec["discipline"], 0.05),
        chase_rate=_jitter(rng, spec["chase_rate"], 0.03, 0.12, 0.5),
        whiff_rate=_jitter(rng, spec["whiff_rate"], 0.03, 0.08, 0.45),
        aggression=_jitter(rng, spec["aggression"], 0.05),
        pull=_jitter(rng, spec["pull"], 0.05, 0.15, 0.85),
        gb_tendency=_jitter(rng, spec["gb"], 0.04, 0.2, 0.7),
        vs_fastball=_jitter(rng, spec["vs_fb"], 0.04), vs_breaking=_jitter(rng, spec["vs_br"], 0.04),
        vs_offspeed=_jitter(rng, spec["vs_off"], 0.04),
        hot_course=spec["hot"], weak_course=spec["weak"],
        weak_pitch=rng.choice(["slider", "curveball", "changeup", "splitter", "sweeper"]),
        guess_bias=_jitter(rng, spec["guess_bias"], 0.05, 0.3, 0.8),
        two_strike_ability=_jitter(rng, spec["two_strike"], 0.05),
        pressure_tolerance=_jitter(rng, spec["pressure"], 0.05),
        speed=int(spec["speed"] + rng.randint(-4, 4)),
    )


def build_sample_lineup(rng=None):
    rng = rng or random
    return [_batter_from_spec(spec, rng) for spec in SAMPLE_LINEUP]


class Lineup:
    def __init__(self, batters, index=0):
        assert len(batters) == 9, "打線は 9 人"
        self.batters = list(batters)
        self.index = index % 9

    def current(self):
        return self.batters[self.index]

    def spot_number(self):
        """今の打者が『何番』か(1〜9)。"""
        return self.index + 1

    def advance(self):
        self.index = (self.index + 1) % 9

    # ---- 将来のデータ差し替え口 ----
    @classmethod
    def from_records(cls, records, index=0):
        """records: 選手 1 人 = dict のリスト(9 件)。

        Batter.__init__ が知っているキーだけを拾う(余分な "_note" 等は無視)。
        MLB / Statcast の CSV・JSON をここに流し込めば実在打線になる。
        """
        if len(records) != 9:
            raise ValueError(f"打線は 9 人ちょうど必要です（{len(records)} 人でした）")
        batters = [Batter(**{k: v for k, v in rec.items() if k in _BATTER_FIELDS})
                   for rec in records]
        return cls(batters, index)


_BATTER_FIELDS = {
    "name", "bats", "coarse_type", "avg", "obp", "slg", "power", "contact",
    "discipline", "chase_rate", "whiff_rate", "aggression", "pull", "gb_tendency",
    "vs_fastball", "vs_breaking", "vs_offspeed", "hot_course", "weak_course",
    "weak_pitch", "guess_bias", "two_strike_ability", "pressure_tolerance", "speed",
}


def load_lineup_file(path, index=0):
    """.json / .csv から打線を読む(将来 実データ用。今はサンプルで十分)。"""
    if path.endswith(".json"):
        with open(path, encoding="utf-8") as fp:
            records = json.load(fp)
    elif path.endswith(".csv"):
        with open(path, encoding="utf-8", newline="") as fp:
            records = [
                {k: (float(v) if _looks_number(v) else v) for k, v in row.items()}
                for row in csv.DictReader(fp)
            ]
    else:
        raise ValueError("対応形式は .json / .csv")
    return Lineup.from_records(records, index)


def _looks_number(value):
    try:
        float(value)
        return True
    except (TypeError, ValueError):
        return False
