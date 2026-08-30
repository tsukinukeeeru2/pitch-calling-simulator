"""試合状況(MatchState)と、終盤の場面をランダムに作る generate_random_situation()。

このゲームの 1 プレイ = 「終盤のある半イニングで 3 アウトを取るまで」。

MatchState は judge.py が期待する属性
  batter / pitcher / defense / history / balls / strikes / outs / runners / score_diff
をそのまま持つ(judge.py は MatchState を直接は import しない)。
"""

import random

from defense import make_default_defense
from lineup import Lineup, build_sample_lineup
from pitch_history import PitchHistory
from pitcher import build_sample_pitcher

INNING_CHOICES = [7, 8, 9, 10]          # 10 = 延長
INNING_WEIGHTS = [3, 3, 3, 1]
DIFF_CHOICES = [-3, -2, -1, 0, 1, 2, 3]  # 自軍 - 相手
DIFF_WEIGHTS = [1, 2, 4, 5, 4, 2, 1]     # 接戦(-1/0/+1)が出やすい
OUT_CHOICES = [0, 1, 2]
OUT_WEIGHTS = [5, 3, 2]


class MatchState:
    def __init__(self, inning, is_extra, half, we_are, our_score, opp_score,
                 outs, runners, lineup, pitcher, defense, runner_speeds=None):
        self.inning = inning
        self.is_extra = is_extra
        self.half = half                 # "top"(表) / "bottom"(裏)
        self.we_are = we_are             # 守備側は自軍。"HOME" / "AWAY"
        self.our_score = our_score
        self.opp_score = opp_score
        self.start_our = our_score        # 開始時スコア(最後の判定用に保存)
        self.start_opp = opp_score
        self.outs = outs
        self.runners = list(runners)
        # 各塁の走者の脚力(20〜90 目安、走者がいない塁は None)。
        # 盗塁の仕掛け頻度・成功率、単打での一塁→三塁、併殺の取りやすさに効く。
        self.runner_speeds = list(runner_speeds) if runner_speeds else [None, None, None]
        self.balls = 0
        self.strikes = 0
        self.lineup = lineup
        self.pitcher = pitcher
        self.defense = defense
        self.history = PitchHistory()
        self.runs_this_inning = 0
        self.pitch_log = []              # 1 球 = dict。試合後分析に使う
        self.spray = []                  # フェア打球の記録(#B 打球傾向チャート)
        self.reads = {}                  # 捕手メモ id(batter) -> {"spot","wait","weak"}
        self.bullpen = []                # 継投用の控え投手(複数可。先頭から出ていく順ではなく選んで出す)
        self.pitching_changes = 0
        self.events = []                 # 盗塁など、打席途中の出来事のログ
        self.sign_leak = 0.0             # サインを見破られた次の1球だけ、打者の狙いを強める(#B)
        self.batter_pitch_memory = {}    # id(batter) -> {"fastball","offspeed"}。打席をまたぐ記憶
        self.batters_faced = 1           # Postgame Report用。最初の打者も1人と数える
        self.opp_tactic = None           # 相手ベンチの作戦 None / "bunt" / "hit_and_run"(#C 小技)
        self.tactic_decided = False       # この打席の作戦を決めたか(begin_at_bat を冪等にする)
        self.framed_strikes = 0          # フレーミングで「ボール→ストライク」に見せた回数(#捕手の craft)
        self.good_call_streak = 0        # 「いい配球」が連続している数(甘め判定。数値自体は表に出さない)

    # ---- judge.py が使う ----
    @property
    def batter(self):
        return self.lineup.current()

    # ---- 捕手メモ(#B) ----
    def record_read(self, wait=None, weak=None):
        note = self.reads.setdefault(id(self.batter), {"spot": self.lineup.spot_number(),
                                                       "wait": None, "weak": None})
        if wait is not None:
            note["wait"] = wait
        if weak is not None:
            note["weak"] = weak

    def current_read(self):
        return self.reads.get(id(self.batter))

    # ---- 「いい配球」ボーナス(#ほめる要素)。しきい値は甘め。 ----
    # 試合後分析の good_call は decision_quality >= 0.25 だが、プレイ中の
    # ご褒美はもっと緩く(>= GOOD_CALL_DQ)。数値そのものは絶対に画面へ出さない。
    GOOD_CALL_DQ = 0.10
    _BAD_CALL_RESULTS = ("単打", "二塁打", "三塁打", "本塁打", "四球", "エラー")

    def good_call_bonus(self):
        """いい配球が 2 球以上続いていると、次の 1 球で投手が少し乗る(最大 +0.15)。"""
        if self.good_call_streak >= 2:
            return min(0.15, 0.05 * self.good_call_streak)
        return 0.0

    def register_call_quality(self, decision_quality, result):
        """1 球ごとに「いい配球」の連続を更新して、今の球が good だったかを返す。"""
        good = (decision_quality >= self.GOOD_CALL_DQ
                and result not in self._BAD_CALL_RESULTS)
        self.good_call_streak = self.good_call_streak + 1 if good else 0
        return good

    def spray_add(self, batted_ball, result):
        self.spray.append({
            "direction": batted_ball.direction,
            "ball_type": batted_ball.ball_type,
            "distance": batted_ball.distance,
            "bats": self.batter.bats,
            "result": result,
        })

    # ---- 継投(#ゲーム性) ----
    def can_change_pitcher(self):
        return bool(self.bullpen)

    def change_pitcher(self, index=0):
        """控え投手(bullpen[index])に交代。ブルペンにいる人数だけ何回でも交代できる。

        降板した投手はこの試合には戻らない(現実のルールどおり)ので、古い投手は
        破棄する。球数はリセットされ、新しい投手の持ち球・調子は投げてみるまで
        分からない(#D)。返り値は交代後の新しい投手(交代できなければ None)。
        """
        if not self.can_change_pitcher() or not (0 <= index < len(self.bullpen)):
            return None
        self.pitcher = self.bullpen.pop(index)
        self.pitching_changes += 1
        return self.pitcher

    # ---- 敬遠(#ゲーム性) ----
    def intentional_walk(self):
        """申告敬遠。打者を歩かせる(押し出しあり)。次打者へ。"""
        self.events.append("敬遠")
        self.advance_on_walk()

    # ---- 走者の脚力(#走者の個性) ----
    def runner_speed_at(self, base):
        """その塁の走者の脚力。走者がいない/不明なら 50(平均)。"""
        s = self.runner_speeds[base] if 0 <= base < 3 else None
        return 50 if s is None else s

    def lead_runner_fast(self):
        """一塁走者が「盗塁の脅威」レベルに速いか(捕手が身構える目安)。"""
        return bool(self.runners[0]) and self.runner_speed_at(0) >= 70

    # ---- 盗塁(#ゲーム性・軽量モデル) ----
    def steal_chance(self, rng):
        """この球で盗塁を仕掛けるか(一塁走者だけ・二塁が空き・2アウト未満)。

        走者が速いほど仕掛けてくる: 脚力50→約12% / 80→約24% / 35→約6%。
        """
        if not (self.runners[0] and not self.runners[1]) or self.outs >= 2:
            return False
        p = max(0.02, 0.12 + (self.runner_speed_at(0) - 50) / 250)
        return rng.random() < p

    def resolve_steal(self, pitch_family, rng, throw_bonus=0.0):
        """盗塁の成否を決める(steal_chance が True のときに呼ぶ)。

        safe_p(セーフ確率) = 0.56 + 走者の脚力補正((脚力-50)/240)
          - 速球なら捕手が刺しやすい (-0.12) / 変化球・ワンバウンドは間に合わない (+0.10)
          - throw_bonus は捕手の送球のうまさ(QTE の結果)。大きいほど刺しやすい。
        返り値: 出来事の文字列 or None。状態はここで書き換える。
        """
        if not (self.runners[0] and not self.runners[1]) or self.outs >= 2:
            return None
        safe_p = 0.56 + (self.runner_speed_at(0) - 50) / 240
        safe_p += -0.12 if pitch_family == "fastball" else 0.10
        safe_p -= throw_bonus
        if rng.random() < max(0.05, min(0.95, safe_p)):
            self.runners = [False, True, self.runners[2]]
            self.runner_speeds = [None, self.runner_speeds[0], self.runner_speeds[2]]
            self.events.append("盗塁成功")
            return "盗塁成功（走者二塁へ）"
        self.runners[0] = False
        self.runner_speeds[0] = None
        self.outs += 1                               # 打者はそのまま。走者アウト
        self.events.append("盗塁刺")
        return "盗塁を刺した！"

    # ---- ワンバウンド / 暴投のブロッキング(#ゲーム性) ----
    def resolve_block(self, block_bonus, rng, hard=False):
        """止められたか。止め損なうと走者が 1 つ進む(後逸)。

        pass_p(後逸する確率) = 0.45 - block_bonus (+ 0.12 if hard)
          block_bonus は捕手のブロッキングのうまさ(QTE の結果)。大きいほど止まる。
          hard は「隅への低め」など体を大きく動かす球。
        返り値: 出来事の文字列 or None。
        """
        if not any(self.runners):
            return None
        pass_p = max(0.03, min(0.9, 0.45 - block_bonus + (0.12 if hard else 0.0)))
        if rng.random() >= pass_p:
            return None                              # 止めた
        if self.runners[2]:
            self._score_runs(1)
        self.runners = [False, self.runners[0], self.runners[1]]
        self.runner_speeds = [None, self.runner_speeds[0], self.runner_speeds[1]]
        self.events.append("後逸")
        return "後逸！ 走者が進んだ"

    # ---- サイン交換(#ゲーム性・リズム) ----
    def sign_steal_chance(self, rng):
        """二塁に走者がいるときだけ、サインを覗われるリスクがある。"""
        if not self.runners[1]:
            return False
        return rng.random() < 0.15

    def resolve_sign_steal(self, sign_bonus, rng):
        """サインを覗われたときの対応(sign_steal_chance が True のときに呼ぶ)。

        defend_p(サインを守れる確率) = 0.55 + sign_bonus(サイン交換 QTE のうまさ)。
        守れなければ次の 1 球だけ、打者の狙いが実際の球種に強く一致する(sign_leak)。
        judge.py がこれを見て guess を上書きし、1 球使ったら 0 に戻す。
        返り値: 出来事の文字列。
        """
        self.events.append("サイン交換")
        defend_p = max(0.10, min(0.95, 0.55 + sign_bonus))
        if rng.random() < defend_p:
            return "サインを変えて事なきを得た"
        self.sign_leak = 0.65
        return "二塁走者にサインを見抜かれた！ 気をつけろ"

    # ---- 相手の小技: 犠打(#C) ----
    def resolve_bunt(self, pitched_out, high_heat, charging, rng, field_bonus=0.0):
        """バント(送りバント)を解決する。engine が opp_tactic=="bunt" のとき呼ぶ。

        pitched_out : 捕手がピッチアウトした(バントを外させた)
        high_heat   : 高めの速球(バントしにくい球)
        charging    : 守備が「バント警戒」で一・三塁が前進している
        field_bonus : 捕手のバント処理リズムゲーム(qte.catcher_field_bunt)の出来。
                      -0.25〜0.25。大きいほど先の走者を封殺しやすく、内野安打を防ぎ、
                      スクイズの走者を本塁で刺せることがある。CLI 以外(Web・テスト)は
                      常に 0.0 で、その場合の挙動は従来とまったく同じ。
        返り値: (messages: list[str], final_result: str, at_bat_end: str|None)
        """
        self.events.append("バント")
        r, sp = self.runners, self.runner_speeds
        bsp = self.batter.speed

        if pitched_out:
            self.balls += 1
            if self.balls >= 4:
                self.advance_on_walk()
                return ["バントを外させた！ 押し出し級のボール球", ">>> 四球"], "四球", "walk"
            self.opp_tactic = None                     # 一度崩れたら普通の打撃に戻る
            return ["バントを外させた！（ボール）"], "ボール", None

        pop_p = 0.10 + (0.16 if high_heat else 0.0) + (0.06 if charging else 0.0)
        foul_p = 0.22
        roll = rng.random()

        if roll < pop_p:
            self.add_out()                            # 小フライを内野が処理
            return ["バントが小フライ！ 打ち取った"], "バント失敗（小フライ）", "out"

        if roll < pop_p + foul_p:
            if self.strikes >= 2:                     # スリーバント失敗 = 三振
                self.add_out()
                return ["2ストライクからのバントファウル … スリーバント失敗で三振"], "三振", "strikeout"
            self.strikes += 1
            return ["バントはファウル（ストライク）"], "ファウル", None

        # 転がった。3塁走者はスクイズで生還を狙う。
        squeeze = bool(r[2])
        # 好フィールディング(リズムゲーム)なら、スクイズの走者を本塁で刺せることがある。
        # field_bonus > 0.0 のときだけ発生 = Web/テスト(常に 0.0)の挙動は不変。
        if squeeze and field_bonus > 0.0 and rng.random() < min(0.33, field_bonus * 1.2):
            self.runners = [True, bool(r[0]), bool(r[1])]      # 打者は一塁(フィルダースチョイス)
            self.runner_speeds = [bsp, sp[0], sp[1]]
            self.outs += 1
            if self.outs < 3:
                self.next_batter()
            return ["前に出て掴み、本塁へ送球！ スクイズの走者を刺した！"], "バント（本封殺）", "out"

        scored = 1 if squeeze else 0
        if scored:
            self._score_runs(1)

        force_p = max(0.02, (0.16 if charging else 0.05) + field_bonus * 0.45)
        single_p = max(0.0, 0.03 + (bsp - 50) / 300.0 - field_bonus * 0.40)
        r2 = rng.random()

        if r2 < force_p and r[0]:
            # 守備が先の走者を封殺(打者は一塁でセーフ = フィルダースチョイス)
            self.runners = [True, bool(r[1]), False]
            self.runner_speeds = [bsp, sp[1], None]
            self.outs += 1
            if self.outs < 3:
                self.next_batter()
            return ["守備が先の走者を封殺！ 打者は一塁"], "バント（封殺）", "out"

        if r2 < force_p + single_p:
            self.runners = [True, bool(r[0]), bool(r[1])]
            self.runner_speeds = [bsp, sp[0], sp[1]]
            self.next_batter()
            return ["セーフ！ 内野安打（バントヒット）"], "バント安打", "hit"

        # 通常の犠打成功: 打者アウト、走者を1つずつ進める
        self.runners = [False, bool(r[0]), bool(r[1])]
        self.runner_speeds = [None, sp[0], sp[1]]
        self.add_out()
        detail = "スクイズ成功！" if scored else "犠打成功。走者を進めた"
        return [detail], "犠打成功", "out"

    # ---- 相手の小技: ヒットエンドラン(#C) ----
    def hit_and_run_runner_go(self, pitched_out, contact, rng, throw_bonus=0.0):
        """エンドランで一塁走者がスタートしたあとの結末を決める(engine が呼ぶ)。

        pitched_out : 捕手が見破ってピッチアウトした → 走者は丸見え、まず刺される
        contact     : 打者がフェアに打った(この場合は判定側で走者を動かすので呼ばれない)
        返り値: 出来事の文字列 or None
        """
        if not self.runners[0]:
            return None
        if pitched_out:
            # 走者は完全に浮いている。resolve_steal を強い送球ボーナスで流用。
            return self.resolve_steal("fastball", rng, throw_bonus=0.35 + throw_bonus)
        # 打者が振ってくれた(空振り/ファウル/凡打)ので捕手は投げにくい → ほぼ無条件で進塁
        self.runners = [False, True, self.runners[2]]
        self.runner_speeds = [None, self.runner_speeds[0], self.runner_speeds[2]]
        self.events.append("エンドラン")
        return "エンドラン、走者は二塁へ"

    # ---- 振り逃げ(#ゲーム性) ----
    def dropped_third_eligible(self):
        """一塁が空いている、または 2 アウト → 振り逃げが成立しうる(公式ルールどおり)。"""
        return (not self.runners[0]) or self.outs >= 2

    def resolve_dropped_third(self, block_bonus, throw_bonus, rng, hard=False):
        """振り逃げ。ワンバウンドの三振を捕手が後逸/捕れなかったときに呼ぶ
        (dropped_third_eligible が True の前提)。

        block_bonus : ブロッキングのうまさ / throw_bonus : 一塁送球のうまさ
          (どちらも -0.25〜0.25。CLI/Web の QTE か、非対話なら 0.0)

        1) pass_p(完全に後逸する確率)は resolve_block と同じ式。後逸なら打者は無条件で
           一塁へ、ほかの走者も 1 つ進む(3 塁走者は生還) — 通常の後逸と同じ扱い。
        2) 後逸でなければ一塁へ送球。safe_p は打者の脚力と送球のうまさで決まる。
           セーフなら打者は一塁へ(強制進塁のみ)。アウトなら普通の三振と同じ。
        返り値: (safe: bool, message: str)。
        """
        self.events.append("振り逃げ")
        pass_p = max(0.05, min(0.9, 0.45 - block_bonus + (0.12 if hard else 0.0)))
        if rng.random() < pass_p:
            if self.runners[2]:
                self._score_runs(1)
            self.runners = [True, self.runners[0], self.runners[1]]
            self.runner_speeds = [self.batter.speed, self.runner_speeds[0], self.runner_speeds[1]]
            self.next_batter()
            return True, "後逸！ 振り逃げが成立、打者は一塁へ生きた"

        speed = self.batter.speed
        safe_p = 0.28 + (speed - 50) / 100 * 0.40 - throw_bonus
        if rng.random() < max(0.04, min(0.9, safe_p)):
            self.advance_on_walk()
            return True, "一塁送球が間に合わず、振り逃げが成立！"

        self.outs += 1
        if self.outs < 3:
            self.next_batter()
        return False, "一塁で刺した！ 三振"

    @property
    def score_diff(self):
        return self.our_score - self.opp_score

    # ---- カウント / 打者交代 ----
    def reset_count(self):
        self.balls = 0
        self.strikes = 0

    def next_batter(self):
        self._remember_batter_history()
        self.batters_faced += 1
        self.reset_count()
        self.history.clear()
        self.lineup.advance()
        self.opp_tactic = None           # 作戦は打者ごとに決め直す
        self.tactic_decided = False

    def _remember_batter_history(self):
        """次の打者へ移る前に、今の打者に見せた球種の傾向を打席をまたいで覚えておく。

        打者一巡などで同じ打者と再び対戦したとき、predict_guess() がこれを見て
        『前の打席で速球を多く見せられた』のような記憶を薄く反映する。
        """
        counts = self.history.recent_class_counts(len(self.history.records))
        if counts["fastball"] + counts["offspeed"] == 0:
            return
        mem = self.batter_pitch_memory.setdefault(id(self.batter), {"fastball": 0, "offspeed": 0})
        mem["fastball"] += counts["fastball"]
        mem["offspeed"] += counts["offspeed"]

    def batter_memory(self, batter):
        """この半イニングで、この打者の過去の打席に見せた球種の内訳(なければ None)。"""
        return self.batter_pitch_memory.get(id(batter))

    def add_out(self):
        self.outs += 1
        if self.outs < 3:
            self.next_batter()

    # ---- 失点(相手が打っている) ----
    def _score_runs(self, n):
        self.opp_score += n
        self.runs_this_inning += n

    def apply_play(self, play):
        """baserunning.resolve() の結果(フェア打球のあとの走塁)をまとめて反映する。"""
        self.runners = list(play["new_runners"])
        self.runner_speeds = list(play.get("new_runner_speeds", [None, None, None]))
        self._score_runs(play["runs"])
        self.outs = min(3, self.outs + play["outs_added"])
        if not self.is_over():
            self.next_batter()

    def advance_on_walk(self):
        """四球/敬遠/死球。詰まっている走者だけが押し出される(脚力も一緒に運ぶ)。"""
        r, sp, b = self.runners, self.runner_speeds, self.batter.speed
        if r[0] and r[1] and r[2]:
            self._score_runs(1)                        # 満塁押し出し(三塁走者が生還)
            self.runners = [True, True, True]
            self.runner_speeds = [b, sp[0], sp[1]]
        elif r[0] and r[1]:
            self.runners = [True, True, True]
            self.runner_speeds = [b, sp[0], sp[1]]
        elif r[0]:
            self.runners = [True, True, bool(r[2])]
            self.runner_speeds = [b, sp[0], sp[2]]
        else:
            self.runners = [True, bool(r[1]), bool(r[2])]
            self.runner_speeds = [b, sp[1], sp[2]]
        self.next_batter()

    # ---- 終了判定 ----
    def is_walkoff(self):
        """相手が裏の攻撃で勝ち越したか(サヨナラ)。

        元々リード or 同点で守っていて、相手(=裏の攻撃側)が勝ち越したら、
        3 アウトを待たずにその場で試合が決まる。開始時点で既にビハインドの
        場面(逆転される『前』が無い)には当てはまらない。
        """
        return (self.half == "bottom"
                and (self.start_our - self.start_opp) >= 0
                and self.opp_score > self.our_score)

    def is_over(self):
        return self.outs >= 3 or self.is_walkoff()

    def game_over(self):
        return self.is_over()

    # ---- 表示・集計 ----
    def half_label(self):
        return "表" if self.half == "top" else "裏"

    def inning_label(self):
        if self.is_extra:
            return f"延長{self.inning}回{self.half_label()}"
        return f"{self.inning}回{self.half_label()}"

    def runners_text(self):
        labels = ["1B", "2B", "3B"]
        parts = []
        for i in range(3):
            if not self.runners[i]:
                continue
            tag = "▶" if self.runner_speed_at(i) >= 70 else ""   # ▶ = 俊足(盗塁・進塁の脅威)
            parts.append(labels[i] + tag)
        return "なし" if not parts else "・".join(parts)

    def result_summary(self):
        runs = self.runs_this_inning
        run_txt = "無失点" if runs == 0 else ("1失点" if runs == 1 else f"{runs}失点（複数失点）")

        start_lead = self.start_our - self.start_opp
        end_lead = self.our_score - self.opp_score
        if start_lead > 0:
            if end_lead > 0:
                status = "リード維持"
            elif end_lead == 0:
                status = "同点に追いつかれた"
            else:
                status = "逆転された"
        elif start_lead == 0:
            status = "同点を維持" if end_lead == 0 else ("勝ち越された" if end_lead < 0 else "勝ち越した")
        else:  # 開始時ビハインド
            if end_lead >= 0:
                status = "追いついた・勝ち越した"
            elif end_lead > start_lead:
                status = "さらに離された" if runs else "点差そのまま"
            else:
                status = "点差そのまま" if runs == 0 else "さらに離された"
        return run_txt, status


