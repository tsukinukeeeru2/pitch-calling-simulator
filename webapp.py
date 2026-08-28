"""ブラウザで遊べる軽量 Web UI(標準ライブラリのみ、追加インストール不要)。

    python webapp.py                 # http://127.0.0.1:8765 で起動(自分の PC からだけ)
    python webapp.py --port 9000
    python webapp.py --lineup lineups/sample_team.json
    python webapp.py --host 0.0.0.0  # 同じ Wi-Fi の他端末(スマホ等)からもアクセス可能に

--host 0.0.0.0 は同じネットワーク上の誰でもアクセスできてしまうので、
信頼できるネットワーク(自宅 Wi-Fi 等)でのみ使うこと。

main.py と同じく「進行役」に徹する。ゲームロジック(judge.py 以下)には
一切触れない ―― main.resolve_one_pitch() をそのまま呼び出し、結果を
ANSI ではなく HTML で組み立てるだけ。表示層を差し替えれば GUI 化できる、
という元々の設計方針どおり(README/HANDOFF 参照)。

QTE(リズムミニゲーム)は HTTP のリクエスト/レスポンスでは体で覚える系の
入力ができないため、常に無効(確率モデルに一任)にしている。
"""

import argparse
import html
import http.server
import os
import random
import socketserver
import urllib.parse

# QTE はブラウザの1リクエストでは成立しないので、qte.qte_enabled() を強制 OFF にする。
# main 以下のインポートより前に設定すること。
os.environ["PITCHSIM_QTE"] = "0"

from constants import COURSE_SHORT
from defense import ALIGNMENTS
from lineup import load_lineup_file
from main import maybe_resolve_sign_steal, resolve_one_pitch, save_game_log
from match_state import generate_random_situation
from pitch_data import repertoire_options
from strategy import PITCH_INTENTS, build_analysis, grade_reads

_COURSE_GRID = ["in_hi", "mid_hi", "out_hi",
               "in_mid", "mid_mid", "out_mid",
               "in_lo", "mid_lo", "out_lo"]


class GameSession:
    """1 プロセス = 1 ゲーム(ローカルで自分ひとりが遊ぶ想定。同時アクセスは考慮しない)。"""

    def __init__(self, lineup_batters=None):
        self.lineup_batters = lineup_batters
        self.state = None
        self.messages = []
        self.reaction = None
        self.log_path = None
        self.new_game()

    def new_game(self):
        self.state = generate_random_situation(lineup_batters=self.lineup_batters)
        self.messages = []
        self.reaction = None
        self.log_path = None

    def finish_if_over(self):
        if self.state.is_over() and self.log_path is None:
            try:
                self.log_path = save_game_log(self.state)
            except OSError:
                self.log_path = ""


SESSION = GameSession()


def _e(text):
    return html.escape(str(text))


# ---------------------------------------------------------------------------
# HTML の組み立て(ui.py の ANSI 版と同じ情報を、HTML で出すだけ)
# ---------------------------------------------------------------------------
_PAGE = """<!doctype html>
<html lang="ja"><head><meta charset="utf-8">
<title>配球判断シミュレーター</title>
<style>
 body {{ font-family: system-ui, sans-serif; max-width: 720px; margin: 1.5em auto; background:#f4f1ea; }}
 h1 {{ font-size: 1.2em; }}
 .card {{ background:#fff; border:1px solid #ccc; border-radius:8px; padding:1em; margin-bottom:1em; }}
 .row {{ display:flex; gap:.6em; flex-wrap:wrap; align-items:center; }}
 table {{ border-collapse: collapse; }}
 td, th {{ padding: 2px 6px; text-align:center; }}
 .grid3 {{ display:grid; grid-template-columns:repeat(3,3em); gap:4px; }}
 .grid3 label {{ display:block; text-align:center; border:1px solid #999; border-radius:4px; padding:.3em 0; cursor:pointer; }}
 .grid3 input {{ display:none; }}
 .grid3 input:checked + label {{ background:#3a6; color:#fff; }}
 .msg {{ color:#a33; }}
 .good {{ color:#2a7; }} .bad {{ color:#a33; }} .warn {{ color:#a70; }}
 button {{ padding:.4em .8em; cursor:pointer; }}
 .spray {{ font-family: monospace; white-space: pre; background:#eef7ee; padding:.6em; }}
 .dim {{ color:#888; font-size:.9em; }}
</style></head><body>
<h1>終盤の守り切りゲーム（Web版）</h1>
{body}
</body></html>"""


