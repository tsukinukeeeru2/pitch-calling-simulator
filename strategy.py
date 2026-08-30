"""配球の「意図」「セットアップ(前球が次を活かす)」「捕手の判断の質」。

ここが返すのは、judge.py の確率に足す **小さな補正**と、
試合後の分析用の **メモ(notes)** と、内部だけで使う **decision_quality**。

大方針:
  - 「この配球なら +20%」のような固定ボーナスは作らない。
  - 打者の狙い / 前球 / 配球履歴 / カウント / 左右 / タイミング / 実際のコース が
    噛み合ったときだけ効果が出る。効果量は各ルール ±0.02〜0.06、合計もclampする。
  - decision_quality は結果を決めない。あくまで「判断の質」の内部指標。
"""

from constants import zone_overlap, zone_x, zone_y
from pitch_data import family_of, velocity_of

# 捕手が毎球選ぶ「配球意図」
PITCH_INTENTS = {
    "strike": "ストライク先行（カウントを取る）",
    "chase": "チェイス（ボール球を振らせる）",
    "weak_contact": "打たせて取る（弱い当たり狙い）",
    "waste": "見せ球（次を活かす）",
    "freeze": "見逃しを狙う（きわどいゾーン）",
    "pitchout": "ピッチアウト（大きく外して走者を狙う）",
}

_ADJ_KEYS = ("timing_shift", "swing_adj", "whiff_adj", "contact_adj", "walk_adj")


def _clamp(x, lo, hi):
    return max(lo, min(hi, x))


# ---------------------------------------------------------------------------
# 1. 配球意図の効果(カウントと相性で ±)
# ---------------------------------------------------------------------------
def intent_effects(intent, balls, strikes, batter, family):
    adj = dict.fromkeys(_ADJ_KEYS, 0.0)
    dq = 0.0

    if intent == "strike":
        adj["swing_adj"] += 0.03
        adj["whiff_adj"] -= 0.04
        dq += 0.10 if (balls >= strikes) else -0.05
        if strikes == 2:
            adj["contact_adj"] += 0.04         # 追い込んで置きにいくと痛打も
            dq -= 0.05

    elif intent == "chase":
        adj["swing_adj"] -= 0.05               # ゾーン外なので基本は見送られる
        if strikes == 2:
            adj["whiff_adj"] += 0.06
            adj["swing_adj"] += 0.08 * batter.aggression
            dq += 0.15
        else:
            adj["walk_adj"] += 0.05
            dq -= 0.20 if balls >= 3 else -0.05

    elif intent == "weak_contact":
        adj["contact_adj"] -= 0.05
        adj["whiff_adj"] -= 0.02
        dq += 0.06
        if family != "fastball":
            dq -= 0.03                         # 変化球で「打たせて取る」は噛み合いにくい

    elif intent == "waste":
        adj["swing_adj"] -= 0.10
        if strikes == 2 and balls <= 1:
            dq += 0.10                         # 追い込んでからの見せ球は理にかなう
        else:
            dq -= 0.15                         # 苦しいカウントでの見せ球は無駄球

    elif intent == "freeze":
        adj["swing_adj"] -= 0.06
        if strikes < 2:
            dq += 0.08

    elif intent == "pitchout":
        # ほぼ確実にボール。走者を狙う球なので打者への影響はほぼ無し。
        adj["swing_adj"] -= 0.40
        adj["walk_adj"] += 0.30
        # 判断の質: 走者が動いてくる場面なら価値がある/そうでなければ 1 球損なだけ
        dq += -0.20 if balls >= 2 else -0.05

    return adj, dq


