"""保存済みの1ゲームを、名場面だけ抜き出して振り返るリプレイビューア。

    python replay.py                       # logs/ で一番新しいゲーム
    python replay.py logs/game_XXXX.json   # 指定したゲーム

strategy.build_analysis() がすでに持っている「良い配球/危険な配球/
不運/幸運/長打/守備と噛み合った/狙いを外せた」の分類をそのまま使い、
起きた順番(pitch_number)に並べ直して見せるだけ。判定ロジックは増やさない。
"""

import glob
import json
import os
import sys

import ansi
from strategy import build_analysis

_TAGS = [
    ("good_calls", "◎良い配球", "bgreen"),
    ("risky_calls", "△危険な配球", "byellow"),
    ("unlucky", "★不運（判断は良かった）", "bcyan"),
    ("lucky", "★幸運（判断は甘かった）", "byellow"),
    ("extra_base", "◇長打を浴びた", "bred"),
    ("defense_moments", "守備と噛み合った", "bgreen"),
    ("fooled_moments", "狙いを外せた", "bcyan"),
]


def _latest_log():
    paths = sorted(glob.glob("logs/game_*.json"))
    return paths[-1] if paths else None


def _collect_highlights(analysis):
    """pitch_number -> [(タグ表示, 色), ...] にまとめる。"""
    tags = {}
    for key, label, color in _TAGS:
        for item in analysis[key]:
            tags.setdefault(item[0], []).append((label, color))
    return tags


def render_replay(log):
    situation = log.get("situation", {})
    final = log.get("final", {})
    pitch_log = log.get("pitches", [])
    by_number = {p["pitch_number"]: p for p in pitch_log}
    analysis = build_analysis(pitch_log)
    highlights = _collect_highlights(analysis)

    lines = [
        ansi.bold(ansi.cyan(f"=== リプレイ: {situation.get('inning', '?')} ===")),
        f"開始 {situation.get('start', ['?', '?'])[0]} - {situation.get('start', ['?', '?'])[1]}"
        f"　→　結果 {final.get('our', '?')} - {final.get('opp', '?')}"
        f"（{final.get('result', '?')} / {final.get('status', '?')}）",
        "",
        ansi.bold(f"名場面プレイバック（{len(highlights)} 球）"),
    ]
    if not highlights:
        lines.append(ansi.dim("  目立った場面はありませんでした（淡々とした半イニング）"))
    for num in sorted(highlights):
        entry = by_number.get(num)
        if entry is None:
            continue
        tag_text = " / ".join(ansi.paint(label, color) for label, color in highlights[num])
        call_label = entry.get("call_label_ja", entry["call_label"])
        lines.append(f"  {num:>2}球目  {call_label:<20} → {entry['result']:<6}"
                     f"  decision_quality={entry['decision_quality']:+.2f}")
        lines.append(f"          {tag_text}")
        if entry.get("sequence_label") and entry["sequence_label"] != "特筆なし":
            lines.append(ansi.dim(f"          配球: {entry['sequence_label']}"))

    reads = log.get("reads")
    if reads and reads.get("rows"):
        lines.append("")
        lines.append(ansi.magenta(f"捕手メモの採点: {reads['correct']} / {reads['graded']} 当たり"))
        lines.extend(f"  {row.strip()}" for row in reads["rows"])

    lines.append("")
    lines.append(ansi.bold(f"判断の質（平均）: {analysis['decision_quality_label']}"))
    return "\n".join(lines)


def main():
    path = sys.argv[1] if len(sys.argv) > 1 else _latest_log()
    if path is None or not os.path.exists(path):
        print("ログが見つかりません。まず python main.py で遊んでください"
             "（または python replay.py <logs/game_....json> でパスを指定）。")
        return
    with open(path, encoding="utf-8") as fp:
        log = json.load(fp)
    print(ansi.dim(f"（{path}）"))
    print(render_replay(log))


if __name__ == "__main__":
    main()
