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

基本はフォーム送信だけで組み、安定性・自動テストのしやすさを見た目より
優先している(詳細は README「Web UI」参照)。唯一の例外が捕手のリズムゲーム
(受球=フレーミング / 盗塁の送球 / ワンバウンドのブロッキング / 振り逃げ /
サイン交換 / バント処理。render_qte)で、ここだけタイミング入力に小さな JS を
使う ―― JS 無効でもボタン送信で中立(bonus 0)で先へ進める(<noscript>)。

仕組み: engine.resolve_pitch_flow() のジェネレータが QTE 地点で ("種別",
payload) を yield し、webapp はそれを HTTP リクエストをまたいで駆動する
(SESSION.pitch_gen に生きたジェネレータを持ち、/qte の POST ごとに .send())。
PITCHSIM_QTE=0 は据え置き(qte.* の CLI 用 print/input を止めるため)だが、
Web はジェネレータ経由で独自のリズム UI を出すので QTE は"入る"。
"""

import argparse
import html
import http.server
import os
import random
import re
import socketserver
import urllib.parse

os.environ["PITCHSIM_QTE"] = "0"

from constants import COURSE_SHORT
from defense import ALIGNMENTS
import qte
from engine import begin_at_bat, resolve_pitch_flow, save_game_log
from fielders import POSITIONS, position_fit, weakest_fielder
from lineup import load_lineup_file
from match_state import generate_random_situation
from pitch_data import pitch_name, repertoire_options, velocity_of
from strategy import PITCH_INTENTS, build_analysis, build_postgame_report, grade_reads

_COURSE_ROWS = ["hi", "mid", "lo"]


def _course_cols(bats):
    """打者から見て「内→外」の並び。右打者=in/mid/out、左打者=out/mid/in
    (捕手・投手側から見た実際の左右がミラーする、放送のストライクゾーン表示と同じ考え方)。"""
    return ["in", "mid", "out"] if bats == "R" else ["out", "mid", "in"]


def _course_grid(bats):
    return [[f"{c}_{r}" for c in _course_cols(bats)] for r in _COURSE_ROWS]


# 球種ごとの「ざっくりした変化」。捕手視点の小さな枠に、ボールがどこから入って
# どう曲がって落ちるかを1本の矢印で描くだけ(雑でいい・見て分かればいい)。
#   drop  : 落ち量(0=落ちない 〜 1=大きく落ちる)
#   sweep : 横変化(- = 三塁側へ滑る / + = 一塁側へ食い込む。右投手基準の目安)
_PITCH_MOVE = {
    "four_seam": (0.10, 0.05), "two_seam": (0.30, 0.35), "sinker": (0.44, 0.42),
    "cutter": (0.24, -0.32), "slider": (0.50, -0.55), "sweeper": (0.52, -0.92),
    "curveball": (0.88, -0.30), "changeup": (0.58, 0.40), "splitter": (0.80, 0.10),
}


def _pitch_arc_svg(key, size=34):
    """球種の変化を捕手視点で1本の矢印にした小さな SVG(雑な軌道イメージ)。"""
    drop, sweep = _PITCH_MOVE.get(key, (0.35, 0.0))
    x0, y0 = 20.0, 3.0
    x1 = 20.0 + sweep * 12.0
    y1 = 12.0 + drop * 23.0
    cx = 20.0 + sweep * 2.5              # 変化は「遅れて」出る感じにする
    cy = y0 + (y1 - y0) * 0.32
    return (f'<svg class="parc" width="{size}" height="{size}" viewBox="0 0 40 40" aria-hidden="true">'
            f'<rect x="6" y="4" width="28" height="30" rx="2" class="pz"/>'
            f'<path d="M{x0:.1f} {y0:.1f} Q{cx:.1f} {cy:.1f} {x1:.1f} {y1:.1f}" class="pl"/>'
            f'<circle cx="{x1:.1f}" cy="{y1:.1f}" r="2.6" class="pb"/></svg>')


_SCENARIOS = ("steal", "block", "dropped3", "bunt", "signs")


def _apply_scenario(state, name):
    """動作確認用: 開始場面を特定のリズムゲームが出やすい形に仕込む(--scenario)。
    ゲームの確率はいじらない ―― 「その場面で正しい球を投げれば出る」ところまで。"""
    if name == "steal":
        # 一塁だけに俊足の走者。見送り/空振り/ボールで盗塁を仕掛けてくる
        state.runners, state.runner_speeds, state.outs = [True, False, False], [85, None, None], 0
    elif name == "block":
        # 走者一・三塁(二塁は空けてサイン盗みを避ける)。低め(内角低め/外角低め)に
        # 大きく外す球を何球か投げるとワンバウンド → ブロッキング
        state.runners, state.runner_speeds, state.outs = [True, False, True], [55, None, 60], 1
    elif name == "dropped3":
        # 一塁が空 + 2アウト。低めの空振り三振でワンバウンドなら振り逃げ
        state.runners, state.runner_speeds, state.outs = [False, False, True], [None, None, 55], 2
    elif name == "bunt":
        # 一塁走者・接戦・弱い打者。相手はバント(初球からバント処理のリズムゲーム)
        state.runners, state.runner_speeds, state.outs = [True, False, False], [60, None, None], 0
        state.our_score = state.opp_score = max(state.our_score, state.opp_score)
        state.start_our = state.start_opp = state.our_score
        b = state.lineup.current()
        b.coarse_type, b.power = "weak", 0.30
        state.opp_tactic, state.tactic_decided = "bunt", True
    elif name == "signs":
        # 二塁に走者 → 投球前にサインを覗かれることがある(まれ。何球か投げる)
        state.runners, state.runner_speeds, state.outs = [False, True, False], [None, 55, None], 0


# 開始前の操作練習で 1 回ずつ成功させるリズム 6 種。(key, 表示名, ひとこと)
_TUTORIAL_ITEMS = [
    ("tempo", "返球のテンポ", "捕った球を投手へリズムよく返す。緑の帯の中央で Space を 3 回。"),
    ("frame", "受球（フレーミング）", "きわどい見送りを静かに捕ってストライクに見せる。中央で 1 回。"),
    ("wild_block", "ブロッキング", "ワンバウンドを体で止める。まず方向（内/中/外）→ タイミング 1 回。"),
    ("steal_throw", "盗塁の送球", "走者を刺す二塁送球。握り替え → 送球の 2 回。"),
    ("field_bunt", "バント処理", "前に出て掴む → 送球の 2 回。"),
    ("change_signs", "サイン交換", "二塁走者に覗かれる前にサインを組み替える。1 回。"),
]
_TUT_DIR = {"wild_block": "mid"}   # 練習ではボールは「中」に来る


class GameSession:
    """1 プロセス = 1 ゲーム(ローカルで自分ひとりが遊ぶ想定。同時アクセスは考慮しない)。"""

    def __init__(self, lineup_batters=None, seed=None, difficulty="normal", scenario=None):
        self.lineup_batters = lineup_batters
        self.seed = seed
        self.difficulty = difficulty
        self.scenario = scenario   # 動作確認用に開始場面を仕込む(--scenario)
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
        # リズムゲーム進行中の状態: 生きたジェネレータと「今出す QTE」(kind, payload)
        self.pitch_gen = None
        self.pending_qte = None
        # 開始前の操作練習: 6 種のリズムをそれぞれ 1 回 GOOD 以上でクリア
        self.tutorial = {k: False for k, _, _ in _TUTORIAL_ITEMS}
        self.tutorial_skipped = False
        self.log_path = None

    def start_new_game(self):
        # --seed のときは毎回同じ場面から始まる(再現性優先)。それ以外は毎回ランダム。
        rng = random.Random(self.seed) if self.seed is not None else random
        self.rng = rng
        self.state = generate_random_situation(rng=rng, lineup_batters=self.lineup_batters)
        if self.scenario:
            _apply_scenario(self.state, self.scenario)
        self.started = True
        self.showing_intro = True
        self.messages = []
        self.reaction = None
        self.last_call = None
        self.last_outcome = None
        self.pending_swap = None
        self.pitch_gen = None
        self.pending_qte = None
        self.tutorial = {k: False for k, _, _ in _TUTORIAL_ITEMS}
        self.tutorial_skipped = False
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


_NAME_PREFIX = re.compile(r"^\d+\s+")


def _bname(batter):
    """打者の表示名。設計データの先頭にある打順番号（"3 三宅" など）は
    打順バッジで別に出しているので、名前からは落として重複を避ける。"""
    return _e(_NAME_PREFIX.sub("", batter.name))


# --- 野球を知らない人向けの用語説明(hover / タップで出る) ---
def _term(label, tip):
    """点線の下線つきの用語。カーソルを乗せる/タップすると説明が出る。"""
    return f'<span class="term" tabindex="0" data-tip="{_e(tip)}">{_e(label)}</span>'


def _help(tip):
    """見出しの横に置く「？」バッジ。乗せる/タップで説明。"""
    return (f'<span class="term badge" tabindex="0" role="note" '
            f'aria-label="用語の説明: {_e(tip)}" data-tip="{_e(tip)}">?</span>')


_TIP = {
    "pitch_type": "球種。速い球（フォーシーム＝直球など）と、曲がる／落ちる球（スライダー・カーブなど）がある。"
                  "数字は球速の目安（mph）。速い球と遅い球を交互に見せると打者のタイミングを外せる。",
    "course": "狙うコース。3×3のマスでストライクゾーンと少し外側を表す。真ん中＝打ちごろ、"
              "隅＝きわどい（打たれにくいが失投で甘く入るリスクも）。左右は捕手から見た向き。",
    "intent": "その1球の狙い。ストライク先行＝カウントを取りにいく／チェイス＝ボール球を振らせる／"
              "打たせて取る＝弱い当たりを打たせる／見せ球＝次を活かす捨て球／"
              "見逃しを狙う＝きわどい所で見逃し三振を取りにいく／ピッチアウト＝走者を刺すため大きく外す。",
    "count": "カウント。B（ボール）が4つで打者を歩かせる＝四球で一塁へ。S（ストライク）が3つで三振＝アウト。"
             "OUT が3つでこの回は終了。ストライクが先行すると投手が有利。",
    "runners": "塁に出ている相手の走者。1B＝一塁 2B＝二塁 3B＝三塁。"
               "三塁の走者は外野フライや内野ゴロでも生還する（＝失点）ので特に危ない。▶は俊足。",
    "alignment": "守備シフト。打者の傾向に合わせて野手の守る位置をずらす作戦。"
                 "引っ張り警戒／逆方向警戒／前進守備（一歩前で失点を防ぐ）／長打警戒／バント警戒 など。"
                 "読みが当たればアウトが増え、逆を突かれるとヒットになる。",
    "fit": "適性。その選手をそのポジションで使ったときの守備力の目安（0〜99）。"
           "同じ選手でも守る場所によって変わる。難しいポジション（遊撃＝SS など）ほど差が出る。",
    "ede": "守備全体のかみ合い具合の目安（参考値）。高いほど守備が安定している、という程度の指標。",
    "ops": "出塁率＋長打率。打者の総合力のざっくり指標。.800超なら強打者、.700未満なら怖くない打者。",
    "reaction": "打者の反応（顔文字）。本音とは限らない（約6割は素直・約1.5割は逆・残りはぼかし）。"
                "1球だけで信じすぎないこと。",
    "framing": "フレーミング。捕手の技術で、ゾーンをわずかに外れた見逃しを、静かに捕って"
               "「ストライク」に見せること。きわどい隅を要求しているほど決まりやすい。",
    "quality": "球の出来（0〜100）。高いほど打ちにくい鋭い球。投手の調子と球種で毎球変わる。",
}


# ---------------------------------------------------------------------------
# ページの外枠(CSS はここに集約)
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="color-scheme" content="dark">
<meta name="theme-color" content="#0b111c">
<title>配球判断シミュレーター</title>
<style>
:root {{
  --bg:#0b111c; --bg-2:#0f1826; --panel:#141f30; --panel-2:#1b2a40;
  --line:#27374f; --line-2:#324660;
  --ink:#e9eef7; --muted:#93a1b8; --faint:#5f6f88;
  --accent:#3fa9ff; --accent-ink:#06121f;
  --good:#2fd08a; --bad:#ff6060; --warn:#f4b942;
  --grass:#2f8046; --dirt:#b07b3e;
  --r:14px; --r-sm:9px;
  --shadow:0 1px 2px rgba(0,0,0,.5), 0 12px 30px rgba(0,0,0,.28);
}}
* {{ box-sizing:border-box; }}
html {{ -webkit-text-size-adjust:100%; font-size:17.5px; }}
@media (max-width:520px) {{ html {{ font-size:16px; }} }}
body {{
  margin:0; padding:0 0 4em; background:var(--bg); color:var(--ink);
  font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans", "Hiragino Kaku Gothic ProN",
               "Yu Gothic UI", "Segoe UI", "Noto Sans JP", system-ui, sans-serif;
  font-variant-numeric: tabular-nums; line-height:1.5;
  background-image:
    radial-gradient(1100px 500px at 78% -8%, rgba(63,169,255,.10), transparent 60%),
    radial-gradient(900px 500px at -6% 108%, rgba(47,208,138,.07), transparent 60%);
  background-attachment:fixed;
}}
.wrap {{ max-width:1120px; margin:0 auto; padding:0 18px; }}
a {{ color:var(--accent); }}
h1, h2, h3, h4 {{ margin:0; }}
button:focus-visible, input:focus-visible + label, select:focus-visible, a:focus-visible {{
  outline:2px solid var(--accent); outline-offset:2px;
}}

/* --- ブランドバー --- */
.brand {{ display:flex; align-items:baseline; gap:.6em; padding:.85em 0 .55em; }}
.brand .mark {{ font-weight:800; letter-spacing:.16em; font-size:.92em;
  color:var(--ink); text-transform:uppercase; }}
.brand .mark .dot {{ color:var(--accent); }}
.brand .sub {{ color:var(--faint); font-size:.72em; letter-spacing:.14em; text-transform:uppercase; }}

/* --- スコアボード帯(sticky) --- */
.scorebar {{
  position:sticky; top:0; z-index:20;
  background:linear-gradient(180deg, #0c1622, #0a121c);
  border:1px solid var(--line); border-radius:12px;
  box-shadow:var(--shadow), inset 0 1px 0 rgba(255,255,255,.03);
  padding:.55em .9em; margin-bottom:1.1em;
}}
.sb-row {{ display:flex; align-items:center; gap:.75em 1.15em; flex-wrap:wrap; }}
.sb-team {{ display:flex; align-items:baseline; gap:.4em; }}
.sb-team .abbr {{ font-size:.68em; letter-spacing:.14em; color:var(--muted); text-transform:uppercase; }}
.sb-team .run {{ font-size:1.5em; font-weight:800; line-height:1; }}
.sb-team.lead .run {{ color:var(--good); }}
.sb-inning {{ font-weight:800; letter-spacing:.05em; color:#9fd4ff; padding:0 .1em; }}
.sb-sep {{ width:1px; align-self:stretch; background:var(--line); }}
.sb-grp {{ display:flex; align-items:center; gap:.5em; font-size:.82em; color:var(--muted); }}
.sb-grp b {{ color:var(--ink); }}
.pip {{ display:inline-block; width:.62em; height:.62em; border-radius:50%; margin:0 1px;
        background:transparent; border:1.5px solid var(--faint); vertical-align:middle; }}
.pip.on {{ border-color:transparent; }}
.pip.b.on {{ background:var(--warn); }} .pip.s.on {{ background:var(--accent); }}
.pip.o.on {{ background:var(--bad); }}
.warnpill {{ font-size:.72em; font-weight:800; letter-spacing:.06em; color:#fff;
  background:var(--bad); border-radius:1em; padding:.12em .6em;
  animation:pulse 1.3s ease-in-out infinite; }}
@keyframes pulse {{ 0%,100%{{ opacity:1; }} 50%{{ opacity:.45; }} }}
.mini-diamond {{ vertical-align:middle; }}
.mini-diamond .base {{ fill:#22344c; stroke:var(--line-2); stroke-width:2; }}
.mini-diamond .base.on {{ fill:var(--warn); stroke:var(--warn); }}
.mini-diamond .base.fast {{ fill:var(--accent); stroke:var(--accent); }}

/* --- カード --- */
.card {{
  background:linear-gradient(180deg, var(--panel), var(--bg-2));
  border:1px solid var(--line); border-radius:var(--r);
  padding:1.05em 1.15em; margin-bottom:1.1em; box-shadow:var(--shadow);
}}
.card > h2, .card > h3 {{
  font-size:.72em; color:var(--muted); font-weight:700; letter-spacing:.14em;
  text-transform:uppercase; margin-bottom:.7em;
}}
.card > h2 {{ font-size:.9em; color:var(--ink); }}
.grid2 {{ display:grid; grid-template-columns:1.08fr 1fr; gap:1.1em; align-items:start; }}
.grid2.gcols {{ grid-template-columns:1fr 1.02fr; }}
.eyebrow {{ font-size:.7em; letter-spacing:.14em; text-transform:uppercase; color:var(--muted);
  font-weight:700; margin:.9em 0 .35em; }}
.eyebrow:first-child {{ margin-top:0; }}

/* --- 球場図 --- */
.field {{ position:relative; width:100%; aspect-ratio:1.7; border-radius:12px; overflow:hidden;
  border:1px solid var(--line);
  background:
    radial-gradient(130% 100% at 50% 120%, #4aa862 0%, #388f52 45%, #2c7a43 74%, #256b3b 100%); }}
.field svg {{ position:absolute; inset:0; width:100%; height:100%; }}
.field .align-badge {{ position:absolute; left:8px; top:8px; font-size:.66em; font-weight:800;
  letter-spacing:.08em; text-transform:uppercase; color:#eaf6ee;
  background:rgba(6,16,10,.6); border:1px solid rgba(255,255,255,.18);
  border-radius:1em; padding:.2em .7em; }}
.posbtn {{ position:absolute; transform:translate(-50%,-50%); }}
.posbtn form {{ margin:0; }}
.posbtn button {{
  width:66px; padding:.34em .2em .3em; border-radius:10px;
  border:1px solid rgba(255,255,255,.28); background:rgba(9,18,12,.72);
  color:#eef6f0; font-size:.72em; line-height:1.28; cursor:pointer; text-align:center;
  backdrop-filter:blur(2px); box-shadow:0 4px 12px rgba(0,0,0,.35);
  transition:transform .12s, box-shadow .12s, border-color .12s;
  border-top:2px solid var(--tier, #6fd39a);
}}
.posbtn button:hover {{ transform:translateY(-2px); box-shadow:0 8px 18px rgba(0,0,0,.45); }}
.posbtn button b {{ display:block; font-size:1.08em; letter-spacing:.04em; }}
.posbtn button .fit {{ color:#bfe8cd; font-size:.9em; }}
.posbtn.selected button {{ border-color:#ffd35a; box-shadow:0 0 0 2px #ffd35a, 0 8px 18px rgba(0,0,0,.5); }}
.posbtn.weak button {{ border-top-color:var(--bad); box-shadow:0 0 0 1.5px rgba(255,96,96,.7); }}
.field .homeplate {{ position:absolute; left:50%; bottom:3%; transform:translateX(-50%);
  color:#eef6f0; font-size:.7em; text-align:center; letter-spacing:.06em; }}
.field-legend {{ font-size:.78em; color:var(--muted); margin-top:.55em; }}
@media (max-width:520px) {{ .posbtn button {{ width:52px; padding:.26em .12em; font-size:.64em; }} }}
@media (max-width:380px) {{ .posbtn button {{ width:44px; font-size:.58em; }} }}

/* --- 打者カード --- */
.batter-head {{ display:flex; align-items:center; gap:.7em; }}
.batter-num {{ width:2.1em; height:2.1em; flex:none; border-radius:50%; display:grid; place-items:center;
  background:var(--panel-2); border:1px solid var(--line-2); font-weight:800; }}
.batter-head .name {{ font-size:1.18em; font-weight:800; letter-spacing:.02em; }}
.hand {{ font-size:.66em; font-weight:800; letter-spacing:.08em; padding:.15em .5em; border-radius:.5em;
  background:var(--panel-2); border:1px solid var(--line-2); color:var(--muted); }}
.hand.L {{ color:#8fd0ff; border-color:#2b5b82; }}
.hand.R {{ color:#ffcf8f; border-color:#7a5a2c; }}
.tag {{ display:inline-block; font-size:.7em; padding:.12em .55em; border-radius:1em;
  background:var(--panel-2); border:1px solid var(--line-2); color:var(--muted); }}
.slash {{ display:flex; gap:1.1em; font-size:.82em; color:var(--muted); margin:.6em 0 .1em; }}
.slash b {{ color:var(--ink); font-weight:700; }}
.scout {{ font-size:.85em; color:var(--muted); }}
.myread {{ font-size:.82em; color:#e59ad8; margin-top:.35em; }}
.batter-mini {{ display:flex; justify-content:space-between; gap:.6em; font-size:.8em; color:var(--muted);
  padding:.42em .1em; border-top:1px dashed var(--line); }}
.batter-mini:first-of-type {{ margin-top:.5em; }}

/* --- 反応(顔文字) --- */
.reaction {{ text-align:center; padding:.7em 0 .3em; }}
.reaction .face {{ font-size:2.5em; font-weight:800; line-height:1.1;
  text-shadow:0 2px 12px rgba(0,0,0,.4); }}
.reaction .text {{ color:var(--muted); margin-top:.25em; font-size:.92em; }}
.rc-jammed .face, .rc-out_front .face, .rc-frustrated .face {{ color:var(--bad); }}
.rc-surprised .face {{ color:#c58bff; }}
.rc-confident .face, .rc-locked_in .face {{ color:var(--good); }}
.rc-comfortable .face {{ color:var(--warn); }}
.rc-defensive .face, .rc-uncertain .face {{ color:var(--accent); }}
.rc-neutral .face {{ color:var(--muted); }}

/* --- ストライクゾーン --- */
.zonewrap {{ display:flex; gap:1.5em; flex-wrap:wrap; align-items:flex-start; }}
.zone-shadow {{ display:inline-block; padding:12px; border:1px dashed var(--line-2); border-radius:12px;
  background:radial-gradient(120% 120% at 50% 50%, rgba(63,169,255,.06), transparent 72%); }}
.zone {{ display:grid; grid-template-columns:repeat(3,58px); grid-template-rows:repeat(3,58px); gap:5px; }}
.zone .cell {{ position:relative; border:1px solid var(--line-2); border-radius:7px;
  background:var(--panel-2); }}
.zone label {{ position:absolute; inset:0; display:flex; align-items:center; justify-content:center;
  font-size:.76em; color:var(--muted); cursor:pointer; border-radius:7px;
  transition:background .1s, color .1s; }}
.zone label:hover {{ background:rgba(63,169,255,.14); color:var(--ink); }}
.zone input {{ position:absolute; opacity:0; pointer-events:none; }}
.zone input:checked + label {{ background:var(--accent); color:var(--accent-ink); font-weight:800; }}
.marker {{ position:absolute; width:23px; height:23px; border-radius:50%; display:flex;
  align-items:center; justify-content:center; font-size:.7em; font-weight:800; pointer-events:none; }}
.marker.target {{ top:3px; left:3px; background:transparent; color:#bcd8f2;
  border:2px solid var(--accent); box-shadow:0 0 0 1px rgba(0,0,0,.3); }}
.marker.actual {{ bottom:3px; right:3px; background:var(--bad); color:#fff;
  border:2px solid var(--bg); }}
.marker.actual.ontarget {{ background:var(--good); }}
.zone-key {{ font-size:.78em; color:var(--muted); }}
.zone-key .dot {{ display:inline-block; width:10px; height:10px; border-radius:50%; margin:0 .3em 0 .1em;
  vertical-align:middle; }}

/* --- チップ(球種・意図) --- */
.callgrid {{ display:flex; flex-wrap:wrap; gap:1.4em 1.8em; align-items:flex-start; margin-top:.3em; }}
.callgrid-zone {{ flex:0 0 auto; }}
.callgrid-intent {{ flex:1 1 200px; min-width:200px; }}
.chiprow {{ display:flex; flex-wrap:wrap; gap:.45em; }}
.chiprow input {{ position:absolute; opacity:0; pointer-events:none; }}
.chiprow label {{ border:1px solid var(--line-2); border-radius:999px; padding:.42em .9em;
  font-size:.86em; cursor:pointer; background:var(--panel-2); color:var(--ink);
  transition:background .1s, border-color .1s, transform .1s; user-select:none; }}
.chiprow label:hover {{ border-color:var(--accent); }}
.chiprow label .v {{ color:var(--muted); font-size:.86em; margin-left:.35em; }}
.chiprow input:checked + label {{ background:var(--accent); color:var(--accent-ink); border-color:var(--accent);
  font-weight:800; }}
.chiprow input:checked + label .v {{ color:var(--accent-ink); opacity:.75; }}

/* --- 球種の軌道イメージ(雑) --- */
.parc {{ vertical-align:middle; margin-right:.35em; }}
.parc .pz {{ fill:none; stroke:var(--line-2); stroke-width:1; }}
.parc .pl {{ fill:none; stroke:var(--muted); stroke-width:2.2; stroke-linecap:round; }}
.parc .pb {{ fill:var(--muted); }}
.chiprow input:checked + label .parc .pl,
.chiprow input:checked + label .parc .pb {{ stroke:var(--accent-ink); fill:var(--accent-ink); }}
.chiprow input:checked + label .parc .pz {{ stroke:rgba(6,18,31,.4); }}

/* --- 用語ツールチップ(野球を知らない人向け。hover / タップで説明) --- */
.term {{ border-bottom:1px dotted var(--muted); cursor:help; position:relative; outline:none; }}
.term.badge {{ border:1px solid var(--line-2); border-radius:50%; display:inline-flex;
  align-items:center; justify-content:center; width:1.4em; height:1.4em; font-size:.72em;
  font-weight:800; color:var(--muted); background:var(--panel-2); vertical-align:middle;
  margin-left:.35em; line-height:1; }}
.term.badge:hover, .term:focus.badge {{ color:var(--accent); border-color:var(--accent); }}
.term::after {{
  content:attr(data-tip); position:absolute; left:0; top:calc(100% + .45em); z-index:60;
  width:min(20em, 74vw); white-space:normal; text-align:left;
  background:#0c1826; color:var(--ink); border:1px solid var(--line-2); border-radius:9px;
  padding:.6em .75em; font-size:.8rem; font-weight:400; line-height:1.5; letter-spacing:0;
  text-transform:none; box-shadow:0 10px 30px rgba(0,0,0,.55);
  opacity:0; visibility:hidden; transform:translateY(-3px);
  transition:opacity .12s ease, transform .12s ease; pointer-events:none;
}}
.term:hover::after, .term:focus::after {{ opacity:1; visibility:visible; transform:translateY(0); }}
@media (max-width:560px) {{ .term::after {{ left:auto; right:0; }} }}

/* --- 受球リズムゲーム(きわどい球のフレーミング) --- */
.rg-wrap {{ text-align:center; padding:.4em 0 .2em; }}
.rg-call {{ color:var(--muted); font-size:.95em; margin:.2em 0 .1em; }}
.rg-call b {{ color:var(--ink); }}
.rg-track {{ position:relative; height:48px; max-width:440px; margin:1.2em auto .4em;
  border-radius:10px; background:linear-gradient(90deg,#18243c,#24344f,#18243c);
  border:1px solid var(--line-2); overflow:hidden; cursor:pointer;
  touch-action:manipulation; user-select:none; }}
.rg-target {{ position:absolute; top:0; bottom:0; left:41%; width:18%;
  background:rgba(47,208,138,.20);
  border-left:2px solid var(--good); border-right:2px solid var(--good); }}
.rg-target .core {{ position:absolute; top:0; bottom:0; left:50%; width:2px;
  margin-left:-1px; background:var(--good); opacity:.8; }}
.rg-marker {{ position:absolute; top:-3px; bottom:-3px; width:5px; margin-left:-2.5px; left:0;
  background:var(--accent); border-radius:3px; box-shadow:0 0 14px var(--accent); }}
.rg-hint {{ color:var(--muted); font-size:.9em; margin:.55em 0; }}
.rg-key {{ display:inline-block; border:1px solid var(--line-2); border-radius:6px;
  padding:.02em .55em; font-weight:800; color:var(--ink); background:var(--panel-2); }}
.rg-verdict {{ font-size:1.45em; font-weight:800; letter-spacing:.06em; min-height:1.4em; margin:.35em 0; }}
.rg-verdict.perfect {{ color:var(--good); }}
.rg-verdict.good {{ color:var(--warn); }}
.rg-verdict.miss {{ color:var(--bad); }}
.qte-dirs {{ display:flex; gap:.7em; justify-content:center; margin:.9em 0 .2em; }}
.qte-dirs button {{ font-weight:800; padding:.7em 1.5em; font-size:1.05em; }}
.qte-announce {{ text-align:center; font-size:2em; font-weight:800; letter-spacing:.1em;
  min-height:1.35em; margin:.25em 0; color:var(--accent);
  opacity:0; transform:scale(.75); transition:opacity .12s ease, transform .14s ease; }}
.qte-announce.show {{ opacity:1; transform:scale(1); }}
.qte-label {{ text-align:center; font-weight:800; font-size:1.2em; margin:.35em 0 0;
  color:var(--ink); min-height:1.4em; }}
.catcher-line {{ font-size:1.15em; font-weight:800; letter-spacing:.02em; margin:.5em 0;
  padding:.4em .7em; border-radius:.5em; border-left:4px solid; }}
.catcher-line.ok {{ color:var(--good); border-left-color:var(--good); background:rgba(47,208,138,.10); }}
.catcher-line.ng {{ color:var(--bad); border-left-color:var(--bad); background:rgba(255,96,96,.10); }}
.good-call-line {{ margin:.4em 0; font-size:.92em; }}
.gc-badge {{ display:inline-block; font-weight:800; font-size:.9em; color:var(--good);
  border:1px solid var(--good); border-radius:.4em; padding:.05em .5em; margin-right:.4em;
  background:rgba(47,208,138,.10); }}
.tut-list {{ list-style:none; padding:0; margin:.7em 0 .3em; text-align:left; }}
.tut-list li {{ padding:.4em .1em; border-bottom:1px solid var(--line); font-size:.92em; }}
.tut-list li:last-child {{ border-bottom:none; }}
.tut-list li.done {{ color:var(--good); }}
.tut-list li.now {{ color:var(--accent); font-weight:700; }}
.prev-strip {{ font-size:.85em; font-weight:700; margin:.1em 0 .4em; }}
/* --- 返球のテンポ(配球コール画面の一番上。他のリズムゲームと同じ形式を3回) --- */
.tempo-box {{ border:1px solid var(--accent); border-radius:10px; padding:.75em .95em;
  margin-bottom:1em; background:rgba(63,169,255,.08); }}
.tempo-head {{ display:flex; align-items:baseline; gap:.9em; flex-wrap:wrap; }}
.tempo-react {{ font-size:.95em; }}
#tempo-track {{ margin:.6em auto .3em; }}
#tempo-label {{ font-weight:800; color:var(--accent); }}

/* --- ボタン --- */
button, .btn {{
  padding:.55em 1.05em; border-radius:var(--r-sm); border:1px solid var(--line-2);
  background:var(--panel-2); cursor:pointer; font-size:.9em; color:var(--ink);
  transition:background .12s, border-color .12s, transform .1s; font-family:inherit;
}}
button:hover {{ border-color:var(--accent); }}
button:active {{ transform:translateY(1px); }}
button.primary {{ background:linear-gradient(180deg, #4fb2ff, #2f97ec); color:var(--accent-ink);
  border-color:transparent; font-weight:800; letter-spacing:.03em;
  box-shadow:0 6px 18px rgba(63,169,255,.28); }}
button.primary:hover {{ filter:brightness(1.06); }}
button.big {{ font-size:1.05em; padding:.8em 1.9em; }}
select {{ padding:.5em .7em; border-radius:var(--r-sm); border:1px solid var(--line-2);
  background:var(--panel-2); color:var(--ink); font-family:inherit; font-size:.9em; }}
.form-actions {{ display:flex; flex-wrap:wrap; gap:.6em; align-items:center; }}

/* --- 直近の1球 --- */
.resultline {{ display:inline-block; font-size:1.2em; font-weight:800; letter-spacing:.03em;
  padding:.28em .85em; border-radius:.6em; margin:.5em 0;
  background:var(--panel-2); border:1px solid var(--line-2); border-left-width:4px; }}
.resultline .eyebrow {{ display:inline; font-size:.55em; opacity:.7; letter-spacing:.14em; }}
.resultline.r-out {{ color:var(--good); border-left-color:var(--good); background:rgba(47,208,138,.10); }}
.resultline.r-hit {{ color:var(--bad); border-left-color:var(--bad); background:rgba(255,96,96,.10); }}
.resultline.r-ball {{ color:var(--warn); border-left-color:var(--warn); background:rgba(244,185,66,.10); }}
.resultline.r-strike {{ color:var(--accent); border-left-color:var(--accent); background:rgba(63,169,255,.10); }}
.callline {{ font-size:.9em; color:var(--muted); margin:.15em 0; }}
.callline b {{ color:var(--ink); }}
.msglist {{ margin-top:.7em; border-top:1px solid var(--line); padding-top:.5em; }}
.msg {{ padding:.14em 0; font-size:.9em; color:var(--muted); }}
.msg::before {{ content:"▸ "; color:var(--faint); }}
.msg.bad {{ color:var(--bad); }}
.msg.good {{ color:var(--good); }}

/* --- 配球履歴 --- */
.tablescroll {{ overflow-x:auto; -webkit-overflow-scrolling:touch; }}
table.history {{ width:100%; min-width:340px; border-collapse:collapse; font-size:.86em; }}
table.history th {{ text-align:left; color:var(--muted); font-weight:700; padding:.3em .5em;
  border-bottom:1px solid var(--line); white-space:nowrap; letter-spacing:.03em; }}
table.history td {{ padding:.36em .5em; border-bottom:1px solid var(--line); white-space:nowrap; }}
table.history tr:last-child td {{ border-bottom:none; }}
.rdot {{ display:inline-block; width:.55em; height:.55em; border-radius:50%; margin-right:.45em;
  vertical-align:middle; }}
.res-strike {{ color:var(--accent); }} .res-strike .rdot {{ background:var(--accent); }}
.res-ball {{ color:var(--warn); }} .res-ball .rdot {{ background:var(--warn); }}
.res-hit {{ color:var(--bad); font-weight:700; }} .res-hit .rdot {{ background:var(--bad); }}
.res-out {{ color:var(--good); }} .res-out .rdot {{ background:var(--good); }}
.framed-mark {{ color:var(--accent); font-size:.82em; }}

/* --- 打球チャート --- */
pre.spray {{ background:var(--bg); border:1px solid var(--line); color:#9fe3b6;
  padding:.7em .9em; border-radius:8px; font-size:.9em; line-height:1.35; overflow-x:auto; }}

/* --- ヒント / 作戦警告 --- */
.hint {{ background:rgba(244,185,66,.09); border:1px solid #6a5320; border-radius:10px;
  padding:.75em 1em; font-size:.88em; color:#f0d9a6; margin-bottom:1.1em; }}
.tactic-alert {{ background:rgba(255,96,96,.10); border:1px solid #7a2b2b; border-radius:12px;
  padding:.8em 1em; margin-bottom:1.1em; color:#ffc9c9; font-weight:600; }}
.tactic-alert b {{ color:#fff; }}

/* --- スタットタイル / メーター --- */
.report-hero {{ text-align:center; padding:1.4em 1em 1.2em; }}
.report-hero .headline {{ font-size:1.5em; font-weight:800; letter-spacing:.02em; margin:.15em 0; }}
.report-hero .score {{ font-size:1.05em; color:var(--muted); }}
.report-stats {{ display:grid; grid-template-columns:repeat(auto-fit, minmax(96px,1fr)); gap:.65em; }}
.stat {{ background:var(--panel-2); border:1px solid var(--line-2); border-radius:10px;
  padding:.7em .5em; text-align:center; }}
.stat .n {{ font-size:1.7em; font-weight:800; line-height:1; }}
.stat .label {{ font-size:.68em; color:var(--muted); letter-spacing:.06em; text-transform:uppercase;
  margin-top:.3em; }}
.meter {{ height:8px; border-radius:5px; background:var(--panel-2); border:1px solid var(--line-2);
  overflow:hidden; margin:.35em 0 .1em; }}
.meter > span {{ display:block; height:100%; background:linear-gradient(90deg,#2f97ec,#3fd0a0); }}
.meter.bad > span {{ background:linear-gradient(90deg,#f4b942,#ff6060); }}
.kv {{ display:flex; justify-content:space-between; font-size:.85em; color:var(--muted); }}
.kv b {{ color:var(--ink); }}
.split2 {{ display:grid; grid-template-columns:1fr 1fr; gap:.8em; }}
.split2 .box {{ background:var(--panel-2); border:1px solid var(--line-2); border-radius:10px; padding:.7em .8em; }}
.split2 .box .cap {{ font-size:.7em; letter-spacing:.08em; text-transform:uppercase; font-weight:800; }}
.split2 .box.unlucky .cap {{ color:var(--good); }}
.split2 .box.lucky .cap {{ color:var(--bad); }}
.split2 .box p {{ font-size:.85em; margin:.3em 0; }}
.calllist p {{ font-size:.88em; margin:.28em 0; color:var(--ink); }}
.calllist p .num {{ color:var(--muted); }}

.dim {{ color:var(--faint); }}
.center {{ text-align:center; }}

@media (max-width:720px) {{
  .grid2, .grid2.gcols, .split2 {{ grid-template-columns:1fr; }}
  .callgrid {{ gap:1.1em; }}
  .callgrid-zone, .callgrid-intent {{ flex:1 1 100%; min-width:0; }}
  .zone {{ grid-template-columns:repeat(3,52px); grid-template-rows:repeat(3,52px); }}
  .sb-team .run {{ font-size:1.3em; }}
}}
.bgm-btn {{ margin-left:auto; font-size:.72em; letter-spacing:.06em; padding:.3em .8em;
  border-radius:999px; color:var(--muted); }}
</style></head><body>
<div class="wrap">
  <div class="brand">
    <span class="mark">CATCHER<span class="dot">'</span>S CALL</span>
    <span class="sub">終盤の守り切りゲーム</span>
    <button type="button" id="bgm-btn" class="bgm-btn" title="応援 BGM（オリジナル）">♪ 応援 ON</button>
  </div>
{body}
</div>
<script>{bgm}</script>
</body></html>"""


