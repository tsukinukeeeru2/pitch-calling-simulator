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
import urllib.parse
import urllib.request
from contextlib import redirect_stdout

from baserunning import resolve as resolve_baserunning
from batted_ball import BattedBall, generate_batted_ball
from batter import Batter
from constants import COURSES
from defense import ALIGNMENTS, Defense, make_default_defense
from fielders import POSITIONS, Fielder, make_default_fielders, position_fit, weakest_fielder
from fielding import resolve_batted_ball
from ballpark_view import render_field
from judge import judge_pitch
from lineup import Lineup, build_sample_lineup, load_lineup_file
from engine import log_pitch
from match_state import generate_random_situation
from pitch_data import (
    PITCH_LIBRARY,
    all_pitch_keys,
    family_of,
    get_pitch,
    guess_class_of,
    repertoire_options,
    velocity_of,
)
from pitcher import Pitcher, build_sample_pitcher
from qte import catcher_block, catcher_change_signs, catcher_throw, qte_enabled
from reaction import FACE_CATEGORIES, describe_reaction, render_reaction_block
import replay
import stats
from strategy import PITCH_INTENTS, build_analysis, evaluate_sequencing, grade_reads
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
        self.assertEqual(state.outs, 3)


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

    def test_change_pitcher_once_and_resets_pitch_count(self):
        st = _state(2)
        old = st.pitcher
        st.pitcher.pitches_thrown = 20
        self.assertTrue(st.can_change_pitcher())
        self.assertTrue(st.change_pitcher())
        self.assertIsNot(st.pitcher, old)
        self.assertEqual(st.pitcher.pitches_thrown, 0)   # 新しい投手はフレッシュ
        self.assertFalse(st.can_change_pitcher())        # 2 回目は不可
        self.assertFalse(st.change_pitcher())

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
        self.assertFalse(any(st.steal_chance(random.Random(s)) for s in range(50)))
        st.runners, st.outs = [True, False, False], 2   # 2 アウト
        self.assertFalse(any(st.steal_chance(random.Random(s)) for s in range(50)))
        st.runners, st.outs = [True, False, False], 0
        self.assertTrue(any(st.steal_chance(random.Random(s)) for s in range(50)))

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
            called = []
            safe, msg = st.resolve_dropped_third(
                -0.9, lambda: called.append(1) or 0.0, random.Random(s))
            if "後逸" in msg:
                self.assertTrue(safe)
                self.assertEqual(st.runners, [True, False, True])   # 2塁→3塁、3塁は生還
                self.assertEqual(st.opp_score, before_score + 1)
                self.assertEqual(called, [])         # 後逸なら送球QTEは呼ばれない
                return
        self.fail("後逸が一度も発生しなかった")

    def test_dropped_third_resolves_to_safe_or_out(self):
        safe_n = out_n = 0
        for s in range(400):
            st = _state(s % 20)
            st.runners, st.outs = [False, False, False], 0
            safe, msg = st.resolve_dropped_third(0.25, lambda: 0.0, random.Random(s))
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
                safe, msg = st.resolve_dropped_third(0.25, lambda: throw_bonus, random.Random(s))
                if safe and "後逸" not in msg:
                    safe_n += 1
            return safe_n / 500
        self.assertGreater(safe_rate(-0.25), safe_rate(0.25) + 0.1)

    def test_dropped_third_safe_by_throw_only_forces_runners(self):
        for s in range(300):
            st = _state(s % 20)
            st.runners, st.outs = [False, True, False], 0    # 二塁のみ
            safe, msg = st.resolve_dropped_third(0.9, lambda: -0.9, random.Random(s))
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
        webapp.SESSION.new_game()

    def _get(self, path=""):
        with urllib.request.urlopen(self.base + path) as r:
            return r.read().decode("utf-8")

    def _post(self, path, data):
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body, method="POST")
        with urllib.request.urlopen(req) as r:
            return r.status

    def _first_pitch_key(self, page):
        m = re.search(r'name="pitch_type">.*?<option value="([^"]+)"', page, re.S)
        return m.group(1)

    def test_get_root_renders_the_pitch_call_form(self):
        page = self._get("/")
        self.assertIn("終盤の守り切りゲーム", page)
        self.assertIn('action="/pitch"', page)
        self.assertIn("投球", page)

    def test_post_pitch_advances_the_count_or_the_at_bat(self):
        page = self._get("/")
        pitch_key = self._first_pitch_key(page)
        status = self._post("/pitch", {"pitch_type": pitch_key, "course": "mid_mid",
                                       "intent": "strike"})
        self.assertEqual(status, 200)
        page2 = self._get("/")
        self.assertIn("カウント", page2)

    def test_qte_is_forced_off_for_the_web_process(self):
        self.assertFalse(webapp.SESSION.state is None)
        import qte
        self.assertFalse(qte.qte_enabled())

    def test_driving_pitches_eventually_reaches_the_analysis_page(self):
        rng = random.Random(0)
        page = self._get("/")
        for _ in range(200):
            if "配球のふり返り" in page:
                break
            pitch_key = self._first_pitch_key(page)
            self._post("/pitch", {"pitch_type": pitch_key, "course": "mid_mid",
                                  "intent": "strike"})
            page = self._get("/")
        else:
            self.fail("200球投げてもイニングが終わらなかった")
        self.assertIn("もう一度プレイ", page)

        self._post("/newgame", {})
        fresh = self._get("/")
        self.assertNotIn("配球のふり返り", fresh)
        self.assertIn('action="/pitch"', fresh)

    def test_alignment_change_is_reflected(self):
        status = self._post("/alignment", {"alignment": "bunt"})
        self.assertEqual(status, 200)
        self.assertEqual(webapp.SESSION.state.defense.alignment, "bunt")


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

    def test_weak_zone_overlap_bites_partially(self):
        # 「外角低めが苦手」な打者は「外角」でもいくらか差し込まれる
        from constants import zone_overlap
        self.assertEqual(zone_overlap("out_mid", "out_lo"), 0.5)


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

            self.assertEqual(state.outs, 3)
            total_runs += state.runs_this_inning
            run_txt, status = state.result_summary()
            self.assertIsInstance(status, str)
            build_analysis(state.pitch_log)   # 例外なく回ること

        self.assertGreater(total_pitches, games * 3)
        # 改良A: 長打も失点も出るようになっているはず(現実的な範囲)
        self.assertGreater(xbh, 100)
        self.assertGreater(total_runs, 200)
        self.assertLess(total_runs / games, 4.0)   # 1半イニングの平均失点が暴走しない


if __name__ == "__main__":
    unittest.main(verbosity=2)
