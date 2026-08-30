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

import opponent
from constants import COURSE_SHORT, is_corner, zone_x, zone_y
from judge import judge_pitch
from pitch_data import family_of, pitch_name, velocity_of
from qte import catcher_block, catcher_change_signs, catcher_field_bunt, catcher_throw
from reaction import describe_reaction
from strategy import PITCH_INTENTS, grade_reads

FAIR_HITS = ("単打", "二塁打", "三塁打", "本塁打")

_NEUTRAL_ANALYSIS = {
    "guess": {"class": "fastball", "class_strength": 0.0}, "decision_quality": 0.0,
    "sequence_label": "特筆なし", "sequence_notes": [],
    "fooled_guess": False, "alignment_helped": False,
}


def begin_at_bat(state, rng):
    """打席の初球の前に 1 回呼ぶ。相手ベンチの作戦(#C)を決め、捕手に見える文言を返す。

    バントの構えは見えるが、エンドランは見えない(空文字を返す)。
    """
    # 冪等: 打席ごとに一度だけ乱数を引く。Web 版はページを再描画するたびに
    # begin_at_bat を呼ぶので、フラグが無いと GET のたびに decide_tactic が
    # 乱数を消費し、--seed の再現性が崩れる(作戦の有無まで変わりうる)。
    if (state.history.records or state.opp_tactic is not None
            or getattr(state, "tactic_decided", False)):
        return ""
    state.tactic_decided = True
    state.opp_tactic = opponent.decide_tactic(state, rng)
    return opponent.describe(state.opp_tactic) if state.opp_tactic else ""


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
        # ふり返り画面(CLI/Web共通)の表示専用。内部キーそのままだと
        # "sinker/mid_mid" のように読みにくいので、球種名・コースの短縮表記に
        # 差し替えたもの。call_label 自体は stats.py が正規表現で解析する
        # 決まった書式なので変えない。
        "call_label_ja": f"{pitch_name(outcome['pitch_type'])}/{COURSE_SHORT[outcome['actual_course']]}"
                         f"({PITCH_INTENTS[outcome['intent']].split('（')[0]})",
        "result": outcome["result"],
        "decision_quality": an["decision_quality"],
        "sequence_label": an["sequence_label"],
        "guess": an["guess"],
        "fooled_guess": an["fooled_guess"],
        "alignment_helped": an["alignment_helped"],
        "missed": outcome["missed"],
        "quality": round(outcome["quality"], 2),
        "framed": outcome.get("framed", False),
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


# ---------------------------------------------------------------------------
# QTE(リズムゲーム)の駆け橋
#
# 1 球の判定は resolve_pitch_events() というジェネレータで行う。捕手のリズム
# プレーが要るところで ("種別", payload) を yield し、呼び出し側が答え
#   frame        → "perfect" / "good" / "miss" / None
#   それ以外      → bonus(float、-0.25〜0.25)
# を .send() で返す。最後に (outcome, messages) を return する。
#
#   CLI (main.py) / テスト : resolve_one_pitch() が qte.* を即時に呼んで答える
#                            (端末なら実際のリズム入力、非対話なら 0.0 / None)
#   Web (webapp.py)        : ジェネレータを HTTP リクエストをまたいで駆動し、
#                            ブラウザのタイミングUIの結果を答えとして返す
# 判定ロジックそのものは変えていない ―― QTE の呼び出し方だけを差し替えられる
# ように yield にした。
# ---------------------------------------------------------------------------


def _cli_answer(kind, payload, rng, frame_timing):
    """CLI / テスト用: yield された QTE に qte.* を即座に呼んで答える。"""
    if kind == "frame":
        return frame_timing                      # 明示指定があればそれ、無ければ None=確率のみ
    if kind == "change_signs":
        return catcher_change_signs(rng)
    if kind == "steal_throw":
        return catcher_throw(rng)
    if kind == "d3_throw":
        return catcher_throw(rng, base="一塁", title="拾った！ 一塁へ送れ")
    if kind in ("wild_block", "d3_block"):
        return catcher_block(payload["dir"], wild=payload["wild"], rng=rng)
    if kind == "field_bunt":
        return catcher_field_bunt(rng)
    return 0.0


def _drive(gen, answer_fn):
    """ジェネレータを最後まで回し、各 yield を answer_fn(kind, payload) で答える。"""
    answer = None
    try:
        while True:
            kind, payload = gen.send(answer)
            answer = answer_fn(kind, payload)
    except StopIteration as stop:
        return stop.value