# BGM を Web Audio で合成する。トランペット＋太鼓の自作曲(著作物は不使用)。
# D 自然短調・♩=162・全16小節ループ。8〜11小節目が盛り上がる「サビ」。
# フォーム送信のたびにページが再読み込みされるので、localStorage に開始時刻を持ち、
# 曲の正しい位置から鳴らし直す(1球ごとに一瞬だけ途切れる)。
_BGM_JS = """
window.CCBGM = (function () {
  // トランペット＋太鼓の自作曲(著作物は不使用)。D 自然短調・♩=162・全16小節ループ。
  // 8〜11小節目を「サビ」にして、高い音＋ハモリ＋16分の太鼓で盛り上げる。
  var A = null, running = false, sched = null, step = 0, nextT = 0, master = null;
  var BPM = 162, SPB = 60 / BPM / 4;
  var N = {
    A4:440.00, C5:523.25, D5:587.33, E5:659.25, F5:698.46, G5:783.99,
    A5:880.00, Bb5:932.33, C6:1046.50, D6:1174.66, E6:1318.51
  };
  var H3b = {D6:'Bb5', C6:'A5', E6:'C6', Bb5:'G5', A5:'F5', G5:'E5', F5:'D5'}; // 3度下ハモリ
  // --- 旋律(1マス = 16分音符、0 = 休符) ---
  var MEL = [
    'D5',0,'D5',0, 'F5',0,'D5',0, 'A5',0,0,0, 'A5',0,'G5',0,        // m1
    'F5',0,0,0, 'E5',0,'F5',0, 'D5',0,0,0, 0,0,0,0,                 // m2
    'D5',0,'D5',0, 'F5',0,'A5',0, 'C6',0,0,0, 'C6',0,'Bb5',0,       // m3
    'A5',0,0,0, 'G5',0,'A5',0, 'F5',0,0,0, 0,0,'A5','C6',           // m4
    'D6',0,'A5',0, 'F5',0,'A5',0, 'D6',0,'A5',0, 'F5',0,0,0,        // m5
    'C6',0,'A5',0, 'Bb5',0,'C6',0, 'A5',0,0,0, 0,'G5','A5','Bb5',   // m6
    'C6',0,'C6',0, 'A5',0,'C6',0, 'D6',0,0,0, 'D6',0,'C6',0,        // m7
    'Bb5',0,'C6',0, 'D6',0,'E6',0, 'D6',0,'C6',0, 'A5','C6','D6','E6', // m8 (サビへ)
    'E6',0,0,0, 'D6',0,'C6',0, 'D6',0,0,0, 0,0,0,0,                 // m9  サビ
    'C6',0,0,0, 'D6',0,'A5',0, 'Bb5',0,0,0, 0,'A5','Bb5','C6',      // m10 サビ
    'D6',0,0,0, 'E6',0,'D6',0, 'C6',0,'A5',0, 0,0,0,0,              // m11 サビ
    'Bb5',0,'A5',0, 'G5',0,'A5',0, 'D6',0,0,0, 'C6',0,'Bb5',0,      // m12 サビ
    'A5',0,0,0, 'F5',0,'A5',0, 'D5',0,'F5',0, 'A5',0,0,0,           // m13
    'G5',0,'F5',0, 'E5',0,'F5',0, 'D5',0,0,0, 0,0,'E5',0,           // m14
    'F5',0,0,0, 'E5',0,'D5',0, 'E5',0,'F5',0, 'G5',0,'A5',0,        // m15
    'Bb5',0,'A5',0, 'G5',0,'F5',0, 'E5',0,0,0, 0,'A4','C5','D5'     // m16
  ];
  var STEPS = MEL.length, LOOP = SPB * STEPS;                      // 16小節 = 256マス
  function osc(freq, t, dur, type, gain, slideTo) {
    var o = A.createOscillator(), g = A.createGain();
    o.type = type; o.frequency.setValueAtTime(freq, t);
    if (slideTo) o.frequency.exponentialRampToValueAtTime(slideTo, t + dur * 0.6);
    g.gain.setValueAtTime(0.0001, t);
    g.gain.exponentialRampToValueAtTime(gain, t + 0.006);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    o.connect(g); g.connect(master); o.start(t); o.stop(t + dur + 0.04);
  }
  function noise(t, dur, type, freq, gain) {
    var len = Math.max(1, Math.floor(A.sampleRate * dur));
    var b = A.createBuffer(1, len, A.sampleRate), d = b.getChannelData(0);
    for (var i = 0; i < len; i++) d[i] = Math.random() * 2 - 1;
    var n = A.createBufferSource(); n.buffer = b;
    var f = A.createBiquadFilter(); f.type = type; f.frequency.value = freq; f.Q.value = 1;
    var g = A.createGain(); g.gain.setValueAtTime(gain, t);
    g.gain.exponentialRampToValueAtTime(0.0001, t + dur);
    n.connect(f); f.connect(g); g.connect(master); n.start(t); n.stop(t + dur);
  }
  function taiko(t, f0, gain) {                          // 大太鼓 ドーン
    osc(f0, t, 0.34, 'sine', gain, f0 * 0.4);
    osc(f0 * 1.5, t, 0.12, 'triangle', gain * 0.45, f0 * 0.7);
    noise(t, 0.06, 'lowpass', 160, gain * 0.5);
  }
  function snare(t, gain) { noise(t, 0.10, 'bandpass', 1900, gain); noise(t, 0.09, 'highpass', 3600, gain * 0.55); }
  function hat(t, gain) { noise(t, 0.03, 'highpass', 9000, gain); }
  function crash(t) { noise(t, 1.0, 'highpass', 4200, 0.20); noise(t, 0.4, 'bandpass', 7000, 0.12); }
  function trumpet(t, dur, freq, gain) {                 // トランペット(ユニゾン)
    osc(freq, t, dur, 'sawtooth', gain, freq * 1.004);
    osc(freq * 1.006, t, dur, 'sawtooth', gain * 0.7);
    osc(freq, t, dur * 0.85, 'square', gain * 0.28);
    osc(freq * 2, t, dur * 0.4, 'triangle', gain * 0.18);
  }
  function scheduleStep(s, t) {
    var bar = Math.floor(s / 16), k = s % 16;
    var hook = (bar >= 8 && bar < 12);                    // サビ(盛り上がり)
    var fill = (bar === 7 || bar === 15);                 // サビ前 / ループ前のフィル

    // --- 太鼓(ドラム) ---
    if (k === 0 || k === 8) taiko(t, 100, 0.68);          // 大太鼓 1・3拍
    if (hook && (k === 4 || k === 12)) taiko(t, 116, 0.44); // サビは4つ打ち気味
    if (k === 4 || k === 12) snare(t, 0.28);              // スネア 2・4拍
    else if (k % 2 === 1) snare(t, hook ? 0.09 : 0.03);   // 16分(サビは強く)
    if (k % 2 === 0) hat(t, hook ? 0.045 : 0.03);         // ハイハット 8分
    if (hook && k % 2 === 1) hat(t, 0.02);                // サビは16分ハット
    if (k === 0 && (bar === 0 || bar === 8)) crash(t);    // ループ頭 / サビ頭
    if (fill && k >= 10) snare(t, 0.06 + (k - 10) * 0.02); // フィル
    if (bar === 15 && k === 14) taiko(t, 84, 0.85);       // キメ

    // --- 旋律(トランペット) ---
    var m = MEL[s];
    if (m) {
      var held = (MEL[(s + 1) % STEPS] === 0 && MEL[(s + 2) % STEPS] === 0);
      var dur = SPB * (held ? 3.4 : 1.9);
      trumpet(t, dur, N[m], hook ? 0.17 : 0.15);
      osc(N[m] / 2, t, dur, 'sawtooth', 0.055);           // オクターブ下で厚み
      if (hook && held && H3b[m]) trumpet(t, dur, N[H3b[m]], 0.06); // サビの伸ばしに3度下ハモリ
    }
  }
  function tick() {
    if (!A) return;
    while (nextT < A.currentTime + 0.12) {
      scheduleStep(step, nextT); nextT += SPB; step = (step + 1) % STEPS;
    }
  }
  function startAt(phaseSec) {
    step = Math.floor(((phaseSec % LOOP) + LOOP) % LOOP / SPB) % STEPS;
    nextT = A.currentTime + 0.06; running = true;
    if (sched) clearInterval(sched);
    sched = setInterval(tick, 25);
  }
  // 既定は ON。ユーザーが自分で切ったとき('0')だけ鳴らさない。
  function want() { try { return localStorage.getItem('cc_bgm') !== '0'; } catch (e) { return true; } }
  function startEpoch() {
    var now = Date.now();
    try { return parseInt(localStorage.getItem('cc_bgm_start') || now, 10); } catch (e) { return now; }
  }
  function ensureAudio() {
    if (!A) {
      A = new (window.AudioContext || window.webkitAudioContext)();
      master = A.createGain(); master.gain.value = 0.32; master.connect(A.destination);
    }
  }
  return {
    running: function () { return running; },
    toggle: function () {
      if (running) { this.stop(); return; }
      ensureAudio(); A.resume();
      try {
        if (localStorage.getItem('cc_bgm') === '0' || !localStorage.getItem('cc_bgm_start'))
          localStorage.setItem('cc_bgm_start', String(Date.now()));
        localStorage.setItem('cc_bgm', '1');
      } catch (e) {}
      startAt((Date.now() - startEpoch()) / 1000);
    },
    stop: function () {
      running = false; if (sched) { clearInterval(sched); sched = null; }
      try { localStorage.setItem('cc_bgm', '0'); } catch (e) {}
      if (A) A.suspend();
    },
    resumeIfWanted: function () {
      if (!want() || running) return;
      ensureAudio();
      try { if (!localStorage.getItem('cc_bgm_start')) localStorage.setItem('cc_bgm_start', String(Date.now())); } catch (e) {}
      A.resume().then(function () { startAt((Date.now() - startEpoch()) / 1000); }).catch(function () {});
    }
  };
})();
(function () {
  var btn = document.getElementById('bgm-btn'); if (!btn) return;
  function label() {
    var on = true; try { on = localStorage.getItem('cc_bgm') !== '0'; } catch (e) {}
    btn.textContent = on ? '♪ 応援 ON' : '♪ 応援 OFF';
    btn.style.color = on ? 'var(--accent)' : 'var(--muted)';
  }
  label();
  btn.addEventListener('click', function () { window.CCBGM.toggle(); label(); });
  // 最初から鳴らす。自動再生がブロックされても、最初の操作(クリック/キー/タップ)で開始。
  window.CCBGM.resumeIfWanted();
  var evs = ['pointerdown', 'keydown', 'touchstart'];
  function kick() {
    window.CCBGM.resumeIfWanted();
    evs.forEach(function (e) { document.removeEventListener(e, kick, true); });
  }
  evs.forEach(function (e) { document.addEventListener(e, kick, true); });
})();
"""