def _base_html(state):
    b = state.batter
    hand = "右投" if state.pitcher.throws == "R" else "左投"
    runners = ["1B", "2B", "3B"]
    on_base = "・".join(r for r, on in zip(runners, state.runners) if on) or "なし"
    return f"""
<div class="card">
  <div class="row">
    <b>{_e(state.inning_label())}</b>
    <span>自軍 {state.our_score} - {state.opp_score} 相手</span>
    <span>{state.outs} OUT</span>
    <span>走者: {_e(on_base)}</span>
  </div>
  <div class="row dim">
    <span>投手: リリーフ（{hand}） 球数 {state.pitcher.pitches_thrown}</span>
    <span>カウント: B{state.balls} S{state.strikes}</span>
  </div>
  <div class="row">
    <b>#{state.lineup.spot_number()} {_e(b.name)}</b>
    <span>{_e(b.public_line())}</span>
    <span>{_e(b.type_label())}</span>
  </div>
  <div class="dim">偵察メモ: {_e(b.scouting_note())}</div>
</div>"""


_BAD_MESSAGE_HINTS = ("三振", "打たれた", "後逸", "エラー", "四球", "振り逃げが成立")


def _messages_html(messages, reaction=None):
    if not messages and reaction is None:
        return ""
    lines = []
    if reaction is not None:
        lines.append(f"<div>{_e(reaction['face'])} {_e(reaction['text'])}</div>")
    for m in messages:
        cls = "bad" if any(hint in m for hint in _BAD_MESSAGE_HINTS) else ""
        lines.append(f"<div class=\"msg {cls}\">{_e(m)}</div>")
    return f'<div class="card">{"".join(lines)}</div>'


