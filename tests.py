"""自動テスト。

    python tests.py

標準ライブラリの unittest だけ。乱数は random.Random(seed) で固定。
最後の StressTests で「ランダム条件 1000 ゲーム」を回し、
クラッシュ・不正カウント・打順破綻・無限ループが無いことを確認する。
"""

import io
import random
import re
import socket
import socketserver
import threading
import unittest
import urllib.error
import urllib.parse
import urllib.request
from contextlib import redirect_stdout
from unittest import mock

from baserunning import resolve as resolve_baserunning
from batted_ball import BattedBall, generate_batted_ball
from batter import Batter
from constants import COURSE_SHORT, COURSES
from defense import ALIGNMENTS, Defense, make_default_defense
from fielders import POSITIONS, Fielder, make_default_fielders, position_fit, weakest_fielder
from fielding import resolve_batted_ball
from ballpark_view import render_field
from judge import judge_pitch
from lineup import Lineup, build_sample_lineup, load_lineup_file
from engine import log_pitch, maybe_resolve_sign_steal, resolve_one_pitch
from match_state import generate_random_situation
from pitch_data import (
    PITCH_LIBRARY,
    all_pitch_keys,
    family_of,
    get_pitch,
    guess_class_of,
    pitch_name,
    repertoire_options,
    velocity_of,
)
from pitcher import Pitcher, build_sample_pitcher
from qte import catcher_block, catcher_change_signs, catcher_throw, qte_enabled
from reaction import FACE_CATEGORIES, describe_reaction, render_reaction_block
import mlb_data_adapter
import replay
import stats
from strategy import PITCH_INTENTS, build_analysis, build_postgame_report, evaluate_sequencing, grade_reads
from ui import render_analysis, render_dashboard, render_intro, render_play_result, render_spray
import webapp

VALID_RESULTS = {"ストライク", "ボール", "空振り", "ファウル", "アウト",
                 "単打", "二塁打", "三塁打", "本塁打"}


# ---------- ヘルパー ----------
def _batter(bats="R", coarse="contact", pull=0.5, gb=0.45, **kw):
    return Batter(name="T", bats=bats, coarse_type=coarse, pull=pull, gb_tendency=gb, **kw)


def _defense_all_equal(skill=55):
    return Defense([Fielder(f"g{i}", skill, skill, skill, skill, skill) for i in range(7)])


def _state(seed=0):
    return generate_random_situation(random.Random(seed))


def _silent_apply(state, outcome):
    result = outcome["result"]
    if result in ("ストライク", "空振り"):
        state.strikes += 1
        if state.strikes >= 3:
            state.add_out()
    elif result == "ボール":
        state.balls += 1
        if state.balls >= 4:
            state.advance_on_walk()
    elif result == "ファウル":
        if state.strikes < 2:
            state.strikes += 1
    else:  # フェア打球(単打/二塁打/三塁打/本塁打/アウト)
        state.apply_play(outcome["play"])


def _drive_pitch(state, rng, intents, courses):
    pt = rng.choice(list(repertoire_options(state.pitcher)))
    co = rng.choice(courses)
    it = rng.choice(intents)
    outcome = judge_pitch(state, pt, co, rng, intent=it)
    outcome["reaction"] = describe_reaction(pt, outcome["actual_course"], outcome, rng)
    log_pitch(state, outcome)
    state.history.add(pt, co, outcome["result"], outcome["timing"], outcome["swung"],
                      actual_course=outcome["actual_course"], intent=it,
                      velocity=velocity_of(pt), family=family_of(pt), in_zone=outcome["in_zone"])
    _silent_apply(state, outcome)
    return outcome


# ---------- 球種データ ----------
class PitchDataTests(unittest.TestCase):
    def test_entries_have_required_fields(self):
        required = {"name", "pitch_class", "velocity", "movement", "whiff_rate",
                    "groundball_rate", "contact_quality", "platoon"}
        for key, data in PITCH_LIBRARY.items():
            self.assertTrue(required.issubset(data), key)
            self.assertIn(data["pitch_class"], ("fastball", "breaking", "offspeed"))

    def test_guess_class_two_way(self):
        for key in all_pitch_keys():
            self.assertIn(guess_class_of(key), ("fastball", "offspeed"))

    def test_new_pitch_by_data_only(self):
        PITCH_LIBRARY["test_pitch"] = {
            "name": "テスト球", "pitch_class": "breaking", "velocity": 80, "movement": 5,
            "whiff_rate": 0.2, "groundball_rate": 0.5, "contact_quality": 0.4,
            "platoon": {"same": -0.05, "opposite": 0.05}}
        try:
            state = _state(1)
            out = judge_pitch(state, "test_pitch", "mid_mid", random.Random(0))
            self.assertIn(out["result"], VALID_RESULTS)
        finally:
            del PITCH_LIBRARY["test_pitch"]


# ---------- 野手・守備 ----------
class FielderTests(unittest.TestCase):
    def test_fit_amplified_at_hard_position(self):
        below = Fielder("x", 40, 40, 40, 40, 40)
        above = Fielder("y", 65, 65, 65, 65, 65)
        self.assertLess(position_fit(below, "SS"), position_fit(below, "1B"))
        self.assertGreater(position_fit(above, "SS"), position_fit(above, "1B"))

    def test_one_clear_liability(self):
        fs = make_default_fielders(random.Random(3))
        self.assertEqual(len(fs), 7)
        overalls = sorted(f.overall() for f in fs)
        self.assertLess(overalls[0], overalls[1] - 8)
        self.assertIs(weakest_fielder(fs), min(fs, key=lambda f: f.overall()))


class DefenseTests(unittest.TestCase):
    def test_swap_permutation(self):
        d = make_default_defense()
        before = {id(f) for f in d.assignment.values()}
        d.swap("SS", "1B")
        self.assertEqual(before, {id(f) for f in d.assignment.values()})

    def test_alignment_validates(self):
        d = make_default_defense()
        for key in ALIGNMENTS:
            d.set_alignment(key)
        with self.assertRaises(ValueError):
            d.set_alignment("nonsense")

    def test_ede_rewards_hiding_weak_at_1b(self):
        good = [Fielder(f"s{i}", 62, 62, 62, 62, 62) for i in range(6)]
        weak = Fielder("W", 30, 30, 30, 30, 30)
        at_1b = Defense(good + [weak])
        at_1b.assignment = dict(zip(POSITIONS, good + [weak]))
        at_1b.assignment["1B"], at_1b.assignment["RF"] = at_1b.assignment["RF"], at_1b.assignment["1B"]
        at_ss = Defense(good + [weak])
        at_ss.assignment = dict(zip(POSITIONS, good + [weak]))
        at_ss.assignment["SS"], at_ss.assignment["RF"] = at_ss.assignment["RF"], at_ss.assignment["SS"]
        for d in (at_1b, at_ss):
            self.assertTrue(0 <= d.expected_defensive_efficiency() <= 100)
        self.assertGreater(at_1b.expected_defensive_efficiency(),
                           at_ss.expected_defensive_efficiency())

    def test_bunt_alignment_helps_soft_grounders_hurts_hard_ones(self):
        d = _defense_all_equal()
        soft = BattedBall("pull", "ground", "soft", 0.1, "infield")
        hard = BattedBall("pull", "ground", "hard", 0.9, "infield")
        d.set_alignment("standard")
        self.assertEqual(d.alignment_out_adjust("3B", soft), 0.0)
        d.set_alignment("bunt")
        self.assertGreater(d.alignment_out_adjust("3B", soft), 0.0)
        self.assertLess(d.alignment_out_adjust("3B", hard), 0.0)
        self.assertEqual(d.alignment_out_adjust("CF", soft), 0.0)   # 外野は影響なし


# ---------- 打球・守備判定 ----------
class BattedBallTests(unittest.TestCase):
    def _pull_share(self, batter, timing="on_time", course="mid_mid", n=4000):
        rng = random.Random(11)
        return sum(generate_batted_ball(batter, get_pitch("four_seam"), course, timing, 0.0, rng)
                   .direction == "pull" for _ in range(n)) / n

    def test_pull_tendency_and_timing(self):
        self.assertGreater(self._pull_share(_batter(pull=0.85)), self._pull_share(_batter(pull=0.30)))
        b = _batter(pull=0.5)
        self.assertGreater(self._pull_share(b, timing="early"), self._pull_share(b, timing="on_time"))


class FieldingTests(unittest.TestCase):
    def _out_rate(self, bb, defense, batter, n=4000, seed=5):
        rng = random.Random(seed)
        return sum(resolve_batted_ball(bb, defense, batter, rng)["result"] == "アウト"
                   for _ in range(n)) / n

    def test_hard_contact_lowers_out_rate(self):
        d, b = _defense_all_equal(), _batter()
        soft = BattedBall("center", "ground", "soft", 0.2, "infield")
        hard = BattedBall("center", "ground", "hard", 0.9, "infield")
        self.assertGreater(self._out_rate(soft, d, b), self._out_rate(hard, d, b))

    def test_alignment_match_raises_out_rate(self):
        b = _batter(bats="R")
        matched = _defense_all_equal(); matched.set_alignment("pull")
        bb = BattedBall("pull", "ground", "medium", 0.5, "infield")
        self.assertGreater(self._out_rate(bb, matched, b), self._out_rate(bb, _defense_all_equal(), b))

    def test_hide_weak_fielder_depends_on_handedness(self):
        weak = Fielder("W", 26, 26, 26, 26, 26)

        def out_rate(bats, weak_pos, n=4000):
            d = _defense_all_equal(55)
            d.assignment[weak_pos] = weak
            b = _batter(bats=bats, coarse="power", pull=0.85)
            rng = random.Random(999)
            return sum(resolve_batted_ball(
                generate_batted_ball(b, get_pitch("four_seam"), "mid_mid", "on_time", 0.0, rng),
                d, b, rng)["result"] == "アウト" for _ in range(n)) / n

        self.assertGreater(out_rate("R", "RF"), out_rate("R", "LF") + 0.015)
        self.assertGreater(out_rate("L", "LF"), out_rate("L", "RF") + 0.015)

    def test_fast_runner_beats_out_grounders(self):
        d = _defense_all_equal()
        slow = _batter(coarse="power"); slow.speed = 30
        fast = _batter(coarse="power"); fast.speed = 80
        bb = BattedBall("center", "ground", "medium", 0.5, "infield")
        self.assertGreater(self._out_rate(bb, d, slow), self._out_rate(bb, d, fast))

    def test_weak_fielder_commits_more_errors(self):
        def error_rate(skill, n=6000):
            d, b = _defense_all_equal(skill), _batter()
            bb = BattedBall("center", "ground", "medium", 0.5, "infield")
            rng = random.Random(3)
            return sum(resolve_batted_ball(bb, d, b, rng)["result"] == "エラー"
                       for _ in range(n)) / n
        self.assertGreater(error_rate(25), error_rate(75) + 0.01)

    def test_error_is_never_scored_as_an_out(self):
        d, b = _defense_all_equal(20), _batter()
        bb = BattedBall("center", "ground", "hard", 0.9, "infield")
        rng = random.Random(11)
        for _ in range(2000):
            fd = resolve_batted_ball(bb, d, b, rng)
            if fd["result"] == "エラー":
                self.assertFalse(fd["batter_out"])
                self.assertFalse(fd["air_out"])
                self.assertTrue(fd["error"])


# ---------- 長打・走塁 (改良A) ----------
def _fd(result, position="SS", batter_out=None, air_out=False):
    if batter_out is None:
        batter_out = (result == "アウト")
    return {"result": result, "batter_out": batter_out, "air_out": air_out,
            "position": position, "fielder": "X", "out_probability": 0.5,
            "breakdown": {"alignment": 0.0}}