def _page(body):
    return _PAGE.format(body=body, bgm=_BGM_JS)


# ---------------------------------------------------------------------------
# START 画面
# ---------------------------------------------------------------------------
def render_start_screen(session):
    seed_note = (f'<p class="dim">seed = {session.seed} / 再現可能モード</p>'
                 if session.seed is not None else "")
    return f"""
<div class="card center" style="padding:2.6em 1.2em;">
  <p class="eyebrow center" style="margin-bottom:.6em;">Late-inning · Defensive half · 3 outs to survive</p>
  <p style="font-size:1.25em; font-weight:800;">試合終盤、捕手として1イニングを守り切る</p>
  <p class="dim" style="max-width:34em; margin:.6em auto 0;">
    球種・狙うコース（3×3）・配球意図を選び、3アウトを取るまでが1ゲーム。
    プレイ中に正解は出ません。打者の反応・配球履歴・打者情報から自分で読みます。</p>
  {seed_note}
  <form method="post" action="/start" style="margin-top:1.4em;">
    <button class="primary big" type="submit">START GAME</button>
  </form>
</div>"""


def render_situation_intro(state):
    b = state.batter
    hand = "右投" if state.pitcher.throws == "R" else "左投"
    lead = "自軍" if state.our_score > state.opp_score else ("相手" if state.opp_score > state.our_score else "")
    lead_txt = f"{lead}が{abs(state.our_score - state.opp_score)}点リード" if lead else "同点"
    return f"""
<div class="card center" style="padding:2.2em 1.2em;">
  <p class="eyebrow center">{_e(state.inning_label())} · {_e(lead_txt)}</p>
  <div style="font-size:2.2em; font-weight:800; letter-spacing:.02em; margin:.25em 0;">
    <span class="dim" style="font-size:.5em; letter-spacing:.14em;">自軍</span>
    {state.our_score} <span class="dim" style="font-size:.6em;">–</span> {state.opp_score}
    <span class="dim" style="font-size:.5em; letter-spacing:.14em;">相手</span></div>
  <div style="margin:.6em 0;">{_base_diamond_svg(state, size=64)}</div>
  <p>{state.outs} OUT　{_term("走者", _TIP["runners"])}: {_e(state.runners_text())}</p>
  <p class="dim">投手 リリーフ（{hand}）</p>
  <p style="margin-top:1.1em;">BATTER #{state.lineup.spot_number()}　<b>{_bname(b)}</b>
     <span class="hand {b.bats}">{_e(b.bats)}</span>　{_e(b.public_line())}
     <span class="tag">{_e(b.type_label())}</span></p>
  <p style="font-weight:800; letter-spacing:.14em; margin-top:1.3em; color:var(--accent);">DEFEND THE LEAD.</p>
  <form method="post" action="/playball" style="margin-top:.8em;">
    <button class="primary big" type="submit">PLAY BALL</button>
  </form>
</div>"""


