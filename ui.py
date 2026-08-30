"""CLI 表示の組み立て(色つき)。

ここを差し替えれば GUI 化できる(ロジックは触らない)。
色は ansi.py が担当し、パイプ時などは自動で無効になる。

decision_quality などの内部指標はプレイ中は絶対に出さない(試合後だけ)。
"""

import ansi
from constants import COURSE_SHORT
from pitch_data import pitch_name
from reaction import render_reaction_block
from strategy import PITCH_INTENTS, build_analysis, grade_reads

_COURSE_JP = COURSE_SHORT


def _pips(filled, total, color):
    filled = max(0, min(total, filled))
    return ansi.paint("●" * filled, color) + ansi.dim("○" * (total - filled))


def _result_color(result):
    if result in ("アウト", "空振り", "三振"):
        return "bgreen"
    if result in ("単打", "二塁打", "三塁打", "本塁打", "四球", "エラー"):
        return "bred"
    if result == "ボール":
        return "byellow"
    return "bcyan"          # ストライク / ファウル


_BEGINNER_HINTS = (
    "\n" + ansi.dim("--- 初心者向けヒント（--difficulty beginner）---") + "\n"
    " ・顔文字は本音とは限りません（60% 本音 / 15% 逆 / 25% ぼかし）。1 球だけで信じすぎない。\n"
    " ・配球意図の（）内は狙いの説明です。カウントや場面に合っているか意識してみましょう。\n"
    " ・3 アウト後の「配球のふり返り」では、結果と判断の質を分けて評価します。"
)


def render_intro(hints=False):
    text = (
        ansi.bold(ansi.cyan("══════════ 終盤の守り切りゲーム ══════════")) + "\n"
        " 毎回ちがう終盤の場面から。この半イニングで 3 アウトを取るまでが 1 ゲーム。\n"
        " 捕手として 球種 / 狙うコース / 配球意図 を選びます。\n"
        " 正解は出ません。反応・履歴・打者・守備・打球チャートから読んでください。\n"
        + ansi.dim("═══════════════════════════════════════════")
    )
    if hints:
        text += _BEGINNER_HINTS
    return text


# ---------------------------------------------------------------------------
# 開始時の状況カード
# ---------------------------------------------------------------------------
def render_situation_card(state):
    b = state.batter
    hand = "右投" if state.pitcher.throws == "R" else "左投"
    return "\n".join([
        ansi.dim("--------------------------------"),
        ansi.bold(ansi.cyan(state.inning_label())),
        f"自軍 {ansi.green(state.our_score)}  -  {ansi.bold(state.opp_score)} 相手",
        "",
        f"{state.outs} OUT    走者: {ansi.yellow(state.runners_text())}",
        f"投手: リリーフ ({hand})",
        "",
        ansi.bold(f"BATTER #{state.lineup.spot_number()}  {b.public_line()}"),
        ansi.yellow(b.type_label()),
        ansi.dim("--------------------------------"),
    ])


# ---------------------------------------------------------------------------
# 毎球のダッシュボード = スコアボード + ベース図 + 打者カード
# ---------------------------------------------------------------------------
def _diamond(state):
    occ = lambda i: (ansi.paint("◆", "bold", "byellow") if state.runners[i] else ansi.dim("◇"))
    return [
        f"        {occ(1)}",
        f"     {occ(2)}     {occ(0)}",
        f"        {ansi.dim('⌂')}",
    ]


def render_dashboard(state, last_outcome):
    b = state.batter
    hand = "右投" if state.pitcher.throws == "R" else "左投"
    tire = {0: "", 1: ansi.yellow("  ⚠疲れ"), 2: ansi.red("  ⚠⚠バテ")}[state.pitcher.fatigue_level()]

    head = ansi.bold(ansi.cyan(f"═══ {state.inning_label()} ═══"))
    score = f"相手 {ansi.bold(state.opp_score)} - {ansi.green(state.our_score)} 自軍"
    line1 = f" {head}  {score}   投手{hand}　球数 {state.pitcher.pitches_thrown}{tire}"
    line2 = (f"   OUT {_pips(state.outs, 3, 'bred')}    "
             f"B {_pips(state.balls, 4, 'byellow')}   S {_pips(state.strikes, 3, 'bcyan')}"
             f"   走者 {ansi.yellow(state.runners_text())}")
    if state.lead_runner_fast() and not state.runners[1]:
        line2 += ansi.red("  ⚡盗塁警戒")

    dia = _diamond(state)
    card = [
        ansi.bold(f" ┌ BATTER #{state.lineup.spot_number()}  {b.type_label()}  "
                  f"{b.public_line()}"),
        ansi.dim(f" │ 偵察: {b.scouting_note()}"),
    ]
    read = state.current_read()
    if read and (read["wait"] or read["weak"]):
        bits = []
        if read["wait"]:
            bits.append("速球待ち" if read["wait"] == "fastball" else "変化球待ち")
        if read["weak"]:
            bits.append(f"弱点={read['weak']}")
        card.append(ansi.magenta(f" │ あなたの読み: {' / '.join(bits)}"))
    if last_outcome is not None and last_outcome.get("reaction"):
        r = last_outcome["reaction"]
        card.append(f" │   {ansi.bold(r['face'])}   {ansi.dim(r['text'])}")
    card.append(ansi.dim(f" └ 配球: {state.history.summary_text()}"))

    # ベース図を右側に添える
    body = []
    for i, cl in enumerate(card):
        d = dia[i] if i < len(dia) else ""
        body.append(f"{cl}    {d}")
    body += [f"{' ' * 40}{d}" for d in dia[len(card):]]

    return "\n".join([line1, line2, ""] + body)