# ---------------------------------------------------------------------------
# 2. セットアップ(前球が次の球を活かす。#6 #7)
#    各ルールは「条件が揃ったときだけ」小さな補正 + メモを返す。
# ---------------------------------------------------------------------------
def _seq_rules(history, call, actual, batter, guess, state):
    pt = call["pitch_type"]
    fam = family_of(pt)
    velo = velocity_of(pt)
    actual_course = actual["actual_course"]
    ax, ay = zone_x(actual_course), zone_y(actual_course)
    is_meatball = actual_course == "mid_mid"
    last = history.last()
    records = history.records

    out = []   # (adjust_dict, note, dq_delta)

    # --- 初球 ---
    if last is None:
        if guess["class"] == "fastball" and fam != "fastball":
            out.append(({"whiff_adj": 0.04, "contact_adj": -0.04},
                        "初球の変化球で直球待ちを外した", 0.10))
        if is_meatball and guess["class"] == "fastball":
            out.append(({"contact_adj": 0.06}, "初球を狙われる真ん中に置いた", -0.12))
        return out

    lam_c = last["actual_course"]
    lam_x, lam_y = zone_x(lam_c), zone_y(lam_c)
    lam_v = last.get("velocity")
    lam_f = last.get("family")

    # --- ハシゴ(低め→高め / 高め→低め) ---
    if lam_y == "lo" and ay == "hi" and fam == "fastball":
        out.append(({"whiff_adj": 0.05, "timing_shift": -0.05},
                    "低め→高めの釣り球", 0.08))
    if lam_y == "hi" and ay == "lo" and fam in ("breaking", "offspeed"):
        out.append(({"whiff_adj": 0.04, "contact_adj": -0.05},
                    "高め見せてからの低め変化球", 0.08))

    # --- 球速差(前球との緩急) ---
    if lam_v is not None:
        gap = abs(velo - lam_v)
        if gap >= 8:
            out.append(({"whiff_adj": 0.03, "timing_shift": -0.03},
                        "前球との大きな球速差", 0.05))
            if lam_f is not None and lam_f != fam:
                out.append(({"whiff_adj": 0.02}, "球種と球速の両方を変えた", 0.03))
        elif gap <= 3 and lam_f == fam:
            out.append(({"contact_adj": 0.04},
                        "似た球速・同系統が続き読みやすい", -0.08))

    # --- 内外の揺さぶり ---
    if {lam_x, ax} == {"in", "out"}:
        out.append(({"swing_adj": -0.03, "whiff_adj": 0.02},
                    "内外を大きく揺さぶった", 0.05))

    # --- ストレート → チェンジアップ ---
    if lam_f == "fastball" and fam == "offspeed":
        out.append(({"whiff_adj": 0.05, "timing_shift": -0.04},
                    "ストレートからのチェンジアップ", 0.07))

    # --- ゾーンの変化球 → ボールゾーンの変化球 ---
    if lam_f == "breaking" and last.get("in_zone") and fam == "breaking" and ay != "mid":
        out.append(({"whiff_adj": 0.05, "swing_adj": 0.03},
                    "ゾーン内の変化球からボールゾーンの変化球", 0.07))

    # --- 差し込まれている打者へインコース速球 ---
    if records[-1].get("timing") == "late" and ax == "in" and fam == "fastball":
        out.append(({"whiff_adj": 0.04, "contact_adj": -0.05},
                    "差し込まれている打者へのインコース速球", 0.08))

    # --- 泳いでいる打者へ変化球 / 外の球 ---
    if records[-1].get("timing") == "early" and (fam != "fastball" or ax == "out"):
        out.append(({"whiff_adj": 0.04},
                    "泳いでいる打者への変化球・外の球", 0.06))

    # --- 右投手 vs 左打者 ---
    if state.pitcher.throws != batter.bats and (fam == "offspeed" or ax == "out"):
        out.append(({"contact_adj": -0.03},
                    "対左への逃げる球（プラトーン）", 0.04))

    # --- 同じ (球種, コース) を打席内で繰り返し ---
    same_combo = sum(1 for r in records
                     if r["pitch_type"] == pt and r["actual_course"] == actual_course)
    if same_combo >= 1:
        out.append(({"whiff_adj": -0.06, "contact_adj": 0.06},
                    "同じ球・同じコースの見せすぎ", -0.15))

    # --- 同系統が 3 連続以上 ---
    if history.same_type_streak() >= 3:
        out.append(({"contact_adj": 0.05},
                    "同系統の球が続き修正されやすい", -0.10))

    # --- 追い込んでから真ん中 ---
    if strikes_is_two(state) and is_meatball:
        out.append(({"contact_adj": 0.08},
                    "追い込んでから甘い球", -0.20))

    return out


def strikes_is_two(state):
    return state.strikes == 2


def evaluate_sequencing(history, call, actual, batter, guess, state):
    """セットアップ効果をまとめて返す。"""
    total = dict.fromkeys(_ADJ_KEYS, 0.0)
    dq_seq = 0.0
    notes = []
    label_bits = []

    for adjust, note, dq_delta in _seq_rules(history, call, actual, batter, guess, state):
        for key, value in adjust.items():
            total[key] += value
        dq_seq += dq_delta
        notes.append(note)
        label_bits.append(note)

    # 効果量が過剰にならないよう clamp(固定ボーナスにしない)
    for key in ("swing_adj", "whiff_adj", "contact_adj", "walk_adj"):
        total[key] = _clamp(total[key], -0.15, 0.15)
    total["timing_shift"] = _clamp(total["timing_shift"], -0.10, 0.10)
    dq_seq = _clamp(dq_seq, -0.25, 0.25)

    total["notes"] = notes
    total["dq_seq"] = dq_seq
    total["sequence_label"] = " / ".join(label_bits) if label_bits else "特筆なし"
    return total