class BaserunningTests(unittest.TestCase):
    def test_hit_extent_homerun_needs_hard_deep_air(self):
        from fielding import _hit_extent
        slugger = _batter(coarse="power"); slugger.power = 0.9
        rng = random.Random(0)
        hard_deep = BattedBall("pull", "fly", "hard", 0.9, "deep")
        soft_grounder = BattedBall("pull", "ground", "soft", 0.2, "infield")
        hr = sum(_hit_extent(hard_deep, slugger, rng) == "本塁打" for _ in range(3000))
        never = sum(_hit_extent(soft_grounder, slugger, rng) == "本塁打" for _ in range(3000))
        self.assertGreater(hr, 200)
        self.assertEqual(never, 0)

    def test_double_clears_the_bases(self):
        play = resolve_baserunning(BattedBall("pull", "line", "hard", 0.8, "deep"),
                                   _fd("二塁打"), [True, True, True], 0, _batter(),
                                   random.Random(0))
        self.assertEqual(play["runs"], 3)
        self.assertEqual(play["new_runners"], [False, True, False])
        self.assertEqual(play["outs_added"], 0)

    def test_error_advances_batter_and_runners_like_a_single(self):
        play = resolve_baserunning(BattedBall("center", "ground", "hard", 0.8, "infield"),
                                   _fd("エラー", "SS"), [False, False, True], 0, _batter(),
                                   random.Random(0))
        self.assertEqual(play["batter_result"], "エラー")
        self.assertEqual(play["label"], "エラー")
        self.assertEqual(play["outs_added"], 0)
        self.assertTrue(play["new_runners"][0])          # 打者は一塁へ
        self.assertEqual(play["runs"], 1)                # 三塁走者は生還

    def test_double_play_removes_first_runner_and_adds_two_outs(self):
        bb = BattedBall("center", "ground", "medium", 0.5, "infield")
        got_dp = 0
        for s in range(400):
            play = resolve_baserunning(bb, _fd("アウト", "SS"), [True, False, False], 0,
                                       _batter(coarse="average"), random.Random(s))
            if play["outs_added"] == 2:
                got_dp += 1
                self.assertFalse(play["new_runners"][0])
                self.assertEqual(play["label"], "併殺")
        self.assertGreater(got_dp, 100)   # そこそこ起きる

    def test_sac_fly_scores_from_third(self):
        bb = BattedBall("center", "fly", "medium", 0.5, "deep")
        runs = 0
        for s in range(300):
            play = resolve_baserunning(bb, _fd("アウト", "CF", air_out=True),
                                       [False, False, True], 0, _batter(), random.Random(s))
            runs += play["runs"]
            if play["runs"]:
                self.assertEqual(play["label"], "犠飛")
                self.assertEqual(play["outs_added"], 1)
        self.assertGreater(runs, 150)

    def test_no_runs_when_play_makes_third_out(self):
        # 2 アウトから併殺 → 3 アウト目成立、得点は数えない
        bb = BattedBall("center", "ground", "medium", 0.5, "infield")
        for s in range(200):
            play = resolve_baserunning(bb, _fd("アウト", "SS"), [True, False, True], 2,
                                       _batter(), random.Random(s))
            if play["outs_added"] >= 1:
                self.assertEqual(play["runs"], 0)

    def test_single_with_runner_on_second_scores_more_from_outfield(self):
        def score_rate(ball_type):
            bb = BattedBall("center", ball_type, "medium", 0.5,
                            "shallow" if ball_type != "ground" else "infield")
            return sum(resolve_baserunning(bb, _fd("単打", "CF"), [False, True, False], 1,
                                           _batter(), random.Random(s))["runs"]
                       for s in range(500)) / 500
        self.assertGreater(score_rate("line"), score_rate("ground"))


# ---------- 試合状況の生成 ----------
class SituationTests(unittest.TestCase):
    def test_random_situation_valid(self):
        rng = random.Random(0)
        for _ in range(500):
            s = generate_random_situation(rng)
            self.assertIn(s.inning, (7, 8, 9, 10))
            self.assertIn(s.outs, (0, 1, 2))
            self.assertGreaterEqual(s.our_score, 0)
            self.assertGreaterEqual(s.opp_score, 0)
            self.assertTrue(1 <= s.lineup.spot_number() <= 9)
            self.assertEqual(len(s.runners), 3)

    def test_close_games_more_common(self):
        rng = random.Random(1)
        close = blowout = 0
        for _ in range(3000):
            s = generate_random_situation(rng)
            d = abs(s.our_score - s.opp_score)
            close += d <= 1
            blowout += d >= 3
        self.assertGreater(close, blowout * 2)

    def test_lineup_wraps_like_real_baseball(self):
        lu = Lineup(build_sample_lineup(random.Random(0)), index=6)   # 7番から
        spots = []
        for _ in range(11):
            spots.append(lu.spot_number())
            lu.advance()
        self.assertEqual(spots, [7, 8, 9, 1, 2, 3, 4, 5, 6, 7, 8])

    def test_nine_distinct_batters(self):
        lu = build_sample_lineup(random.Random(0))
        self.assertEqual(len(lu), 9)
        # 打者タイプがちゃんとばらけている
        self.assertGreaterEqual(len({b.coarse_type for b in lu}), 4)
        # 出塁型と長打型で discipline がはっきり違う
        self.assertGreater(lu[7].discipline, lu[4].discipline)

    def _defending(self, half, our, opp, start_our=None, start_opp=None):
        st = generate_random_situation(random.Random(0))
        st.half = half
        st.our_score, st.opp_score = our, opp
        st.start_our = our if start_our is None else start_our
        st.start_opp = opp if start_opp is None else start_opp
        st.outs, st.runners, st.runner_speeds = 0, [False, False, False], [None, None, None]
        return st

    def test_walkoff_ends_the_half_inning_immediately(self):
        """相手が裏の攻撃で勝ち越したら、3 アウトを待たずに試合終了。"""
        st = self._defending("bottom", our=3, opp=2)     # 1点リードで守っている
        self.assertFalse(st.is_over())
        st._score_runs(2)                                # 相手が2点 → 3-4 で勝ち越される
        self.assertTrue(st.is_walkoff())
        self.assertTrue(st.is_over())
        self.assertEqual(st.outs, 0)

    def test_no_walkoff_in_the_top_half(self):
        """表の攻撃(相手が先攻)は勝ち越されても試合は続く。"""
        st = self._defending("top", our=3, opp=2)
        st._score_runs(5)
        self.assertFalse(st.is_over())

    def test_no_walkoff_when_already_behind_at_the_start(self):
        """開始時点で既にビハインドなら『逆転』ではないので短絡終了しない。"""
        st = self._defending("bottom", our=2, opp=3)
        self.assertFalse(st.is_over())

    def test_tie_game_walkoff(self):
        st = self._defending("bottom", our=4, opp=4)
        st._score_runs(1)
        self.assertTrue(st.is_over())

    def test_same_seed_reproduces_the_defense_too(self):
        """--seed の再現性: 守備 7 人の能力・弱点も含めて完全に同じ場面になる
        （make_default_defense に rng を渡し忘れると global random に乗って崩れる）。"""
        import random as _r

        def fielder_stats(seed):
            st = generate_random_situation(rng=_r.Random(seed))
            return [(f.name, f.range, f.hands, f.arm, f.reaction, f.speed)
                    for f in st.defense.fielders]

        _r.seed()                                  # global random をかき混ぜる
        a = fielder_stats(31337)
        _r.seed()
        b = fielder_stats(31337)
        self.assertEqual(a, b)
        self.assertNotEqual(a, fielder_stats(31338))


# ---------- 投手の実投球 (Pitch Execution) ----------
class ExecutionTests(unittest.TestCase):
    def test_can_hit_and_miss_spot(self):
        p = build_sample_pitcher(random.Random(0))
        rng = random.Random(0)
        hit = miss = 0
        for _ in range(2000):
            a = p.execute_pitch(p.repertoire[0], "mid_lo", "chase", rng)
            self.assertTrue(0.0 <= a["quality"] <= 1.0)
            self.assertIn(a["actual_course"], COURSES)
            if a["missed"]:
                miss += 1
            else:
                hit += 1
        self.assertGreater(hit, 0)
        self.assertGreater(miss, 0)

    def test_low_control_misses_more(self):
        wild = Pitcher(control=45, command=42, repertoire=["four_seam"])
        sharp = Pitcher(control=75, command=75, repertoire=["four_seam"])

        def miss_rate(p):
            rng = random.Random(7)
            return sum(p.execute_pitch("four_seam", "out_mid", "freeze", rng)["missed"]
                       for _ in range(4000)) / 4000

        self.assertGreater(miss_rate(wild), miss_rate(sharp) + 0.03)

    def test_return_tempo_helps_execution(self):
        """捕手の返球リズム(tempo)が良いほど失投が減り球威が上がる。0.0 は無影響。"""
        p = Pitcher(control=55, command=55, stuff=55, velocity=55, repertoire=["four_seam"])

        def stats(tempo, n=6000):
            miss = q = 0
            for s in range(n):
                p.pitches_thrown = 0
                r = p.execute_pitch("four_seam", "out_lo", "strike", random.Random(s), tempo=tempo)
                miss += r["missed"]; q += r["quality"]
            return miss / n, q / n

        bad_miss, bad_q = stats(-0.25)
        neu_miss, neu_q = stats(0.0)
        good_miss, good_q = stats(0.25)
        self.assertGreater(bad_miss, good_miss)          # 悪いテンポは失投が増える
        self.assertGreater(good_q, bad_q)               # 良いテンポは球威が上がる
        self.assertAlmostEqual(neu_q, (bad_q + good_q) / 2, delta=0.02)


# ---------- 顔文字つき反応 ----------
class ReactionTests(unittest.TestCase):
    def _mk_outcome(self, timing="late", swung=True, in_zone=True, result="ファウル"):
        return {"timing": timing, "swung": swung, "in_zone": in_zone, "result": result}

    def test_returns_structured_dict(self):
        r = describe_reaction("slider", "in_mid", self._mk_outcome(), random.Random(0))
        self.assertEqual(set(r), {"face", "ascii_face", "text", "category", "kind"})
        self.assertIn(r["category"], FACE_CATEGORIES)
        self.assertTrue(r["face"])
        self.assertTrue(r["ascii_face"].isascii())      # ASCII フォールバックは純ASCII

    def test_face_not_deterministic_for_state(self):
        rng = random.Random(0)
        oc = self._mk_outcome(timing="late")
        faces, cats, kinds = set(), set(), set()
        for _ in range(400):
            r = describe_reaction("four_seam", "in_mid", oc, rng)
            faces.add(r["face"]); cats.add(r["category"]); kinds.add(r["kind"])
        self.assertGreaterEqual(len(faces), 4)           # 同じ状態でも顔文字は複数
        self.assertGreaterEqual(len(cats), 3)            # ミスリード/ぼかしで散る
        self.assertEqual(kinds, {"reveal", "mislead", "ambiguous"})

    def test_ascii_fallback_uses_ascii_face(self):
        r = describe_reaction("slider", "mid_lo", self._mk_outcome(), random.Random(1))
        ascii_block = render_reaction_block(r, ascii_only=True)
        kaomoji_block = render_reaction_block(r, ascii_only=False)
        # ASCII 版は顔文字の行が純 ASCII / 顔文字版は kaomoji を含む
        face_line = ascii_block.splitlines()[1]
        self.assertTrue(face_line.isascii())
        self.assertIn(r["ascii_face"], ascii_block)
        self.assertIn(r["face"], kaomoji_block)