# ---------------------------------------------------------------------------
# スコアボード帯
# ---------------------------------------------------------------------------
def _pip_row(filled, total, kind):
    return "".join(
        f'<span class="pip {kind}{" on" if i < filled else ""}"></span>' for i in range(total))


# ベースの並び: 2B(上) / 3B(左) / 1B(右) / home(下)。走者インデックスは 0=1B 1=2B 2=3B。
_DIAMOND_POINTS = {0: "right", 1: "top", 2: "left"}


def _base_diamond_svg(state, size=22):
    def base(idx, cx, cy):
        cls = "base"
        if state.runners[idx]:
            cls += " fast" if state.runner_speed_at(idx) >= 70 else " on"
        return (f'<rect class="{cls}" x="{cx-5}" y="{cy-5}" width="10" height="10" '
                f'rx="1.5" transform="rotate(45 {cx} {cy})"/>')
    return (f'<svg class="mini-diamond" width="{size}" height="{size*0.8:.0f}" viewBox="0 0 44 36">'
            f'{base(1,22,9)}{base(2,9,20)}{base(0,35,20)}'
            f'<path d="M22 31 l-4 -4 h8 z" fill="#556" />'
            f'</svg>')


def render_scorebar(state):
    hand = "右" if state.pitcher.throws == "R" else "左"
    our_lead = "lead" if state.our_score > state.opp_score else ""
    opp_lead = "lead" if state.opp_score > state.our_score else ""
    tire = ""
    lvl = state.pitcher.fatigue_level()
    if lvl == 1:
        tire = ' <span style="color:var(--warn);">⚠疲</span>'
    elif lvl == 2:
        tire = ' <span style="color:var(--bad);">⚠⚠バテ</span>'
    steal = ('<span class="warnpill">⚡STEAL</span>'
             if state.lead_runner_fast() and not state.runners[1] else "")
    return f"""
<div class="scorebar"><div class="sb-row">
  <span class="sb-team {our_lead}"><span class="abbr">自軍</span><span class="run">{state.our_score}</span></span>
  <span class="sb-inning">{_e(state.inning_label())}</span>
  <span class="sb-team {opp_lead}"><span class="run">{state.opp_score}</span><span class="abbr">相手</span></span>
  <span class="sb-sep"></span>
  <span class="sb-grp">{_base_diamond_svg(state)} {_e(state.runners_text())} {steal}</span>
  <span class="sb-sep"></span>
  <span class="sb-grp">B {_pip_row(state.balls, 4, 'b')}　S {_pip_row(state.strikes, 3, 's')}
     　OUT {_pip_row(state.outs, 3, 'o')}{_help(_TIP["count"])}</span>
  <span class="sb-sep"></span>
  <span class="sb-grp">投手 <b>{hand}投</b>　球数 <b>{state.pitcher.pitches_thrown}</b>{tire}</span>
</div></div>"""


