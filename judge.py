"""配球判断の中心ロジック(1 球の判定)。

流れ(#5 の順番を守る):

    Pitch Call          捕手の要求(球種 / 狙うコース / 配球意図)
      ↓  pitcher.execute_pitch()
    Actual Pitch        実際のコース・球の出来(quality)。失投でズレる
      ↓  batter.predict_guess() / strategy.evaluate_sequencing()
    Batter Reaction     狙いが当たったか → タイミング → 振る/見る → 空振り/ファウル/フェア
      ↓  batted_ball / fielding
    Result             フェアなら打球と守備でアウト/ヒット

捕手の判断は確率を動かすが、結果は
  打者能力・投手能力・quality・実際のコース・打者の狙い・守備・乱数
の積み重ねで決まる。捕手だけで 100% は操作できない。

返り値の "_analysis" は試合後の分析専用。プレイ中の画面には出さない。
"""

import random

from baserunning import resolve as resolve_baserunning
from batted_ball import generate_batted_ball
from constants import COURSE_PROFILE, zone_overlap, zone_x, zone_y
from fielding import resolve_batted_ball
from pitch_data import family_of, get_pitch, guess_class_of, velocity_of
from strategy import (
    catcher_decision_quality,
    evaluate_sequencing,
    intent_effects,
)


def _decide_timing(guess_class, guess_strength, actual_class, seq, history, pitch_type, rng):
    """打者の狙いと実際の球種クラスから、タイミングを決める。"""
    shift = seq["timing_shift"]        # セットアップで崩せていれば負(= mistime しやすい)

    if guess_class == actual_class:
        on_time_chance = 0.5 + 0.4 * guess_strength + shift
        if rng.random() < max(0.05, on_time_chance):
            timing = "on_time"
        else:
            timing = rng.choice(["early", "late"])
    else:
        if guess_class == "fastball" and actual_class == "offspeed":
            missed = "early"          # 速球待ちに変化球
        else:
            missed = "late"           # 変化球待ちに速球
        if rng.random() < 0.25 * (1 - guess_strength) + shift * 0.5:
            timing = "on_time"
        else:
            timing = missed

    # 前球との球速差でも mistime する
    last = history.last()
    if timing == "on_time" and last is not None and last.get("velocity") is not None:
        gap = velocity_of(pitch_type) - last["velocity"]
        if abs(gap) >= 8 and rng.random() < 0.30:
            timing = "late" if gap > 0 else "early"
    return timing


def _history_whiff_adjust(history, pitch_type, course, batter):
    """配球履歴と打者の隠し traits から、空振り確率の増減。

    苦手/得意ゾーンは zone_overlap で「近ければ部分的に」効く(9 分割対応)。
    """
    adjust = 0.0
    if pitch_type == batter.weak_pitch:
        adjust += 0.12
    adjust += 0.08 * zone_overlap(course, batter.weak_course)
    adjust -= 0.08 * zone_overlap(course, batter.hot_course)
    if history.same_type_streak() >= 2:
        adjust -= 0.05
    return adjust


def _contact_quality_bias(history, pitch_type, course, batter):
    """『配球や相性で作った打ち頃度』。プラスほど強い打球になりやすい。

    直接ヒット/アウトを決めるのではなく、打球の "強さ" を動かす。
    """
    bias = 0.0
    bias += (batter.power - 0.5) * 0.30                 # 打者のパワー
    bias += COURSE_PROFILE[course]["contact_bonus"]     # コースの甘さ/厳しさ
    if history.same_course_streak() >= 3:
        bias += 0.06
    last = history.last()
    if (last is not None and last["swung"]
            and last["pitch_type"] == pitch_type and last["timing"] != "on_time"):
        bias += 0.06
    bias += 0.14 * zone_overlap(course, batter.hot_course)     # 隠しホットゾーン
    if pitch_type == batter.weak_pitch:
        bias -= 0.12
    bias -= 0.09 * zone_overlap(course, batter.weak_course)    # 隠し苦手ゾーン
    return bias


# 受球リズムゲーム(Web版のみ)の出来 → フレーミング確率の増減。
# frame_timing=None(CLI・きわどくない球・テスト)のときは 0.0 = 従来どおり。
_FRAME_TIMING_BONUS = {"perfect": 0.22, "good": 0.06, "miss": -0.10}


