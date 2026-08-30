"""実行の入口(CLI)。終盤の 1 イニングを捕手として守り切るゲーム。

    python main.py
    python main.py --lineup lineups/sample_team.json   # 打線を差し替え(#C)

進行役に徹する:
    状況の生成・保持       → match_state.py
    打線 / 投手            → lineup.py / pitcher.py
    1 球の判定 + 周辺処理  → engine.py（judge.py を内部で呼ぶ Game Engine）
    配球意図・分析         → strategy.py
    画面の組み立て(CLI)   → ui.py / ballpark_view.py / reaction.py

webapp.py(Web版)も同じ engine.py を呼ぶ。判定ロジックの二重実装はしない。
"""

import argparse
import os
import random
import sys

# Windows で `python main.py > log.txt` のように出力をリダイレクトすると、
# 端末以外では cp932 になり、罫線や顔文字で UnicodeEncodeError で落ちる。
# 表示できない文字は「?」に置き換えて、進行だけは止めないようにする。
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8", errors="replace")
    except (AttributeError, ValueError):
        pass

import ansi
from ballpark_view import render_field
from defense import ALIGNMENTS
from engine import begin_at_bat, maybe_resolve_sign_steal, resolve_one_pitch, save_game_log
from qte import catcher_return_ball
from fielders import POSITIONS
from lineup import load_lineup_file
from match_state import generate_random_situation
from pitch_data import repertoire_options
from strategy import PITCH_INTENTS
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
    if not all(state.runners):          # 満塁でなければ敬遠できる(押し出しになる満塁だけ除外)
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


def change_pitcher_menu(state):
    """控え投手が複数いれば選ばせる(1 人なら自動で出す)。"""
    if len(state.bullpen) == 1:
        index = 0
    else:
        options = {str(i): f"{p.name}（{'右' if p.throws == 'R' else '左'}投）"
                   for i, p in enumerate(state.bullpen)}
        index = int(choose("継投: マウンドに送る投手を選択:", options))
    new_pitcher = state.change_pitcher(index)
    if new_pitcher:
        print(f">>> 継投。マウンドに {new_pitcher.name}（{'右' if new_pitcher.throws == 'R' else '左'}投）。"
              "球数はリセット。持ち球・調子は投げてみるまで分からない。")


def memo_menu(state):
    """この打者の『読み』を記録する(#B)。試合後に正解と照合される。"""
    print(f"\n打者 #{state.lineup.spot_number()} についての読み")
    wait = choose("待ち球はどっち?", {"fastball": "速球待ち", "offspeed": "変化球待ち",
                                       "unknown": "わからない"})
    weak = choose_course("弱そうなコースは?", allow_unknown=True)
    state.record_read(wait=None if wait == "unknown" else wait, weak=weak)
    print("→ メモしました（試合後に採点されます）。")


def play_one_pitch(state, rng):
    tactic_msg = begin_at_bat(state, rng)
    if tactic_msg:
        print(ansi.bold(ansi.yellow(f">>> {tactic_msg}（ピッチアウトや高め速球で対抗できる）")))
    for msg in maybe_resolve_sign_steal(state, rng):
        print(msg)

    # 直前の球を捕球していたら、投手へリズムよく返球する(連打なし・3拍)
    tempo = 0.0
    last = state.history.last()
    if last is not None and last.get("result") in ("ボール", "ストライク", "空振り"):
        tempo = catcher_return_ball(rng)

    pitch_type = choose("球種:", repertoire_options(state.pitcher))
    course = choose_course()
    intent = choose("配球意図:", PITCH_INTENTS)

    outcome, messages = resolve_one_pitch(state, pitch_type, course, intent, rng, tempo=tempo)

    print()
    print(render_play_result(outcome))
    for msg in messages:
        print(msg)
    return outcome


_DIFFICULTIES = ("beginner", "normal", "expert")


def _parse_args():
    p = argparse.ArgumentParser(description="終盤の守り切りゲーム")
    p.add_argument("--lineup", help=".json / .csv の相手打線ファイル")
    p.add_argument("--difficulty", choices=_DIFFICULTIES,
                   default=os.environ.get("PITCHSIM_DIFFICULTY", "normal"),
                   help="beginner=ヒント表示を増やす / expert=打球チャートを隠す（既定: normal）")
    p.add_argument("--seed", type=int, default=None,
                   help="乱数シード。指定すると場面生成・投球判定が再現可能になる")
    p.add_argument("--mlb-demo", action="store_true",
                   help="実在選手のサンプル打線を使う(mlb_data_adapter 経由)")
    p.add_argument("--mlb-live", action="store_true",
                   help="--mlb-demo と併用。MLB Stats API から実際に取得を試みる"
                       "(失敗したら自動でサンプルデータにフォールバック)")
    return p.parse_args()


def main():
    args = _parse_args()
    rng = random.Random(args.seed) if args.seed is not None else random
    batters = None
    if args.mlb_demo:
        from mlb_data_adapter import build_demo_lineup
        lineup, source = build_demo_lineup(prefer_live=args.mlb_live)
        batters = lineup.batters
        print(f"MLBサンプル打線を読み込みました（{'MLB Stats API' if source == 'live' else 'サンプルデータ'}）")
    elif args.lineup:
        batters = load_lineup_file(args.lineup).batters
        print(f"打線ファイルを読み込みました: {args.lineup}")

    state = generate_random_situation(rng=rng, lineup_batters=batters)
    print(render_intro(hints=(args.difficulty == "beginner")))
    print()
    print(render_situation_card(state))

    last_outcome = None
    while not state.is_over():
        print()
        print(render_dashboard(state, last_outcome))
        if args.difficulty != "expert":            # 上級者は打球チャートなしで読む
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
            change_pitcher_menu(state)
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
