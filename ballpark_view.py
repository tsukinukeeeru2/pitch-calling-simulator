"""守備位置を「球場を上から見た図」にする(色つき)。

文字列を組み立てるだけ。ゲームロジックには依存しない。
"""

import ansi
from fielders import position_fit, weakest_fielder


def _cell(defense, position, weak):
    f = defense.fielder_at(position)
    fit = position_fit(f, position)
    name = f"{f.name}:{fit:02.0f}"
    if f is weak:
        name = ansi.red("▼" + name)          # ▼ + 赤
    elif fit >= 58:
        name = ansi.green(name)
    elif fit <= 46:
        name = ansi.yellow(name)
    return f"{ansi.dim(position)} {name}"


def render_field(defense, batter=None):
    weak = weakest_fielder(defense.fielders)
    c = lambda pos: _cell(defense, pos, weak)

    title = ansi.bold(f"守備位置  [{defense.describe_alignment()}]")
    lines = [
        ansi.dim("━" * 6) + " " + title + " " + ansi.dim("━" * 18),
        "",
        f"                    {c('CF')}",
        "",
        f"        {c('LF')}                 {c('RF')}",
        "",
        f"                {c('SS')}     {c('2B')}",
        "",
        f"        {c('3B')}                 {c('1B')}",
        "",
        ansi.dim("                   P      C(あなた)"),
    ]
    if batter is not None:
        lines.append(ansi.dim(f"              ▶ 打者  {batter.public_hand_name()}"
                              f" / {batter.type_label()}"))
    lines.append("")
    lines.append(ansi.dim("  ▼=最も守備が不安な選手   数字=そのポジションでの適性(0-99)"))
    lines.append(ansi.dim("━" * 58))
    return "\n".join(lines)