# ---------------------------------------------------------------------------
# 球場図(守備配置。クリック→クリックでSwap)
# ---------------------------------------------------------------------------
_FIELD_XY = {
    "CF": (50, 16), "LF": (23, 33), "RF": (77, 33),
    "SS": (38, 57), "2B": (62, 57),
    "3B": (25, 78), "1B": (75, 78),
}

# 放送っぽい上からの球場図。芝は CSS グラデ、内野の土・塁・ファウルライン・マウンドを SVG で。
# viewBox は .field の aspect-ratio(1.7)に合わせ、preserveAspectRatio=none で目一杯に伸ばす。
_FIELD_SVG = """
<svg viewBox="0 0 100 59" preserveAspectRatio="none">
  <defs><radialGradient id="dirt" cx="50%" cy="88%" r="72%">
    <stop offset="0%" stop-color="#cd934f"/><stop offset="100%" stop-color="#a2703a"/>
  </radialGradient></defs>
  <path d="M50 55 L90 26 A48 48 0 0 0 10 26 Z" fill="#3f9a58" opacity="0.30"/>
  <polygon points="50,55 24,33 50,11 76,33" fill="url(#dirt)"/>
  <polygon points="50,55 24,33 50,11 76,33" fill="none" stroke="#f5eeda" stroke-width="0.4" opacity="0.55"/>
  <line x1="50" y1="55" x2="4" y2="18" stroke="#f5eeda" stroke-width="0.4" opacity="0.5"/>
  <line x1="50" y1="55" x2="96" y2="18" stroke="#f5eeda" stroke-width="0.4" opacity="0.5"/>
  <circle cx="50" cy="37" r="3.6" fill="#b47a3e" stroke="#f5eeda" stroke-width="0.25"/>
  <g fill="#f8f2e2">
    <rect x="48.6" y="9.6" width="2.8" height="2.8" transform="rotate(45 50 11)"/>
    <rect x="74.6" y="31.6" width="2.8" height="2.8" transform="rotate(45 76 33)"/>
    <rect x="22.6" y="31.6" width="2.8" height="2.8" transform="rotate(45 24 33)"/>
    <path d="M50 53.5 l-2 -2 h4 z"/>
  </g>
</svg>
"""


def _fit_tier(fit):
    return "#3fd0a0" if fit >= 58 else ("#f4b942" if fit >= 46 else "#ff7a7a")


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
<div class="{cls}" style="left:{x}%; top:{y}%; --tier:{_fit_tier(fit)};">
  <form method="post" action="/swap">
    <input type="hidden" name="position" value="{pos}">
    <button type="submit" title="{_e(f.name)} を {pos} で使う">
      <b>{pos}</b>{_e(f.name)}<br><span class="fit">適性 {fit:.0f}{' ▼' if f is weak else ''}</span>
    </button>
  </form>
</div>""")
    cancel = ""
    if session.pending_swap:
        cancel = (f'<p style="margin-top:.5em;"><span class="tag">{session.pending_swap} を選択中 '
                 f'→ 交換先をクリック</span> '
                 '<form method="post" action="/cancel_swap" style="display:inline;">'
                 '<button type="submit">選択解除</button></form></p>')
    align_opts = "".join(
        f'<option value="{k}"{" selected" if k == defense.alignment else ""}>{v}</option>'
        for k, v in ALIGNMENTS.items())
    ede = defense.expected_defensive_efficiency()
    return f"""
<div class="card">
  <h3>Defense — 守備配置</h3>
  <div class="field">
    <div class="align-badge">{_e(defense.describe_alignment())}</div>
    {_FIELD_SVG}{''.join(cells)}
    <div class="homeplate">⌂ HOME · You (C)</div>
  </div>
  <p class="field-legend">数字＝そのポジションでの{_term("適性", _TIP["fit"])}(0-99)。上端の色は適性帯。▼＝総合力が最も低い選手。
    ポジション→交換先の順にクリックで Swap。　{_term("EDE", _TIP["ede"])}(参考値) {ede:.0f}/100</p>
  {cancel}
  <form method="post" action="/alignment" class="form-actions" style="margin-top:.5em;">
    <select name="alignment">{align_opts}</select>
    <button type="submit">守備シフト変更</button>{_help(_TIP["alignment"])}
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
    read = state.current_read()
    read_html = ""
    if read and (read["wait"] or read["weak"]):
        bits = []
        if read["wait"]:
            bits.append("速球待ち" if read["wait"] == "fastball" else "変化球待ち")
        if read["weak"]:
            bits.append(f"弱点={_e(COURSE_SHORT.get(read['weak'], read['weak']))}")
        read_html = f'<div class="myread">あなたの読み: {" / ".join(bits)}</div>'
    return f"""
<div class="card">
  <h3>At Bat — 打者</h3>
  <div class="batter-head">
    <span class="batter-num">{lu.spot_number()}</span>
    <span class="name">{_bname(cur)}</span>
    <span class="hand {cur.bats}">{_e(cur.bats)}</span>
    <span class="tag">{_e(cur.type_label())}</span>
  </div>
  <div class="slash">
    <span>AVG <b>{cur.avg:.3f}</b></span><span>OBP <b>{cur.obp:.3f}</b></span>
    <span>SLG <b>{cur.slg:.3f}</b></span>
    <span>{_term("OPS", _TIP["ops"])} <b>{cur.ops:.3f}</b></span>
  </div>
  <div class="scout">偵察: {_e(cur.scouting_note())}</div>
  {read_html}
  <div class="batter-mini"><span>NEXT · #{lu.batters.index(nxt) + 1} {_bname(nxt)} ({nxt.bats})</span>
    <span>OPS {nxt.ops:.3f}</span></div>
  <div class="batter-mini"><span>ON DECK · #{lu.batters.index(deck) + 1} {_bname(deck)} ({deck.bats})</span>
    <span>OPS {deck.ops:.3f}</span></div>
</div>"""


# ---------------------------------------------------------------------------
# 反応(顔文字)
# ---------------------------------------------------------------------------
def render_reaction(reaction):
    if reaction is None:
        return ('<div class="card"><h3>Batter Reaction — 打者の反応</h3>'
                '<p class="dim">初球を投げるとここに出ます。</p></div>')
    return f"""
<div class="card">
  <h3>Batter Reaction — 打者の反応{_help(_TIP["reaction"])}</h3>
  <div class="reaction rc-{_e(reaction.get('category', 'neutral'))}">
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


_RESULT_KIND = {
    "アウト": "r-out", "空振り": "r-out", "三振": "r-out",
    "犠打成功": "r-out", "バント（封殺）": "r-out", "バント（本封殺）": "r-out",
    "バント失敗（小フライ）": "r-out",
    "単打": "r-hit", "二塁打": "r-hit", "三塁打": "r-hit", "本塁打": "r-hit", "エラー": "r-hit",
    "四球": "r-hit", "バント安打": "r-hit",
    "ボール": "r-ball",
}


def _result_kind(result):
    return _RESULT_KIND.get(result, "r-strike")


_PRAISE = "これが進学校のキャッチャーか！ 感心"
_TAUNT = "あーあ、こんなもんか。進学校のキャッチャーはがっかり"


def _catcher_line(session):
    """よく配球がハマった / 痛打を浴びた ときだけ出す一言（作者が実際に言われたセリフ）。
    プレイ中は decision_quality を出さない方針なので、見えている結果だけで判定する。"""
    o = session.last_outcome
    if not o:
        return ""
    res = o.get("result", "")
    end = o.get("at_bat_end")
    play = o.get("play") if isinstance(o.get("play"), dict) else {}
    runs = play.get("runs", 0)
    label = play.get("label", "")
    # 痛打・失点・押し出し → がっかり
    if res in ("本塁打", "二塁打", "三塁打") or runs >= 1 or end == "walk":
        return f'<p class="catcher-line ng">{_TAUNT}</p>'
    # 三振・フレーミングでストライク奪取・併殺 → 感心
    if end == "strikeout" or o.get("framed") or label == "併殺":
        return f'<p class="catcher-line ok">{_PRAISE}</p>'
    return ""


_GOOD_CALL_LINES = (
    "ナイスリード。打者の狙いを外せてる",
    "いい球。バッテリーの主導権はこっちだ",
    "落ち着いた配球。投手も投げやすそうだ",
    "よく考えられた1球。この読みでいい",
)


def _good_call_line(session):
    """いい配球ができたときの軽いほめ言葉＋ボーナスの可視化（Web の Result カード）。
    大きな見せ場は _catcher_line（感心）が担当するので、そこと重ならない範囲で。"""
    o = session.last_outcome
    if not o or not o.get("good_call"):
        return ""
    n = len(getattr(session.state, "pitch_log", []) or [1])
    line = _GOOD_CALL_LINES[n % len(_GOOD_CALL_LINES)]
    streak = o.get("good_call_streak", 0)
    badge = (f'<span class="gc-badge">◯ いい配球 ×{streak}・ボーナス</span>'
             if streak >= 2 else '<span class="gc-badge">◯ いい配球</span>')
    return f'<p class="good-call-line">{badge} <span class="dim">{line}</span></p>'


def render_pitch_result(session):
    if session.last_call is None:
        return ""
    call = session.last_call
    outcome = session.last_outcome
    zone = _zone_markers(session.last_batter_bats, call["target_course"], outcome["actual_course"])
    if outcome["missed"]:
        miss_note = ' <span class="msg bad" style="font-size:.85em;">失投</span>'
    else:
        miss_note = ' <span class="dim" style="font-size:.85em;">要求どおり</span>'
    framed = (f' <span class="framed-mark">⌂→ {_term("フレーミング", _TIP["framing"])}</span>'
              if outcome.get("framed") else "")
    bb = outcome.get("batted_ball")
    bb_line = f'<p class="callline">打球　<b>{_e(bb.describe())}</b></p>' if bb else ""
    play = outcome.get("play")
    play_line = ""
    if play and (play["label"] != outcome["result"] or play["detail"] or play["runs"]):
        bits = [play["label"]] if play["label"] != outcome["result"] else []
        if play["detail"]:
            bits.append(play["detail"])
        if play["runs"]:
            bits.append(f'{play["runs"]}失点')
        play_line = f'<p class="callline">走塁　<b>{_e(" / ".join(bits))}</b></p>'
    msgs_html = "".join(f'<div class="msg{" bad" if _looks_bad(m) else ""}">{_e(m)}</div>'
                        for m in session.messages)
    return f"""
