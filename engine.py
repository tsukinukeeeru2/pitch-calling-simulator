"""Game Engine ―― CLI(main.py)と Web(webapp.py)が共有する「1 球を判定して
状態を進める」ロジック。

    Game Engine (engine.py)
        ↑            ↑
      main.py     webapp.py
      (CLI)         (Web)

このファイルは何も print() しない・input() もしない。表示層(CLI か Web か)
は resolve_one_pitch() が返す (outcome, メッセージのリスト) を、それぞれの
やり方(print / HTML)で見せるだけ。ロジックの二重実装を避けるための境界線。

    Pitch Call(球種・コース・意図)
      ↓  judge.judge_pitch()
    Actual Pitch → 打者反応 → (フェアなら)打球・守備・走塁
      ↓
    振り逃げ / ブロッキング / 盗塁 / サイン盗み見 などの周辺イベント
      ↓
    apply_result()  ―― カウント・アウト・走者を確定させる
"""

import datetime as _dt
import json
import os

from constants import is_corner, zone_x, zone_y
from judge import judge_pitch
from pitch_data import family_of, velocity_of
from qte import catcher_block, catcher_change_signs, catcher_throw
from reaction import describe_reaction
from strategy import PITCH_INTENTS, grade_reads

FAIR_HITS = ("単打", "二塁打", "三塁打", "本塁打")


def apply_result(state, outcome, on_event=print):
    """1 球の結果をカウント・状況に反映。三振・四球・走塁はここで確定。

    on_event : 出来事の文字列を受け取るコールバック(既定は print)。
              CLI 以外の表示層(webapp.py 等)はメッセージのリストに積むだけの
              関数を渡せば、このロジックをそのまま再利用できる。

    打席が終わったら、直前に log_pitch() が積んだ pitch_log の最後の 1 件に
    at_bat_end("strikeout"/"walk"/"hit"/"error"/"out")を付け足す。Postgame
    Report(#Batters Faced / Strikeouts / Walks / Hits)が使うだけの集計用タグで、
    判定そのものには一切影響しない。
    """
    result = outcome["result"]
    pa_end = None
    if result in ("ストライク", "空振り"):
        state.strikes += 1
        if state.strikes >= 3:
            on_event(">>> 三振!")
            state.add_out()
            pa_end = "strikeout"
    elif result == "ボール":
        state.balls += 1
        if state.balls >= 4:
            on_event(">>> 四球")
            state.advance_on_walk()
            pa_end = "walk"
    elif result == "ファウル":
        if state.strikes < 2:
            state.strikes += 1
    elif result in FAIR_HITS or result in ("アウト", "エラー"):
        play = outcome["play"]
        if result in FAIR_HITS:
            extra = f"（{play['detail']}）" if play["detail"] else ""
            on_event(f">>> {play['label']}を打たれた!{extra}")
        elif result == "エラー":
            extra = f"（{play['detail']}）" if play["detail"] else ""
            on_event(f">>> 味方のエラー！ 出塁を許した{extra}")
        else:
            note = play["label"] if play["label"] != "アウト" else play["detail"]
            on_event(">>> 打ち取った!" + (f"（{note}）" if note else ""))
        if play["runs"]:
            on_event(f">>> {play['runs']} 点を失った…")
        state.spray_add(outcome["batted_ball"], result)
        state.apply_play(play)
        pa_end = "hit" if result in FAIR_HITS else ("error" if result == "エラー" else "out")

    if pa_end and state.pitch_log:
        state.pitch_log[-1]["at_bat_end"] = pa_end


def log_pitch(state, outcome):
    """試合後の分析用に 1 球を記録する。"""
    an = outcome["_analysis"]
    state.pitch_log.append({
        "pitch_number": len(state.pitch_log) + 1,
        "pitch_type": outcome["pitch_type"],
        "target_course": outcome["target_course"],
        "actual_course": outcome["actual_course"],
        "intent": outcome["intent"],
        "call_label": f"{outcome['pitch_type']}/{outcome['actual_course']}"
                      f"({PITCH_INTENTS[outcome['intent']].split('（')[0]})",
        "result": outcome["result"],
        "decision_quality": an["decision_quality"],
        "sequence_label": an["sequence_label"],
        "guess": an["guess"],
        "fooled_guess": an["fooled_guess"],
        "alignment_helped": an["alignment_helped"],
        "missed": outcome["missed"],
        "quality": round(outcome["quality"], 2),
        "outcome_flags": {"swung": outcome["swung"], "in_zone": outcome["in_zone"]},
    })