# ---------------------------------------------------------------------------
# 打球チャート(#B 打球傾向がゲーム中に溜まって見える)
# ---------------------------------------------------------------------------
_SPRAY_W, _SPRAY_H = 25, 6


def _spray_cell(entry):
    # 左右(打者の左右を考慮した実方向)
    d, bats = entry["direction"], entry["bats"]
    if d == "center":
        side = "center"
    elif (d == "pull" and bats == "R") or (d == "oppo" and bats == "L"):
        side = "left"
    else:
        side = "right"
    col = {"left": 5, "center": 12, "right": 19}[side]
    # 前後(飛距離)
    row = {"deep": 0, "shallow": 2, "infield": 4}[entry["distance"]]
    glyph, color = {
        "アウト": ("·", "grey"), "単打": ("o", "white"), "エラー": ("e", "byellow"),
        "二塁打": ("O", "byellow"), "三塁打": ("O", "byellow"), "本塁打": ("H", "bred"),
    }.get(entry["result"], ("?", "white"))
    return row, col, glyph, color


def render_spray(state):
    title = ansi.bold("打球チャート（このゲーム）") + ansi.dim(
        "   · アウト  o 単打  e エラー  O 長打  H 本塁打")
    if not state.spray:
        return title + "\n  " + ansi.dim("（まだ打球なし）")

    grid = [[" "] * _SPRAY_W for _ in range(_SPRAY_H)]
    for entry in state.spray:
        row, col, glyph, color = _spray_cell(entry)
        for dc in range(2):                       # ちょっと横に散らす
            r, c = row + (dc and entry["ball_type"] == "fly"), col + dc
            if 0 <= r < _SPRAY_H and 0 <= c < _SPRAY_W and grid[r][c] == " ":
                grid[r][c] = ansi.paint(glyph, color)
                break
    grid[_SPRAY_H - 1][_SPRAY_W // 2] = ansi.dim("⌂")
    fence = ansi.dim("  " + "-" * _SPRAY_W)
    return "\n".join([title] + ["  " + "".join(r) for r in grid] + [fence])


# ---------------------------------------------------------------------------
# 守備陣の能力表
# ---------------------------------------------------------------------------
def render_fielder_table(defense):
    from fielders import ATTRS, POSITIONS, position_fit, weakest_fielder
    weak = weakest_fielder(defense.fielders)
    header = "  Pos 選手  " + " ".join(f"{a[:5]:>5}" for a in ATTRS) + "   総合  適性"
    rows = [ansi.dim(header), ansi.dim("  " + "─" * (len(header) - 2))]
    for pos in POSITIONS:
        f = defense.fielder_at(pos)
        stats = " ".join(f"{getattr(f, a):5.0f}" for a in ATTRS)
        line = f"  {pos:<3} {f.name:<4} {stats}   {f.overall():4.0f}  {position_fit(f, pos):4.0f}"
        rows.append(ansi.red(line) if f is weak else line)
    rows.append("")
    rows.append(ansi.bold(f"  Expected Defensive Efficiency (参考値): "
                          f"{defense.expected_defensive_efficiency():.0f} / 100"))
    rows.append(ansi.dim("  ※目安であって最適解ではありません。"))
    return "\n".join(rows)


# ---------------------------------------------------------------------------
# 1 球の結果
# ---------------------------------------------------------------------------
def render_play_result(outcome, ascii_only=None):
    pn = pitch_name(outcome["pitch_type"])
    tgt = f"{pn} / {_COURSE_JP[outcome['target_course']]}"
    if outcome["missed"]:
        act = f"{pn} / {ansi.red(_COURSE_JP[outcome['actual_course']])}  {ansi.red('← 失投')}"
    else:
        act = f"{pn} / {_COURSE_JP[outcome['actual_course']]}"
    rc = _result_color(outcome["result"])
    lines = [
        ansi.dim(f"要求: {tgt}    意図: {PITCH_INTENTS[outcome['intent']]}"),
        f"実投: {act}    球の出来: {int(outcome['quality'] * 100)}",
        f">>> 結果: {ansi.bold(ansi.paint(outcome['result'], rc))}",
    ]
    bb, fd = outcome.get("batted_ball"), outcome.get("fielding")
    if bb is not None and fd is not None:
        lines.append(ansi.dim(f">>> 打球: {bb.describe()} → {fd['position']} の {fd['fielder']}"
                              f"（アウト確率 {fd['out_probability'] * 100:.0f}%）"))
    play = outcome.get("play")
    if play is not None:
        bits = []
        if play["label"] != outcome["result"]:
            bits.append(play["label"])
        if play["detail"]:
            bits.append(play["detail"])
        if play["runs"]:
            bits.append(ansi.red(f"{play['runs']}失点"))
        if play["outs_added"] >= 2:
            bits.append("2アウト")
        if bits:
            lines.append(f">>> 走塁: {' / '.join(bits)}")
    r = outcome.get("reaction")
    if r is not None:
        lines.append("")
        lines.append(render_reaction_block(r, ascii_only))
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# 試合後(3 アウト後だけ)
# ---------------------------------------------------------------------------
def render_inning_result(state):
    run_txt, status = state.result_summary()
    runc = "bgreen" if state.runs_this_inning == 0 else "bred"
    return "\n".join([
        ansi.dim("═" * 44),
        ansi.bold(f" イニング終了：{ansi.paint(run_txt, runc)} ／ {ansi.yellow(status)}"),
        f" 自軍 {state.our_score} - {state.opp_score} 相手"
        + ansi.dim(f"（開始 {state.start_our} - {state.start_opp}）"),
        ansi.dim("═" * 44),
    ])


def _fmt_moments(moments, limit=3, color=None):
    if not moments:
        return ansi.dim("  なし")
    out = []
    for item in moments[:limit]:
        num, label = item[0], item[1]
        tail = f" → {item[2]}" if len(item) > 2 else ""
        line = f"  {num}球目 {label}{tail}"
        out.append(ansi.paint(line, color) if color else line)
    if len(moments) > limit:
        out.append(ansi.dim(f"  …ほか {len(moments) - limit} 件"))
    return "\n".join(out)


def render_analysis(state):
    a = build_analysis(state.pitch_log)
    reads = grade_reads(state.reads, state.lineup.batters)
    lines = [
        ansi.bold("--- 配球のふり返り（結果と判断は別ものとして評価）---"),
        f"総投球: {a['total_pitches']} 球",
        "",
        ansi.green("◎ 良かった配球（狙いを外す / 崩す / 守備と噛み合う）:"),
        _fmt_moments(a["good_calls"], color="green"),
        "",
        ansi.yellow("△ 危険だった配球（甘い / 見せすぎ / カウントに合わない）:"),
        _fmt_moments(a["risky_calls"], color="byellow"),
        "",
        f"狙いを外せた場面: {len(a['fooled_moments'])} 球   "
        f"読まれていた場面: {len(a['read_moments'])} 球",
        f"守備配置と噛み合った場面: {len(a['defense_moments'])} 球   "
        f"失投で結果が悪化: {len(a['misfire_moments'])} 球",
        "",
        ansi.red("◇ 浴びた長打:"),
        _fmt_moments(a["extra_base"], color="bred"),
        "",
        ansi.cyan("★ 結果は悪いが、判断としては良かった球（不運）:"),
        _fmt_moments(a["unlucky"], color="bcyan"),
        ansi.yellow("★ 判断は悪いが、結果に救われた球（幸運）:"),
        _fmt_moments(a["lucky"], color="byellow"),
        "",
    ]
    if reads["rows"]:
        lines.append(ansi.magenta(f"あなたの読み: {reads['correct']} / {reads['graded']} 当たり"))
        lines += reads["rows"]
        lines.append("")
    framed = getattr(state, "framed_strikes", 0)
    if framed:
        lines.append(ansi.cyan(f"フレーミングで奪ったストライク: {framed} 球"))
    lines.append(ansi.bold(f"判断の質（平均）: {a['decision_quality_label']}"))
    lines.append(ansi.dim("※試合後の振り返りです。プレイ中に正解は表示していません。"))
    return "\n".join(lines)
