"""相手ベンチの作戦(小技) ―― 犠打(バント) / ヒットエンドラン。

打席の初球の前に decide_tactic() を1回だけ呼ぶ。返り値:
    None            … 普通に打ってくる
    "bunt"          … 犠打(送りバント)。打者がバントの構えを見せる = 捕手に見える
    "hit_and_run"   … エンドラン。走者がスタート + 打者は必ず振る = 捕手には見えない

捕手はこれに対して「ピッチアウト」「高め速球(バントしにくい球)」などで
対抗できる ―― engine.py と match_state.py がその読み合いを解決する。

このゲームは常に終盤の接戦なので、状況(点差・アウト・走者)と打者タイプで
「教科書どおりに仕掛けてくる」かどうかだけを判断する。相手を強くしすぎない
ため、確率は控えめ(最大でも 35%)。
"""


def _weak_hitter(batter):
    return (batter.coarse_type in ("weak", "patient")
            or getattr(batter, "power", 0.5) < 0.42)


def _contact_hitter(batter):
    return (getattr(batter, "contact", 0.5) >= 0.60
            and getattr(batter, "whiff_rate", 0.24) <= 0.22)


def decide_tactic(state, rng):
    """相手ベンチが仕掛けてくる作戦を返す(None / "bunt" / "hit_and_run")。"""
    if state.outs >= 2:
        return None
    r1, r2, r3 = state.runners
    forceable = r1 or r2                     # 送れる走者(封殺されうる塁の走者)がいる
    if not forceable:
        return None

    # 守備側(自軍)から見た点差。1点差以内なら「1点を取りにくる」状況。
    close = abs(state.score_diff) <= 2
    one_run_game = -2 <= state.score_diff <= 1   # 同点〜1点ビハインド(守る側)= 相手は手堅く

    batter = state.batter

    # --- 犠打: 弱打者 + 走者一塁(or 一二塁) + 僅差 ---
    if _weak_hitter(batter) and r1 and not r3 and close:
        p = 0.35 if one_run_game else 0.18
        if rng.random() < p:
            return "bunt"

    # --- エンドラン: 巧打者 + 走者一塁のみ + 僅差 ---
    if _contact_hitter(batter) and r1 and not r2 and not r3 and close:
        if rng.random() < 0.20:
            return "hit_and_run"

    return None


def describe(tactic):
    return {
        "bunt": "打者がバントの構えを見せた",
        "hit_and_run": "",   # エンドランは捕手に見えない
    }.get(tactic, "")
