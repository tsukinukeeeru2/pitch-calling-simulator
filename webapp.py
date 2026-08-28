"""ブラウザで遊べる Web UI(標準ライブラリのみ、追加インストール不要)。

    python webapp.py                 # http://127.0.0.1:8765 で起動
    python webapp.py --port 9000
    python webapp.py --lineup lineups/sample_team.json
    python webapp.py --seed 12345    # 場面・投球判定を再現可能にする
    python webapp.py --host 0.0.0.0  # 同じ Wi-Fi の他端末からもアクセス可能に

main.py(CLI)と同じ engine.py を呼ぶだけの「進行役」。ゲームロジック
(judge.py 以下)には一切触れない ―― ロジックの二重実装をしない、という
このプロジェクトの一貫した方針どおり。

    Game Engine (engine.py)
        ↑            ↑
      main.py     webapp.py
      (CLI)         (Web)

JavaScript は使わない(フォーム送信だけ)。安定性・自動テストのしやすさを
見た目より優先している(詳細は README「Web UI」参照)。

QTE(リズムミニゲーム)は HTTP のリクエスト/レスポンスでは体で覚える系の
入力ができないため、起動時に PITCHSIM_QTE=0 を強制して常に確率モデルに
一任する。
"""

import argparse
import html
import http.server
import os
import random
import socketserver
import urllib.parse

os.environ["PITCHSIM_QTE"] = "0"

from constants import COURSE_SHORT
from defense import ALIGNMENTS
from engine import maybe_resolve_sign_steal, resolve_one_pitch, save_game_log
from fielders import POSITIONS, position_fit, weakest_fielder
from lineup import load_lineup_file
from match_state import generate_random_situation
from pitch_data import pitch_name, repertoire_options
from strategy import PITCH_INTENTS, build_analysis, build_postgame_report, grade_reads

_COURSE_ROWS = ["hi", "mid", "lo"]


def _course_cols(bats):
    """打者から見て「内→外」の並び。右打者=in/mid/out、左打者=out/mid/in
    (捕手・投手側から見た実際の左右がミラーする、放送のストライクゾーン表示と同じ考え方)。"""
    return ["in", "mid", "out"] if bats == "R" else ["out", "mid", "in"]


def _course_grid(bats):
    return [[f"{c}_{r}" for c in _course_cols(bats)] for r in _COURSE_ROWS]


class GameSession:
    """1 プロセス = 1 ゲーム(ローカルで自分ひとりが遊ぶ想定。同時アクセスは考慮しない)。"""

    def __init__(self, lineup_batters=None, seed=None, difficulty="normal"):
        self.lineup_batters = lineup_batters
        self.seed = seed
        self.difficulty = difficulty
        self.rng = random.Random(seed) if seed is not None else random
        self.state = None
        self.started = False
        self.showing_intro = False
        self.messages = []
        self.reaction = None
        self.last_batter_bats = "R"
        self.last_call = None      # {"pitch_type","course","intent"}
        self.last_outcome = None
        self.pending_swap = None
        self.log_path = None

    def start_new_game(self):
        # --seed のときは毎回同じ場面から始まる(再現性優先)。それ以外は毎回ランダム。
        rng = random.Random(self.seed) if self.seed is not None else random
        self.rng = rng
        self.state = generate_random_situation(rng=rng, lineup_batters=self.lineup_batters)
        self.started = True
        self.showing_intro = True
        self.messages = []
        self.reaction = None
        self.last_call = None
        self.last_outcome = None
        self.pending_swap = None
        self.log_path = None

    def finish_if_over(self):
        if self.state is not None and self.state.is_over() and self.log_path is None:
            try:
                self.log_path = save_game_log(self.state)
            except OSError:
                self.log_path = ""


SESSION = GameSession()


def _e(text):
    return html.escape(str(text))


# ---------------------------------------------------------------------------
# ページの外枠(CSS はここに集約)
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>配球判断シミュレーター</title>
<style>
:root {{
  --ink:#1c2430; --sub:#5b6673; --line:#dde2e8; --bg:#eef1f4; --card:#ffffff;
  --accent:#1d6fa5; --good:#1c8a5a; --bad:#c23b3b; --warn:#b8860b; --dim:#8a94a1;
}}
* {{ box-sizing:border-box; }}
body {{
  margin:0; padding:0 0 3em; background:var(--bg); color:var(--ink);
  font-family: system-ui, "Hiragino Kaku Gothic ProN", "Yu Gothic", "IPAGothic", sans-serif;
}}
.wrap {{ max-width:1080px; margin:0 auto; padding:0 16px; }}
a {{ color:var(--accent); }}