<div class="card">
  <h3>Result — 直前の1球</h3>
  <div class="zonewrap">
    <div class="zone-shadow"><div class="zone">{zone}</div></div>
    <div style="flex:1; min-width:12em;">
      <p class="callline">{_pitch_arc_svg(call['pitch_type'], size=40)}
         <b>{_e(pitch_name(call['pitch_type']))}</b>
         <span class="dim" style="font-size:.85em;">の球筋（雑）</span></p>
      <p class="callline">CALL　<b>{_e(pitch_name(call['pitch_type']))} / {_e(COURSE_SHORT[call['target_course']])}</b>
         / {_e(PITCH_INTENTS[call['intent']].split('（')[0])}</p>
      <p class="callline">ACTUAL　<b>{_e(pitch_name(call['pitch_type']))} / {_e(COURSE_SHORT[outcome['actual_course']])}</b>{miss_note}
         <span class="dim" style="font-size:.85em;">{_term("出来", _TIP["quality"])} {int(outcome['quality']*100)}</span></p>
      <p class="resultline {_result_kind(outcome['result'])}"><span class="eyebrow" style="margin:0;">RESULT</span> {_e(outcome['result'])}{framed}</p>
      {_catcher_line(session)}
      {_good_call_line(session)}
      {bb_line}{play_line}
    </div>
  </div>
  {render_reaction(session.reaction)}
  {f'<div class="msglist">{msgs_html}</div>' if msgs_html else ''}
</div>"""


_BAD_HINTS = ("三振", "打たれた", "後逸", "エラー", "四球", "振り逃げが成立")


def _looks_bad(msg):
    return any(h in msg for h in _BAD_HINTS)


# ---------------------------------------------------------------------------
# 捕手のリズムゲーム(Web 版。webapp.py で JavaScript を使う唯一の画面)
#
# engine.resolve_pitch_flow() のジェネレータが ("種別", payload) を yield し、
# ここで種別ごとの小さなタイミングゲームを出す。結果(perfect/good/miss の列
# ＋方向)を /qte に POST し、engine 側の採点(qte.bonus_from_verdicts など)を
# 通して gen.send() で返す。JS 無効でもボタン送信で中立(bonus 0)で進める。
# ---------------------------------------------------------------------------
_QTE_SPEC = {
    "frame": {
        "title": "Receive — 受球", "jp": "受球！", "tip": "framing", "btn": "受ける",
        "hint": "流れる線が<b style=\"color:var(--good);\">緑の帯</b>の中央に来た瞬間に "
                "<span class=\"rg-key\">Space</span>（帯をタップでも可）。"
                "ジャストで捕るほど、ボール球でも「ストライク」に見せやすくなる。",
        "labels": ["受ける"], "direction": False,
    },
    "change_signs": {
        "title": "Change Signs — サインを変えろ", "jp": "サインを変えろ！",
        "tip": "reaction", "btn": "サインを変える",
        "hint": "二塁走者にサインを覗かれている。タイミングよく "
                "<span class=\"rg-key\">Space</span> でサインを組み替える。",
        "labels": ["サインを変える"], "direction": False,
    },
    "steal_throw": {
        "title": "Throw Down — 二塁へ送球", "jp": "盗塁！！", "tip": None, "btn": "送球",
        "hint": "走者がスタート！ ① 握り替え → ② 素早く連打 → ③ 二塁送球。",
        "labels": ["① 握り替え", "② 連打！", "③ 二塁へ送球"], "direction": False,
        "mash": [1],
    },
    "d3_throw": {
        "title": "Throw to First — 一塁へ送球", "jp": "一塁へ！", "tip": None, "btn": "送球",
        "hint": "ワンバウンド三振を拾った。① 拾って握る → ② 一塁送球。",
        "labels": ["① 拾って握る", "② 一塁へ送球"], "direction": False,
    },
    "wild_block": {
        "title": "Block — ワンバウンドを止めろ", "jp": "ワンバウンド！", "tip": None, "btn": "止める",
        "hint": "まずボールが逸れた方向へ体を入れる（内 / 中 / 外）。"
                "次に <span class=\"rg-key\">Space</span> で体を落とす。",
        "labels": ["体を落とす"], "direction": True,
    },
    "d3_block": {
        "title": "Block — ワンバウンド三振を止めろ", "jp": "振り逃げ！", "tip": None, "btn": "止める",
        "hint": "方向へ体を入れて（内 / 中 / 外）、<span class=\"rg-key\">Space</span> で止める。",
        "labels": ["体を落とす"], "direction": True,
    },
    "field_bunt": {
        "title": "Field the Bunt — バント処理", "jp": "バント！！", "tip": None, "btn": "処理",
        "hint": "バントだ！ ① 飛び出して掴む（やや早め＝帯は左）→ ② 送球（遅らせて＝帯は右端）。",
        "labels": ["① 飛び出して掴む", "② 送球"], "direction": False,
        "targets": [0.40, 0.82],
    },
    # チュートリアル専用(本編では返球のテンポは配球コール画面に埋め込み)
    "tempo": {
        "title": "Return — 返球のテンポ", "jp": "返球！", "tip": None, "btn": "返球",
        "hint": "流れる線を<b style=\"color:var(--good);\">緑の帯</b>の中央で "
                "<span class=\"rg-key\">Space</span>（帯タップ可）。一定のリズムで 3 回。",
        "labels": ["①", "②", "③"], "direction": False,
    },
}

_QTE_JS = """
(function () {
  var root = document.getElementById('qte'); if (!root) return;
  var nbars = parseInt(root.dataset.bars, 10) || 1;
  var labels = (root.dataset.labels || '').split('|');
  var wantDir = root.dataset.direction === '1';
  var jp = root.dataset.jp || '';
  var targets = (root.dataset.targets || '').split('|').map(function (x) { return parseFloat(x); });
  var mash = {};
  (root.dataset.mash || '').split(',').forEach(function (x) { if (x !== '') mash[parseInt(x, 10)] = 1; });
  var track = document.getElementById('rg-track');
  var marker = document.getElementById('rg-marker');
  var band = document.getElementById('rg-target');
  var vEl = document.getElementById('rg-verdict');
  var labEl = document.getElementById('qte-label');
  var annEl = document.getElementById('qte-announce');
  var dirsEl = document.getElementById('qte-dirs');
  var fV = document.getElementById('f-verdicts');
  var fD = document.getElementById('f-dir');
  var form = document.getElementById('rg-form');
  var IDLE = 0, SWEEP = 1, MASH = 2;
  var verdicts = [], bar = 0, done = false, phase = IDLE, period = 1150, start = 0;
  var mashN = 0, mashEnd = 0;

  function announce(text, ms) {
    if (!annEl) return;
    annEl.textContent = text; annEl.className = 'qte-announce show';
    setTimeout(function () { annEl.className = 'qte-announce'; }, ms || 750);
  }
  function center() { var c = targets[bar]; return (isNaN(c) ? 0.5 : c); }
  function frac(now) { var t = ((now - start) % (period * 2)) / period; return t <= 1 ? t : 2 - t; }
  function place(c, w) { if (band) { band.style.left = (Math.max(0, Math.min(1 - w, c - w / 2)) * 100) + '%'; band.style.width = (w * 100) + '%'; } }
  function tick(now) { if (phase !== SWEEP) return; marker.style.left = (frac(now) * 100) + '%'; requestAnimationFrame(tick); }
  function mashTick(now) {
    if (phase !== MASH) return;
    place(0.5, Math.max(0.03, (mashEnd - now) / 1800));   // 帯が縮む＝残り時間
    requestAnimationFrame(mashTick);
  }
  function recordVerdict(v, txt) {
    verdicts.push(v); phase = IDLE;
    vEl.textContent = txt || (v.toUpperCase() + (v === 'perfect' ? '!' : '')); vEl.className = 'rg-verdict ' + v;
    bar++;
  }
  function beginStep() {
    if (bar >= nbars) return finish();
    vEl.textContent = ''; vEl.className = 'rg-verdict'; phase = IDLE;
    var lbl = labels[bar] || ('タイミング ' + (bar + 1));
    labEl.textContent = lbl;                 // 太字ラベルで「次に何をやるか」を表示
    if (mash[bar]) { announce('連打！！ はじめ', 900); setTimeout(startMash, 950); }
    else { setTimeout(startSweep, bar === 0 ? 120 : 620); }   // 少し間を置いてから流す
  }
  function startSweep() {
    place(center(), 0.18); marker.style.left = '0%';
    start = performance.now(); phase = SWEEP; requestAnimationFrame(tick);
  }
  function startMash() {
    place(0.5, 1.0); mashN = 0; marker.style.left = '0%';
    labEl.textContent = labels[bar] + '  0';
    phase = MASH; mashEnd = performance.now() + 1800;
    requestAnimationFrame(mashTick);
    setTimeout(endMash, 1800);
  }
  function endMash() {
    if (phase !== MASH) return;
    place(0.5, 0.18);
    var v = mashN >= 12 ? 'perfect' : (mashN >= 6 ? 'good' : 'miss');
    recordVerdict(v, mashN + ' 連打 → ' + v.toUpperCase());
    announce('連打 おわり', 700);
    setTimeout(beginStep, 1150);                          // 明確に間を置いてから次へ
  }
  function hit() {
    if (phase === MASH) {
      mashN++; marker.style.left = Math.min(100, mashN / 12 * 100) + '%';
      labEl.textContent = labels[bar] + '  ' + mashN; return;
    }
    if (phase !== SWEEP) return;
    var d = Math.abs(frac(performance.now()) - center());
    recordVerdict(d <= 0.05 ? 'perfect' : (d <= 0.14 ? 'good' : 'miss'));
    setTimeout(beginStep, 600);
  }
  function finish() {
    if (done) return; done = true;
    while (verdicts.length < nbars) verdicts.push('miss');
    fV.value = verdicts.join(',');
    setTimeout(function () { form.submit(); }, 450);
  }
  document.addEventListener('keydown', function (e) {
    if (e.code === 'Space' || e.code === 'Enter') { e.preventDefault(); hit(); }
  });
  track.addEventListener('click', hit);

  // まず大きく「盗塁！！」等を出してから始める
  if (jp) announce(jp, 1000);
  setTimeout(function () {
    if (wantDir && dirsEl) {
      dirsEl.hidden = false;
      announce('方向を選べ', 700);
      Array.prototype.forEach.call(dirsEl.querySelectorAll('button'), function (b) {
        b.addEventListener('click', function () { fD.value = b.dataset.dir; dirsEl.hidden = true; beginStep(); });
      });
    } else { beginStep(); }
  }, jp ? 1050 : 0);
  setTimeout(finish, 15000);
})();
"""


def _rhythm_widget(spec, action, call_html="", extra_hidden="", header_extra="", targets=None):
    """QTE / チュートリアル共通のタイミングバー・ウィジェット。
    targets: 各ビートの帯の中心(0..1)。省略時は spec["targets"] か全て中央(0.5)。"""
    tip = _help(_TIP[spec["tip"]]) if spec.get("tip") else ""
    tgt = targets if targets is not None else spec.get("targets", [])
    mash = spec.get("mash", [])
    dir_html = ""
    if spec["direction"]:
        dir_html = ('<div id="qte-dirs" class="qte-dirs" hidden>'
                    '<button type="button" data-dir="in">◀ 内</button>'
                    '<button type="button" data-dir="mid">● 中</button>'
                    '<button type="button" data-dir="out">外 ▶</button></div>')
    return f"""
<div class="card rg-wrap">
  <h3>{_e(spec["title"])}{tip}</h3>
  {header_extra}
  {call_html}
  <p class="rg-hint">{spec["hint"]}</p>
  <div id="qte" data-bars="{len(spec['labels'])}" data-direction="{1 if spec['direction'] else 0}"
       data-jp="{_e(spec.get('jp', ''))}"
       data-labels="{_e('|'.join(spec['labels']))}"
       data-targets="{_e('|'.join(str(t) for t in tgt))}"
       data-mash="{_e(','.join(str(m) for m in mash))}">
    <div id="qte-announce" class="qte-announce"></div>
    {dir_html}
    <div id="qte-label" class="qte-label"></div>
    <div id="rg-track" class="rg-track" tabindex="0" aria-label="タイミングを合わせて Space を押す">
      <div class="rg-target" id="rg-target"><div class="core"></div></div>
      <div id="rg-marker" class="rg-marker"></div>
    </div>
    <div id="rg-verdict" class="rg-verdict"></div>
  </div>
  <form id="rg-form" method="post" action="{action}">
    <input type="hidden" name="verdicts" id="f-verdicts" value="">
    <input type="hidden" name="dir" id="f-dir" value="">
    {extra_hidden}
    <noscript><p class="rg-hint">JavaScript が無効です。「{_e(spec["btn"])}」でそのまま先へ進みます。</p></noscript>
    <button type="submit" class="primary big">{_e(spec["btn"])}</button>
  </form>