# ---------- 打席をまたぐ配球バレ(打者の記憶) ----------
class BatterMemoryTests(unittest.TestCase):
    def test_batter_memory_is_none_until_recorded(self):
        st = _state(5)
        self.assertIsNone(st.batter_memory(st.batter))

    def test_next_batter_remembers_pitch_tendency_until_batting_around(self):
        st = _state(5)
        batter = st.batter
        for _ in range(6):
            st.history.add(pitch_type="four_seam", course="mid_mid", result="ストライク",
                           timing="on_time", swung=False, family="fastball", in_zone=True)
        for _ in range(9):                    # 打者一巡させて同じ打者に戻す
            st.next_batter()
        self.assertIs(st.batter, batter)
        mem = st.batter_memory(batter)
        self.assertEqual(mem, {"fastball": 6, "offspeed": 0})

    def test_predict_guess_leans_toward_remembered_tendency(self):
        st = _state(6)
        batter = st.batter

        def signed_lean(guess):
            return guess["class_strength"] if guess["class"] == "fastball" else -guess["class_strength"]

        without = signed_lean(batter.predict_guess(st.history, st, st.pitcher))
        st.batter_pitch_memory[id(batter)] = {"fastball": 20, "offspeed": 0}
        leans_fastball = signed_lean(batter.predict_guess(st.history, st, st.pitcher))
        self.assertGreater(leans_fastball, without)

        st.batter_pitch_memory[id(batter)] = {"fastball": 0, "offspeed": 20}
        leans_offspeed = signed_lean(batter.predict_guess(st.history, st, st.pitcher))
        self.assertLess(leans_offspeed, without)


# ---------- 配球セットアップ / 判断の質 ----------
class StrategyTests(unittest.TestCase):
    def _seq(self, records, call):
        state = _state(3)
        for rec in records:
            state.history.add(**rec)
        batter = state.batter
        guess = batter.predict_guess(state.history, state, state.pitcher)
        actual = {"pitch_type": call["pitch_type"], "target_course": call["target_course"],
                  "actual_course": call["target_course"], "quality": 0.5,
                  "missed": False, "miss_kind": None}
        return evaluate_sequencing(state.history, call, actual, batter, guess, state)

    def test_sequencing_is_conditional_not_flat(self):
        call = {"pitch_type": "four_seam", "target_course": "mid_hi", "intent": "chase"}
        low_before = [{"pitch_type": "four_seam", "course": "mid_lo", "result": "ストライク",
                       "timing": "on_time", "swung": False, "actual_course": "mid_lo",
                       "velocity": 95, "family": "fastball", "in_zone": True}]
        with_ladder = self._seq(low_before, call)
        without = self._seq([], call)
        self.assertNotEqual(with_ladder["notes"], without["notes"])
        self.assertLessEqual(abs(with_ladder["whiff_adj"]), 0.15)   # 固定大ボーナスにしない

    def test_repeating_same_pitch_hurts(self):
        call = {"pitch_type": "slider", "target_course": "out_mid", "intent": "chase"}
        repeated = [{"pitch_type": "slider", "course": "out_mid", "result": "ボール",
                     "timing": "on_time", "swung": False, "actual_course": "out_mid",
                     "velocity": 85, "family": "breaking", "in_zone": False}]
        seq = self._seq(repeated, call)
        self.assertLess(seq["dq_seq"], 0)
        self.assertLess(seq["contact_adj"] * -1, 0.001 + 0)  # contact_adj は + 方向(打たれやすく)

    def test_analysis_separates_result_and_judgment(self):
        log = [
            {"pitch_number": 1, "call_label": "a", "result": "単打",
             "decision_quality": 0.5, "sequence_label": "x",
             "guess": {"class_strength": 0.6}, "fooled_guess": True,
             "alignment_helped": False, "missed": False,
             "outcome_flags": {"swung": True, "in_zone": True}},
            {"pitch_number": 2, "call_label": "b", "result": "アウト",
             "decision_quality": -0.5, "sequence_label": "y",
             "guess": {"class_strength": 0.6}, "fooled_guess": False,
             "alignment_helped": False, "missed": False,
             "outcome_flags": {"swung": True, "in_zone": True}},
        ]
        a = build_analysis(log)
        self.assertEqual(len(a["unlucky"]), 1)   # 良い判断・悪い結果
        self.assertEqual(len(a["lucky"]), 1)     # 悪い判断・良い結果


# ---------- judge 結合 / 内部指標の秘匿 ----------
class IntegrationTests(unittest.TestCase):
    def test_batted_ball_only_on_fair(self):
        state = _state(4)
        rng = random.Random(0)
        seen = set()
        fair = {"アウト", "エラー", "単打", "二塁打", "三塁打", "本塁打"}
        for _ in range(3000):
            o = judge_pitch(state, "four_seam", "mid_mid", rng, intent="strike")
            seen.add(o["result"])
            if o["result"] in fair:
                self.assertIsNotNone(o["batted_ball"])
                self.assertIn("breakdown", o["fielding"])
                self.assertIsNotNone(o["play"])
            else:
                self.assertIsNone(o["batted_ball"])
        self.assertIn("アウト", seen)
        self.assertTrue(seen & {"単打", "二塁打", "三塁打", "本塁打"})

    def test_decision_quality_hidden_from_live_ui(self):
        state = _state(5)
        rng = random.Random(0)
        last = None
        for _ in range(6):
            last = _drive_pitch(state, rng, list(PITCH_INTENTS), list(COURSES))
            if state.is_over():
                break
        dash = render_dashboard(state, last)
        play = render_play_result(last)
        dq = str(last["_analysis"]["decision_quality"])
        for text in (dash, play):
            self.assertNotIn("decision", text.lower())
            self.assertNotIn("判断の質", text)
            self.assertNotIn(dq, text)

    def test_postgame_report_uses_localized_pitch_and_course_names(self):
        """ふり返り画面(CLI/Web共通)に "sinker/mid_mid" のような内部キーの
        生の文字列ではなく、日本語の球種名・コース短縮表記が出ること。
        call_label(stats.py が解析する内部表記)自体は変えない。
        """
        state = _state(11)
        rng = random.Random(11)
        while not state.is_over():
            resolve_one_pitch(state, "sinker", "mid_mid", "strike", rng)
        entry = state.pitch_log[0]
        self.assertEqual(entry["call_label"], f"sinker/mid_mid({PITCH_INTENTS['strike'].split('（')[0]})")
        self.assertIn(pitch_name("sinker"), entry["call_label_ja"])
        self.assertIn(COURSE_SHORT["mid_mid"], entry["call_label_ja"])
        self.assertNotIn("sinker", entry["call_label_ja"])
        self.assertNotIn("mid_mid", entry["call_label_ja"])

    def test_reaction_does_not_print_hidden_pitch_key(self):
        state = _state(6)
        rng = random.Random(1)
        for _ in range(300):
            o = judge_pitch(state, "slider", "mid_lo", rng)
            r = describe_reaction("slider", o["actual_course"], o, rng)
            self.assertNotIn(state.batter.weak_pitch, r["text"])

    def test_endgame_autoplay_one_game(self):
        state = _state(7)
        rng = random.Random(7)
        guard = 0
        while not state.is_over():
            guard += 1
            self.assertLess(guard, 400)
            _drive_pitch(state, rng, list(PITCH_INTENTS), list(COURSES))
        self.assertTrue(state.is_over())
        with redirect_stdout(io.StringIO()):
            build_analysis(state.pitch_log)
        run_txt, status = state.result_summary()
        self.assertTrue(run_txt and status)
        # 3 アウト、またはサヨナラで決着していること
        self.assertTrue(state.outs == 3 or state.is_walkoff())

    def test_same_seed_gives_same_engine_result_cli_and_web_share(self):
        """CLI(main.py)・Web(webapp.py)はどちらも engine.resolve_one_pitch() を
        直接呼ぶだけなので、この関数自体が seed で再現できれば両方とも再現できる。
        """
        def play(seed):
            rng = random.Random(seed)
            state = generate_random_situation(rng=rng)
            calls = [("four_seam", "in_hi", "freeze"), ("slider", "out_lo", "chase"),
                    ("changeup", "mid_lo", "weak_contact")]
            out = []
            for pt, co, it in calls:
                if state.is_over():
                    break
                outcome, msgs = resolve_one_pitch(state, pt, co, it, rng)
                out.append((outcome["result"], outcome["actual_course"], outcome["quality"]))
            return out, state.our_score, state.opp_score, state.outs

        self.assertEqual(play(4242), play(4242))
        self.assertNotEqual(play(4242), play(4243))

    def test_defense_swap_changes_who_fields_the_ball(self):
        """Swap後は実際の判定(fielding)にも新しい配置がそのまま使われる。"""
        state = _state(8)
        before = state.defense.fielder_at("SS")
        other = state.defense.fielder_at("1B")
        state.defense.swap("SS", "1B")
        self.assertIs(state.defense.fielder_at("SS"), other)
        self.assertIs(state.defense.fielder_at("1B"), before)
        # 判定チェーン(fielding.resolve_batted_ball)はDefenseを直接参照するので、
        # 同じオブジェクトを見ている以上、Swap後の配置がそのまま使われる
        bb = BattedBall("center", "ground", "medium", 0.5, "infield")
        fd = resolve_batted_ball(bb, state.defense, state.batter, random.Random(0))
        if fd["position"] in ("SS", "1B"):
            self.assertEqual(fd["fielder"], state.defense.fielder_at(fd["position"]).name)

    def test_postgame_report_separates_result_from_judgment(self):
        from strategy import build_postgame_report
        state = _state(9)
        rng = random.Random(9)
        guard = 0
        while not state.is_over():
            guard += 1
            self.assertLess(guard, 400)
            pt = rng.choice(list(repertoire_options(state.pitcher)))
            co = rng.choice(list(COURSES))
            it = rng.choice(list(PITCH_INTENTS))
            resolve_one_pitch(state, pt, co, it, rng)
        report = build_postgame_report(state)
        for key in ("runs_allowed", "batters_faced", "pitches", "strikeouts", "walks", "hits"):
            self.assertIn(key, report)
        self.assertIn("avg_quality", report["pitch_execution"])
        self.assertIn("final_ede", report["defense"])
        a = build_analysis(state.pitch_log)
        # 「結果」だけの report と「判断」だけの analysis は別々の入れ物になっている
        self.assertNotIn("decision_quality", report)
        self.assertIn("avg_decision_quality", a)


# ---------- 改良B/C/D/E ----------
class BCDETests(unittest.TestCase):
    def test_lineup_from_records_ignores_unknown_keys(self):
        rec = dict(name="X", bats="R", coarse_type="power", power=0.8,
                   _note="これは無視される", team="Fake")
        lu = Lineup.from_records([rec] * 9)
        self.assertEqual(len(lu.batters), 9)
        self.assertEqual(lu.batters[0].name, "X")

    def test_sample_team_json_loads(self):
        import os
        path = os.path.join(os.path.dirname(__file__), "lineups", "sample_team.json")
        lu = load_lineup_file(path)
        self.assertEqual(len(lu.batters), 9)
        self.assertTrue(all(b.bats in ("R", "L") for b in lu.batters))
        self.assertGreaterEqual(len({b.coarse_type for b in lu.batters}), 4)

    def test_intro_shows_hints_only_when_requested(self):
        self.assertNotIn("初心者向けヒント", render_intro())
        self.assertNotIn("初心者向けヒント", render_intro(hints=False))
        self.assertIn("初心者向けヒント", render_intro(hints=True))

    def test_custom_lineup_is_used(self):
        batters = build_sample_lineup(random.Random(0))
        batters[0].name = "MARKER"
        st = generate_random_situation(random.Random(1), lineup_batters=batters)
        self.assertIn("MARKER", [b.name for b in st.lineup.batters])

    def test_pitcher_fatigue_raises_miss_rate(self):
        def miss_rate(fixed_count):
            p = Pitcher(control=60, command=60, repertoire=["four_seam"])
            rng = random.Random(3)
            hits = 0
            for _ in range(4000):
                p.pitches_thrown = fixed_count      # 毎回同じ「球数」に固定して比較
                hits += p.execute_pitch("four_seam", "mid_lo", "freeze", rng)["missed"]
            return hits / 4000

        self.assertGreater(miss_rate(60), miss_rate(0) + 0.05)
        self.assertEqual(Pitcher(repertoire=["four_seam"]).fatigue(), 0.0)

    def test_read_grading_matches_truth(self):
        st = _state(4)
        b = st.batter
        st.record_read(wait="fastball", weak=b.weak_course)
        graded = grade_reads(st.reads, st.lineup.batters)
        self.assertEqual(graded["graded"], 2)
        truth_wait = "fastball" if b.guess_bias >= 0.5 else "offspeed"
        expected_correct = (truth_wait == "fastball") + 1   # weak は必ず当たり
        self.assertEqual(graded["correct"], expected_correct)

    def test_spray_accumulates_only_on_fair(self):
        st = _state(5)
        rng = random.Random(5)
        while not st.is_over():
            _drive_pitch(st, rng, list(PITCH_INTENTS), list(COURSES))
        self.assertTrue(all(s["result"] in ("アウト", "単打", "二塁打", "三塁打", "本塁打")
                            for s in st.spray))

    def test_dashboard_and_analysis_render_without_error(self):
        st = _state(6)
        rng = random.Random(6)
        st.record_read(wait="offspeed", weak="mid_lo")
        last = None
        while not st.is_over():
            last = _drive_pitch(st, rng, list(PITCH_INTENTS), list(COURSES))
        for text in (render_dashboard(st, last), render_spray(st),
                     render_field(st.defense, st.batter), render_analysis(st)):
            self.assertIsInstance(text, str)
            self.assertTrue(text)


