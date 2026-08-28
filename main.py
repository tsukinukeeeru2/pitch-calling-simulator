"""実行の入口。終盤の 1 イニングを捕手として守り切るゲーム。

    python main.py
    python main.py --lineup lineups/sample_team.json   # 打線を差し替え(#C)

進行役に徹する:
    状況の生成・保持   → match_state.py
    打線 / 投手        → lineup.py / pitcher.py
    1 球の判定         → judge.py（実投球・狙い・セットアップを内部で処理）
    配球意図・分析     → strategy.py
    画面の組み立て     → ui.py / ballpark_view.py / reaction.py
"""

import argparse
import datetime as _dt
import json
import os
import random

from ballpark_view import render_field
from defense import ALIGNMENTS
from fielders import POSITIONS
from judge import judge_pitch
from lineup import load_lineup_file
from match_state import generate_random_situation
from constants import is_corner, zone_x, zone_y
from pitch_data import family_of, repertoire_options, velocity_of
from qte import catcher_block, catcher_throw
from reaction import describe_reaction
from strategy import PITCH_INTENTS, grade_reads
from ui import (
    render_analysis,
    render_dashboard,
    render_fielder_table,
    render_inning_result,
    render_intro,
    render_play_result,
    render_situation_card,
    render_spray,
)

FAIR_HITS = ("単打", "二塁打", "三塁打", "本塁打")


def choose(prompt, options):
    """options(辞書 key->表示名)から 1 つ選ばせ、選ばれた key を返す。"""
    keys = list(options.keys())
    print(prompt)
    for i, key in enumerate(keys, start=1):
        print(f"  {i}: {options[key]}")
    while True:
        answer = input("番号を入力: ").strip()
        if answer.isdigit() and 1 <= int(answer) <= len(keys):
            return keys[int(answer) - 1]
        print("正しい番号を入力してください。")


_COURSE_GRID = ["in_hi", "mid_hi", "out_hi",
                "in_mid", "mid_mid", "out_mid",
                "in_lo", "mid_lo", "out_lo"]


def choose_course(prompt="狙うコース（3x3、番号で）:", allow_unknown=False):
    print(prompt)
    print("   1 内高   2 高め   3 外高")
    print("   4 内角   5 真中   6 外角")
    print("   7 内低   8 低め   9 外低")
    if allow_unknown:
        print("   0 わからない")
    lo = 0 if allow_unknown else 1
    while True:
        a = input("番号を入力: ").strip()
        if a.isdigit() and lo <= int(a) <= 9:
            return None if a == "0" else _COURSE_GRID[int(a) - 1]
        print("正しい番号を入力してください。")


def choose_action(state):
    extras = ""
    if state.runners[0] and not state.runners[1]:
        extras += "    [w] 敬遠"
    if state.can_change_pitcher():
        extras += "    [p] 継投"
    print(f"[Enter] 投球へ    [d] 守備を変更    [m] 打者の読みをメモ{extras}    [q] 終了")
    answer = input("> ").strip().lower()
    return {"d": "defense", "m": "memo", "w": "walk", "p": "change", "q": "quit"}.get(answer, "pitch")


def defense_menu(defense):
    position_options = {pos: pos for pos in POSITIONS}
    while True:
        print()
        print(render_field(defense))
        print(render_fielder_table(defense))
        print(" 1: 選手を入れ替える(Swap)   2: 守備シフトを変える   0: 戻る")
        cmd = input("> ").strip()
        if cmd == "1":
            a = choose("動かす選手のポジション:", position_options)
            b = choose("入れ替え先のポジション:", position_options)
            defense.swap(a, b)
            print(f"→ {a} と {b} を入れ替えました。")
        elif cmd == "2":
            key = choose("守備シフト:", ALIGNMENTS)
            defense.set_alignment(key)
            print(f"→ シフトを「{ALIGNMENTS[key]}」にしました。")
        elif cmd == "0":
            return


def memo_menu(state):
    """この打者の『読み』を記録する(#B)。試合後に正解と照合される。"""
    print(f"\n打者 #{state.lineup.spot_number()} についての読み")
    wait = choose("待ち球はどっち?", {"fastball": "速球待ち", "offspeed": "変化球待ち",
                                       "unknown": "わからない"})
    weak = choose_course("弱そうなコースは?", allow_unknown=True)
    state.record_read(wait=None if wait == "unknown" else wait, weak=weak)
    print("→ メモしました（試合後に採点されます）。")