def _spray_html(state):
    """打球チャートを簡易テキストグリッドで(ui._spray_cell と同じ考え方)。"""
    w, h = 21, 5
    grid = [[" "] * w for _ in range(h)]
    glyphs = {"アウト": "·", "単打": "o", "エラー": "e",
             "二塁打": "O", "三塁打": "O", "本塁打": "H"}
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
        g = glyphs.get(entry["result"], "?")
        for dc in range(2):
            c = col + dc
            if 0 <= row < h and 0 <= c < w and grid[row][c] == " ":
                grid[row][c] = g
                break
    grid[h - 1][w // 2] = "⌂"
    text = "\n".join("".join(r) for r in grid)
    return (f'<div class="card"><div class="dim">打球チャート　'
           f'· アウト  o 単打  e エラー  O 長打  H 本塁打</div>'
           f'<div class="spray">{_e(text)}</div></div>')


def _pitch_form_html(state):
    pitch_opts = "".join(
        f'<option value="{_e(k)}">{_e(v)}</option>'
        for k, v in repertoire_options(state.pitcher).items())
    intent_opts = "".join(
        f'<option value="{_e(k)}">{_e(v)}</option>' for k, v in PITCH_INTENTS.items())
    course_cells = "".join(
        f'<input type="radio" name="course" value="{c}" id="c_{c}"'
        f'{" checked" if c == "mid_mid" else ""}>'
        f'<label for="c_{c}">{_e(COURSE_SHORT[c])}</label>'
        for c in _COURSE_GRID)

    extras = ""
    if state.runners[0] and not state.runners[1]:
        extras += ('<form method="post" action="/walk" style="display:inline">'
                  '<button>敬遠</button></form> ')
    if state.can_change_pitcher():
        extras += ('<form method="post" action="/change" style="display:inline">'
                  '<button>継投</button></form> ')
    align_opts = "".join(
        f'<option value="{k}"{" selected" if k == state.defense.alignment else ""}>{v}</option>'
        for k, v in ALIGNMENTS.items())

    return f"""
<div class="card">
  <form method="post" action="/pitch">
    <div class="row">
      <label>球種 <select name="pitch_type">{pitch_opts}</select></label>
      <label>意図 <select name="intent">{intent_opts}</select></label>
    </div>
    <p>狙うコース</p>
    <div class="grid3">{course_cells}</div>
    <p><button type="submit">投球</button></p>
  </form>
  <div class="row">
    {extras}
    <form method="post" action="/alignment" style="display:inline">
      <select name="alignment">{align_opts}</select>
      <button>守備シフト変更</button>
    </form>
    <form method="post" action="/newgame" style="display:inline">
      <button>ゲームを中断して新規開始</button>
    </form>
  </div>
</div>"""


def _analysis_html(state, log_path):
    a = build_analysis(state.pitch_log)
    reads = grade_reads(state.reads, state.lineup.batters)
    run_txt, status = state.result_summary()

    def fmt(items, limit=5):
        if not items:
            return "<div class=\"dim\">なし</div>"
        rows = [f"<div>{n}球目 {_e(label)}"
               + (f" → {_e(item[2])}" if len(item) > 2 else "") + "</div>"
               for item in items[:limit] for n, label in [(item[0], item[1])]]
        if len(items) > limit:
            rows.append(f'<div class="dim">…ほか {len(items) - limit} 件</div>')
        return "".join(rows)

    reads_html = ""
    if reads["rows"]:
        reads_html = (f"<p>捕手メモ: {reads['correct']} / {reads['graded']} 当たり</p>"
                     + "".join(f"<div class=\"dim\">{_e(r.strip())}</div>" for r in reads["rows"]))

    return f"""
<div class="card">
  <h2>イニング終了：{_e(run_txt)} ／ {_e(status)}</h2>
  <p>自軍 {state.our_score} - {state.opp_score} 相手</p>
</div>
<div class="card">
  <h3>配球のふり返り（結果と判断は別ものとして評価）</h3>
  <p>総投球: {a['total_pitches']} 球　判断の質（平均）: {_e(a['decision_quality_label'])}</p>
  <p class="good">◎ 良かった配球</p>{fmt(a['good_calls'])}
  <p class="warn">△ 危険だった配球</p>{fmt(a['risky_calls'])}
  <p class="bad">◇ 浴びた長打</p>{fmt(a['extra_base'])}
  <p>★ 不運（判断は良かったが結果が悪い）</p>{fmt(a['unlucky'])}
  <p>★ 幸運（判断は甘いが結果に救われた）</p>{fmt(a['lucky'])}
  {reads_html}
  <p class="dim">{'ログを保存しました: ' + _e(log_path) if log_path else ''}</p>
</div>
<form method="post" action="/newgame"><button>もう一度プレイ</button></form>"""


def render_page(session):
    state = session.state
    session.finish_if_over()
    if state.is_over():
        body = _analysis_html(state, session.log_path)
    else:
        body = (_base_html(state) + _messages_html(session.messages, session.reaction)
               + _spray_html(state) + _pitch_form_html(state))
    return _PAGE.format(body=body)


# ---------------------------------------------------------------------------
# HTTP サーバ(標準ライブラリのみ)
# ---------------------------------------------------------------------------
class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass   # main() 実行時のアクセスログで画面を汚さない

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
        self._send_html(render_page(SESSION))

    def do_POST(self):
        state = SESSION.state
        form = self._read_form()

        if self.path == "/newgame":
            SESSION.new_game()
            self._redirect()
            return

        if state.is_over():
            self._redirect()
            return

        if self.path == "/pitch":
            pitch_type = form.get("pitch_type")
            course = form.get("course", "mid_mid")
            intent = form.get("intent", "strike")
            if pitch_type not in repertoire_options(state.pitcher):
                self._redirect()
                return
            rng = random
            messages = maybe_resolve_sign_steal(state, rng)
            outcome, more = resolve_one_pitch(state, pitch_type, course, intent, rng)
            SESSION.messages = messages + more
            SESSION.reaction = outcome.get("reaction")
            self._redirect()
            return

        if self.path == "/walk":
            state.intentional_walk()
            SESSION.messages = [">>> 申告敬遠。打者を歩かせた。"]
            SESSION.reaction = None
            self._redirect()
            return

        if self.path == "/change":
            SESSION.reaction = None
            if state.change_pitcher():
                SESSION.messages = [f">>> 継投。マウンドに {state.pitcher.name}。"
                                   "球数はリセット。持ち球・調子は投げてみるまで分からない。"]
            self._redirect()
            return

        if self.path == "/alignment":
            key = form.get("alignment")
            SESSION.reaction = None
            if key in ALIGNMENTS:
                state.defense.set_alignment(key)
                SESSION.messages = [f">>> 守備シフトを「{ALIGNMENTS[key]}」にしました。"]
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
    return p.parse_args()


def main():
    args = _parse_args()
    batters = None
    if args.lineup:
        batters = load_lineup_file(args.lineup).batters
    global SESSION
    SESSION = GameSession(lineup_batters=batters)

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