# ---------- MLB実データ Adapter ----------
class MLBDataAdapterTests(unittest.TestCase):
    def test_normalize_maps_public_stats_directly(self):
        raw = {"name": "Test Player", "bats": "L", "avg": 0.300, "obp": 0.400, "slg": 0.550,
              "whiff_pct": 20, "chase_pct": 25, "pull_pct": 40, "k_pct": 18, "bb_pct": 12,
              "gb_pct": 40, "speed_pct": 60}
        rec = mlb_data_adapter.normalize_batter_record(raw)
        self.assertEqual(rec["name"], "Test Player")
        self.assertEqual(rec["bats"], "L")
        self.assertEqual(rec["avg"], 0.300)
        self.assertEqual(rec["obp"], 0.400)
        self.assertEqual(rec["slg"], 0.550)
        self.assertEqual(rec["speed"], 60)
        # Batter が受け取れるキーがすべて揃っている
        b = Batter(**rec)
        self.assertEqual(b.name, "Test Player")

    def test_normalize_is_deterministic_for_the_same_player(self):
        raw = {"name": "Same Player", "bats": "R", "avg": 0.270, "obp": 0.330, "slg": 0.430}
        a = mlb_data_adapter.normalize_batter_record(raw)
        b = mlb_data_adapter.normalize_batter_record(raw)
        self.assertEqual(a, b)   # 同じ選手なら hot_course 等の「山勘」も毎回同じ

    def test_normalize_uses_real_zone_profile_when_given(self):
        """zone_profile(実データのゾーン別成績)を渡したら、山勘ではなく
        そのデータから hot_course/weak_course を決める。
        """
        raw = {"name": "Zone Player", "bats": "R", "avg": 0.280, "obp": 0.350, "slg": 0.450,
              "zone_profile": {"out_lo": 0.120, "in_hi": 0.480, "mid_mid": 0.300}}
        rec = mlb_data_adapter.normalize_batter_record(raw)
        self.assertEqual(rec["hot_course"], "in_hi")     # 一番数値が高い
        self.assertEqual(rec["weak_course"], "out_lo")   # 一番数値が低い

    def test_normalize_ignores_unknown_keys_and_needs_at_least_two_zones(self):
        raw = {"name": "Sparse Zone Player", "bats": "R", "avg": 0.260, "obp": 0.320, "slg": 0.400,
              "zone_profile": {"not_a_course": 0.9, "in_hi": 0.5}}
        rec = mlb_data_adapter.normalize_batter_record(raw)
        # 有効なコースが1つしかないので、山勘にフォールバックする
        fallback = mlb_data_adapter.normalize_batter_record(
            {"name": "Sparse Zone Player", "bats": "R", "avg": 0.260, "obp": 0.320, "slg": 0.400})
        self.assertEqual(rec["hot_course"], fallback["hot_course"])
        self.assertEqual(rec["weak_course"], fallback["weak_course"])

    def test_normalize_without_zone_profile_falls_back_to_seeded_guess(self):
        raw = {"name": "No Zone Player", "bats": "R", "avg": 0.260, "obp": 0.320, "slg": 0.400}
        rec = mlb_data_adapter.normalize_batter_record(raw)
        self.assertEqual(rec["hot_course"],
                        mlb_data_adapter._seeded_pick("No Zone Player", "hot", list(COURSES)))

    def test_normalize_fills_missing_advanced_stats_with_defaults(self):
        raw = {"name": "Minimal", "bats": "R", "avg": 0.260, "obp": 0.320, "slg": 0.400}
        rec = mlb_data_adapter.normalize_batter_record(raw)
        for key in ("chase_rate", "whiff_rate", "pull", "gb_tendency", "power", "contact"):
            self.assertTrue(0.0 <= rec[key] <= 1.0)

    def test_load_fixture_lineup_gives_nine_real_named_batters(self):
        lu = mlb_data_adapter.load_fixture_lineup()
        self.assertEqual(len(lu.batters), 9)
        names = {b.name for b in lu.batters}
        self.assertEqual(len(names), 9)             # 全員別人
        self.assertTrue(all(b.bats in ("R", "L") for b in lu.batters))
        self.assertTrue(all(0.150 <= b.avg <= 0.400 for b in lu.batters))

    def test_seeded_hot_and_weak_course_are_never_the_same_cell(self):
        """zone_profile が無いときの「山勘」で、得意コースと苦手コースが
        同じセルになってしまわないこと(2 回の抽選が衝突するケースを潰す)。"""
        for b in mlb_data_adapter.load_fixture_lineup().batters:
            self.assertNotEqual(b.hot_course, b.weak_course, b.name)
        for nm in ("Mookie Betts", "Aaron Judge", "Kyle Tucker", "AAA", "sample-x"):
            rec = mlb_data_adapter.normalize_batter_record(
                {"name": nm, "bats": "R", "avg": 0.270, "obp": 0.330, "slg": 0.450})
            self.assertNotEqual(rec["hot_course"], rec["weak_course"], nm)
            self.assertIn(rec["hot_course"], COURSES)
            self.assertIn(rec["weak_course"], COURSES)

    def test_fixture_lineup_plays_through_the_engine(self):
        """アダプタが作った Batter が Game Engine を1試合分きちんと通ること。"""
        lu = mlb_data_adapter.load_fixture_lineup()
        rng = random.Random(21)
        state = generate_random_situation(rng, lineup_batters=lu.batters)
        guard = 0
        while not state.is_over():
            guard += 1
            self.assertLess(guard, 400)
            pt = rng.choice(list(repertoire_options(state.pitcher)))
            co = rng.choice(list(COURSES))
            it = rng.choice(list(PITCH_INTENTS))
            resolve_one_pitch(state, pt, co, it, rng)
        self.assertTrue(state.outs == 3 or state.is_walkoff())
        build_analysis(state.pitch_log)
        from strategy import build_postgame_report
        build_postgame_report(state)

    def test_live_fetch_failure_falls_back_to_fixture_without_crashing(self):
        """API 取得が失敗したら、必ずサンプルデータへフォールバックしてゲームは落ちない。

        実ネットワークの有無に依存しないよう、fetch を強制的に失敗させて検証する。
        """
        with mock.patch.object(mlb_data_adapter, "fetch_live_roster_raw",
                               side_effect=RuntimeError("boom")):
            with redirect_stdout(io.StringIO()):
                lineup, source = mlb_data_adapter.build_demo_lineup(prefer_live=True)
        self.assertEqual(source, "fixture")
        self.assertEqual(len(lineup.batters), 9)

    def test_live_fetch_success_path_uses_live_source(self):
        """fetch が成功すれば source は "live" になる(ネットワークに依存させない)。"""
        fake_raw = [{"name": f"Live Player {i}", "bats": "R" if i % 2 else "L",
                     "avg": 0.270, "obp": 0.340, "slg": 0.440} for i in range(9)]
        with mock.patch.object(mlb_data_adapter, "fetch_live_roster_raw",
                               return_value=fake_raw):
            lineup, source = mlb_data_adapter.build_demo_lineup(prefer_live=True)
        self.assertEqual(source, "live")
        self.assertEqual(len(lineup.batters), 9)

    def test_fetch_live_roster_raw_raises_a_clear_error_when_unreachable(self):
        """接続不能なら RuntimeError で理由を包んで投げる(実ネットワークに触らない)。"""
        with mock.patch("mlb_data_adapter.urllib.request.urlopen",
                        side_effect=urllib.error.URLError("unreachable")):
            with self.assertRaises(RuntimeError):
                mlb_data_adapter.fetch_live_roster_raw(147, 2024)


