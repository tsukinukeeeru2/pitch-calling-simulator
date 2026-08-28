"""フェア打球のあとの走塁を解決する。

judge.py が「状態を変えずに」呼び、プラン(dict)を返す。
実際に試合状況へ反映するのは match_state.apply_play()。

返り値:
    {
      "batter_result": "アウト"/"単打"/"二塁打"/"三塁打"/"本塁打",
      "outs_added":  int,   # 打者アウト・併殺を含む、このプレーで増えるアウト数
      "runs":        int,   # このプレーの得点
      "new_runners": [bool, bool, bool],   # プレー後の 一塁・二塁・三塁
      "label":       str,   # 表示用("単打" "併殺" "犠飛" など)
      "detail":      str,   # 補足("走者一掃" "タッチアップ" など)
    }

物理シミュレーションではなく、読める確率モデル。
"""

import random


def _finish(result, outs_added, runs, new_runners, label, detail, outs_before):
    # このプレーで 3 アウト目が成立するなら、その回の得点は数えない(簡略化)
    if outs_before + outs_added >= 3:
        runs = 0
        detail = detail or "スリーアウトチェンジ"
    return {
        "batter_result": result,
        "outs_added": outs_added,
        "runs": runs,
        "new_runners": list(new_runners),
        "label": label,
        "detail": detail,
    }


def resolve(batted_ball, fielding, runners, outs, batter, rng=None):
    rng = rng or random
    result = fielding["result"]           # "アウト" / "エラー" / "単打" / "二塁打" / "三塁打" / "本塁打"
    r1, r2, r3 = runners
    on = sum(runners)
    to_outfield = batted_ball.ball_type in ("line", "fly")
    speed = getattr(batter, "speed", 50)

    # ---------------- 長打・単打 ----------------
    if result == "本塁打":
        runs = 1 + on
        detail = "ソロ" if on == 0 else f"{runs}ラン"
        return _finish("本塁打", 0, runs, [False, False, False], "本塁打", detail, outs)

    if result == "三塁打":
        runs = on
        return _finish("三塁打", 0, runs, [False, False, True],
                       "三塁打", "走者還る" if runs else "", outs)

    if result == "二塁打":
        runs = on
        detail = "走者一掃" if runs >= 2 else ("1点" if runs == 1 else "")
        return _finish("二塁打", 0, runs, [False, True, False], "二塁打", detail, outs)

    if result in ("単打", "エラー"):
        new = [False, False, False]
        runs = 0
        if r3:
            runs += 1                                   # 三塁走者は生還
        if r2:
            if to_outfield and rng.random() < 0.65:
                runs += 1                               # 二塁走者も外野前なら還る
            else:
                new[2] = True
        if r1:
            if to_outfield and rng.random() < 0.35:
                new[2] = True                           # 一塁走者が三塁まで
            else:
                new[1] = True
        new[0] = True                                   # 打者は一塁
        return _finish(result, 0, runs, new, result, f"{runs}点" if runs else "", outs)

    # ---------------- アウト系 ----------------
    ball_type = batted_ball.ball_type
    infield = fielding["position"] in ("1B", "2B", "3B", "SS")

    # 併殺: 一塁に走者 & 2アウト未満 & 内野ゴロ
    if r1 and outs < 2 and ball_type == "ground" and infield:
        dp_p = 0.55 - (speed - 50) / 100 * 0.60
        if batted_ball.hardness == "soft":
            dp_p -= 0.15                                # ボテボテは間に合わない
        elif batted_ball.hardness == "hard":
            dp_p -= 0.05                                # 速すぎても抜けたり難しい
        if rng.random() < max(0.05, dp_p):
            return _finish("アウト", 2, 0, [False, r2, r3], "併殺", "ゲッツー", outs)
        # 併殺は取れず打者アウトのみ。一塁走者は二塁へ、詰まっていれば押し出し的に進む
        new = [False, True, r2 or r3]
        runs = 1 if (r2 and r3) else 0
        return _finish("アウト", 1, runs, new, "アウト", "併殺崩れ", outs)

    # 犠飛: 三塁走者 & 2アウト未満 & フライアウト
    if fielding["air_out"] and r3 and outs < 2 and ball_type == "fly":
        deep = batted_ball.distance == "deep"
        if rng.random() < (0.85 if deep else 0.35):
            new = [False, r2, False]
            if deep and r2 and rng.random() < 0.40:
                new = [False, False, True]              # 二塁走者もタッチアップ
            return _finish("アウト", 1, 1, new, "犠飛", "タッチアップ", outs)

    # ふつうのアウト。内野ゴロなら走者が 1 つ進む「進塁打」がある
    new = list(runners)
    runs = 0
    detail = ""
    if ball_type == "ground" and outs < 2 and rng.random() < 0.45:
        if new[2]:
            runs += 1
        new = [False, new[0], new[1]]
        detail = "進塁打"
    return _finish("アウト", 1, runs, new, "アウト", detail, outs)