# ---------------------------------------------------------------------------
# 3. Catcher Decision Quality(内部指標。プレイ中は絶対に表示しない。#9)
# ---------------------------------------------------------------------------
def _intent_count_fit(intent, balls, strikes):
    if intent == "chase":
        return 0.15 if strikes == 2 else (-0.25 if balls >= 3 else -0.05)
    if intent == "strike":
        return 0.10 if (balls >= strikes) else -0.05
    if intent == "freeze":
        return 0.08 if strikes < 2 else 0.0
    if intent == "weak_contact":
        return 0.05
    if intent == "waste":
        return 0.10 if (strikes == 2 and balls <= 1) else -0.15
    if intent == "pitchout":
        return -0.10 if balls >= 2 else -0.02   # 走者が動けば engine 側で別途プラス評価
    return 0.0


def _defense_synergy(defense, batter):
    align = defense.alignment
    if align == "pull" and batter.pull > 0.6:
        return 0.05
    if align == "oppo" and batter.pull < 0.4:
        return 0.05
    if align in ("pull", "oppo"):
        return -0.03
    return 0.0


def catcher_decision_quality(call, actual, batter, guess, guess_class2, timing,
                             history, state, seq):
    """良し悪しの内部スコア(-1〜+1)。結果は決めない。"""
    actual_course = actual["actual_course"]

    dq = 0.0
    # 狙い(速球/変化球)を外せたか
    fooled = (guess["class"] != guess_class2)
    dq += (0.30 if fooled else -0.30) * guess["class_strength"]
    # コース(打者の隠しゾーン。これは"判断の質"を後から評価するための真実)
    dq += 0.20 * zone_overlap(actual_course, batter.weak_course)
    dq -= 0.25 * zone_overlap(actual_course, batter.hot_course)
    if actual_course == "mid_mid":
        dq -= 0.12
    # タイミングを崩せたか
    if timing != "on_time":
        dq += 0.15
    # 見せすぎ
    if history.same_type_streak() >= 3:
        dq -= 0.15
    # 意図とカウントの相性
    dq += _intent_count_fit(call["intent"], state.balls, state.strikes)
    # セットアップの process
    dq += _clamp(0.6 * seq["dq_seq"], -0.15, 0.15)
    # 守備配置との相性
    dq += _defense_synergy(state.defense, batter)

    return _clamp(dq, -1.0, 1.0)


# ---------------------------------------------------------------------------
# 4. 試合後の分析(#10)。結果と判断を分けて集計する。
# ---------------------------------------------------------------------------
_HIT_RESULTS = ("単打", "二塁打", "三塁打", "本塁打")
_EXTRA_BASE = ("二塁打", "三塁打", "本塁打")


def _result_is_bad(result):
    return result in _HIT_RESULTS or result in ("四球", "エラー")


def _result_is_good(result, outcome):
    if result in ("空振り", "アウト"):
        return True
    if result == "ストライク" and not outcome["swung"] and outcome["in_zone"]:
        return True   # 見逃しストライクを奪えた
    return False


_WAIT_JP = {"fastball": "速球待ち", "offspeed": "変化球待ち"}


def grade_reads(reads, batters):
    """捕手メモ(#B)を、隠しの真実と照合して採点する。

    reads   : {id(batter): {"spot", "wait", "weak"}}
    batters : 打線 9 人(Batter)
    """
    by_id = {id(b): b for b in batters}
    rows, graded, correct = [], 0, 0
    for bid, note in reads.items():
        batter = by_id.get(bid)
        if batter is None:
            continue
        parts = [f"#{note.get('spot', '?')} {getattr(batter, 'name', '')}".strip()]
        if note.get("wait"):
            truth = "fastball" if batter.guess_bias >= 0.5 else "offspeed"
            hit = note["wait"] == truth
            graded += 1
            correct += hit
            mark = "◎当たり" if hit else "×はずれ"
            parts.append(f"待ち: {_WAIT_JP[note['wait']]} → {mark}（実際は{_WAIT_JP[truth]}）")
        if note.get("weak"):
            overlap = zone_overlap(note["weak"], batter.weak_course)
            graded += 1
            correct += (overlap >= 1.0)
            mark = "◎当たり" if overlap >= 1.0 else ("△おしい（同じ列か段）" if overlap >= 0.5 else "×はずれ")
            parts.append(f"弱点コース: {note['weak']} → {mark}（実際は{batter.weak_course}）")
        rows.append("  " + " / ".join(parts))
    return {"rows": rows, "graded": graded, "correct": correct}