# ---------- 追加ゲーム性(敬遠 / 継投 / 盗塁) ----------
class GameplayExtrasTests(unittest.TestCase):
    def test_intentional_walk_advances_batter_and_puts_runner_on(self):
        st = _state(1)
        st.runners = [False, False, False]
        spot_before = st.lineup.spot_number()
        st.intentional_walk()
        self.assertTrue(st.runners[0])
        self.assertNotEqual(st.lineup.spot_number(), spot_before)
        self.assertIn("敬遠", st.events)

    def test_change_pitcher_resets_pitch_count(self):
        st = _state(2)
        old = st.pitcher
        st.pitcher.pitches_thrown = 20
        self.assertTrue(st.can_change_pitcher())
        self.assertIsNotNone(st.change_pitcher())
        self.assertIsNot(st.pitcher, old)
        self.assertEqual(st.pitcher.pitches_thrown, 0)   # 新しい投手はフレッシュ
        self.assertEqual(st.pitching_changes, 1)

    def test_can_change_pitcher_as_many_times_as_bullpen_allows(self):
        st = _state(2)
        st.bullpen = [build_sample_pitcher(random.Random(1), name="A"),
                     build_sample_pitcher(random.Random(2), name="B")]
        self.assertTrue(st.can_change_pitcher())
        self.assertEqual(st.change_pitcher().name, "A")
        self.assertTrue(st.can_change_pitcher())          # ブルペンにまだ1人いる
        self.assertEqual(st.change_pitcher().name, "B")
        self.assertFalse(st.can_change_pitcher())         # ブルペンが空
        self.assertIsNone(st.change_pitcher())

    def test_change_pitcher_by_index_picks_the_chosen_reliever(self):
        st = _state(2)
        st.bullpen = [build_sample_pitcher(random.Random(1), name="A"),
                     build_sample_pitcher(random.Random(2), name="B")]
        new_pitcher = st.change_pitcher(index=1)
        self.assertEqual(new_pitcher.name, "B")
        self.assertEqual(len(st.bullpen), 1)
        self.assertEqual(st.bullpen[0].name, "A")

    def test_good_call_streak_builds_and_resets(self):
        st = _state(4)
        # 甘めのしきい値以上なら連続が伸びる
        self.assertTrue(st.register_call_quality(0.20, "ストライク"))
        self.assertTrue(st.register_call_quality(0.11, "空振り"))
        self.assertEqual(st.good_call_streak, 2)
        # 痛打なら質が良くても途切れる
        self.assertFalse(st.register_call_quality(0.50, "単打"))
        self.assertEqual(st.good_call_streak, 0)
        # しきい値未満でも途切れる
        st.register_call_quality(0.20, "ストライク")
        self.assertFalse(st.register_call_quality(0.02, "ボール"))
        self.assertEqual(st.good_call_streak, 0)

    def test_good_call_bonus_is_zero_until_two_in_a_row_then_capped(self):
        st = _state(5)
        self.assertEqual(st.good_call_bonus(), 0.0)
        st.register_call_quality(0.20, "ストライク")
        self.assertEqual(st.good_call_bonus(), 0.0)          # 1 球目はまだ乗らない
        st.register_call_quality(0.20, "空振り")
        self.assertGreater(st.good_call_bonus(), 0.0)         # 2 球連続で少し乗る
        for _ in range(10):
            st.register_call_quality(0.20, "ストライク")
        self.assertLessEqual(st.good_call_bonus(), 0.15)      # 甘めでも上限あり

    def test_engine_marks_good_call_on_outcome(self):
        st = _state(11)
        rng = random.Random(11)
        saw_flag = False
        for _ in range(30):
            if st.is_over():
                break
            outcome, _ = resolve_one_pitch(st, rng.choice(st.pitcher.repertoire),
                                           "mid_lo", "weak_contact", rng)
            self.assertIn("good_call", outcome)
            self.assertIn("good_call_streak", outcome)
            saw_flag = saw_flag or outcome["good_call"]
        self.assertTrue(saw_flag)

    def test_steal_needs_runner_on_first_only(self):
        st = _state(3)
        rng = random.Random(0)
        st.runners = [False, True, False]               # 二塁だけ → 盗塁なし
        self.assertIsNone(st.resolve_steal("fastball", rng))
        st.runners = [True, True, False]                 # 二塁が埋まっている → なし
        self.assertIsNone(st.resolve_steal("fastball", rng))

    def test_steal_resolves_to_sb_or_cs(self):
        sb = cs = 0
        for s in range(400):
            st = _state(s % 20)
            st.runners = [True, False, False]
            st.outs = 0
            r = st.resolve_steal("changeup", random.Random(s))
            if r == "盗塁成功（走者二塁へ）":
                sb += 1
                self.assertEqual(st.runners, [False, True, False])
            elif r == "盗塁を刺した！":
                cs += 1
                self.assertFalse(st.runners[0])
                self.assertEqual(st.outs, 1)
        self.assertGreater(sb, 0)
        self.assertGreater(cs, 0)

    def test_good_throw_catches_more_stealers(self):
        def cs_rate(throw_bonus):
            caught = 0
            for s in range(600):
                st = _state(s % 20)
                st.runners, st.outs = [True, False, False], 0
                r = st.resolve_steal("changeup", random.Random(s), throw_bonus=throw_bonus)
                caught += (r == "盗塁を刺した！")
            return caught / 600
        self.assertGreater(cs_rate(0.25), cs_rate(-0.25) + 0.15)

    def test_good_block_reduces_passed_ball_and_advances_on_fail(self):
        def pb_rate(block_bonus):
            passed = 0
            for s in range(600):
                st = _state(s % 20)
                st.runners = [True, False, True]
                before = st.opp_score
                msg = st.resolve_block(block_bonus, random.Random(s))
                if msg:
                    passed += 1
                    self.assertEqual(st.runners, [False, True, False])   # 1つずつ進む
                    self.assertEqual(st.opp_score, before + 1)           # 三塁走者は生還
            return passed / 600
        self.assertGreater(pb_rate(-0.25), pb_rate(0.25) + 0.2)

    def test_qte_disabled_in_non_tty_returns_neutral(self):
        self.assertFalse(qte_enabled())              # テストは端末ではない
        self.assertEqual(catcher_throw(random.Random(0)), 0.0)
        self.assertEqual(catcher_block("mid", rng=random.Random(0)), 0.0)
        self.assertEqual(catcher_change_signs(random.Random(0)), 0.0)

    def test_block_direction_grading(self):
        import qte
        orig = qte._read
        try:
            qte._read = lambda: "3"                  # 3 = 外
            with redirect_stdout(io.StringIO()):     # direction_check の演出 print を飲む
                self.assertEqual(qte.direction_check("out"), "perfect")
                self.assertEqual(qte.direction_check("mid"), "good")    # 隣
                self.assertEqual(qte.direction_check("in"), "miss")     # 逆
        finally:
            qte._read = orig

    def test_hard_block_passes_more(self):
        def rate(hard):
            passed = 0
            for s in range(400):
                st = _state(s % 20)
                st.runners = [True, False, False]
                if st.resolve_block(0.0, random.Random(s), hard=hard):
                    passed += 1
            return passed / 400
        self.assertGreater(rate(True), rate(False) + 0.05)

    def test_steal_chance_gate(self):
        st = _state(1)
        st.runners, st.outs = [True, True, False], 0    # 二塁が埋まっている
        st.runner_speeds = [50, 50, None]
        self.assertFalse(any(st.steal_chance(random.Random(s)) for s in range(50)))
        st.runners, st.outs = [True, False, False], 2   # 2 アウト
        self.assertFalse(any(st.steal_chance(random.Random(s)) for s in range(50)))
        st.runners, st.outs = [True, False, False], 0
        st.runner_speeds = [55, None, None]
        self.assertTrue(any(st.steal_chance(random.Random(s)) for s in range(80)))

    # ---- 走者の脚力(#走者の個性) ----
    def _steal_rates(self, speed, n=3000):
        att = caught = 0
        for s in range(n):
            st = _state(s % 30)
            st.runners, st.outs, st.runner_speeds = [True, False, False], 0, [speed, None, None]
            if st.steal_chance(random.Random(s + 700)):
                att += 1
            r = st.resolve_steal("changeup", random.Random(s + 900))
            caught += (r == "盗塁を刺した！")
        return att / n, caught / n

    def test_fast_runner_steals_more_and_is_harder_to_nab(self):
        slow_att, slow_cs = self._steal_rates(35)
        fast_att, fast_cs = self._steal_rates(80)
        self.assertGreater(fast_att, slow_att + 0.05)     # 速い走者は仕掛けてくる
        self.assertGreater(slow_cs, fast_cs + 0.05)       # 遅い走者はよく刺される

    def test_fast_runner_takes_extra_base_on_single(self):
        bb = BattedBall("center", "line", "medium", 0.5, "shallow")
        fd = {"result": "単打", "position": "CF", "air_out": False, "batter_out": False}

        def to_third(speed, n=3000):
            hits = 0
            for s in range(n):
                p = resolve_baserunning(bb, fd, [True, False, False], 0, _batter(),
                                        random.Random(s), runner_speeds=[speed, None, None])
                hits += p["new_runners"][2]
            return hits / n

        self.assertGreater(to_third(85), to_third(35) + 0.1)

    def test_runner_speeds_stay_in_sync_through_a_game(self):
        rng = random.Random(3)
        for _ in range(120):
            st = generate_random_situation(rng)
            guard = 0
            while not st.is_over():
                guard += 1
                self.assertLess(guard, 400)
                self.assertEqual(len(st.runner_speeds), 3)
                for i in range(3):
                    if st.runners[i]:
                        self.assertIsNotNone(st.runner_speeds[i])   # 走者がいる塁は脚力あり
                    else:
                        self.assertIsNone(st.runner_speeds[i])      # いない塁は None
                pt = rng.choice(list(repertoire_options(st.pitcher)))
                co = rng.choice(list(COURSES))
                it = rng.choice(list(PITCH_INTENTS))
                resolve_one_pitch(st, pt, co, it, rng)

    # ---- サイン交換 ----
    def test_sign_steal_needs_runner_on_second(self):
        st = _state(1)
        st.runners = [True, False, True]                # 二塁だけ空き → リスクなし
        self.assertFalse(any(st.sign_steal_chance(random.Random(s)) for s in range(50)))
        st.runners = [False, True, False]
        self.assertTrue(any(st.sign_steal_chance(random.Random(s)) for s in range(50)))

    def test_good_sign_change_defends_more_often(self):
        def defend_rate(sign_bonus):
            defended = 0
            for s in range(400):
                st = _state(s % 20)
                msg = st.resolve_sign_steal(sign_bonus, random.Random(s))
                defended += (msg == "サインを変えて事なきを得た")
            return defended / 400
        self.assertGreater(defend_rate(0.25), defend_rate(-0.25) + 0.2)

    def test_sign_leak_boosts_next_guess_only_once(self):
        st = _state(2)
        st.runners = [False, True, False]
        st.sign_leak = 0.65
        outcome = judge_pitch(st, "changeup", "mid_mid", random.Random(3), intent="strike")
        guess = outcome["_analysis"]["guess"]
        self.assertEqual(guess["class"], "offspeed")     # changeup の実際のクラスに一致
        self.assertGreaterEqual(guess["class_strength"], 0.65)
        self.assertEqual(st.sign_leak, 0.0)              # 1球で消費される

    # ---- 振り逃げ ----
    def test_dropped_third_eligibility(self):
        st = _state(1)
        st.runners, st.outs = [True, False, False], 0    # 一塁埋まり・2アウト未満 → 不成立
        self.assertFalse(st.dropped_third_eligible())
        st.runners, st.outs = [False, False, False], 0   # 一塁が空 → 成立
        self.assertTrue(st.dropped_third_eligible())
        st.runners, st.outs = [True, False, False], 2     # 2アウト → 成立
        self.assertTrue(st.dropped_third_eligible())

    def test_dropped_third_passed_ball_advances_all_runners(self):
        for s in range(200):
            st = _state(s % 20)
            st.runners, st.outs = [False, True, True], 0
            before_score = st.opp_score
            safe, msg = st.resolve_dropped_third(-0.9, 0.0, random.Random(s))
            if "後逸" in msg:
                self.assertTrue(safe)
                self.assertEqual(st.runners, [True, False, True])   # 2塁→3塁、3塁は生還
                self.assertEqual(st.opp_score, before_score + 1)
                return
        self.fail("後逸が一度も発生しなかった")

    def test_dropped_third_resolves_to_safe_or_out(self):
        safe_n = out_n = 0
        for s in range(400):
            st = _state(s % 20)
            st.runners, st.outs = [False, False, False], 0
            safe, msg = st.resolve_dropped_third(0.25, 0.0, random.Random(s))
            if safe:
                safe_n += 1
                self.assertTrue(st.runners[0])
            else:
                out_n += 1
                self.assertEqual(st.outs, 1)
        self.assertGreater(safe_n, 0)
        self.assertGreater(out_n, 0)

    def test_dropped_third_good_throw_catches_more(self):
        def safe_rate(throw_bonus):
            safe_n = 0
            for s in range(500):
                st = _state(s % 20)
                st.runners, st.outs = [False, False, False], 0
                safe, msg = st.resolve_dropped_third(0.25, throw_bonus, random.Random(s))
                if safe and "後逸" not in msg:
                    safe_n += 1
            return safe_n / 500
        self.assertGreater(safe_rate(-0.25), safe_rate(0.25) + 0.1)

    def test_dropped_third_safe_by_throw_only_forces_runners(self):
        for s in range(300):
            st = _state(s % 20)
            st.runners, st.outs = [False, True, False], 0    # 二塁のみ
            safe, msg = st.resolve_dropped_third(0.9, -0.9, random.Random(s))
            if safe and "後逸" not in msg:
                self.assertEqual(st.runners, [True, True, False])   # 二塁走者は進塁を強制されない
                return
        self.fail("送球で生きるケースが一度も発生しなかった")


# ---------- 成績ダッシュボード(stats.py) ----------
class StatsTests(unittest.TestCase):
    def test_parse_call_label(self):
        pitch, course, intent = stats._parse_call("sinker/in_hi(ストライク先行)")
        self.assertEqual((pitch, course, intent), ("sinker", "in_hi", "ストライク先行"))
        self.assertEqual(stats._parse_call("おかしな文字列"), (None, None, None))

    def _game(self, entries, runs=0):
        return {
            "final": {"runs_this_inning": runs},
            "pitches": [
                {"call_label": f"four_seam/out_lo({intent})", "result": result,
                 "decision_quality": dq}
                for result, dq, intent in entries
            ],
        }

    def test_bucket_tracks_bad_rate_and_avg_dq(self):
        b = stats._Bucket()
        b.add("単打", 0.5)
        b.add("アウト", -0.2)
        self.assertEqual(b.n, 2)
        self.assertAlmostEqual(b.bad_rate(), 0.5)
        self.assertAlmostEqual(b.avg_dq(), 0.15)

    def test_build_dashboard_aggregates_by_course_and_intent(self):
        games = [self._game([
            ("単打", 0.4, "ストライク先行"),
            ("アウト", -0.1, "チェイス"),
            ("空振り", 0.2, "ストライク先行"),
        ], runs=1)]
        buf = io.StringIO()
        with redirect_stdout(buf):
            stats.build_dashboard(games)
        out = buf.getvalue()
        self.assertIn("外低", out)                # コース(短縮表記)が出る
        self.assertIn("ストライク先行", out)
        self.assertIn("チェイス", out)
        self.assertIn("1 ゲーム", out)
        self.assertIn("3 球", out)