def _frame_chance(actual_course, intent, state, frame_timing=None):
    """ゾーンを外した見送りを「ストライク」に見せられる確率。

    - 隅(横も縦も端)は最も稼ぎやすい / 片側だけ端は中くらい
    - "freeze"(見逃しを取りにいく)意図のときは少し上がる
    - 3 ボールの見送りは審判も慎重 → 少し下がる
    - frame_timing は Web の受球リズムゲームの結果。perfect なら大きく上がり miss なら下がる。
    """
    both_edge = zone_x(actual_course) != "mid" and zone_y(actual_course) != "mid"
    p = 0.16 if both_edge else 0.09
    if intent == "freeze":
        p += 0.06
    if state.balls >= 3:
        p -= 0.05
    p += _FRAME_TIMING_BONUS.get(frame_timing, 0.0)
    return max(0.0, min(0.9, p))


def _pressure_swing(batter, state):
    """大事な場面で pressure_tolerance が低いと、選球がブレる。"""
    high_leverage = (state.outs >= 1 and any(state.runners)) or abs(state.score_diff) <= 1
    if not high_leverage:
        return 0.0
    return (0.5 - batter.pressure_tolerance) * 0.12   # 弱いと振りやすく/ブレやすい


def judge_pitch(state, pitch_type, target_course="middle", rng=None, intent="strike",
                frame_timing=None, tempo=0.0):
    rng = rng or random

    pitcher = state.pitcher
    batter = state.batter
    history = state.history
    family = family_of(pitch_type)
    guess_class2 = guess_class_of(pitch_type)          # 2 択(速球/変化球)
    fam_strength = batter.family_strength(family)      # 打者のその系統への強さ

    # 1. EXECUTION: 要求 → 実際
    actual = pitcher.execute_pitch(pitch_type, target_course, intent, rng, tempo=tempo)
    actual_course = actual["actual_course"]
    quality = actual["quality"]
    course_p = COURSE_PROFILE[actual_course]

    # 2. GUESS(打者の狙い。隠し情報)
    guess = batter.predict_guess(history, state, pitcher)
    # サインを見破られていたら(#B)、この 1 球だけ狙いを実際の球種に強く合わせる
    if getattr(state, "sign_leak", 0.0) > 0:
        guess = dict(guess, **{"class": guess_class2,
                               "class_strength": max(guess["class_strength"], state.sign_leak)})
        state.sign_leak = 0.0

    # 3. SEQUENCING(前球が次を活かす)
    call = {"pitch_type": pitch_type, "target_course": target_course, "intent": intent}
    seq = evaluate_sequencing(history, call, actual, batter, guess, state)
    intent_adj, intent_dq = intent_effects(intent, state.balls, state.strikes, batter, family)

    # 4. TIMING
    timing = _decide_timing(guess["class"], guess["class_strength"], guess_class2,
                            seq, history, pitch_type, rng)

    # 5. IN-ZONE(実際のコースの strike 率 × 出来 × 意図)
    zone_chance = course_p["strike_rate"]
    if intent in ("chase", "waste"):
        zone_chance *= 0.45
    elif intent == "freeze":
        zone_chance = min(0.95, zone_chance + 0.10)
    zone_chance += (quality - 0.5) * 0.15
    # walk_adj(意図・セットアップが「ボールになりやすい」方向) → ゾーン確率を下げる
    zone_chance -= (intent_adj["walk_adj"] + seq["walk_adj"])
    in_zone = rng.random() < max(0.03, min(0.97, zone_chance))

    dq_total = catcher_decision_quality(call, actual, batter, guess, guess_class2, timing,
                                        history, state, seq) + intent_dq
    dq_total = max(-1.0, min(1.0, dq_total))

    analysis = {
        "guess": guess,
        "decision_quality": round(dq_total, 3),
        "sequence_label": seq["sequence_label"],
        "sequence_notes": seq["notes"],
        "fooled_guess": guess["class"] != guess_class2,
        "alignment_helped": False,
    }

    def _pack(result, swung, batted_ball=None, fielding=None, play=None, framed=False):
        return {
            "result": result, "timing": timing, "swung": swung, "in_zone": in_zone,
            "pitch_type": pitch_type, "target_course": target_course,
            "actual_course": actual_course, "intent": intent,
            "missed": actual["missed"], "quality": round(quality, 2),
            "batted_ball": batted_ball, "fielding": fielding, "play": play, "framed": framed,
            "_analysis": analysis,
        }

    # ピッチアウトは走者を狙って大きく外す球。ほぼ確実にボールで、打者は関与しない。
    if intent == "pitchout":
        in_zone = False
        return _pack("ボール", swung=False)

    # 6. SWING
    swing_chance = (0.60 + 0.22 * (batter.aggression - 0.5)) if in_zone else batter.chase_rate
    if state.strikes == 2:
        swing_chance += 0.15 * (0.6 + 0.4 * (1 - batter.two_strike_ability))
    if guess["class"] == guess_class2:
        swing_chance += 0.10 * guess["class_strength"]
    else:
        swing_chance -= 0.10 * guess["class_strength"]
    if guess["location"] != "any":
        swing_chance += 0.06 * guess["loc_strength"] * zone_overlap(guess["location"], actual_course)
    swing_chance += intent_adj["swing_adj"] + seq["swing_adj"]
    swing_chance += _pressure_swing(batter, state)
    swung = rng.random() < max(0.02, min(0.98, swing_chance))

    if not swung:
        if in_zone:
            return _pack("ストライク", swung)
        # --- フレーミング(捕手の craft) ---
        # ゾーンをわずかに外した見送りだけ、静かに捕って「ストライク」に見せられることがある。
        # きわどい所(端・隅)を要求しているほど効き、真ん中を大きく外した球には効かない。
        edge = zone_x(actual_course) != "mid" or zone_y(actual_course) != "mid"
        clearly_out = intent in ("chase", "waste", "pitchout")   # 元から大きく外す意図
        if (edge and not clearly_out
                and rng.random() < _frame_chance(actual_course, intent, state, frame_timing)):
            state.framed_strikes = getattr(state, "framed_strikes", 0) + 1
            return _pack("ストライク", swung, framed=True)
        return _pack("ボール", swung)

    # 7. WHIFF
    whiff_chance = get_pitch(pitch_type)["whiff_rate"] * (0.7 + 0.6 * quality)
    if not in_zone:
        whiff_chance += 0.14
    if timing != "on_time":
        whiff_chance += 0.12
    whiff_chance += (batter.whiff_rate - 0.24) * 0.6
    whiff_chance -= (fam_strength - 0.5) * 0.15
    whiff_chance += _history_whiff_adjust(history, pitch_type, actual_course, batter)
    whiff_chance += seq["whiff_adj"] + intent_adj["whiff_adj"]
    if rng.random() < max(0.02, min(0.95, whiff_chance)):
        return _pack("空振り", swung)

    # 8. FOUL
    foul_chance = 0.30
    if timing != "on_time":
        foul_chance += 0.13
    if state.strikes == 2:
        foul_chance += 0.06 * batter.two_strike_ability
    if rng.random() < foul_chance:
        return _pack("ファウル", swung)

    # 9. FAIR → 打球 → 守備
    contact_bias = _contact_quality_bias(history, pitch_type, actual_course, batter)
    contact_bias += (quality - 0.5) * -0.30
    contact_bias += (fam_strength - 0.5) * 0.20
    contact_bias += seq["contact_adj"] + intent_adj["contact_adj"]
    batted_ball = generate_batted_ball(batter, get_pitch(pitch_type), actual_course,
                                       timing, contact_bias=contact_bias, rng=rng)
    fielding = resolve_batted_ball(batted_ball, state.defense, batter, rng=rng)
    play = resolve_baserunning(batted_ball, fielding, state.runners, state.outs, batter, rng,
                               runner_speeds=getattr(state, "runner_speeds", None),
                               runner_moving=(getattr(state, "opp_tactic", None) == "hit_and_run"))
    analysis["alignment_helped"] = (
        fielding["breakdown"]["alignment"] > 0 and fielding["result"] == "アウト")
    return _pack(play["batter_result"], swung, batted_ball, fielding, play)