</div>
<script>{_QTE_JS}</script>"""


def _prev_result_strip(outcome):
    """直前の1球の結果を緑(よし)/赤(まずい)で簡単に。"""
    if not outcome:
        return ""
    k = _result_kind(outcome.get("result", ""))
    if k in ("r-out", "r-strike"):
        col, mk = "var(--good)", "◯ よし"
    elif k in ("r-hit", "r-ball"):
        col, mk = "var(--bad)", "✕ まずい"
    else:
        col, mk = "var(--muted)", "—"
    return (f'<p class="prev-strip" style="color:{col};">前の1球　'
            f'<b>{_e(outcome.get("result", ""))}</b>　{mk}</p>')


def render_qte(session):
    kind, payload = session.pending_qte
    spec = _QTE_SPEC[kind]
    call = payload.get("call")
    call_html = f'<p class="rg-call"><b>{_e(call)}</b></p>' if call else ""
    targets = None
    if kind == "frame":
        v = payload.get("velocity", 90)
        # 速い球は早く(帯を左)・遅い球は遅く(帯を右)受ける
        targets = [0.24 if v >= 92 else (0.74 if v <= 84 else 0.5)]
    return _rhythm_widget(spec, "/qte", call_html=call_html, targets=targets,
                          header_extra=_prev_result_strip(session.last_outcome))


def _tutorial_complete(session):
    return session.tutorial_skipped or all(session.tutorial.values())


def _tutorial_ok(item, form):
    """練習の 1 回が「クリア」か。全ビートが GOOD 以上ならクリア。"""
    vs = [v for v in (form.get("verdicts", "") or "").split(",") if v in ("perfect", "good", "miss")]
    if not vs:
        return False
    if item in _TUT_DIR and form.get("dir"):
        vs = [qte.direction_verdict(form.get("dir"), _TUT_DIR[item])] + vs
    return "miss" not in vs


def render_tutorial(session):
    todo = [k for k, _, _ in _TUTORIAL_ITEMS if not session.tutorial[k]]
    done_n = len(_TUTORIAL_ITEMS) - len(todo)
    names = {k: n for k, n, _ in _TUTORIAL_ITEMS}
    rows = "".join(
        f'<li class="{"done" if session.tutorial[k] else ("now" if todo and k == todo[0] else "")}">'
        f'{"✓" if session.tutorial[k] else ("▶" if todo and k == todo[0] else "・")} {_e(name)}'
        f'<span class="dim"> — {_e(desc)}</span></li>'
        for k, name, desc in _TUTORIAL_ITEMS)
    skip = ('<form method="post" action="/skiptutorial" style="margin-top:.8em;">'
            '<button type="submit">スキップして本編を始める</button></form>')
    checklist = (
        f'<div class="card"><h3>操作の練習 <span class="dim">（{done_n} / '
        f'{len(_TUTORIAL_ITEMS)} クリア）</span></h3>'
        '<p class="dim" style="font-size:.88em; margin-bottom:.4em;">'
        '6 つのリズムを各 1 回 <b>GOOD 以上</b>で。MISS が混じったら同じ項目をもう一度。</p>'
        f'<ul class="tut-list">{rows}</ul>{skip}</div>')
    if not todo:
        return checklist
    item = todo[0]
    spec = _QTE_SPEC[item]
    hdr = f'<p class="rg-call">練習　<b>{_e(names[item])}</b></p>'
    if item in _TUT_DIR:
        hdr += '<p class="rg-hint">練習ではボールは <b>中</b> に来ます。</p>'
    widget = _rhythm_widget(spec, "/tutorial",
                            extra_hidden=f'<input type="hidden" name="item" value="{item}">',
                            header_extra=hdr)
    # リズムのウィジェットを先頭に（送信のたび先頭に戻るので、常に見える所に置く）
    return widget + checklist


def _qte_answer(kind, payload, form):
    """提出された verdict 列（＋方向）を、engine が期待する答えに変換する。"""
    verdicts = [v for v in (form.get("verdicts", "") or "").split(",")
                if v in ("perfect", "good", "miss")]
    if kind == "frame":
        return verdicts[0] if verdicts else "good"
    parts = list(verdicts)
    if kind in ("wild_block", "d3_block") and form.get("dir"):
        parts = [qte.direction_verdict(form.get("dir"), payload.get("dir"))] + parts
    return qte.bonus_from_verdicts(*parts) if parts else 0.0


def _tempo_bonus(form):
    """配球コールと一緒に送られてくる返球リズム(3拍)を tempo(-0.25〜0.25)に。"""
    vs = [v for v in (form.get("tempo", "") or "").split(",") if v in ("perfect", "good", "miss")]
    return qte.bonus_from_verdicts(*vs) if vs else 0.0


# 配球コール画面に埋め込む「返球のテンポ」ウィジェット。直前の球を捕球していたときだけ出す。
_TEMPO_JS = """
(function () {
  var box = document.getElementById('tempo'); if (!box) return;
  var track = document.getElementById('tempo-track');
  var marker = document.getElementById('tempo-marker');
  var vEl = document.getElementById('tempo-verdict');
  var labEl = document.getElementById('tempo-label');
  var fld = document.getElementById('f-tempo');
  var msg = document.getElementById('tempo-msg');
  var nbars = 3, bar = 0, vs = [], done = false, running = false, started = false;
  var period = 1050, start = 0;
  function frac(now) { var t = ((now - start) % (period * 2)) / period; return t <= 1 ? t : 2 - t; }
  function tick(now) { if (!running) return; marker.style.left = (frac(now) * 100) + '%'; requestAnimationFrame(tick); }
  function startBar() {
    if (bar >= nbars) return finish();
    labEl.textContent = '(' + (bar + 1) + ' / ' + nbars + ')';
    vEl.textContent = ''; vEl.className = 'rg-verdict';
    start = performance.now(); running = true; requestAnimationFrame(tick);
  }
  function hit() {
    if (!running || done) return; running = false;
    var d = Math.abs(frac(performance.now()) - 0.5) / 0.5;
    var v = d <= 0.06 ? 'perfect' : (d <= 0.18 ? 'good' : 'miss');
    vs.push(v); vEl.textContent = v.toUpperCase() + (v === 'perfect' ? '!' : ''); vEl.className = 'rg-verdict ' + v;
    bar++; setTimeout(startBar, 430);
  }
  function finish() {
    if (done) return; done = true; fld.value = vs.join(','); labEl.textContent = '';
    var p = vs.filter(function (x) { return x === 'perfect'; }).length, m = vs.indexOf('miss') >= 0;
    msg.textContent = p >= 2 ? 'テンポ◎ 投手が乗ってきた' : (m ? 'テンポが乱れた' : 'まずまずのテンポ');
    msg.style.color = p >= 2 ? 'var(--good)' : (m ? 'var(--bad)' : 'var(--muted)');
  }
  document.addEventListener('keydown', function (e) {
    if (e.code === 'Space' && !done) { e.preventDefault(); hit(); }
  });
  track.addEventListener('click', hit);
  function begin() { if (started) return; started = true; setTimeout(startBar, 700); }
  if ('IntersectionObserver' in window) {
    var io = new IntersectionObserver(function (es) {
      es.forEach(function (en) { if (en.isIntersecting) { begin(); io.disconnect(); } });
    }, { threshold: 0.6 });
    io.observe(box);
  } else { begin(); }
})();
"""


def _tempo_widget(state, reaction=None, prev_outcome=None):
    """(ウィジェット HTML, スクリプト HTML)。返球するものが無ければ両方空文字。
    他のリズムゲームと同じ「流れる線を緑の帯の中央で止める」形式を 3 回。"""
    last = state.history.last()
    if last is None or last.get("result") not in ("ボール", "ストライク", "空振り"):
        return "", ""
    rx = ""
    if reaction:
        rx = (f'<span class="tempo-react">{_e(reaction.get("face", ""))}'
              f' <span class="dim">「{_e(reaction.get("text", ""))}」</span></span>')
    html = (
        '<div id="tempo" class="tempo-box">'
        '<div class="tempo-head">'
        '<span class="eyebrow" style="margin:0;">Return — 返球のテンポ</span>'
        f'{rx}</div>'
        f'{_prev_result_strip(prev_outcome)}'
        '<p class="dim" style="font-size:.82em; margin:.15em 0 .5em;">'
        '流れる線を<b style="color:var(--good);">緑の帯</b>の中央で '
        '<span class="rg-key">Space</span>（帯タップ可）。これを3回、一定のテンポで。'
        ' <span id="tempo-label"></span></p>'
        '<div id="tempo-track" class="rg-track" tabindex="0" aria-label="タイミングを合わせて Space">'
        '<div class="rg-target"><div class="core"></div></div>'
        '<div id="tempo-marker" class="rg-marker"></div></div>'
        '<div id="tempo-verdict" class="rg-verdict" style="min-height:1.2em; font-size:1.1em;"></div>'
        '<p id="tempo-msg" class="rg-hint" style="margin:.15em 0 0; min-height:1.1em;"></p>'
        '<input type="hidden" name="tempo" id="f-tempo" value="">'
        '</div>')
    return html, f"<script>{_TEMPO_JS}</script>"


# ---------------------------------------------------------------------------
# 配球コール(球種・コース・意図)
# ---------------------------------------------------------------------------
def render_pitch_call_form(state, reaction=None, prev_outcome=None):
    pitch_opts = "".join(
        f'<input type="radio" name="pitch_type" value="{_e(k)}" id="pt_{k}"'
        f'{" checked" if i == 0 else ""}>'
        f'<label for="pt_{k}">{_pitch_arc_svg(k)}{_e(v)}<span class="v">{velocity_of(k)}</span></label>'
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
    side_hint = "内角 ◀ ▶ 外角" if state.batter.bats == "R" else "外角 ◀ ▶ 内角"
    tempo_html, tempo_js = _tempo_widget(state, reaction, prev_outcome)
    return f"""
<div class="card">
  <h3>Pitch Call — 配球コール</h3>
  <form method="post" action="/pitch">
    {tempo_html}
    <p class="eyebrow">Pitch Type{_help(_TIP["pitch_type"])}
       <span class="dim" style="font-weight:400;">（数字は球速）</span></p>
    <div class="chiprow">{pitch_opts}</div>
    <div class="callgrid">
      <div class="callgrid-zone">
        <p class="eyebrow">Target Location{_help(_TIP["course"])}
           <span class="dim" style="font-weight:400;">（{_e(side_hint)} · 打者 {state.batter.bats} · 捕手視点）</span></p>
        <div class="zone-shadow"><div class="zone">{course_cells}</div></div>
      </div>
      <div class="callgrid-intent">
        <p class="eyebrow">Pitch Intent{_help(_TIP["intent"])}</p>
        <div class="chiprow">{intent_opts}</div>
        <p style="margin:1.1em 0 0;"><button class="primary big" type="submit">CALL PITCH</button></p>
        <p class="hint" style="margin:.8em 0 0;">
          チェイス・見せ球はボールになりやすく、ピッチアウトはほぼ確実にボール。
          カウントと走者を見て選ぶ。</p>
      </div>
    </div>
  </form>
</div>{tempo_js}"""


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
    if not state.spray:
        chart = ('<p class="hint" style="margin:.2em 0 0;">'
                 'フェアの打球が出るとここに記録され、球場のどこに飛んだかが一目で分かります。</p>')
    else:
        chart = (f'<p class="dim" style="margin:.2em 0 .4em;">'
                 f'· アウト　o 単打　e エラー　O 長打　H 本塁打</p>'
                 f'<pre class="spray">{_e(text)}</pre>')
    return (f'<div class="card"><h3>Spray — 打球チャート（このゲーム）</h3>{chart}</div>')


# ---------------------------------------------------------------------------
# 配球履歴
# ---------------------------------------------------------------------------
_RESULT_CSS = {
    "ストライク": "res-strike", "空振り": "res-strike", "ファウル": "res-strike",
    "ボール": "res-ball",
    "アウト": "res-out", "三振": "res-out", "犠打成功": "res-out",
    "バント（封殺）": "res-out", "バント（本封殺）": "res-out", "バント失敗（小フライ）": "res-out",
    "単打": "res-hit", "二塁打": "res-hit", "三塁打": "res-hit", "本塁打": "res-hit",
    "エラー": "res-hit", "四球": "res-hit", "バント安打": "res-hit",
}


def render_pitch_history(state, limit=8):
    entries = state.pitch_log[-limit:][::-1]
    if not entries:
        rows = '<tr><td colspan="5" class="dim">まだ投球なし</td></tr>'
    else:
        rows = "".join(
            f"<tr><td class=\"dim\">{e['pitch_number']}</td>"
            f"<td>{_e(pitch_name(e['pitch_type']))}</td>"
            f"<td>{_e(COURSE_SHORT[e['actual_course']])}</td>"
            f"<td>{_e(PITCH_INTENTS[e['intent']].split('（')[0])}</td>"
            f"<td class=\"{_RESULT_CSS.get(e['result'], '')}\"><span class=\"rdot\"></span>{_e(e['result'])}"
            f"{'<span class=\"framed-mark\"> ⌂→</span>' if e.get('framed') else ''}</td></tr>"
            for e in entries)
    return f"""