# ---------- リプレイビューア(replay.py) ----------
class ReplayTests(unittest.TestCase):
    def _entry(self, num, result, dq, sequence_label="特筆なし"):
        return {
            "pitch_number": num, "call_label": "four_seam/mid_mid(ストライク先行)",
            "result": result, "decision_quality": dq, "sequence_label": sequence_label,
            "guess": {"class": "fastball", "class_strength": 0.1,
                     "location": "any", "loc_strength": 0.1},
            "fooled_guess": False, "alignment_helped": False, "missed": False,
            "outcome_flags": {"swung": True, "in_zone": True},
        }

    def test_collect_highlights_tags_good_and_risky_calls(self):
        pitches = [self._entry(1, "空振り", 0.4), self._entry(2, "本塁打", -0.5)]
        analysis = build_analysis(pitches)
        highlights = replay._collect_highlights(analysis)
        self.assertIn(1, highlights)
        self.assertIn(2, highlights)
        labels_1 = [label for label, _ in highlights[1]]
        self.assertIn("◎良い配球", labels_1)

    def test_render_replay_includes_summary_and_highlighted_pitches(self):
        log = {
            "situation": {"inning": "9回裏", "start": [3, 3]},
            "final": {"our": 3, "opp": 4, "runs_this_inning": 1,
                     "result": "1失点", "status": "逆転された"},
            "pitches": [self._entry(1, "空振り", 0.4), self._entry(2, "本塁打", -0.5),
                       self._entry(3, "ボール", 0.0)],
            "reads": {},
        }
        out = replay.render_replay(log)
        self.assertIn("9回裏", out)
        self.assertIn("1失点", out)
        self.assertIn(" 1球目", out)
        self.assertIn(" 2球目", out)
        self.assertNotIn(" 3球目", out)             # 目立たない球は出さない

    def test_render_replay_handles_no_highlights(self):
        log = {"situation": {}, "final": {},
              "pitches": [self._entry(1, "ボール", 0.0)]}
        out = replay.render_replay(log)
        self.assertIn("目立った場面はありませんでした", out)


# ---------- 軽量Web UI(webapp.py) ----------
class WebAppTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        sock = socket.socket()
        sock.bind(("127.0.0.1", 0))
        cls.port = sock.getsockname()[1]
        sock.close()
        webapp.SESSION = webapp.GameSession()
        cls.httpd = socketserver.TCPServer(("127.0.0.1", cls.port), webapp.Handler)
        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()
        cls.base = f"http://127.0.0.1:{cls.port}"

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def setUp(self):
        # seed=5 は「走者なし・自軍 6-5 リード・0 アウト」の場面(match_state で確認)。
        # 走者がいないので盗塁/ブロッキング/サイン盗みの QTE は割り込まず、テストが安定する。
        webapp.SESSION = webapp.GameSession(seed=5)

    def _get(self, path=""):
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode("utf-8")

    def _post(self, path, data):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status

    def _first_pitch_key(self, page):
        m = re.search(r'name="pitch_type" value="([^"]+)"', page)
        return m.group(1)

    def _start_game(self):
        self._post("/start", {})
        self._post("/skiptutorial", {})     # 操作練習は各テストの対象外なので飛ばす
        self._post("/playball", {})
        return self._get("/")

    def _drain_qtes(self, answer="good"):
        """QTE 画面が続く限り /qte に答えて通常画面へ戻す。"""
        for _ in range(12):
            if webapp.SESSION.pending_qte is None:
                return
            data = {"verdicts": answer}
            if webapp.SESSION.pending_qte[0] in ("wild_block", "d3_block"):
                data["dir"] = "mid"
            self._post("/qte", data)
        self.fail("QTE が終わらない")

    def _pitch(self, key, course="mid_mid", intent="strike"):
        """1 球投げて、途中の QTE をすべて中立で消化する。"""
        self._post("/pitch", {"pitch_type": key, "course": course, "intent": intent})
        self._drain_qtes()

    def test_root_shows_start_screen_first(self):
        page = self._get("/")
        self.assertIn("START GAME", page)
        self.assertNotIn("CALL PITCH", page)

    def test_start_shows_tutorial_then_situation_intro_then_game(self):
        self._post("/start", {})
        tut = self._get("/")
        self.assertIn("操作の練習", tut)              # まず操作練習
        self.assertNotIn("PLAY BALL", tut)

        self._post("/skiptutorial", {})
        intro = self._get("/")
        self.assertIn("PLAY BALL", intro)
        self.assertIn("DEFEND THE LEAD", intro)
        self.assertNotIn("CALL PITCH", intro)

        self._post("/playball", {})
        game = self._get("/")
        self.assertIn("CALL PITCH", game)
        self.assertIn('action="/pitch"', game)

    def test_tutorial_locks_start_until_all_six_cleared(self):
        self._post("/start", {})
        for k, _name, _desc in webapp._TUTORIAL_ITEMS:
            self.assertIn("操作の練習", self._get("/"))     # まだ練習中
            data = {"item": k, "verdicts": "perfect,perfect,perfect"}
            if k in webapp._TUT_DIR:
                data["dir"] = "mid"
            self._post("/tutorial", data)
        self.assertTrue(all(webapp.SESSION.tutorial.values()))
        self.assertIn("PLAY BALL", self._get("/"))          # 全クリアで本編へ

    def test_tutorial_miss_does_not_clear_the_item(self):
        self._post("/start", {})
        self._post("/tutorial", {"item": "tempo", "verdicts": "perfect,miss,good"})
        self.assertFalse(webapp.SESSION.tutorial["tempo"])
        self._post("/tutorial", {"item": "tempo", "verdicts": "good,good,good"})
        self.assertTrue(webapp.SESSION.tutorial["tempo"])

    def test_tutorial_skip_link_reaches_the_game(self):
        self._post("/start", {})
        self._post("/skiptutorial", {})
        self.assertIn("PLAY BALL", self._get("/"))

    def test_borderline_pitch_inserts_a_receive_rhythm_game(self):
        """隅を狙ってストライクを取りにいく球は、投球の前に受球リズムゲームを挟む。
        /qte を送ると結果が出て、投球が1つ記録される。"""
        page = self._start_game()
        key = self._first_pitch_key(page)
        self.assertFalse(any(webapp.SESSION.state.runners))   # seed=5 は走者なし
        before = len(webapp.SESSION.state.pitch_log)
        self._post("/pitch", {"pitch_type": key, "course": "out_lo", "intent": "freeze"})
        self.assertEqual(webapp.SESSION.pending_qte[0], "frame")
        mid = self._get("/")
        self.assertIn("受球", mid)
        self.assertIn('action="/qte"', mid)
        self.assertNotIn("CALL PITCH", mid)                   # コール画面はまだ出ない
        self.assertEqual(len(webapp.SESSION.state.pitch_log), before)   # まだ投げていない

        self._post("/qte", {"verdicts": "perfect"})
        self._drain_qtes()
        self.assertIn("RESULT", self._get("/"))
        self.assertIsNone(webapp.SESSION.pending_qte)
        self.assertEqual(len(webapp.SESSION.state.pitch_log), before + 1)

    def test_center_pitch_with_no_runners_skips_the_rhythm_game(self):
        page = self._start_game()
        key = self._first_pitch_key(page)
        self._post("/pitch", {"pitch_type": key, "course": "mid_mid", "intent": "strike"})
        self.assertIsNone(webapp.SESSION.pending_qte)
        self.assertIn("RESULT", self._get("/"))

    def test_qte_route_without_a_pending_game_is_harmless(self):
        self._start_game()
        status = self._post("/qte", {"verdicts": "perfect"})
        self.assertEqual(status, 200)
        self.assertIn("CALL PITCH", self._get("/"))

    def test_frame_qte_band_moves_with_pitch_speed(self):
        """受球の帯: 速い球は左（早く）・遅い球は右（遅く）。"""
        import re as _re

        def band(velo):
            class S:
                pending_qte = ("frame", {"call": "x", "velocity": velo})
                last_outcome = None
            return float(_re.search(r'data-targets="([\d.]+)"', webapp.render_qte(S())).group(1))
        self.assertLess(band(95), 0.4)      # フォーシーム級 → 左
        self.assertGreater(band(78), 0.6)   # カーブ級 → 右
        self.assertAlmostEqual(band(88), 0.5, delta=0.05)

    def test_steal_throw_qte_has_a_mash_step(self):
        import re as _re
        class S:
            pending_qte = ("steal_throw", {})
            last_outcome = None
        html = webapp.render_qte(S())
        self.assertEqual(_re.search(r'data-mash="([^"]*)"', html).group(1), "1")
        self.assertEqual(len(_re.search(r'data-labels="([^"]*)"', html).group(1).split("|")), 3)

    def test_field_bunt_qte_uses_two_target_positions(self):
        import re as _re
        class S:
            pending_qte = ("field_bunt", {})
            last_outcome = None
        html = webapp.render_qte(S())
        self.assertEqual(_re.search(r'data-targets="([^"]*)"', html).group(1), "0.4|0.82")

    def test_prev_result_strip_is_green_for_outs_red_for_hits(self):
        self.assertEqual(webapp._prev_result_strip(None), "")
        good = webapp._prev_result_strip({"result": "アウト"})
        bad = webapp._prev_result_strip({"result": "単打"})
        self.assertIn("よし", good)
        self.assertIn("var(--good)", good)
        self.assertIn("まずい", bad)
        self.assertIn("var(--bad)", bad)

    def test_catcher_line_praises_on_strikeout_and_taunts_on_bad_result(self):
        """『感心 / がっかり』は直前の球の結果と打席の締めから決まる。"""
        s = webapp.GameSession(seed=5)
        s.last_outcome = {"result": "空振り", "at_bat_end": "strikeout", "play": None}
        self.assertIn("感心", webapp._catcher_line(s))
        s.last_outcome = {"result": "ボール", "at_bat_end": "walk", "play": None}
        self.assertIn("がっかり", webapp._catcher_line(s))
        s.last_outcome = {"result": "本塁打", "at_bat_end": "hit",
                          "play": {"runs": 1, "label": "本塁打"}}
        self.assertIn("がっかり", webapp._catcher_line(s))
        s.last_outcome = {"result": "ストライク", "at_bat_end": None, "play": None}
        self.assertEqual(webapp._catcher_line(s), "")

    def test_at_bat_end_is_propagated_onto_last_outcome(self):
        """打席の締めタグ(三振/四球/安打/アウト)が last_outcome にも乗る
        ―― _catcher_line がそれを見るため。"""
        page = self._start_game()
        key = self._first_pitch_key(page)
        checked = False
        for _ in range(150):
            if webapp.SESSION.state.is_over():
                break
            self._pitch(key, "mid_mid", "strike")
            st = webapp.SESSION.state
            log_end = st.pitch_log[-1].get("at_bat_end") if st.pitch_log else None
            if log_end:
                self.assertEqual(webapp.SESSION.last_outcome.get("at_bat_end"), log_end)
                checked = True
        self.assertTrue(checked)

    def test_return_tempo_widget_appears_only_after_a_caught_pitch(self):
        """配球コール画面の『返球のテンポ』は、直前の球を捕球したときだけ出る。"""
        page = self._start_game()
        self.assertNotIn('id="tempo"', page)             # 初球には無い
        key = self._first_pitch_key(page)
        self._pitch(key, "mid_mid", "strike")            # 見送り/空振りになりやすい球
        after = self._get("/")
        # 打者がフェアに打っていなければ(=捕球していれば)テンポが出る
        if "CATCHER REPORT" not in after and "RESULT" in after:
            last = webapp.SESSION.state.history.last()
            if last and last.get("result") in ("ボール", "ストライク", "空振り"):
                self.assertIn('id="tempo"', after)
                self.assertIn('name="tempo"', after)

    def test_pitch_call_accepts_a_tempo_field(self):
        """CALL PITCH に返球リズムの結果(tempo)が乗っても 500 にならず解決する。"""
        page = self._start_game()
        key = self._first_pitch_key(page)
        before = len(webapp.SESSION.state.pitch_log)
        self._post("/pitch", {"pitch_type": key, "course": "mid_mid", "intent": "strike",
                              "tempo": "perfect,good,perfect"})
        self._drain_qtes()
        self.assertEqual(len(webapp.SESSION.state.pitch_log), before + 1)
        self.assertIn("RESULT", self._get("/"))

    def test_qte_neutral_answer_matches_no_qte_behaviour(self):
        """JS 無効相当(verdicts 空 → bonus 0)で受球しても、フレーミング以外の
        1 球の結果は frame_timing=None と同じ分布になるはず(ここでは通ることだけ確認)。"""
        page = self._start_game()
        key = self._first_pitch_key(page)
        self._post("/pitch", {"pitch_type": key, "course": "in_hi", "intent": "strike"})
        self._post("/qte", {})                # verdicts 無し = 中立
        self._drain_qtes()
        self.assertIn("RESULT", self._get("/"))

    def test_post_pitch_advances_the_count_or_the_at_bat(self):
        page = self._start_game()
        pitch_key = self._first_pitch_key(page)
        status = self._post("/pitch", {"pitch_type": pitch_key, "course": "mid_mid",
                                       "intent": "strike"})
        self.assertEqual(status, 200)
        page2 = self._get("/")
        self.assertIn("Pitch Call", page2)
        self.assertIn("RESULT", page2)         # 直前の1球の結果パネルが出る

    def test_pitch_rejects_malformed_course_or_intent_without_error(self):
        """フォーム以外(手打ちの POST 等)で不正な course / intent が来ても
        500 にせず、静かに弾く(投球は記録されない)。"""
        self._start_game()
        before = len(webapp.SESSION.state.pitch_log)
        key = self._first_pitch_key(self._get("/"))
        for bad in ({"pitch_type": key, "course": "garbage", "intent": "strike"},
                    {"pitch_type": key, "course": "mid_mid", "intent": "zzz"}):
            status = self._post("/pitch", bad)
            self.assertEqual(status, 200)
            self.assertEqual(len(webapp.SESSION.state.pitch_log), before)

    def test_qte_is_forced_off_for_the_web_process(self):
        self._start_game()
        self.assertFalse(webapp.SESSION.state is None)
        import qte
        self.assertFalse(qte.qte_enabled())

    def test_driving_pitches_eventually_reaches_the_postgame_report(self):
        page = self._start_game()
        for _ in range(300):
            if "CATCHER REPORT" in page:
                break
            pitch_key = self._first_pitch_key(page)
            self._pitch(pitch_key, "mid_mid", "strike")   # 途中の QTE も中立で消化
            page = self._get("/")
        else:
            self.fail("300球投げてもイニングが終わらなかった")
        self.assertIn("もう一度プレイ", page)
        self.assertIn("Runs Allowed", page)
        self.assertIn("Batters Faced", page)
        self.assertIn("GOOD CALL", page)
        self.assertIn("BAD CALL", page)

        self._post("/newgame", {})
        self.assertIn("操作の練習", self._get("/"))    # 新規ゲームはまず操作練習
        self._post("/skiptutorial", {})
        fresh = self._get("/")
        self.assertIn("PLAY BALL", fresh)      # そのあと状況カード

    def test_pitch_arc_svg_renders_for_every_pitch_and_curve_drops_more(self):
        """球種の「雑な軌道」SVG が全球種で作れて、カーブはフォーシームより落ちる。"""
        import re as _re
        from pitch_data import all_pitch_keys
        def end_y(key):
            svg = webapp._pitch_arc_svg(key)
            self.assertTrue(svg.startswith("<svg") and "<path" in svg and "<circle" in svg)
            return float(_re.search(r'<circle cx="[\d.]+" cy="([\d.]+)"', svg).group(1))
        for k in all_pitch_keys():
            end_y(k)
        self.assertGreater(end_y("curveball"), end_y("four_seam") + 5)   # カーブの方が下で終わる

    def test_course_grid_mirrors_by_batter_handedness(self):
        r_grid = webapp._course_grid("R")
        l_grid = webapp._course_grid("L")
        # 右打者は内→外が左→右、左打者はその鏡像(放送のストライクゾーン表示と同じ考え方)
        self.assertEqual(r_grid[0], ["in_hi", "mid_hi", "out_hi"])
        self.assertEqual(l_grid[0], ["out_hi", "mid_hi", "in_hi"])
        # どちらも同じ9コースを1回ずつ含む(表示順が違うだけ)
        flat_r = {c for row in r_grid for c in row}
        flat_l = {c for row in l_grid for c in row}
        self.assertEqual(flat_r, flat_l)
        self.assertEqual(len(flat_r), 9)

    def test_alignment_change_is_reflected(self):
        self._start_game()
        status = self._post("/alignment", {"alignment": "bunt"})
        self.assertEqual(status, 200)
        self.assertEqual(webapp.SESSION.state.defense.alignment, "bunt")

    def test_swap_click_click_flow(self):
        self._start_game()
        before = dict(webapp.SESSION.state.defense.assignment)
        self._post("/swap", {"position": "SS"})
        mid = self._get("/")
        self.assertIsNotNone(webapp.SESSION.pending_swap)
        self.assertIn("選択中", mid)
        self._post("/swap", {"position": "1B"})
        self.assertIsNone(webapp.SESSION.pending_swap)
        after = webapp.SESSION.state.defense.assignment
        self.assertIs(after["SS"], before["1B"])
        self.assertIs(after["1B"], before["SS"])

    def test_swap_same_position_twice_cancels_pending(self):
        self._start_game()
        self._post("/swap", {"position": "CF"})
        self.assertEqual(webapp.SESSION.pending_swap, "CF")
        self._post("/swap", {"position": "CF"})
        self.assertIsNone(webapp.SESSION.pending_swap)

    def test_change_pitcher_picks_the_selected_bullpen_index(self):
        self._start_game()
        webapp.SESSION.state.bullpen = [build_sample_pitcher(random.Random(1), name="控えA"),
                                       build_sample_pitcher(random.Random(2), name="控えB")]
        status = self._post("/change", {"index": "1"})
        self.assertEqual(status, 200)
        self.assertEqual(webapp.SESSION.state.pitcher.name, "控えB")
        self.assertEqual(len(webapp.SESSION.state.bullpen), 1)
        self.assertTrue(webapp.SESSION.state.can_change_pitcher())   # まだ1人残っている

    def test_memo_records_a_read(self):
        self._start_game()
        status = self._post("/memo", {"wait": "fastball", "weak": "out_lo"})
        self.assertEqual(status, 200)
        note = webapp.SESSION.state.current_read()
        self.assertEqual(note["wait"], "fastball")
        self.assertEqual(note["weak"], "out_lo")

    def test_seed_reproducible_over_http(self):
        def play_seeded(seed):
            webapp.SESSION = webapp.GameSession(seed=seed)
            self._post("/start", {})
            self._post("/skiptutorial", {})
            self._post("/playball", {})
            page = self._get("/")
            pitch_key = self._first_pitch_key(page)
            self._post("/pitch", {"pitch_type": pitch_key, "course": "mid_mid", "intent": "strike"})
            self._drain_qtes()        # 走者がいれば盗塁 QTE 等が挟まる。中立で消化
            page = self._get("/")
            return re.search(r"RESULT\s*(?:</[^>]+>)?\s*([^<]+)<", page).group(1)

        self.assertEqual(play_seeded(555), play_seeded(555))


