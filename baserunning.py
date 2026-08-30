"""フェア打球のあとの走塁を解決する。

judge.py が「状態を変えずに」呼び、プラン(dict)を返す。
実際に試合状況へ反映するのは match_state.apply_play()。

返り値:
    {
      "batter_result": "アウト"/"エラー"/"単打"/"二塁打"/"三塁打"/"本塁打",
      "outs_added":  int,   # 打者アウト・併殺を含む、このプレーで増えるアウト数
      "runs":        int,   # このプレーの得点
      "new_runners": [bool, bool, bool],       # プレー後の 一塁・二塁・三塁
      "new_runner_speeds": [int|None, ...],    # 同じ並びで、その塁にいる走者の脚力
      "label":       str,   # 表示用("単打" "併殺" "犠飛" など)
      "detail":      str,   # 補足("走者一掃" "タッチアップ" など)
    }

走者の脚力(runner_speeds)は 20〜90 くらい。速い走者は
  - 一塁から一気に三塁まで行きやすい
  - 二塁から単打で還りやすい
  - 併殺を取られにくい(judge/steal 側でも使う)
物理シミュレーションではなく、読める確率モデル。
"""

import random


def _finish(result, outs_added, runs, new_runners, new_speeds, label, detail, outs_before):
    # このプレーで 3 アウト目が成立するなら、その回の得点は数えない(簡略化)
    if outs_before + outs_added >= 3:
        runs = 0
        detail = detail or "スリーアウトチェンジ"
    return {
        "batter_result": result,
        "outs_added": outs_added,
        "runs": runs,
        "new_runners": list(new_runners),
        "new_runner_speeds": list(new_speeds),
        "label": label,
        "detail": detail,
    }


def resolve(batted_ball, fielding, runners, outs, batter, rng=None, runner_speeds=None,
            runner_moving=False):
    """runner_moving=True は「エンドランで一塁走者がすでにスタートしている」状態。
    併殺は起きず、内野ゴロでも走者は必ず 1 つ先へ進む(封殺回避)。"""
    rng = rng or random
    result = fielding["result"]           # "アウト" / "エラー" / "単打" / "二塁打" / "三塁打" / "本塁打"
    r1, r2, r3 = runners
    speeds = list(runner_speeds) if runner_speeds else [None, None, None]
    s1, s2, s3 = speeds
    on = sum(1 for r in runners if r)
    to_outfield = batted_ball.ball_type in ("line", "fly")
    bsp = getattr(batter, "speed", 50)

    def spd(s):   # 走者の脚力(不明なら平均50)を 0.0 中心のズレに
        return ((s if s is not None else 50) - 50) / 100.0

    # ---------------- 長打・単打 ----------------
    if result == "本塁打":
        runs = 1 + on
        detail = "ソロ" if on == 0 else f"{runs}ラン"
        return _finish("本塁打", 0, runs, [False] * 3, [None] * 3, "本塁打", detail, outs)

    if result == "三塁打":
        return _finish("三塁打", 0, on, [False, False, True], [None, None, bsp],
                       "三塁打", "走者還る" if on else "", outs)

    if result == "二塁打":
        detail = "走者一掃" if on >= 2 else ("1点" if on == 1 else "")
        return _finish("二塁打", 0, on, [False, True, False], [None, bsp, None],
                       "二塁打", detail, outs)

    if result in ("単打", "エラー"):
        new = [False, False, False]
        new_sp = [None, None, None]
        runs = 0
        if r3:
            runs += 1                                       # 三塁走者は生還
        if r2:
            # 二塁走者: 外野への打球 + 脚力で生還率が上下(基準 0.55、俊足で最大 +0.30)
            if to_outfield and rng.random() < 0.55 + spd(s2) * 0.6:
                runs += 1
            else:
                new[2], new_sp[2] = True, s2
        if r1 and not new[2]:
            # 一塁走者: 外野への打球 + 脚力で三塁まで(基準 0.30)。
            # エンドラン中はスタートを切っているので、ほぼ三塁まで到達。
            reach = 0.90 if runner_moving else (0.30 + spd(s1) * 0.6 if to_outfield else 0.0)
            if rng.random() < reach:
                new[2], new_sp[2] = True, s1
            else:
                new[1], new_sp[1] = True, s1
        elif r1:
            new[1], new_sp[1] = True, s1
        new[0], new_sp[0] = True, bsp                       # 打者は一塁
        detail = f"{runs}点" if runs else ""
        return _finish(result, 0, runs, new, new_sp, result, detail, outs)

    # ---------------- アウト系 ----------------
    ball_type = batted_ball.ball_type
    infield = fielding["position"] in ("1B", "2B", "3B", "SS")

    # 併殺: 一塁に走者 & 2アウト未満 & 内野ゴロ(エンドラン中は走者が走っているので無し)
    if r1 and outs < 2 and ball_type == "ground" and infield and not runner_moving:
        dp_p = 0.55 - (bsp - 50) / 100 * 0.30    # 打者の脚
        dp_p -= spd(s1) * 0.40                   # 一塁走者の脚(速いと二封が難しい)
        if batted_ball.hardness == "soft":
            dp_p -= 0.15                         # ボテボテは間に合わない
        elif batted_ball.hardness == "hard":
            dp_p -= 0.05                         # 速すぎても抜けたり難しい
        if rng.random() < max(0.05, dp_p):
            return _finish("アウト", 2, 0, [False, r2, r3], [None, s2, s3],
                           "併殺", "ゲッツー", outs)
        # 併殺崩れ: 打者アウトのみ。一塁走者は二塁へ、詰まっていれば前が押し出される
        new = [False, True, bool(r2 or r3)]
        new_sp = [None, s1, s2 if r2 else (s3 if r3 else None)]
        runs = 1 if (r2 and r3) else 0
        return _finish("アウト", 1, runs, new, new_sp, "アウト", "併殺崩れ", outs)

    # 犠飛: 三塁走者 & 2アウト未満 & フライアウト
    if fielding["air_out"] and r3 and outs < 2 and ball_type == "fly":
        deep = batted_ball.distance == "deep"
        if rng.random() < (0.85 if deep else 0.35) + spd(s3) * 0.4:
            new = [False, r2, False]
            new_sp = [None, s2, None]
            if deep and r2 and rng.random() < 0.40 + spd(s2) * 0.5:
                new, new_sp = [False, False, True], [None, None, s2]   # 二塁走者もタッチアップ
            return _finish("アウト", 1, 1, new, new_sp, "犠飛", "タッチアップ", outs)

    # ふつうのアウト。内野ゴロなら走者が 1 つ進む「進塁打」がある
    # (エンドラン中は走者が走っているので、ほぼ確実に進む)
    new = list(runners)
    new_sp = list(speeds)
    runs = 0
    detail = ""
    advance_p = 0.90 if runner_moving else 0.45
    if ball_type == "ground" and outs < 2 and rng.random() < advance_p:
        if new[2]:
            runs += 1
        new = [False, bool(new[0]), bool(new[1])]
        new_sp = [None, speeds[0], speeds[1]]
        detail = "進塁打" + ("（エンドラン）" if runner_moving else "")
    return _finish("アウト", 1, runs, new, new_sp, "アウト", detail, outs)