def _weighted(rng, choices, weights):
    return rng.choices(choices, weights=weights, k=1)[0]


def generate_random_situation(rng=None, lineup_batters=None):
    """毎回違う「終盤の 1 場面」を作る。

    lineup_batters : 9 人の Batter を渡すと、その打線を使う(#C 実データ差し替え)。
                     省略すると build_sample_lineup() の架空 9 人。
    """
    rng = rng or random

    inning = _weighted(rng, INNING_CHOICES, INNING_WEIGHTS)
    is_extra = inning >= 10
    half = rng.choice(["top", "bottom"])
    we_are = "HOME" if half == "top" else "AWAY"   # 守るのは自軍

    diff = _weighted(rng, DIFF_CHOICES, DIFF_WEIGHTS)
    opp_base = rng.randint(1, 5)
    our_score = opp_base + diff
    opp_score = opp_base
    if our_score < 0:                # 0 を下回らないよう両方を持ち上げる
        opp_score -= our_score
        our_score = 0

    outs = _weighted(rng, OUT_CHOICES, OUT_WEIGHTS)
    on_base_p = 0.30 if outs == 0 else 0.38
    runners = [rng.random() < on_base_p for _ in range(3)]
    # 出ている走者の脚力(35〜85)。時々かなりの俊足(盗塁の脅威)がまじる。
    runner_speeds = [rng.randint(35, 85) if runners[i] else None for i in range(3)]

    start_index = rng.randrange(9)
    batters = lineup_batters if lineup_batters is not None else build_sample_lineup(rng)
    lineup = Lineup(list(batters), index=start_index)
    pitcher = build_sample_pitcher(rng, name="リリーフ1")
    defense = make_default_defense(rng)   # 守備能力もシードに乗せる(--seed 再現性)

    state = MatchState(inning, is_extra, half, we_are, our_score, opp_score,
                       outs, runners, lineup, pitcher, defense, runner_speeds=runner_speeds)
    bullpen_size = _weighted(rng, [1, 2, 3], [2, 5, 3])   # ブルペンには 1〜3 人待機
    state.bullpen = [build_sample_pitcher(rng, name=f"リリーフ{i + 2}")
                     for i in range(bullpen_size)]
    return state