# ---------- 9 分割ゾーン ----------
class ZoneTests(unittest.TestCase):
    def test_nine_courses_and_helpers(self):
        from constants import COURSES, is_corner, zone_overlap, zone_x, zone_y
        self.assertEqual(len(COURSES), 9)
        self.assertEqual(zone_x("out_lo"), "out")
        self.assertEqual(zone_y("out_lo"), "lo")
        self.assertTrue(is_corner("in_hi"))
        self.assertFalse(is_corner("mid_hi"))
        self.assertEqual(zone_overlap("out_lo", "out_lo"), 1.0)
        self.assertEqual(zone_overlap("out_lo", "out_hi"), 0.5)   # 同じ列
        self.assertEqual(zone_overlap("out_lo", "in_hi"), 0.0)

    def test_corner_target_misses_more_than_center(self):
        p = Pitcher(control=55, command=55, repertoire=["four_seam"])

        def miss_rate(course):
            rng = random.Random(1)
            return sum(p.execute_pitch("four_seam", course, "freeze", rng)["missed"]
                       for _ in range(4000)) / 4000

        self.assertGreater(miss_rate("out_lo"), miss_rate("mid_mid") + 0.05)

    def test_batter_default_hot_weak_courses_are_valid_zone_keys(self):
        """hot_course / weak_course を省略して Batter を作っても、既定値が
        9 分割ゾーンの正しいキーで、judge_pitch が例外なく通ること。"""
        from batter import Batter
        from constants import COURSES
        b = Batter(name="デフォルト打者")
        self.assertIn(b.hot_course, COURSES)
        self.assertIn(b.weak_course, COURSES)
        st = generate_random_situation(random.Random(0))
        st.lineup.batters[st.lineup.index] = b
        rng = random.Random(0)
        for _ in range(50):
            judge_pitch(st, "four_seam", "out_lo", rng, intent="freeze")

    def test_weak_zone_overlap_bites_partially(self):
        # 「外角低めが苦手」な打者は「外角」でもいくらか差し込まれる
        from constants import zone_overlap
        self.assertEqual(zone_overlap("out_mid", "out_lo"), 0.5)


# ---------- フレーミング(捕手の craft) ----------
class FramingTests(unittest.TestCase):
    def _edge_take_calls(self, course, intent, n=4000, seed=0):
        """見送りになった球のうち、ストライク判定された割合(= フレーミング成功含む)。"""
        st = generate_random_situation(random.Random(seed))
        # 見送りやすい打者に固定
        st.batter.aggression, st.batter.chase_rate = 0.1, 0.05
        rng = random.Random(seed + 1)
        strikes = takes = 0
        for _ in range(n):
            st.balls = st.strikes = 0
            o = judge_pitch(st, "four_seam", course, rng, intent=intent)
            if not o["swung"]:
                takes += 1
                strikes += (o["result"] == "ストライク")
        return strikes / takes if takes else 0.0

    def test_edge_takes_are_framed_into_strikes_sometimes(self):
        # 隅(外角低め)を要求して見送られた球は、真ん中を大きく外した球より
        # 「ストライク」と判定される割合が高い(フレーミングが効く)
        edge = self._edge_take_calls("out_lo", "freeze")
        waste = self._edge_take_calls("mid_mid", "chase")   # chase = 元から大きく外す意図
        self.assertGreater(edge, waste)

    def test_framed_strikes_counter_increments(self):
        st = generate_random_situation(random.Random(3))
        st.batter.aggression, st.batter.chase_rate = 0.05, 0.02
        rng = random.Random(4)
        for _ in range(3000):
            st.balls = st.strikes = 0
            judge_pitch(st, "four_seam", "out_lo", rng, intent="freeze")
        self.assertGreater(st.framed_strikes, 0)

    def test_no_framing_on_pitchout_or_big_waste(self):
        st = generate_random_situation(random.Random(5))
        st.batter.aggression, st.batter.chase_rate = 0.05, 0.02
        rng = random.Random(6)
        for _ in range(2000):
            st.balls = st.strikes = 0
            o = judge_pitch(st, "four_seam", "out_lo", rng, intent="pitchout")
            self.assertEqual(o["result"], "ボール")     # ピッチアウトは絶対にストライクにならない
        self.assertEqual(st.framed_strikes, 0)

    def _take_strike_rate(self, frame_timing, n=5000, seed=7):
        st = generate_random_situation(random.Random(seed))
        st.batter.aggression, st.batter.chase_rate = 0.05, 0.02
        rng = random.Random(seed + 1)
        strikes = takes = 0
        for _ in range(n):
            st.balls = st.strikes = 0
            o = judge_pitch(st, "four_seam", "out_lo", rng, intent="freeze",
                            frame_timing=frame_timing)
            if not o["swung"]:
                takes += 1
                strikes += (o["result"] == "ストライク")
        return strikes / takes if takes else 0.0

    def test_receive_timing_shifts_framing_rate(self):
        """受球リズムゲームの出来: perfect は miss よりフレーミングが決まる。
        frame_timing=None は従来どおり(perfect と miss の間)。"""
        miss = self._take_strike_rate("miss")
        neutral = self._take_strike_rate(None)
        perfect = self._take_strike_rate("perfect")
        self.assertGreater(perfect, miss)
        self.assertGreaterEqual(perfect, neutral)
        self.assertGreaterEqual(neutral, miss)