def _sign_steal_events(state, rng):
    """二塁走者にサインを覗われるリスク(打席途中いつでも)。messages を return。"""
    messages = []
    if state.sign_steal_chance(rng):
        messages.append(">>> 二塁走者がサインを覗っている気配…")
        sign_bonus = yield ("change_signs", {})
        messages.append(f">>> {state.resolve_sign_steal(sign_bonus, rng)}")
    return messages


def maybe_resolve_sign_steal(state, rng):
    """サイン盗みだけを単体で処理して messages を返す(CLI 補助・テスト用)。"""
    return _drive(_sign_steal_events(state, rng),
                  lambda k, p: _cli_answer(k, p, rng, None))


def resolve_one_pitch(state, pitch_type, course, intent, rng, frame_timing=None, tempo=0.0):
    """1 球を判定して状態を更新し、(outcome, メッセージのリスト) を返す。

    振り逃げ・ブロッキング・盗塁・バント処理の QTE は、端末なら実際のリズム
    入力、非対話なら確率モデルで自動処理する。frame_timing を渡すと受球
    タイミング(フレーミング)をその値に固定できる。tempo は捕手の返球リズムの
    出来(-0.25〜0.25)。良いほど投手が乗る。
    """
    return _drive(resolve_pitch_events(state, pitch_type, course, intent, rng, tempo=tempo),
                  lambda k, p: _cli_answer(k, p, rng, frame_timing))


def resolve_pitch_flow(state, pitch_type, course, intent, rng, tempo=0.0):
    """Web 用: サイン盗み → 1 球の判定 を一続きに駆動するジェネレータ。"""
    pre = yield from _sign_steal_events(state, rng)
    outcome, msgs = yield from resolve_pitch_events(state, pitch_type, course, intent, rng,
                                                   tempo=tempo)
    return outcome, pre + msgs


def _call_label(pitch_type, course, intent):
    return (f"{pitch_name(pitch_type)} / {COURSE_SHORT[course]}"
            f" / {PITCH_INTENTS[intent].split('（')[0]}")


