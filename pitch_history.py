"""配球履歴を保持する PitchHistory クラス。

やることは「投げた球を古い順に記録し、パターンを問い合わせられるようにする」
だけ。判定そのものは judge.py が行い、ここはデータ置き場に徹する。
(思考は batter.py、判定は judge.py、記録は pitch_history.py、と役割を分ける)
"""

from constants import COURSE_SHORT
from pitch_data import guess_class_of, pitch_name


class PitchHistory:
    def __init__(self):
        self.records = []   # 1球ごとの辞書を古い順に並べたリスト

    def clear(self):
        """打者が代わったら履歴を空にする。"""
        self.records = []

    def add(self, pitch_type, course, result, timing, swung,
            actual_course=None, intent=None, velocity=None, family=None, in_zone=None):
        """1球分を記録する。

        course        : 捕手が要求したコース
        actual_course : 実際に来たコース(失投でズレることがある)
        family        : "fastball" / "breaking" / "offspeed"(3分類)
        pitch_class   : "fastball" / "offspeed"(打者の待ちは2択で考えるため)
        """
        self.records.append({
            "pitch_type": pitch_type,
            "course": course,
            "actual_course": actual_course if actual_course is not None else course,
            "pitch_class": guess_class_of(pitch_type),
            "family": family,
            "velocity": velocity,
            "intent": intent,
            "result": result,
            "timing": timing,                        # "early" / "on_time" / "late"
            "swung": swung,
            "in_zone": in_zone,
        })

    def is_empty(self):
        return len(self.records) == 0

    def count(self):
        return len(self.records)

    def last(self):
        """直前の1球の記録。まだ無ければ None。"""
        return self.records[-1] if self.records else None

    def same_type_streak(self):
        """末尾から見て、同じ球種が何球連続しているか。"""
        return self._streak("pitch_type")

    def same_course_streak(self):
        """末尾から見て、同じコースが何球連続しているか(実際のコースで見る)。"""
        return self._streak("actual_course")

    def family_streak(self):
        """末尾から見て、同系統(fastball/breaking/offspeed)が何球連続しているか。"""
        return self._streak("family")

    def _streak(self, key):
        if not self.records:
            return 0
        target = self.records[-1][key]
        streak = 0
        for record in reversed(self.records):
            if record[key] == target:
                streak += 1
            else:
                break
        return streak

    def recent_class_counts(self, n):
        """直近 n 球の球種クラス(fastball / offspeed)の内訳を数える。"""
        counts = {"fastball": 0, "offspeed": 0}
        for record in self.records[-n:]:
            counts[record["pitch_class"]] += 1
        return counts

    def summary_text(self):
        """画面表示用。『1.フォーシーム/外 → 2.スライダー/低(失投)』の形。"""
        if not self.records:
            return "（まだ無し）"
        parts = []
        for i, r in enumerate(self.records, start=1):
            loc = COURSE_SHORT.get(r["actual_course"], r["actual_course"])
            drift = "*" if r["actual_course"] != r["course"] else ""
            parts.append(f'{i}.{pitch_name(r["pitch_type"])}/{loc}{drift}')
        return " → ".join(parts)