# ---------- 相手の小技(犠打 / エンドラン / ピッチアウト) ----------
class SmallBallTests(unittest.TestCase):
    def _bunt_state(self, seed=0):
        import opponent
        st = generate_random_situation(random.Random(seed))
        st.outs = 0
        st.runners = [True, False, False]
        st.runner_speeds = [50, None, None]
        st.our_score = st.opp_score = 3
        st.batter.coarse_type = "weak"
        st.batter.power = 0.3
        st.opp_tactic = "bunt"
        return st

    def test_opponent_bunts_in_a_textbook_spot(self):
        import opponent
        fired = 0
        for s in range(2000):
            st = generate_random_situation(random.Random(s))
            st.outs, st.runners, st.runner_speeds = 0, [True, False, False], [50, None, None]
            st.our_score = st.opp_score = 3
            st.batter.coarse_type, st.batter.power = "weak", 0.3
            fired += opponent.decide_tactic(st, random.Random(s + 5000)) == "bunt"
        self.assertGreater(fired, 200)               # そこそこ仕掛けてくる
        self.assertLess(fired, 1400)                 # でも常にではない(相手を強くしすぎない)

    def test_opponent_never_bunts_with_two_outs(self):
        import opponent
        for s in range(200):
            st = generate_random_situation(random.Random(s))
            st.outs, st.runners = 2, [True, False, False]
            st.batter.coarse_type = "weak"
            self.assertIsNone(opponent.decide_tactic(st, random.Random(s)))

    def test_bunt_resolves_to_a_known_outcome(self):
        results = set()
        for s in range(400):
            st = self._bunt_state(s)
            outcome, msgs = resolve_one_pitch(st, "four_seam", "mid_lo", "strike", random.Random(s + 1))
            results.add(outcome["result"])
            self.assertTrue(msgs)
        self.assertTrue(results <= {"犠打成功", "バント失敗（小フライ）", "バント（封殺）",
                                    "バント安打", "ファウル", "三振"})
        self.assertIn("犠打成功", results)

    def test_successful_sacrifice_advances_runner_and_makes_an_out(self):
        # ファウルや小フライを避けて「転がった」ケースだけ見る
        seen = False
        for s in range(300):
            st = self._bunt_state(s)
            before_out = st.outs
            outcome, _ = resolve_one_pitch(st, "four_seam", "mid_lo", "strike", random.Random(s + 7))
            if outcome["result"] == "犠打成功":
                seen = True
                self.assertEqual(st.outs, before_out + 1)
                self.assertTrue(st.runners[1])       # 走者が二塁へ
                self.assertFalse(st.runners[0])
        self.assertTrue(seen)

    def test_pitchout_busts_a_bunt(self):
        busted = 0
        for s in range(200):
            st = self._bunt_state(s)
            outcome, _ = resolve_one_pitch(st, "four_seam", "out_hi", "pitchout", random.Random(s + 2))
            busted += outcome["result"] in ("ボール", "四球")
        self.assertGreater(busted, 150)              # ほぼ必ずバントを外させられる

    def test_pitchout_makes_steal_much_easier_to_catch(self):
        def cs_rate(intent, n=1500):
            caught = 0
            for s in range(n):
                st = generate_random_situation(random.Random(s))
                st.outs, st.runners, st.runner_speeds = 0, [True, False, False], [60, None, None]
                st.opp_tactic = None
                before = st.outs
                resolve_one_pitch(st, "four_seam", "out_mid", intent, random.Random(s + 40))
                caught += (st.outs == before + 1 and not st.runners[0] and st.runners[1] is False)
            return caught / n
        # ピッチアウトのほうが盗塁を刺せている場面が多い(統計的に)
        self.assertGreaterEqual(cs_rate("pitchout"), cs_rate("strike"))

    def test_hit_and_run_runner_advances_without_double_play(self):
        import opponent
        # 内野ゴロでも走者は進み、併殺にならない
        adv = dp = 0
        for s in range(400):
            st = generate_random_situation(random.Random(s))
            st.outs, st.runners, st.runner_speeds = 0, [True, False, False], [55, None, None]
            st.opp_tactic = "hit_and_run"
            outcome, _ = resolve_one_pitch(st, "sinker", "mid_lo", "weak_contact", random.Random(s + 3))
            play = outcome.get("play")
            if play and play["outs_added"] >= 2:
                dp += 1
            if outcome["result"] == "アウト" and (st.runners[1] or st.runners[2]):
                adv += 1
        self.assertEqual(dp, 0)                      # エンドラン中は併殺なし
        self.assertGreater(adv, 20)

    def test_catcher_field_bunt_is_neutral_when_qte_disabled(self):
        """CLI 以外(Web・テスト)では常に 0.0 = バント処理の挙動は従来どおり。"""
        from qte import catcher_field_bunt, qte_enabled
        self.assertFalse(qte_enabled())
        self.assertEqual(catcher_field_bunt(random.Random(0)), 0.0)

    def test_field_bonus_helps_nab_the_lead_runner_and_prevent_bunt_hits(self):
        """バント処理リズムゲームの出来(field_bonus)は結果を確定させず確率をずらす:
        出来が良いほど先の走者を封殺しやすく、内野安打(バント安打)を防ぐ。"""
        def tally(fb, n=2500):
            nab = hit = 0
            for s in range(n):
                st = generate_random_situation(random.Random(s))
                st.outs = 0
                st.runners, st.runner_speeds = [True, False, False], [55, None, None]
                st.our_score = st.opp_score = 3
                st.batter.coarse_type, st.batter.power = "weak", 0.30
                _, final, _ = st.resolve_bunt(False, False, False,
                                              random.Random(s + 1), field_bonus=fb)
                nab += final in ("バント（封殺）", "バント（本封殺）")
                hit += final == "バント安打"
            return nab / n, hit / n
        nab_lo, hit_lo = tally(-0.20)
        nab_hi, hit_hi = tally(0.20)
        self.assertGreater(nab_hi, nab_lo)
        self.assertLessEqual(hit_hi, hit_lo)

    def test_begin_at_bat_is_idempotent_within_an_at_bat(self):
        """begin_at_bat を打席中に何度呼んでも、作戦も乱数の消費量も変わらない。

        Web 版はページを再描画するたびに begin_at_bat を呼ぶので、これが
        崩れると --seed を付けていてもリロードのたびに試合が変わってしまう。
        """
        from engine import begin_at_bat

        def outcome_after(renders):
            st = generate_random_situation(random.Random(4242))
            st.outs = 1
            st.runners, st.runner_speeds = [True, False, False], [55, None, None]
            st.our_score = st.opp_score = 3
            st.start_our = st.start_opp = 3
            st.batter.coarse_type, st.batter.power = "weak", 0.30
            rng = random.Random(99)
            for _ in range(renders):
                begin_at_bat(st, rng)
            tactic = st.opp_tactic
            o, _ = resolve_one_pitch(st, "four_seam", "mid_mid", "strike", rng)
            return tactic, o["result"], o["actual_course"], o["quality"]

        base = outcome_after(1)
        for n in (2, 3, 8):
            self.assertEqual(outcome_after(n), base,
                             f"{n} 回描画すると結果が変わった（begin_at_bat が冪等でない）")


# ---------- 1000 ゲーム ストレス ----------
class StressTests(unittest.TestCase):
    def test_1000_games_no_crash_or_invalid_state(self):
        rng = random.Random(2024)
        intents = list(PITCH_INTENTS)
        courses = list(COURSES)
        games = 1000
        total_pitches = 0
        total_runs = 0
        xbh = 0

        for g in range(games):
            state = generate_random_situation(rng)
            self.assertIn(state.inning, (7, 8, 9, 10))
            self.assertIn(state.outs, (0, 1, 2))
            self.assertTrue(1 <= state.lineup.spot_number() <= 9)

            guard = 0
            while not state.is_over():
                guard += 1
                self.assertLess(guard, 400, f"game {g}: 半イニングが終わらない")
                outcome = _drive_pitch(state, rng, intents, courses)
                total_pitches += 1
                xbh += outcome["result"] in ("二塁打", "三塁打", "本塁打")
                self.assertEqual(len(state.runners), 3)
                if state.is_over():
                    break
                self.assertLessEqual(state.strikes, 2, f"game {g}: 不正なストライク数")
                self.assertLessEqual(state.balls, 3, f"game {g}: 不正なボール数")
                self.assertLess(state.outs, 3)
                self.assertTrue(0 <= state.lineup.index <= 8, f"game {g}: 打順破綻")

            self.assertTrue(state.outs == 3 or state.is_walkoff(), f"game {g}: 未決着で終了")
            total_runs += state.runs_this_inning
            run_txt, status = state.result_summary()
            self.assertIsInstance(status, str)
            build_analysis(state.pitch_log)   # 例外なく回ること

        self.assertGreater(total_pitches, games * 3)
        # 改良A: 長打も失点も出るようになっているはず(現実的な範囲)
        self.assertGreater(xbh, 100)
        self.assertGreater(total_runs, 200)
        self.assertLess(total_runs / games, 4.0)   # 1半イニングの平均失点が暴走しない

    def test_1000_games_through_full_engine_no_crash(self):
        """engine.resolve_one_pitch() ―― CLI/Web が実際に呼ぶ入口 ―― を通しで
        1000 ゲーム回す。振り逃げ・サイン盗み見・ブロッキング・盗塁など、
        judge_pitch() だけでは通らない周辺イベントも含めて検証する。
        """
        rng = random.Random(99)
        intents = list(PITCH_INTENTS)
        courses = list(COURSES)
        games = 1000
        events_seen = set()

        for g in range(games):
            state = generate_random_situation(rng)
            guard = 0
            while not state.is_over():
                guard += 1
                self.assertLess(guard, 400, f"game {g}: 半イニングが終わらない")
                for _ in maybe_resolve_sign_steal(state, rng):
                    pass
                pt = rng.choice(list(repertoire_options(state.pitcher)))
                co = rng.choice(courses)
                it = rng.choice(intents)
                resolve_one_pitch(state, pt, co, it, rng)
                self.assertTrue(0 <= state.lineup.index <= 8)
            self.assertTrue(state.outs == 3 or state.is_walkoff())
            events_seen.update(state.events)

            report = build_postgame_report(state)
            self.assertEqual(report["pitches"], len(state.pitch_log))
            self.assertEqual(report["runs_allowed"], state.runs_this_inning)
            self.assertGreaterEqual(report["batters_faced"], 1)
            # 三振/四球/被安打/エラーの合計は、対戦打者数を超えない
            self.assertLessEqual(
                report["strikeouts"] + report["walks"] + report["hits"] + report["errors"],
                report["batters_faced"])
            build_analysis(state.pitch_log)   # 例外なく回ること

        # 振り逃げ・サイン盗み見・盗塁・後逸など、周辺イベントが実際に一通り起きたか
        for ev in ("振り逃げ", "サイン交換", "盗塁成功", "盗塁刺", "後逸"):
            self.assertIn(ev, events_seen, f"1000ゲームで一度も『{ev}』が発生しなかった")


if __name__ == "__main__":
    unittest.main(verbosity=2)