def save_game_log(state):
    """試合ログを logs/ に JSON 保存する(#E)。"""
    os.makedirs("logs", exist_ok=True)
    path = os.path.join("logs", _dt.datetime.now().strftime("game_%Y%m%d_%H%M%S.json"))
    run_txt, status = state.result_summary()
    reads = grade_reads(state.reads, state.lineup.batters)
    with open(path, "w", encoding="utf-8") as fp:
        json.dump({
            "situation": {"inning": state.inning_label(),
                          "start": [state.start_our, state.start_opp]},
            "final": {"our": state.our_score, "opp": state.opp_score,
                      "runs_this_inning": state.runs_this_inning,
                      "result": run_txt, "status": status},
            "pitches": state.pitch_log,
            "spray": state.spray,
            "events": state.events,
            "reads": {"correct": reads["correct"], "graded": reads["graded"],
                      "rows": reads["rows"]},
        }, fp, ensure_ascii=False, indent=2)
    return path


def maybe_resolve_sign_steal(state, rng):
    """二塁走者にサインを覗われるリスクを判定する(打席途中いつでも)。

    表示メッセージのリストを返すだけで、呼び出し側が印字/表示する。
    """
    if not state.sign_steal_chance(rng):
        return []
    messages = [">>> 二塁走者がサインを覗っている気配…"]
    sign_bonus = catcher_change_signs(rng)
    messages.append(f">>> {state.resolve_sign_steal(sign_bonus, rng)}")
    return messages


def resolve_one_pitch(state, pitch_type, course, intent, rng):
    """1 球を判定して状態を更新し、(outcome, メッセージのリスト) を返す。

    振り逃げ・ブロッキング・盗塁など投球後に起きる周辺イベントもここで解決する。
    print() はしない ―― CLI(main.py)・Web(webapp.py) の両方から呼べるように、
    表示は呼び出し側の責務にしてある。
    """
    messages = []
    outcome = judge_pitch(state, pitch_type, course, rng, intent=intent)
    outcome["reaction"] = describe_reaction(pitch_type, outcome["actual_course"], outcome, rng)

    log_pitch(state, outcome)
    state.history.add(pitch_type, course, outcome["result"], outcome["timing"],
                      outcome["swung"], actual_course=outcome["actual_course"],
                      intent=intent, velocity=velocity_of(pitch_type),
                      family=family_of(pitch_type), in_zone=outcome["in_zone"])

    fam = family_of(pitch_type)
    result = outcome["result"]
    ac = outcome["actual_course"]

    # ワンバウンドの三振 + 振り逃げが成立しうる(一塁が空 or 2アウト) → 振り逃げ判定
    if (result == "空振り" and state.strikes == 2 and outcome["missed"]
            and state.dropped_third_eligible()):
        low_dirt = zone_y(ac) == "lo" and not outcome["in_zone"]
        if low_dirt or rng.random() < 0.30:
            why = "一塁が空いている" if not state.runners[0] else "2アウト"
            messages.append(f">>> ワンバウンド三振！ 振り逃げのチャンス（{why}）")
            hard = is_corner(ac)
            block_bonus = catcher_block(zone_x(ac), wild=not low_dirt, rng=rng)
            _, msg = state.resolve_dropped_third(
                block_bonus, lambda: catcher_throw(rng, base="一塁", title="拾った！ 一塁へ送れ"),
                rng, hard=hard)
            messages.append(f">>> {msg}")
            # 振り逃げは生きても刺されても、打者の成績上は「三振」扱い(実際の野球のルール)
            if state.pitch_log:
                state.pitch_log[-1]["at_bat_end"] = "strikeout"
            return outcome, messages

    # ワンバウンド / 暴投 + 走者あり → ブロッキング(方向ボタン + 1・2・3 のリズム)
    if any(state.runners) and outcome["missed"] and result in ("ボール", "空振り"):
        low_dirt = zone_y(ac) == "lo" and not outcome["in_zone"]
        if low_dirt or rng.random() < 0.30:          # 低め暴投は必ず、他の失投も時々
            hard = is_corner(ac)
            block_bonus = catcher_block(zone_x(ac), wild=not low_dirt, rng=rng)
            msg = state.resolve_block(block_bonus, rng, hard=hard)
            if msg:
                messages.append(f">>> {msg}")

    # 打球が飛ばなかった球 → 走者が盗塁を試みることがある(3 段階 QTE)
    if result in ("ボール", "ストライク", "空振り") and state.steal_chance(rng):
        messages.append(">>> 走者がスタート！")
        throw_bonus = catcher_throw(rng)
        ev = state.resolve_steal(fam, rng, throw_bonus=throw_bonus)
        if ev:
            messages.append(f">>> {ev}")

    if state.is_over():
        return outcome, messages

    apply_result(state, outcome, on_event=messages.append)
    return outcome, messages