def apply_result(state, outcome):
    """1 球の結果をカウント・状況に反映。三振・四球・走塁はここで確定。"""
    result = outcome["result"]
    if result in ("ストライク", "空振り"):
        state.strikes += 1
        if state.strikes >= 3:
            print(">>> 三振!")
            state.add_out()
    elif result == "ボール":
        state.balls += 1
        if state.balls >= 4:
            print(">>> 四球")
            state.advance_on_walk()
    elif result == "ファウル":
        if state.strikes < 2:
            state.strikes += 1
    elif result in FAIR_HITS or result == "アウト":
        play = outcome["play"]
        if result in FAIR_HITS:
            extra = f"（{play['detail']}）" if play["detail"] else ""
            print(f">>> {play['label']}を打たれた!{extra}")
        else:
            note = play["label"] if play["label"] != "アウト" else play["detail"]
            print(">>> 打ち取った!" + (f"（{note}）" if note else ""))
        if play["runs"]:
            print(f">>> {play['runs']} 点を失った…")
        state.spray_add(outcome["batted_ball"], result)
        state.apply_play(play)


def log_pitch(state, outcome):
    """試合後の分析用に 1 球を記録する。"""
    an = outcome["_analysis"]
    state.pitch_log.append({
        "pitch_number": len(state.pitch_log) + 1,
        "call_label": f"{outcome['pitch_type']}/{outcome['actual_course']}"
                      f"({PITCH_INTENTS[outcome['intent']].split('（')[0]})",
        "result": outcome["result"],
        "decision_quality": an["decision_quality"],
        "sequence_label": an["sequence_label"],
        "guess": an["guess"],
        "fooled_guess": an["fooled_guess"],
        "alignment_helped": an["alignment_helped"],
        "missed": outcome["missed"],
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


def play_one_pitch(state, rng):
    pitch_type = choose("球種:", repertoire_options(state.pitcher))
    course = choose_course()
    intent = choose("配球意図:", PITCH_INTENTS)

    outcome = judge_pitch(state, pitch_type, course, intent=intent)
    outcome["reaction"] = describe_reaction(pitch_type, outcome["actual_course"], outcome)

    print()
    print(render_play_result(outcome))

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
            print(f">>> ワンバウンド三振！ 振り逃げのチャンス（{why}）")
            hard = is_corner(ac)
            block_bonus = catcher_block(zone_x(ac), wild=not low_dirt, rng=rng)
            safe, msg = state.resolve_dropped_third(
                block_bonus, lambda: catcher_throw(rng, base="一塁", title="拾った！ 一塁へ送れ"),
                rng, hard=hard)
            print(f">>> {msg}")
            return outcome

    # ワンバウンド / 暴投 + 走者あり → ブロッキング(方向ボタン + 1・2・3 のリズム)
    if any(state.runners) and outcome["missed"] and result in ("ボール", "空振り"):
        low_dirt = zone_y(ac) == "lo" and not outcome["in_zone"]
        if low_dirt or rng.random() < 0.30:          # 低め暴投は必ず、他の失投も時々
            hard = is_corner(ac)
            block_bonus = catcher_block(zone_x(ac), wild=not low_dirt, rng=rng)
            msg = state.resolve_block(block_bonus, rng, hard=hard)
            if msg:
                print(f">>> {msg}")

    # 打球が飛ばなかった球 → 走者が盗塁を試みることがある(3 段階 QTE)
    if result in ("ボール", "ストライク", "空振り") and state.steal_chance(rng):
        print(">>> 走者がスタート！")
        throw_bonus = catcher_throw(rng)
        ev = state.resolve_steal(fam, rng, throw_bonus=throw_bonus)
        if ev:
            print(f">>> {ev}")

    if state.is_over():
        return outcome

    apply_result(state, outcome)
    return outcome


def _parse_args():
    p = argparse.ArgumentParser(description="終盤の守り切りゲーム")
    p.add_argument("--lineup", help=".json / .csv の相手打線ファイル")
    return p.parse_args()


def main():
    args = _parse_args()
    rng = random
    batters = None
    if args.lineup:
        batters = load_lineup_file(args.lineup).batters
        print(f"打線ファイルを読み込みました: {args.lineup}")

    state = generate_random_situation(lineup_batters=batters)
    print(render_intro())
    print()
    print(render_situation_card(state))

    last_outcome = None
    while not state.is_over():
        print()
        print(render_dashboard(state, last_outcome))
        print(render_spray(state))
        print(render_field(state.defense, state.batter))

        action = choose_action(state)
        if action == "quit":
            print("中断します。")
            return
        if action == "defense":
            defense_menu(state.defense)
            continue
        if action == "memo":
            memo_menu(state)
            continue
        if action == "walk":
            print(">>> 申告敬遠。打者を歩かせた。")
            state.intentional_walk()
            last_outcome = None
            continue
        if action == "change":
            if state.change_pitcher():
                print(f">>> 継投。マウンドに {state.pitcher.name}（{'右' if state.pitcher.throws == 'R' else '左'}投）。"
                      "球数はリセット。持ち球・調子は投げてみるまで分からない。")
            continue

        last_outcome = play_one_pitch(state, rng)

    print()
    print(render_inning_result(state))
    print()
    print(render_analysis(state))
    try:
        path = save_game_log(state)
        print("\n" + f"ログを保存しました: {path}")
    except OSError as err:
        print(f"（ログ保存に失敗: {err}）")


if __name__ == "__main__":
    main()