def resolve_pitch_events(state, pitch_type, course, intent, rng, tempo=0.0):
    """1 球の判定＋周辺イベント(ジェネレータ)。QTE 地点で yield する。"""
    messages = []

    # 相手が犠打(バント)なら、通常の打席判定ではなくバント専用の解決へ
    if state.opp_tactic == "bunt":
        return (yield from _resolve_bunt_events(state, pitch_type, course, intent, rng, messages))

    # きわどい球(隅を狙ってストライクを取りにいく)は、投球の前に受球のタイミング。
    # velocity を渡す = 速い球は早く(左で)・遅い球は遅く(右で)受ける、を Web 側で作る。
    frame_timing = None
    if is_corner(course) and intent in ("strike", "freeze", "weak_contact"):
        frame_timing = yield ("frame", {"call": _call_label(pitch_type, course, intent),
                                        "velocity": velocity_of(pitch_type)})

    # いい配球が続いていると、次の 1 球で投手が少し乗る(甘め・最大 +0.15)。
    tempo += state.good_call_bonus()

    outcome = judge_pitch(state, pitch_type, course, rng, intent=intent,
                          frame_timing=frame_timing, tempo=tempo)
    outcome["reaction"] = describe_reaction(pitch_type, outcome["actual_course"], outcome, rng)

    log_pitch(state, outcome)

    # 今の 1 球が「いい配球」だったかを更新(数値は表に出さない。連続時だけ一言)。
    good_call = state.register_call_quality(outcome["_analysis"]["decision_quality"],
                                           outcome["result"])
    outcome["good_call"] = good_call
    outcome["good_call_streak"] = state.good_call_streak
    if good_call:
        s = state.good_call_streak
        if s == 2:
            messages.append(">>> いい配球が続いてる。この調子（配球ボーナス）")
        elif s == 4:
            messages.append(">>> 完全にこっちのペース。投手が乗ってきた（配球ボーナス）")
        elif s >= 6 and s % 3 == 0:
            messages.append(">>> 圧巻のリード。バッテリーで試合を支配してる（配球ボーナス）")
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
            block_bonus = yield ("d3_block", {"dir": zone_x(ac), "wild": not low_dirt})
            throw_bonus = yield ("d3_throw", {})
            _, msg = state.resolve_dropped_third(block_bonus, throw_bonus, rng, hard=hard)
            messages.append(f">>> {msg}")
            # 振り逃げは生きても刺されても、打者の成績上は「三振」扱い(実際の野球のルール)
            if state.pitch_log:
                state.pitch_log[-1]["at_bat_end"] = "strikeout"
            return outcome, messages

    # ブロッキング(方向ボタン + タイミング)。フェアに打たれなかった球で:
    #   ・低めの変化球(breaking / offspeed)は必ずワンバウンド気味 → 毎回ブロッキング
    #   ・それ以外の失投も、走者がいれば時々(低め暴投は必ず)
    low_break = (fam in ("breaking", "offspeed") and zone_y(ac) == "lo"
                 and result in ("ボール", "空振り"))
    low_dirt = zone_y(ac) == "lo" and not outcome["in_zone"]
    wild_case = (any(state.runners) and outcome["missed"] and result in ("ボール", "空振り")
                 and (low_dirt or rng.random() < 0.30))
    if low_break or wild_case:
        hard = is_corner(ac)
        block_bonus = yield ("wild_block", {"dir": zone_x(ac), "wild": not (low_break or low_dirt)})
        msg = state.resolve_block(block_bonus, rng, hard=hard)
        if msg:
            messages.append(f">>> {msg}")

    # エンドラン: 打者がフェアに打たなかった球でも、一塁走者はスタートしている
    if (state.opp_tactic == "hit_and_run" and state.runners[0]
            and result in ("ボール", "ストライク", "空振り", "ファウル")):
        pitched_out = intent == "pitchout"
        tb = (yield ("steal_throw", {})) if pitched_out else 0.0
        ev = state.hit_and_run_runner_go(pitched_out, contact=False, rng=rng, throw_bonus=tb)
        if ev:
            prefix = ">>> エンドランを見破った！ " if pitched_out else ">>> "
            messages.append(prefix + ev)

    # 打球が飛ばなかった球 → 走者が盗塁を試みることがある(送球リズム)
    elif result in ("ボール", "ストライク", "空振り") and state.steal_chance(rng):
        messages.append(">>> 走者がスタート！")
        throw_bonus = yield ("steal_throw", {})
        if intent == "pitchout":
            throw_bonus += 0.35                      # ピッチアウトは走者を刺すための球
            messages.append(">>> ピッチアウトが決まった！")
        ev = state.resolve_steal(fam, rng, throw_bonus=throw_bonus)
        if ev:
            messages.append(f">>> {ev}")

    if state.is_over():
        return outcome, messages

    apply_result(state, outcome, on_event=messages.append)
    return outcome, messages


def _resolve_bunt_events(state, pitch_type, course, intent, rng, messages):
    """相手の犠打を解決する(ジェネレータ。バント処理のリズムを yield する)。"""
    actual = state.pitcher.execute_pitch(pitch_type, course, intent, rng)
    ac = actual["actual_course"]
    fam = family_of(pitch_type)
    pitched_out = intent == "pitchout"
    high_heat = fam == "fastball" and zone_y(ac) == "hi"
    charging = state.defense.alignment == "bunt"

    field_bonus = 0.0 if pitched_out else (yield ("field_bunt", {}))

    msgs, final, pa_end = state.resolve_bunt(pitched_out, high_heat, charging, rng,
                                            field_bonus=field_bonus)
    messages += [m if m.startswith(">>>") else f">>> {m}" for m in msgs]

    outcome = {
        "result": final, "timing": "on_time", "swung": not pitched_out, "in_zone": False,
        "pitch_type": pitch_type, "target_course": course, "actual_course": ac,
        "intent": intent, "missed": actual["missed"], "quality": round(actual["quality"], 2),
        "batted_ball": None, "fielding": None, "play": None, "reaction": None,
        "_analysis": dict(_NEUTRAL_ANALYSIS),
    }
    log_pitch(state, outcome)
    state.register_call_quality(outcome["_analysis"]["decision_quality"], final)
    outcome["good_call"] = False
    outcome["good_call_streak"] = state.good_call_streak
    state.history.add(pitch_type, course, final, "on_time", not pitched_out,
                      actual_course=ac, intent=intent, velocity=velocity_of(pitch_type),
                      family=fam, in_zone=False)
    if state.pitch_log and pa_end:
        state.pitch_log[-1]["at_bat_end"] = pa_end
    return outcome, messages