def build_analysis(pitch_log):
    """pitch_log(1 球 = dict のリスト)を集計して、分かりやすい塊にする。"""
    total = len(pitch_log)
    good_calls, risky_calls = [], []
    fooled_moments, read_moments = [], []
    defense_moments, misfire_moments = [], []
    unlucky, lucky, extra_base = [], [], []

    dq_values = []
    for entry in pitch_log:
        dq = entry["decision_quality"]
        dq_values.append(dq)
        num = entry["pitch_number"]
        label = entry.get("call_label_ja", entry["call_label"])   # 表示用。なければ内部表記のまま
        result = entry["result"]

        if dq >= 0.25:
            good_calls.append((num, label, entry["sequence_label"]))
        if dq <= -0.25:
            risky_calls.append((num, label, entry["sequence_label"]))

        g = entry["guess"]
        if entry["fooled_guess"] and g["class_strength"] >= 0.4:
            fooled_moments.append((num, label))
        if (not entry["fooled_guess"]) and g["class_strength"] >= 0.5:
            read_moments.append((num, label))

        if entry.get("alignment_helped"):
            defense_moments.append((num, label))
        if entry["missed"] and _result_is_bad(result):
            misfire_moments.append((num, label))

        if dq >= 0.25 and _result_is_bad(result):
            unlucky.append((num, label, result))
        if dq <= -0.25 and _result_is_good(result, entry["outcome_flags"]):
            lucky.append((num, label, result))
        if result in _EXTRA_BASE:
            extra_base.append((num, label, result))

    avg_dq = sum(dq_values) / total if total else 0.0
    if avg_dq >= 0.15:
        dq_label = "良い（狙いを外し、崩せていた）"
    elif avg_dq >= 0.0:
        dq_label = "やや良い"
    elif avg_dq >= -0.15:
        dq_label = "やや甘い"
    else:
        dq_label = "甘い（読まれ・置きにいく球が多かった）"

    return {
        "total_pitches": total,
        "good_calls": good_calls,
        "risky_calls": risky_calls,
        "fooled_moments": fooled_moments,
        "read_moments": read_moments,
        "defense_moments": defense_moments,
        "misfire_moments": misfire_moments,
        "unlucky": unlucky,      # 判断は良かったが結果が悪かった
        "lucky": lucky,          # 判断は悪かったが結果に救われた
        "extra_base": extra_base,   # 浴びた長打
        "avg_decision_quality": round(avg_dq, 3),
        "decision_quality_label": dq_label,
    }


def build_postgame_report(state):
    """3 アウト後の Catcher Report 用の集計(#Postgame Report)。

    build_analysis() が持つ「判断の質」の分析に、結果だけでひと目で分かる
    数字(失点/対戦打者数/球数/三振/四球/被安打)と、判断の質(decision_quality)
    とは別軸の Pitch Execution(投手の実行力)・Defense(守備)の要約を足す。
    判定ロジックには一切触れない、集計だけの純粋関数。
    """
    log = state.pitch_log
    strikeouts = sum(1 for p in log if p.get("at_bat_end") == "strikeout")
    walks = sum(1 for p in log if p.get("at_bat_end") == "walk") + state.events.count("敬遠")
    hits = sum(1 for p in log if p.get("at_bat_end") == "hit")
    errors = sum(1 for p in log if p.get("at_bat_end") == "error")

    missed_pitches = sum(1 for p in log if p["missed"])
    avg_quality = sum(p["quality"] for p in log) / len(log) if log else 0.0

    return {
        "runs_allowed": state.runs_this_inning,
        "batters_faced": state.batters_faced,
        "pitches": len(log),
        "strikeouts": strikeouts,
        "walks": walks,
        "hits": hits,
        "errors": errors,
        "framed_strikes": getattr(state, "framed_strikes", 0),
        "pitch_execution": {
            "missed_pitches": missed_pitches,
            "missed_rate": round(missed_pitches / len(log), 3) if log else 0.0,
            "avg_quality": round(avg_quality, 3),
        },
        "defense": {
            "alignment_helped": sum(1 for p in log if p["alignment_helped"]),
            "errors": errors,
            "final_ede": round(state.defense.expected_defensive_efficiency(), 1),
        },
    }