/* --- スコアボード帯(常時表示) --- */
.scorebar {{
  background:#12202e; color:#eef3f7; padding:.6em 0; margin-bottom:1em;
  font-variant-numeric: tabular-nums;
}}
.scorebar .row {{ display:flex; align-items:center; gap:1.4em; flex-wrap:wrap; }}
.scorebar b.title {{ color:#8fd0ff; letter-spacing:.04em; }}
.scorebar .pips {{ letter-spacing:.15em; }}
.scorebar .dim {{ color:#96a5b3; }}

/* --- カード共通 --- */
.card {{
  background:var(--card); border:1px solid var(--line); border-radius:10px;
  padding:1em 1.1em; margin-bottom:1em;
}}
.card h2, .card h3 {{ margin:0 0 .5em; font-size:1em; color:var(--sub); font-weight:600;
                      text-transform:uppercase; letter-spacing:.04em; }}
.grid2 {{ display:grid; grid-template-columns: 1.1fr 1fr; gap:1em; align-items:start; }}
@media (max-width: 760px) {{ .grid2 {{ grid-template-columns: 1fr; }} }}

/* --- 球場図 --- */
.field {{ position:relative; width:100%; aspect-ratio: 4 / 3; border-radius:10px; overflow:hidden;
          background: radial-gradient(ellipse at 50% 100%, #3f8f4f 0%, #2f7a41 55%, #256536 100%); }}
.field svg {{ position:absolute; inset:0; width:100%; height:100%; }}
.posbtn {{ position:absolute; transform:translate(-50%,-50%); }}
.posbtn form {{ margin:0; }}
.posbtn button {{
  width:64px; padding:.3em .2em; border-radius:8px; border:2px solid #fff;
  background:rgba(15,30,20,.78); color:#fff; font-size:.74em; line-height:1.25;
  cursor:pointer; text-align:center;
}}
.posbtn button b {{ display:block; font-size:1.05em; }}
.posbtn.selected button {{ border-color:#ffd54a; background:#5a4a12; }}
.posbtn.weak button {{ box-shadow: 0 0 0 2px #ff6b6b inset; }}
.field .homeplate {{ position:absolute; left:50%; bottom:4%; transform:translateX(-50%);
                     color:#fff; font-size:.75em; text-align:center; }}
.field-legend {{ font-size:.82em; color:var(--sub); margin-top:.4em; }}

/* --- 打者カード --- */
.batter-now {{ border:1px solid var(--line); border-radius:8px; padding:.7em .8em; }}
.batter-now .name {{ font-size:1.15em; font-weight:700; }}
.batter-mini {{ display:flex; justify-content:space-between; padding:.35em .1em;
                border-top:1px dashed var(--line); font-size:.9em; color:var(--sub); }}
.tag {{ display:inline-block; font-size:.72em; padding:.05em .5em; border-radius:1em;
        background:#eef2f6; color:var(--sub); margin-left:.4em; }}

/* --- 反応(顔文字) --- */
.reaction {{ text-align:center; padding:.6em 0; }}
.reaction .face {{ font-size:2.1em; font-weight:700; }}
.reaction .text {{ color:var(--sub); margin-top:.15em; }}

/* --- ストライクゾーン --- */
.zonewrap {{ display:flex; gap:1.4em; flex-wrap:wrap; align-items:flex-start; }}
.zone {{ display:grid; grid-template-columns:repeat(3, 56px); grid-template-rows:repeat(3, 56px); gap:4px; }}
.zone .cell {{ position:relative; border:1px solid var(--line); border-radius:6px; background:#f7f9fb; }}
.zone label {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
               font-size:.78em; color:var(--sub); cursor:pointer; border-radius:6px; }}
.zone input {{ display:none; }}
.zone input:checked + label {{ background:var(--accent); color:#fff; }}
.marker {{ position:absolute; width:22px; height:22px; border-radius:50%; display:flex;
           align-items:center; justify-content:center; font-size:.72em; font-weight:800;
           color:#fff; pointer-events:none; }}
.marker.target {{ top:2px; left:2px; background:#2c6fae; border:2px solid #fff; }}
.marker.actual {{ bottom:2px; right:2px; background:var(--bad); border:2px solid #fff; }}
.marker.actual.ontarget {{ background:var(--good); }}
.zone-key {{ font-size:.8em; color:var(--sub); }}
.zone-key .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin-right:.3em; }}

/* --- ボタン群(球種・意図) --- */
.btnrow {{ display:flex; flex-wrap:wrap; gap:.4em; }}
.btnrow label {{ border:1px solid var(--line); border-radius:8px; padding:.4em .8em;
                 font-size:.9em; cursor:pointer; background:#f7f9fb; }}
.btnrow input {{ display:none; }}
.btnrow input:checked + label {{ background:var(--accent); color:#fff; border-color:var(--accent); }}

button, .btn {{
  padding:.5em 1em; border-radius:8px; border:1px solid var(--line); background:#f7f9fb;
  cursor:pointer; font-size:.92em; color:var(--ink);
}}
button.primary {{ background:var(--accent); color:#fff; border-color:var(--accent); font-weight:700; }}
button.big {{ font-size:1.1em; padding:.7em 1.6em; }}

/* --- 配球履歴 --- */
table.history {{ width:100%; border-collapse:collapse; font-size:.9em; }}
table.history th {{ text-align:left; color:var(--sub); font-weight:600; padding:.25em .4em;
                     border-bottom:1px solid var(--line); }}
table.history td {{ padding:.3em .4em; border-bottom:1px solid #eef1f4; }}
.res-strike {{ color:var(--accent); }} .res-ball {{ color:var(--warn); }}
.res-hit {{ color:var(--bad); font-weight:700; }} .res-out {{ color:var(--good); }}

/* --- メッセージ --- */
.msg {{ padding:.2em 0; }}
.msg.bad {{ color:var(--bad); }}
.msg.good {{ color:var(--good); }}

.dim {{ color:var(--dim); }}
.hint {{ background:#fff8e6; border:1px solid #f0dca0; border-radius:8px; padding:.7em 1em;
         font-size:.9em; margin-bottom:1em; }}
.report-stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(110px,1fr)); gap:.6em; }}
.stat {{ background:#f7f9fb; border-radius:8px; padding:.6em .7em; text-align:center; }}
.stat .n {{ font-size:1.5em; font-weight:800; }}
.stat .label {{ font-size:.76em; color:var(--sub); }}
</style></head><body>
<div class="wrap">
<h1 style="font-size:1.15em; margin:0.6em 0;">終盤の守り切りゲーム</h1>
{body}
</div>
</body></html>"""


def _page(body):
    return _PAGE.format(body=body)


# ---------------------------------------------------------------------------
# START 画面
# ---------------------------------------------------------------------------
def render_start_screen(session):
    seed_note = f'<p class="dim">seed = {session.seed}(再現可能)</p>' if session.seed is not None else ""
    return f"""
<div class="card" style="text-align:center; padding:2.4em 1em;">
  <p style="font-size:1.15em;">試合終盤に捕手として1イニングを守るゲーム</p>
  <p class="dim">球種・コース・配球意図を選び、3アウトを取るまでが1ゲーム。<br>
     プレイ中に正解は表示されません。反応・履歴・打者情報から自分で読みます。</p>
  {seed_note}
  <form method="post" action="/start">
    <button class="primary big" type="submit">START GAME</button>
  </form>
</div>"""


def render_situation_intro(state):
    b = state.batter
    hand = "右投" if state.pitcher.throws == "R" else "左投"
    return f"""
<div class="card" style="text-align:center; padding:2em 1em;">
  <p class="dim" style="letter-spacing:.1em;">{_e(state.inning_label().upper())}</p>
  <p style="font-size:1.6em; font-weight:800; margin:.2em 0;">
    自軍 {state.our_score} - {state.opp_score} 相手</p>
  <p style="font-size:1.1em;">{state.outs} OUT　走者: {_e(state.runners_text())}</p>
  <p class="dim">投手: リリーフ（{hand}）</p>
  <p style="margin-top:1em;">BATTER #{state.lineup.spot_number()}　{_e(b.name)}
     {_e(b.public_line())}　{_e(b.type_label())}</p>
  <p style="font-weight:800; letter-spacing:.06em; margin-top:1.2em;">DEFEND THE LEAD.</p>
  <form method="post" action="/playball">
    <button class="primary big" type="submit">PLAY BALL</button>
  </form>
</div>"""


# ---------------------------------------------------------------------------
# スコアボード帯
# ---------------------------------------------------------------------------
def _pips(filled, total):
    return "●" * filled + "○" * (total - filled)


def render_scorebar(state):
    hand = "右" if state.pitcher.throws == "R" else "左"
    return f"""
<div class="scorebar"><div class="wrap row">
  <b class="title">{_e(state.inning_label())}</b>
  <span>自軍 <b>{state.our_score}</b> - <b>{state.opp_score}</b> 相手</span>
  <span>{state.outs} OUT</span>
  <span class="pips">B {_pips(state.balls,4)}　S {_pips(state.strikes,3)}</span>
  <span class="dim">走者 {_e(state.runners_text())}</span>
  <span class="dim">投手{hand}投　球数{state.pitcher.pitches_thrown}</span>
</div></div>"""


# ---------------------------------------------------------------------------
# 球場図(守備配置。クリック→クリックでSwap)
# ---------------------------------------------------------------------------
_FIELD_XY = {
    "CF": (50, 14), "LF": (21, 30), "RF": (79, 30),
    "SS": (37, 56), "2B": (63, 56),
    "3B": (23, 75), "1B": (77, 75),
}

_FIELD_SVG = """
<svg viewBox="0 0 100 75" preserveAspectRatio="none">
  <polygon points="50,68 20,40 50,10 80,40" fill="#8a6a45" opacity="0.55" />
  <polygon points="50,68 20,40 50,10 80,40" fill="none" stroke="#e8e2d0" stroke-width="0.5" opacity="0.7"/>
  <line x1="50" y1="68" x2="8" y2="18" stroke="#e8e2d0" stroke-width="0.4" opacity="0.6"/>
  <line x1="50" y1="68" x2="92" y2="18" stroke="#e8e2d0" stroke-width="0.4" opacity="0.6"/>
  <circle cx="50" cy="68" r="14" fill="#8a6a45" opacity="0.35" />
</svg>
"""


def render_field_diagram(session):
    state = session.state
    defense = state.defense
    weak = weakest_fielder(defense.fielders)
    cells = []
    for pos in POSITIONS:
        x, y = _FIELD_XY[pos]
        f = defense.fielder_at(pos)
        fit = position_fit(f, pos)
        cls = "posbtn"
        if session.pending_swap == pos:
            cls += " selected"
        if f is weak:
            cls += " weak"
        cells.append(f"""
<div class="{cls}" style="left:{x}%; top:{y}%;">
  <form method="post" action="/swap">
    <input type="hidden" name="position" value="{pos}">
    <button type="submit" title="{_e(f.name)} を {pos} で使う">
      <b>{pos}</b>{_e(f.name)}<br>適性{fit:.0f}{' ▼' if f is weak else ''}
    </button>
  </form>
</div>""")
    cancel = ""
    if session.pending_swap:
        cancel = (f'<p><span class="tag">{session.pending_swap} を選択中 → '
                 f'交換したいポジションをクリック</span> '
                 '<form method="post" action="/cancel_swap" style="display:inline">'
                 '<button type="submit">選択解除</button></form></p>')
    align_opts = "".join(
        f'<option value="{k}"{" selected" if k == defense.alignment else ""}>{v}</option>'
        for k, v in ALIGNMENTS.items())
    return f"""
<div class="card">
  <h3>守備配置（{_e(defense.describe_alignment())}）</h3>
  <div class="field">{_FIELD_SVG}{''.join(cells)}
    <div class="homeplate">⌂ HOME<br>You (C)</div>
  </div>
  <p class="field-legend">数字はそのポジションでの適性(0-99)。▼は総合力が最も低い選手。
    ポジションをクリック→交換したいポジションをクリックでSwap。</p>
  {cancel}
  <form method="post" action="/alignment">
    <select name="alignment">{align_opts}</select>
    <button type="submit">守備シフト変更</button>
  </form>
</div>"""


# ---------------------------------------------------------------------------
# 打者カード(現在 / NEXT / ON DECK)
# ---------------------------------------------------------------------------
def render_batter_card(state):
    lu = state.lineup
    cur = lu.current()
    nxt = lu.batters[(lu.index + 1) % 9]
    deck = lu.batters[(lu.index + 2) % 9]
    return f"""
<div class="card">
  <h3>Batter</h3>
  <div class="batter-now">
    <span class="dim">#{lu.spot_number()}</span>
    <span class="name">{_e(cur.name)}</span>
    <span class="tag">{_e(cur.bats)}</span>
    <span class="tag">{_e(cur.type_label())}</span>
    <div class="dim" style="margin-top:.3em;">
      AVG {cur.avg:.3f}　OBP {cur.obp:.3f}　SLG {cur.slg:.3f}　OPS {cur.ops:.3f}</div>
    <div class="dim">偵察: {_e(cur.scouting_note())}</div>
  </div>
  <div class="batter-mini"><span>NEXT #{lu.batters.index(nxt) + 1} {_e(nxt.name)} ({nxt.bats})</span>
    <span>OPS {nxt.ops:.3f}</span></div>
  <div class="batter-mini"><span>ON DECK #{lu.batters.index(deck) + 1} {_e(deck.name)} ({deck.bats})</span>
    <span>OPS {deck.ops:.3f}</span></div>
</div>"""


# ---------------------------------------------------------------------------
# 反応(顔文字)
# ---------------------------------------------------------------------------
def render_reaction(reaction):
    if reaction is None:
        return '<div class="card"><h3>Batter Reaction</h3><p class="dim">まだ投球なし</p></div>'
    return f"""
<div class="card">
  <h3>Batter Reaction</h3>
  <div class="reaction">
    <div class="face">{_e(reaction['face'])}</div>
    <div class="text">「{_e(reaction['text'])}」</div>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# Pitch Call → Actual Pitch(ストライクゾーン上に Target / Actual)
# ---------------------------------------------------------------------------
def _zone_markers(bats, target_course, actual_course):
    grid = _course_grid(bats)
    cells = []
    for row in grid:
        for course in row:
            marks = ""
            if course == target_course:
                marks += '<span class="marker target">T</span>'
            if course == actual_course:
                cls = "marker actual" + (" ontarget" if actual_course == target_course else "")
                marks += f'<span class="{cls}">A</span>'
            cells.append(f'<div class="cell">{marks}</div>')
    return "".join(cells)


def render_pitch_result(session):
    if session.last_call is None:
        return ""
    call = session.last_call
    outcome = session.last_outcome
    zone = _zone_markers(session.last_batter_bats, call["target_course"], outcome["actual_course"])
    miss_note = ('<span class="dim">（要求どおり）</span>' if not outcome["missed"]
                else '<span class="msg bad">（失投で要求からズレた）</span>')
    msgs_html = "".join(f'<div class="msg{" bad" if _looks_bad(m) else ""}">{_e(m)}</div>'
                        for m in session.messages)
    return f"""
<div class="card">
  <h3>Pitch Call → Actual Pitch</h3>
  <div class="zonewrap">
    <div>
      <div class="zone">{zone}</div>
      <p class="zone-key"><span class="dot" style="background:#2c6fae"></span>Target(要求)
         <span class="dot" style="background:var(--bad)"></span>Actual(実投)</p>
    </div>
    <div>
      <p>CALL　{_e(pitch_name(call['pitch_type']))} / {_e(COURSE_SHORT[call['target_course']])}
         / {_e(PITCH_INTENTS[call['intent']].split('（')[0])}</p>
      <p>ACTUAL　{_e(pitch_name(call['pitch_type']))} / {_e(COURSE_SHORT[outcome['actual_course']])}
         {miss_note}</p>
      <p style="font-size:1.2em; font-weight:800;">RESULT　{_e(outcome['result'])}</p>
      {render_reaction(session.reaction)}
    </div>
  </div>
  {msgs_html}
</div>"""


_BAD_HINTS = ("三振", "打たれた", "後逸", "エラー", "四球", "振り逃げが成立")


def _looks_bad(msg):
    return any(h in msg for h in _BAD_HINTS)


# ---------------------------------------------------------------------------
# 配球コール(球種・コース・意図)
# ---------------------------------------------------------------------------
def render_pitch_call_form(state):
    pitch_opts = "".join(
        f'<input type="radio" name="pitch_type" value="{_e(k)}" id="pt_{k}"'
        f'{" checked" if i == 0 else ""}>'
        f'<label for="pt_{k}">{_e(v)}</label>'
        for i, (k, v) in enumerate(repertoire_options(state.pitcher).items()))
    intent_opts = "".join(
        f'<input type="radio" name="intent" value="{k}" id="it_{k}"'
        f'{" checked" if k == "strike" else ""}>'
        f'<label for="it_{k}">{_e(v.split("（")[0])}</label>'
        for k, v in PITCH_INTENTS.items())
    grid = _course_grid(state.batter.bats)
    course_cells = ""
    for row in grid:
        for c in row:
            checked = " checked" if c == "mid_mid" else ""
            course_cells += (f'<div class="cell"><input type="radio" name="course" value="{c}" '
                            f'id="c_{c}"{checked}><label for="c_{c}">{_e(COURSE_SHORT[c])}</label></div>')
    side_hint = "内角 ← → 外角" if state.batter.bats == "R" else "外角 ← → 内角"
    return f"""
<div class="card">
  <h3>Pitch Call</h3>
  <form method="post" action="/pitch">
    <p class="dim" style="margin-bottom:.3em;">Pitch Type</p>
    <div class="btnrow">{pitch_opts}</div>
    <p class="dim" style="margin:.8em 0 .3em;">Target Location（{_e(side_hint)}、打者 {state.batter.bats}）</p>
    <div class="zone">{course_cells}</div>
    <p class="dim" style="margin:.8em 0 .3em;">Pitch Intent</p>
    <div class="btnrow">{intent_opts}</div>
    <p style="margin-top:1em;"><button class="primary big" type="submit">CALL PITCH</button></p>
  </form>
</div>"""


# ---------------------------------------------------------------------------
# 打球チャート(#B。expert 難易度では隠す)
# ---------------------------------------------------------------------------
_SPRAY_GLYPH = {"アウト": "·", "単打": "o", "エラー": "e",
               "二塁打": "O", "三塁打": "O", "本塁打": "H"}


def render_spray_chart(state):
    w, h = 21, 5
    grid = [[" "] * w for _ in range(h)]
    for entry in state.spray:
        d, bats = entry["direction"], entry["bats"]
        if d == "center":
            side = "center"
        elif (d == "pull" and bats == "R") or (d == "oppo" and bats == "L"):
            side = "left"
        else:
            side = "right"
        col = {"left": 4, "center": 10, "right": 16}[side]
        row = {"deep": 0, "shallow": 2, "infield": 4}[entry["distance"]]
        g = _SPRAY_GLYPH.get(entry["result"], "?")
        for dc in range(2):
            c = col + dc
            if 0 <= row < h and 0 <= c < w and grid[row][c] == " ":
                grid[row][c] = g
                break
    grid[h - 1][w // 2] = "⌂"
    text = "\n".join("".join(r) for r in grid)
    return (f'<div class="card"><h3>打球チャート</h3>'
           f'<p class="dim">· アウト　o 単打　e エラー　O 長打　H 本塁打</p>'
           f'<pre style="background:#eef7ee; padding:.6em; border-radius:6px;">{_e(text)}</pre></div>')


# ---------------------------------------------------------------------------
# 配球履歴
# ---------------------------------------------------------------------------
_RESULT_CSS = {
    "ストライク": "res-strike", "空振り": "res-strike", "ファウル": "res-strike",
    "ボール": "res-ball",
    "アウト": "res-out",
    "単打": "res-hit", "二塁打": "res-hit", "三塁打": "res-hit", "本塁打": "res-hit", "エラー": "res-hit",
}


def render_pitch_history(state, limit=8):
    entries = state.pitch_log[-limit:][::-1]
    if not entries:
        rows = '<tr><td colspan="4" class="dim">まだ投球なし</td></tr>'
    else:
        rows = "".join(
            f"<tr><td>{e['pitch_number']}</td><td>{_e(pitch_name(e['pitch_type']))}</td>"
            f"<td>{_e(COURSE_SHORT[e['actual_course']])}</td>"
            f"<td class=\"{_RESULT_CSS.get(e['result'], '')}\">{_e(e['result'])}</td></tr>"
            for e in entries)
    return f"""
<div class="card">
  <h3>Pitch History（直近{limit}球・新しい順）</h3>
  <table class="history">
    <tr><th>#</th><th>球種</th><th>コース</th><th>結果</th></tr>
    {rows}
  </table>
</div>"""


# ---------------------------------------------------------------------------
# サイドアクション(敬遠・継投・捕手メモ・中断)
# ---------------------------------------------------------------------------
def render_side_actions(state, difficulty):
    extras = ""
    if state.runners[0] and not state.runners[1]:
        extras += '<form method="post" action="/walk" style="display:inline">'\
                 '<button>敬遠</button></form> '
    if state.can_change_pitcher():
        extras += '<form method="post" action="/change" style="display:inline">'\
                 '<button>継投</button></form> '
    course_opts = "".join(f'<option value="{c}">{_e(v)}</option>' for c, v in COURSE_SHORT.items())
    hint = ""
    if difficulty == "beginner":
        hint = ('<div class="hint">ヒント: 顔文字は本音とは限りません（60%本音/15%逆/15%ぼかし）。'
               '1球だけで信じすぎないこと。</div>')
    return f"""
{hint}
<div class="card">
  <h3>捕手メモ（読みを記録・試合後に採点）</h3>
  <form method="post" action="/memo">
    <select name="wait"><option value="unknown">待ち球: わからない</option>
      <option value="fastball">速球待ち</option><option value="offspeed">変化球待ち</option></select>
    <select name="weak"><option value="">弱点コース: わからない</option>{course_opts}</select>
    <button type="submit">メモする</button>
  </form>
</div>
<div class="card">
  <h3>その他の操作</h3>
  {extras}
  <form method="post" action="/newgame" style="display:inline">
    <button>ゲームを中断して新規開始</button></form>
</div>"""


# ---------------------------------------------------------------------------
# メイン試合画面
# ---------------------------------------------------------------------------
def render_game_page(session):
    state = session.state
    body = [render_scorebar(state)]
    body.append('<div class="grid2">')
    body.append(render_field_diagram(session))
    body.append('<div>' + render_batter_card(state)
               + (render_pitch_result(session) if session.last_call else render_reaction(None))
               + '</div>')
    body.append('</div>')
    body.append(render_pitch_call_form(state))
    body.append(render_pitch_history(state))
    if session.difficulty != "expert":
        body.append(render_spray_chart(state))
    body.append(render_side_actions(state, session.difficulty))
    return "".join(body)


# ---------------------------------------------------------------------------
# Postgame Report(Catcher Report)
# ---------------------------------------------------------------------------
def render_postgame_report(state, log_path):
    report = build_postgame_report(state)
    a = build_analysis(state.pitch_log)
    reads = grade_reads(state.reads, state.lineup.batters)
    run_txt, status = state.result_summary()

    def stat(n, label):
        return f'<div class="stat"><div class="n">{n}</div><div class="label">{label}</div></div>'

    stats_html = "".join([
        stat(report["runs_allowed"], "Runs Allowed"),
        stat(report["batters_faced"], "Batters Faced"),
        stat(report["pitches"], "Pitches"),
        stat(report["strikeouts"], "Strikeouts"),
        stat(report["walks"], "Walks"),
        stat(report["hits"], "Hits"),
    ])

    def fmt(items, limit=5):
        if not items:
            return '<p class="dim">なし</p>'
        rows = "".join(f'<p>{n}球目　{_e(label)}' + (f' → {_e(item[2])}' if len(item) > 2 else '')
                       + '</p>' for item in items[:limit] for n, label in [(item[0], item[1])])
        more = f'<p class="dim">…ほか{len(items) - limit}件</p>' if len(items) > limit else ""
        return rows + more

    reads_html = ""
    if reads["rows"]:
        reads_html = (f'<div class="card"><h3>捕手メモの採点</h3>'
                     f'<p>{reads["correct"]} / {reads["graded"]} 当たり</p>'
                     + "".join(f'<p class="dim">{_e(r.strip())}</p>' for r in reads["rows"])
                     + '</div>')

    pe = report["pitch_execution"]
    de = report["defense"]

    return f"""
<div class="card" style="text-align:center;">
  <h2 style="letter-spacing:.08em;">CATCHER REPORT</h2>
  <p style="font-size:1.3em; font-weight:800;">{_e(run_txt)} ／ {_e(status)}</p>
  <p class="dim">自軍 {state.our_score} - {state.opp_score} 相手</p>
</div>
<div class="card"><h3>Line</h3><div class="report-stats">{stats_html}</div></div>
<div class="grid2">
  <div class="card"><h3>Decision Quality（配球判断）</h3>
    <p>平均: <b>{_e(a['decision_quality_label'])}</b></p>
    <p class="msg good">GOOD CALL / BAD RESULT（判断は良かったが結果が悪い）</p>
    {fmt(a['unlucky'])}
    <p class="msg bad">BAD CALL / GOOD RESULT（判断は危険だったが結果的にアウト）</p>
    {fmt(a['lucky'])}
  </div>
  <div>
    <div class="card"><h3>Pitch Execution（投手の実行力）</h3>
      <p>失投 {pe['missed_pitches']} / {report['pitches']} 球（{pe['missed_rate']*100:.0f}%）</p>
      <p>平均 quality: {pe['avg_quality']:.2f}</p>
    </div>
    <div class="card"><h3>Defense（守備）</h3>
      <p>シフトが噛み合った場面: {de['alignment_helped']} 球</p>
      <p>エラー: {de['errors']} 件</p>
      <p>最終 EDE（参考値）: {de['final_ede']}</p>
    </div>
  </div>
</div>
<div class="card"><h3>◎ 良かった配球</h3>{fmt(a['good_calls'])}</div>
<div class="card"><h3>△ 危険だった配球</h3>{fmt(a['risky_calls'])}</div>
<div class="card"><h3>◇ 浴びた長打</h3>{fmt(a['extra_base'])}</div>
{reads_html}
<div class="card dim">{'ログを保存しました: ' + _e(log_path) if log_path else ''}
  <p style="margin-top:.6em;">※プレイ中に正解は表示していません。これは3アウト後だけの振り返りです。</p></div>
<form method="post" action="/newgame"><button class="primary big" type="submit">もう一度プレイ</button></form>"""


def render_page(session):
    if not session.started:
        return render_start_screen(session)
    if session.showing_intro:
        return render_situation_intro(session.state)
    state = session.state
    session.finish_if_over()
    if state.is_over():
        return render_postgame_report(state, session.log_path)
    return render_game_page(session)


# ---------------------------------------------------------------------------
# HTTP サーバ(標準ライブラリのみ)
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def _send_html(self, body, status=200):
        data = body.encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _redirect(self, path="/"):
        self.send_response(303)
        self.send_header("Location", path)
        self.end_headers()

    def _read_form(self):
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length).decode("utf-8") if length else ""
        parsed = urllib.parse.parse_qs(body)
        return {k: v[0] for k, v in parsed.items()}

    def do_GET(self):
        if self.path not in ("/", ""):
            self._send_html("not found", status=404)
            return
        self._send_html(_page(render_page(SESSION)))

    def do_POST(self):
        form = self._read_form()

        if self.path == "/start":
            SESSION.start_new_game()
            self._redirect()
            return

        if not SESSION.started:
            self._redirect()
            return

        if self.path == "/playball":
            SESSION.showing_intro = False
            self._redirect()
            return

        if self.path == "/newgame":
            SESSION.start_new_game()
            self._redirect()
            return

        state = SESSION.state
        if SESSION.showing_intro or state.is_over():
            self._redirect()
            return

        if self.path == "/pitch":
            pitch_type = form.get("pitch_type")
            course = form.get("course", "mid_mid")
            intent = form.get("intent", "strike")
            if pitch_type not in repertoire_options(state.pitcher):
                self._redirect()
                return
            rng = SESSION.rng
            SESSION.last_batter_bats = state.batter.bats
            pre_messages = maybe_resolve_sign_steal(state, rng)
            outcome, more = resolve_one_pitch(state, pitch_type, course, intent, rng)
            SESSION.messages = pre_messages + more
            SESSION.reaction = outcome.get("reaction")
            SESSION.last_call = {"pitch_type": pitch_type, "target_course": course, "intent": intent}
            SESSION.last_outcome = outcome
            self._redirect()
            return

        if self.path == "/walk":
            state.intentional_walk()
            SESSION.messages, SESSION.reaction, SESSION.last_call = [">>> 申告敬遠。打者を歩かせた。"], None, None
            self._redirect()
            return

        if self.path == "/change":
            SESSION.reaction, SESSION.last_call = None, None
            if state.change_pitcher():
                SESSION.messages = [f">>> 継投。マウンドに {state.pitcher.name}。"
                                   "球数はリセット。持ち球・調子は投げてみるまで分からない。"]
            self._redirect()
            return

        if self.path == "/alignment":
            key = form.get("alignment")
            if key in ALIGNMENTS:
                state.defense.set_alignment(key)
            self._redirect()
            return

        if self.path == "/swap":
            pos = form.get("position")
            if pos in POSITIONS:
                if SESSION.pending_swap is None:
                    SESSION.pending_swap = pos
                elif SESSION.pending_swap == pos:
                    SESSION.pending_swap = None
                else:
                    state.defense.swap(SESSION.pending_swap, pos)
                    SESSION.pending_swap = None
            self._redirect()
            return

        if self.path == "/cancel_swap":
            SESSION.pending_swap = None
            self._redirect()
            return

        if self.path == "/memo":
            wait = form.get("wait")
            weak = form.get("weak") or None
            state.record_read(wait=None if wait in (None, "unknown") else wait, weak=weak)
            self._redirect()
            return

        self._redirect()


def _parse_args():
    p = argparse.ArgumentParser(description="配球判断シミュレーター（Web版）")
    p.add_argument("--port", type=int, default=8765)
    p.add_argument("--host", default="127.0.0.1",
                   help="0.0.0.0 にすると同じネットワーク上の他端末(スマホ等)からも"
                       "アクセス可能になる。信頼できるネットワークでのみ使うこと")
    p.add_argument("--lineup", help=".json / .csv の相手打線ファイル")
    p.add_argument("--seed", type=int, default=None,
                   help="乱数シード。指定すると場面生成・投球判定が再現可能になる")
    p.add_argument("--difficulty", choices=("beginner", "normal", "expert"),
                   default=os.environ.get("PITCHSIM_DIFFICULTY", "normal"))
    p.add_argument("--mlb-demo", action="store_true",
                   help="実在選手のサンプル打線を使う(mlb_data_adapter 経由)")
    p.add_argument("--mlb-live", action="store_true",
                   help="--mlb-demo と併用。MLB Stats API から実際に取得を試みる"
                       "(失敗したら自動でサンプルデータにフォールバック)")
    return p.parse_args()


def main():
    args = _parse_args()
    batters = None
    if args.mlb_demo:
        from mlb_data_adapter import build_demo_lineup
        lineup, source = build_demo_lineup(prefer_live=args.mlb_live)
        batters = lineup.batters
        print(f"MLBサンプル打線を読み込みました（{'MLB Stats API' if source == 'live' else 'サンプルデータ'}）")
    elif args.lineup:
        batters = load_lineup_file(args.lineup).batters
    global SESSION
    SESSION = GameSession(lineup_batters=batters, seed=args.seed, difficulty=args.difficulty)

    with socketserver.TCPServer((args.host, args.port), Handler) as httpd:
        print(f"http://{args.host}:{args.port} で起動しました（Ctrl+C で終了）")
        if args.host == "0.0.0.0":
            import socket
            try:
                lan_ip = socket.gethostbyname(socket.gethostname())
                print(f"同じ Wi-Fi のスマホからは http://{lan_ip}:{args.port} でアクセスできます"
                     "（うまく繋がらない場合は ipconfig / ifconfig で調べた IP を使ってください）")
            except OSError:
                pass
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
