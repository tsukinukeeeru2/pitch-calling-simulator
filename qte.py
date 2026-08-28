"""キャッチャーの『体で覚える』プレーを、リズム / QTE のミニゲームにする。

標準ライブラリ(time / sys / os)だけ。端末が対話的でない(パイプ)ときや
PITCHSIM_QTE=0 のときは自動でスキップし、確率モデルにそのまま任せる
―― テストやデモを壊さないため。

各ミニゲームは "perfect" / "good" / "miss" を返し、
catcher_throw() / catcher_block() がそれを -0.25〜+0.25 の "うまさ補正" にまとめる。
この補正は結果を確定させず、盗塁刺・ブロッキングの成功確率をずらすだけ。
"""

import os
import sys
import time

import ansi

_LABEL = {"perfect": ansi.green("PERFECT!"), "good": ansi.yellow("GOOD"), "miss": ansi.red("MISS")}
_BONUS = {"perfect": 0.20, "good": 0.05, "miss": -0.18}


def qte_enabled():
    flag = os.environ.get("PITCHSIM_QTE")
    if flag == "0":
        return False
    if flag == "1":
        return True
    return bool(getattr(sys.stdin, "isatty", lambda: False)()
               and getattr(sys.stdout, "isatty", lambda: False)())


def _read():
    try:
        return input()
    except (EOFError, KeyboardInterrupt):
        return None


def _grade(value, perfect, good):
    return "perfect" if value <= perfect else ("good" if value <= good else "miss")


_DIR_ORDER = ["in", "mid", "out"]
_DIR_KEY = {"1": "in", "2": "mid", "3": "out"}
_DIR_JP = {"in": "内(左)", "mid": "中(正面)", "out": "外(右)"}


# --- 方向(ワンバウンドが来た側へ体を寄せる: 1=内 2=中 3=外) ---
def direction_check(correct_dir):
    print("  " + ansi.bold("→ どっちへ入る？  1=内  2=中  3=外"))
    s = _read()
    picked = _DIR_KEY.get((s or "").strip()[:1]) if s is not None else None
    if picked == correct_dir:
        print(f"  {_DIR_JP[correct_dir]} … {_LABEL['perfect']}")
        return "perfect"
    if picked in _DIR_ORDER and abs(_DIR_ORDER.index(picked) - _DIR_ORDER.index(correct_dir)) == 1:
        print(f"  半歩ずれた（{_DIR_JP[picked]}）… {_LABEL['good']}")
        return "good"
    print(f"  逆をついた（正解は{_DIR_JP[correct_dir]}）… {_LABEL['miss']}")
    return "miss"


# --- 単発の反応(①気づいたら即) ---
def reaction_check(cue, perfect=0.42, good=0.9):
    print("  " + cue)
    print("  " + ansi.bold("→ いま！ Enter！"))
    t0 = time.time()
    if _read() is None:
        return "miss"
    dt = time.time() - t0
    g = _grade(dt, perfect, good)
    print(f"  反応 {dt * 1000:.0f}ms … {_LABEL[g]}")
    return g


# --- 連打(②握り替えを速く) ---
def mash_check(cue, need=10, limit=2.4):
    print("  " + cue)
    print("  " + ansi.bold(f"→ 同じキーを連打してから Enter（{limit:.0f}秒以内に{need}回）"))
    t0 = time.time()
    s = _read()
    if s is None:
        return "miss"
    dt = time.time() - t0
    n = len(s.strip())
    if n >= need and dt <= limit:
        g = "perfect"
    elif n >= int(need * 0.6):
        g = "good"
    else:
        g = "miss"
    print(f"  {n}連打 / {dt:.1f}秒 … {_LABEL[g]}")
    return g


# --- リズム(③送球 / 1・2・3のブロッキング) ---
def rhythm_check(cue, beats=3, interval=0.55, perfect=0.35, good=0.75):
    print("  " + cue)
    print("  " + ansi.dim(f"一定のリズムで {beats} 回。数字が出たら即 Enter。"))
    time.sleep(0.6)
    latencies = []
    for i in range(1, beats + 1):
        time.sleep(interval)
        print("   " + ansi.bold(ansi.cyan(f"▶ {i}")), flush=True)
        t0 = time.time()
        if _read() is None:
            return "miss"
        latencies.append(time.time() - t0)
    good_beats = sum(1 for x in latencies if x <= good)
    avg = sum(latencies) / len(latencies)
    if good_beats == beats and avg <= perfect:
        g = "perfect"
    elif good_beats >= beats - 1:
        g = "good"
    else:
        g = "miss"
    print("  リズム " + "/".join(f"{x * 1000:.0f}" for x in latencies) + f"ms … {_LABEL[g]}")
    return g


# --- 組み合わせ ---
def catcher_throw(rng=None, base="二塁", title="走った！ 盗塁を刺せ"):
    """走者を刺す 3 段階(盗塁の二塁送球 / 振り逃げの一塁送球で共用)。

    base  : 送球先の表示("二塁" / "一塁")
    返り値: throw_bonus(-0.25〜0.25、大きいほど刺しやすい)。
    """
    if not qte_enabled():
        return 0.0
    print(ansi.bold(ansi.yellow(f"\n═══ {title} ═══")))
    g1 = reaction_check("① スタートに気づけ", perfect=0.42, good=0.95)
    g2 = mash_check("② グラブから握り替え", need=10, limit=2.4)
    g3 = rhythm_check(f"③ {base}へ一直線に送球", beats=1, interval=0.5, perfect=0.35, good=0.7)
    total = _BONUS[g1] + _BONUS[g2] + _BONUS[g3]
    return max(-0.25, min(0.25, total))


def catcher_change_signs(rng=None):
    """二塁走者にサインを覗われかけたとき、素早くサインを変える。

    返り値: sign_bonus(-0.25〜0.25、大きいほどサインを守れる)。
    """
    if not qte_enabled():
        return 0.0
    print(ansi.bold(ansi.yellow("\n═══ 二塁走者がサインを覗っている！ サインを変えろ ═══")))
    g1 = mash_check("① 指を素早く動かしてサインを変える", need=8, limit=2.0)
    g2 = reaction_check("② 新しいサインで合図し直す", perfect=0.40, good=0.9)
    total = _BONUS[g1] + _BONUS[g2]
    return max(-0.25, min(0.25, total))


def catcher_block(correct_dir="mid", wild=False, rng=None):
    """ワンバウンド / 暴投を体で止める。

    correct_dir : ボールが来た側 "in" / "mid" / "out"(実投コースの zone_x)
    wild        : 投手の暴投(低め以外の大きな失投)なら True ―― 表示だけ変える
    返り値: block_bonus(-0.25〜0.25、大きいほど後逸しない)。
    """
    if not qte_enabled():
        return 0.0
    head = "投手の暴投！ 前に出て止めろ" if wild else "ワンバウンド！ 体で止めろ"
    print(ansi.bold(ansi.yellow(f"\n═══ {head} ═══")))
    gd = direction_check(correct_dir)
    gr = rhythm_check("1・2・3 のリズムで体を落とせ", beats=3, interval=0.5)
    return max(-0.25, min(0.25, _BONUS[gd] + _BONUS[gr]))