<div class="card">
  <h3>Pitch History — 配球履歴（直近{limit}球・新しい順）</h3>
  <div class="tablescroll"><table class="history">
    <tr><th>#</th><th>球種</th><th>コース</th><th>意図</th><th>結果</th></tr>
    {rows}
  </table></div>
</div>"""


# ---------------------------------------------------------------------------
# サイドアクション(敬遠・継投・捕手メモ・中断)
# ---------------------------------------------------------------------------
def render_side_actions(state, difficulty):
    extras = ""
    if not all(state.runners):          # 満塁でなければ敬遠できる(押し出しになる満塁だけ除外)
        extras += '<form method="post" action="/walk" style="display:inline">'\
                 '<button>敬遠</button></form> '
    if state.can_change_pitcher():
        if len(state.bullpen) == 1:
            extras += ('<form method="post" action="/change" style="display:inline">'
                      '<input type="hidden" name="index" value="0">'
                      f'<button>継投（{_e(state.bullpen[0].name)}）</button></form> ')
        else:
            pitcher_opts = "".join(
                f'<option value="{i}">{_e(p.name)}（{"右" if p.throws == "R" else "左"}投）</option>'
                for i, p in enumerate(state.bullpen))
            extras += ('<form method="post" action="/change" style="display:inline">'
                      f'<select name="index">{pitcher_opts}</select>'
                      '<button>継投</button></form> ')
    course_opts = "".join(f'<option value="{c}">{_e(v)}</option>' for c, v in COURSE_SHORT.items())
    hint = ""
    if difficulty == "beginner":
        hint = ('<div class="hint"><b>ヒント</b> 顔文字は本音とは限りません（60%本音 / 15%逆 / 15%ぼかし）。'
               '1球だけで信じすぎないこと。</div>')
    return f"""
{hint}
<div class="card">
  <h3>Catcher's Notes — 捕手メモ（読みを記録 · 試合後に採点）</h3>
  <form method="post" action="/memo" class="form-actions">
    <select name="wait"><option value="unknown">待ち球: わからない</option>
      <option value="fastball">速球待ち</option><option value="offspeed">変化球待ち</option></select>
    <select name="weak"><option value="">弱点コース: わからない</option>{course_opts}</select>
    <button type="submit">メモする</button>
  </form>
</div>
<div class="card">
  <h3>Bench — その他の操作</h3>
  <div class="form-actions">
    {extras}
    <form method="post" action="/newgame" style="display:inline;">
      <button>中断して新規開始</button></form>
  </div>
</div>"""


# ---------------------------------------------------------------------------
# メイン試合画面
# ---------------------------------------------------------------------------
def render_game_page(session):
    state = session.state
    tactic_msg = begin_at_bat(state, session.rng)
    body = [render_scorebar(state)]
    if tactic_msg:
        body.append(f'<div class="tactic-alert">⚠ <b>{_e(tactic_msg)}</b>'
                    f'　― ピッチアウトや高め速球で対抗できる</div>')
    # 配球コールを一番上に（スクロールせず「返球のテンポ＋次の1球」に手が届くように）。
    # その下に読む材料: 守備配置 ｜ 打者＋直前の1球 → 配球履歴 → 打球チャート → ベンチ。
    body.append(render_pitch_call_form(state, session.reaction, session.last_outcome))
    body.append('<div class="grid2 gcols">')
    body.append('<div>' + render_field_diagram(session) + '</div>')
    body.append('<div>' + render_batter_card(state)
               + (render_pitch_result(session) if session.last_call else render_reaction(None))
               + '</div>')
    body.append('</div>')
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

    def fmt(items, limit=5, cls="calllist"):
        if not items:
            return '<p class="dim">なし</p>'
        rows = "".join(
            f'<p><span class="num">{n}球目</span>　{_e(label)}'
            + (f' → {_e(item[2])}' if len(item) > 2 else '') + '</p>'
            for item in items[:limit] for n, label in [(item[0], item[1])])
        more = f'<p class="dim">…ほか{len(items) - limit}件</p>' if len(items) > limit else ""
        return f'<div class="{cls}">{rows}{more}</div>'

    reads_html = ""
    if reads["rows"]:
        reads_html = (f'<div class="card"><h3>捕手メモの採点</h3>'
                     f'<p><b>{reads["correct"]} / {reads["graded"]}</b> 当たり</p>'
                     + "".join(f'<p class="dim" style="font-size:.85em;">{_e(r.strip())}</p>'
                               for r in reads["rows"])
                     + '</div>')

    pe = report["pitch_execution"]
    de = report["defense"]
    runc = "var(--good)" if report["runs_allowed"] == 0 else "var(--bad)"
    dq_bad = "bad" if "甘い" in a["decision_quality_label"] else ""
    dq_pct = {"良い（狙いを外し、崩せていた）": 92, "やや良い": 68,
              "やや甘い": 40, "甘い（読まれ・置きにいく球が多かった）": 18}.get(
                  a["decision_quality_label"], 50)
    q_pct = int(pe["avg_quality"] * 100)
    m_pct = int(pe["missed_rate"] * 100)

    return f"""
<div class="card report-hero">
  <h2 style="letter-spacing:.14em; color:var(--muted); font-size:.8em;">CATCHER REPORT</h2>
  <p class="headline" style="color:{runc};">{_e(run_txt)} ／ {_e(status)}</p>
  <p class="score">自軍 <b>{state.our_score}</b> – <b>{state.opp_score}</b> 相手
     <span class="dim">（開始 {state.start_our}–{state.start_opp}）</span></p>
</div>

<div class="card"><h3>Line — 内容</h3><div class="report-stats">{stats_html}</div></div>

<div class="grid2">
  <div class="card"><h3>Decision Quality — 配球判断</h3>
    <div class="kv"><span>平均</span><b>{_e(a['decision_quality_label'])}</b></div>
    <div class="meter {dq_bad}"><span style="width:{dq_pct}%;"></span></div>
    <p class="dim" style="font-size:.82em; margin:.5em 0 .7em;">結果と判断は別もの。下は「結果 ≠ 判断」だった球。</p>
    <div class="split2">
      <div class="box unlucky"><div class="cap">GOOD CALL / 悪結果（不運）</div>{fmt(a['unlucky'], 3)}</div>
      <div class="box lucky"><div class="cap">BAD CALL / 好結果（幸運）</div>{fmt(a['lucky'], 3)}</div>
    </div>
  </div>
  <div>
    <div class="card"><h3>Pitch Execution — 投手の実行力</h3>
      <div class="kv"><span>失投率</span><b>{pe['missed_pitches']} / {report['pitches']}（{m_pct}%）</b></div>
      <div class="meter bad"><span style="width:{m_pct}%;"></span></div>
      <div class="kv" style="margin-top:.5em;"><span>平均 quality</span><b>{pe['avg_quality']:.2f}</b></div>
      <div class="meter"><span style="width:{q_pct}%;"></span></div>
      <p class="kv" style="margin-top:.5em;"><span>フレーミングで奪ったストライク</span>
         <b style="color:var(--accent);">{report['framed_strikes']} 球</b></p>
    </div>
    <div class="card"><h3>Defense — 守備</h3>
      <p class="kv"><span>シフトが噛み合った場面</span><b>{de['alignment_helped']} 球</b></p>
      <p class="kv"><span>エラー</span><b>{de['errors']} 件</b></p>
      <p class="kv"><span>最終 EDE（参考値）</span><b>{de['final_ede']} / 100</b></p>
      <div class="meter"><span style="width:{de['final_ede']:.0f}%;"></span></div>
    </div>
  </div>
</div>

<div class="grid2">
  <div class="card"><h3>◎ 良かった配球</h3>{fmt(a['good_calls'])}</div>
  <div class="card"><h3>△ 危険だった配球</h3>{fmt(a['risky_calls'])}</div>
</div>
<div class="card"><h3>◇ 浴びた長打</h3>{fmt(a['extra_base'])}</div>
{reads_html}
<div class="card dim" style="font-size:.85em;">{'ログ: ' + _e(log_path) if log_path else ''}
  <p style="margin-top:.4em;">※プレイ中に正解は表示していません。これは3アウト後だけの振り返りです。</p></div>
<form method="post" action="/newgame"><button class="primary big" type="submit">もう一度プレイ</button></form>"""


def render_page(session):
    if not session.started:
        return render_start_screen(session)
    if not _tutorial_complete(session):
        return render_tutorial(session)
    if session.showing_intro:
        return render_situation_intro(session.state)
    state = session.state
    session.finish_if_over()
    if state.is_over():
        return render_postgame_report(state, session.log_path)
    if session.pending_qte is not None:
        return render_scorebar(state) + render_qte(session)
    return render_game_page(session)


def _advance_pitch_gen(answer):
    """進行中の resolve_pitch_flow ジェネレータを 1 手進める。
    次の QTE が来たら pending_qte に積む。終わったら結果を SESSION へ。"""
    gen = SESSION.pitch_gen
    if gen is None:
        return
    try:
        SESSION.pending_qte = gen.send(answer)          # (kind, payload)
    except StopIteration as stop:
        SESSION.pitch_gen = None
        SESSION.pending_qte = None
        outcome, messages = stop.value
        SESSION.messages = messages
        SESSION.reaction = outcome.get("reaction")
        # 打席の締め(三振/四球/安打/アウト)は engine が pitch_log の最後の 1 件に
        # 付けるので、直前の 1 球の結果を見る _catcher_line 用に outcome へも移す。
        if SESSION.state is not None and SESSION.state.pitch_log:
            outcome.setdefault("at_bat_end",
                               SESSION.state.pitch_log[-1].get("at_bat_end"))
        SESSION.last_outcome = outcome


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

        if self.path == "/tutorial":
            item = form.get("item")
            if item in SESSION.tutorial and _tutorial_ok(item, form):
                SESSION.tutorial[item] = True
            self._redirect()
            return

        if self.path == "/skiptutorial":
            SESSION.tutorial_skipped = True
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

        # リズムゲーム進行中は /qte と新規開始以外を受け付けない(操作の取り違え防止)
        if SESSION.pending_qte is not None and self.path not in ("/qte", "/newgame", "/start"):
            self._redirect()
            return

        if self.path == "/pitch":
            pitch_type = form.get("pitch_type")
            course = form.get("course", "mid_mid")
            intent = form.get("intent", "strike")
            # フォーム以外(手打ちの POST 等)で不正な値が来ても 500 にせず弾く
            if (pitch_type not in repertoire_options(state.pitcher)
                    or course not in COURSE_SHORT or intent not in PITCH_INTENTS):
                self._redirect()
                return
            SESSION.last_batter_bats = state.batter.bats
            SESSION.last_call = {"pitch_type": pitch_type, "target_course": course, "intent": intent}
            # 1 球の判定を engine のジェネレータで開始。QTE が要れば pending_qte に積まれる。
            SESSION.pitch_gen = resolve_pitch_flow(state, pitch_type, course, intent, SESSION.rng,
                                                   tempo=_tempo_bonus(form))
            _advance_pitch_gen(None)
            self._redirect()
            return

        if self.path == "/qte":
            if SESSION.pending_qte is None:
                self._redirect()
                return
            kind, payload = SESSION.pending_qte
            _advance_pitch_gen(_qte_answer(kind, payload, form))
            self._redirect()
            return

        if self.path == "/walk":
            state.intentional_walk()
            SESSION.messages, SESSION.reaction, SESSION.last_call = [">>> 申告敬遠。打者を歩かせた。"], None, None
            self._redirect()
            return

        if self.path == "/change":
            SESSION.reaction, SESSION.last_call = None, None
            try:
                index = int(form.get("index", "0"))
            except ValueError:
                index = 0
            new_pitcher = state.change_pitcher(index)
            if new_pitcher:
                SESSION.messages = [f">>> 継投。マウンドに {new_pitcher.name}。"
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
    p.add_argument("--scenario", choices=_SCENARIOS,
                   help="動作確認用に開始場面を仕込む: steal(盗塁の送球) / block(ブロッキング) / "
                       "dropped3(振り逃げ) / bunt(バント処理) / signs(サイン交換)")
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
    SESSION = GameSession(lineup_batters=batters, seed=args.seed, difficulty=args.difficulty,
                          scenario=args.scenario)
    if args.scenario:
        print(f"シナリオ「{args.scenario}」で開始します（開始場面を仕込みます）")

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
