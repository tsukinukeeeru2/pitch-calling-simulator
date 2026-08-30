"""複数ゲームのログ(logs/game_*.json)を集計する成績ダッシュボード。

    python stats.py                  # logs/ 以下すべて
    python stats.py logs/game_2026*.json

ゲームロジックには一切触らない。pitch_log の call_label
("投手/コース(意図)")を読み解いて、コース別・意図別の傾向を出すだけ。
"""

import glob
import json
import re
import sys

import ansi
from constants import COURSE_SHORT

_CALL_RE = re.compile(r"^(?P<pitch>[^/]+)/(?P<course>[^(]+)\((?P<intent>.+)\)$")

# strategy.py の分類と揃えている(打者が塁に出た = 守備にとって悪い結果)
_BAD_RESULTS = {"単打", "二塁打", "三塁打", "本塁打", "四球", "エラー"}
_GOOD_RESULTS = {"アウト", "空振り", "三振"}


def _load_logs(patterns):
    paths = []
    for pat in patterns:
        paths.extend(sorted(glob.glob(pat)))
    games = []
    for path in paths:
        try:
            with open(path, encoding="utf-8") as fp:
                games.append(json.load(fp))
        except (OSError, json.JSONDecodeError) as err:
            print(f"（{path} を読めませんでした: err）".replace("err", str(err)))
    return games


def _parse_call(call_label):
    m = _CALL_RE.match(call_label)
    if not m:
        return None, None, None
    return m.group("pitch"), m.group("course"), m.group("intent")


class _Bucket:
    """1 つの切り口(コース / 意図)ごとの集計。"""

    def __init__(self):
        self.n = 0
        self.bad = 0
        self.good = 0
        self.dq_sum = 0.0

    def add(self, result, dq):
        self.n += 1
        self.dq_sum += dq
        if result in _BAD_RESULTS:
            self.bad += 1
        elif result in _GOOD_RESULTS:
            self.good += 1

    def avg_dq(self):
        return self.dq_sum / self.n if self.n else 0.0

    def bad_rate(self):
        return self.bad / self.n if self.n else 0.0


def _print_table(title, rows, label_width=10):
    print(ansi.bold(title))
    header = f"  {'':<{label_width}} {'球数':>6} {'被弾率':>8} {'平均決断':>8}"
    print(ansi.dim(header))
    for label, bucket in rows:
        if bucket.n == 0:
            continue
        rate = bucket.bad_rate()
        color = "bred" if rate >= 0.35 else ("byellow" if rate >= 0.20 else "bgreen")
        print(f"  {label:<{label_width}} {bucket.n:>6} "
              f"{ansi.paint(f'{rate * 100:5.1f}%', color):>8} {bucket.avg_dq():>8.3f}")


def build_dashboard(games):
    by_course, by_intent = {}, {}
    total_pitches = 0
    total_runs = 0
    dq_sum = 0.0

    for g in games:
        total_runs += g.get("final", {}).get("runs_this_inning", 0)
        for entry in g.get("pitches", []):
            pitch, course, intent = _parse_call(entry["call_label"])
            result = entry["result"]
            dq = entry["decision_quality"]
            total_pitches += 1
            dq_sum += dq
            if course:
                by_course.setdefault(course, _Bucket()).add(result, dq)
            if intent:
                by_intent.setdefault(intent, _Bucket()).add(result, dq)

    n_games = len(games)
    print(ansi.bold(ansi.cyan(f"=== 成績ダッシュボード（{n_games} ゲーム / {total_pitches} 球） ===")))
    if n_games:
        print(f"  平均失点: {total_runs / n_games:.2f} 点/半イニング"
              f"　平均 decision_quality: {(dq_sum / total_pitches if total_pitches else 0):.3f}")
    print()

    course_rows = sorted(by_course.items(), key=lambda kv: -kv[1].n)
    _print_table("コース別（被弾率が高いほど赤）",
                 [(COURSE_SHORT.get(c, c), b) for c, b in course_rows])
    print()
    intent_rows = sorted(by_intent.items(), key=lambda kv: -kv[1].n)
    _print_table("配球意図別", intent_rows, label_width=14)


def main():
    patterns = sys.argv[1:] or ["logs/game_*.json"]
    games = _load_logs(patterns)
    if not games:
        print("ログが見つかりません（logs/game_*.json）。まず python main.py で何試合か遊んでください。")
        return
    build_dashboard(games)


if __name__ == "__main__":
    main()
