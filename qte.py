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


# --- Web からも使う純粋な採点ヘルパー(print も input もしない) ---
def bonus_from_verdicts(*verdicts):
    """perfect/good/miss を合計して -0.25〜0.25 の bonus にする。"""
    total = sum(_BONUS.get(v, 0.0) for v in verdicts)
    return max(-0.25, min(0.25, total))


def direction_verdict(picked, correct):
    """ブロッキングの方向当て: 一致=perfect / 隣=good / 逆=miss。"""
    if picked == correct:
        return "perfect"
    if (picked in _DIR_ORDER and correct in _DIR_ORDER
            and abs(_DIR_ORDER.index(picked) - _DIR_ORDER.index(correct)) == 1):
        return "good"
    return "miss"


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
def catcher_return_ball(rng=None):
    """捕球した球を投手へリズムよく返す(連打なし・3拍)。

    速すぎず遅すぎず一定のテンポで返すと投手が乗ってくる。
    返り値: tempo_bonus(-0.25〜0.25、大きいほど投手のテンポが整う)。
    """
    if not qte_enabled():
        return 0.0
    print(ansi.bold(ansi.cyan("\n♪ リズムよく投手へ返球 ―― 1・2・3 の一定テンポで即 Enter")))
    g = rhythm_check("", beats=3, interval=0.6, perfect=0.32, good=0.72)
    return {"perfect": 0.22, "good": 0.06, "miss": -0.16}[g]


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


def catcher_field_bunt(rng=None):
    """相手がバントしてきた打球を、マスクを外して前進 → 素手で掴む → 送球、の 3 段階。

    返り値: field_bonus(-0.25〜0.25、大きいほど先の走者を封殺しやすく、内野安打を防ぐ)。
    盗塁刺・ブロッキングと同じで、結果は確定させず確率をずらすだけ。
    """
    if not qte_enabled():
        return 0.0
    print(ansi.bold(ansi.yellow("\n═══ バントだ！ マスクを外して前へ ═══")))
    g1 = reaction_check("① 打った瞬間に飛び出せ", perfect=0.40, good=0.9)
    g2 = mash_check("② 素手で掴んで握り替え", need=8, limit=2.0)
    g3 = rhythm_check("③ 先の塁へ一直線に送球", beats=1, interval=0.5, perfect=0.35, good=0.7)
    return max(-0.25, min(0.25, _BONUS[g1] + _BONUS[g2] + _BONUS[g3]))


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
