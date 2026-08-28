"""打者の反応を「顔文字 + 短い文」で表すモジュール。

思想(前バージョンから引き継ぎ):
  返すのは「打者の本当の状態そのもの」ではない。
  judge.py が出した 1 球の結果(タイミング・振ったか・見逃し方)だけを見て
  「素直なカテゴリ」を決め、そのうえで
    - REACTION_MISLEAD_RATE の確率で「逆の印象」を返す
    - REACTION_REVEAL_RATE の確率で素直なカテゴリを返す
    - 残りは情報の薄いカテゴリ(neutral / uncertain / frustrated)を返す

顔文字が「正解」を直接教えないための工夫:
  1. 1 カテゴリに複数の顔文字候補・複数の文がある(毎回変わる)
  2. 同じ内部状態でも複数カテゴリに散らす余地がある
  3. neutral / mislead のカテゴリを混ぜる
  4. 顔文字だけでは内部状態を 100% 特定できない

戻り値:
    {"face": "(・_・;)", "ascii_face": ":-S",
     "text": "一瞬タイミングを取り直した",
     "category": "uncertain", "kind": "reveal"}

このファイルが「表情表示の責務」を持つ。judge.py / batter.py に顔文字は書かない。
"""

import random
import sys

import ansi
from constants import REACTION_MISLEAD_RATE, REACTION_REVEAL_RATE, zone_x, zone_y

# カテゴリ → 表示色(顔文字を彩る)
_CAT_COLOR = {
    "jammed": "bred", "out_front": "bred", "frustrated": "bred", "surprised": "bmagenta",
    "confident": "bgreen", "locked_in": "bgreen",
    "comfortable": "byellow", "defensive": "bcyan",
    "uncertain": "bcyan", "neutral": "grey",
}

# --- 8〜12 個に絞った表情カテゴリ ---
# kaomoji : Windows Terminal などで読める顔文字(第一候補)
# ascii   : ASCII だけの代替(環境依存で化けるとき用)
# texts   : そのカテゴリの短い反応文(複数)
FACE_TABLE = {
    "jammed": {  # 差し込まれた
        "kaomoji": ["(-_-;)", "(>_<)", "(๑•́ㅿ•̀)"],
        "ascii": [">_<", "-_-;", "x_x"],
        "texts": ["少し詰まったように見えた", "差し込まれて振り遅れた", "手が出たが窮屈そうだった"],
    },
    "out_front": {  # 泳いだ・前に出た
        "kaomoji": ["(´・ω・｀)", "(＞ｍ＜)", "( ˘•ω•˘ )"],
        "ascii": [":-/", "x_x", ":-("],
        "texts": ["体が前に泳いだ", "タイミングが early で崩れた", "上体が突っ込んでいた"],
    },
    "confident": {  # 自信・狙い通り
        "kaomoji": ["(￣ー￣)", "(｀・ω・´)", "( ¯꒳¯ )"],
        "ascii": ["^_~", ">:)", "-_-b"],
        "texts": ["打席で落ち着いている", "自信ありげにバットを構えた", "軽くうなずいて構え直した"],
    },
    "comfortable": {  # 余裕を持った見送り
        "kaomoji": ["(⌒,_ゝ⌒)", "(´ー｀)", "( ˘ω˘ )"],
        "ascii": [":-)", "=)", "^_^"],
        "texts": ["外の球を余裕を持って見送った", "きわどい球を楽に見極めた", "無理をせず1球見た"],
    },
    "uncertain": {  # 迷い
        "kaomoji": ["(・_・;)", "(◎_◎;)", "(・・;)"],
        "ascii": [":-S", "o_O", "?_?"],
        "texts": ["一瞬タイミングを取り直した", "迷ったように見えた", "打席を外して間を取った"],
    },
    "surprised": {  # 意表を突かれた
        "kaomoji": ["(ﾟДﾟ)", "(°口°)", "Σ(・□・;)"],
        "ascii": [":-O", "O_o", "!_!"],
        "texts": ["意表を突かれた様子だった", "のけぞって驚いた", "反応が一歩遅れた"],
    },
    "frustrated": {  # イラつき
        "kaomoji": ["(-\"-)", "(￢_￢)", "(#`д´)"],
        "ascii": [">:(", "-.-", "`_`"],
        "texts": ["いらだった様子でバットを振った", "首をひねっていた", "小さく舌打ちしたように見えた"],
    },
    "locked_in": {  # しっかり振り抜いた
        "kaomoji": ["(๑•̀ㅂ•́)و", "(ｷﾘｯ)", "( •̀ω•́ )"],
        "ascii": ["\\o/", ">_>b", "=D"],
        "texts": ["鋭くバットを振り抜いた", "力強いスイングだった", "振り切って残心があった"],
    },
    "defensive": {  # 追い込まれて粘り
        "kaomoji": ["(；・∀・)", "(・∀・;)", "( ；´Д｀)"],
        "ascii": [";-)", ":-|", ";_;"],
        "texts": ["食らいついてカットした", "粘って的を絞っていた", "追い込まれて守りに入った"],
    },
    "neutral": {  # 無表情・情報が薄い
        "kaomoji": ["(´・_・`)", "(・_・)", "( ᐛ )"],
        "ascii": [":-|", "._.", "-_-"],
        "texts": ["表情を変えずに構えた", "淡々とボックスに入り直した", "とくに変化は見えなかった"],
    },
}

FACE_CATEGORIES = list(FACE_TABLE)

# MISLEAD のときに使う「逆の印象」対応表
_OPPOSITE = {
    "jammed": "confident",
    "out_front": "comfortable",
    "confident": "uncertain",
    "comfortable": "surprised",
    "uncertain": "confident",
    "surprised": "confident",
    "frustrated": "comfortable",
    "locked_in": "defensive",
    "defensive": "confident",
    "neutral": "neutral",
}

# 情報の薄い「ぼかし」カテゴリ
_AMBIGUOUS = ["neutral", "uncertain", "frustrated"]


def _true_category(course, outcome):
    """内部の真実(タイミング・見逃し方)に対応する『素直なカテゴリ』。"""
    timing = outcome["timing"]
    swung = outcome["swung"]
    in_zone = outcome["in_zone"]
    result = outcome["result"]

    if not swung:
        if not in_zone and (zone_x(course) == "out" or zone_y(course) != "mid"):
            return "comfortable"
        if zone_x(course) == "in":
            return "surprised"
        return "neutral"

    if timing == "late":
        return "jammed"
    if timing == "early":
        return "out_front"

    # timing == "on_time" で振った
    if result == "空振り":
        return "locked_in"
    if result == "ファウル":
        return "defensive"
    if result in ("単打", "二塁打", "三塁打", "本塁打"):
        return "confident"
    return "locked_in"   # アウト(いい当たりでもアウト、など)


def _stdout_supports_kaomoji():
    """今の出力先で顔文字(非 ASCII)が化けずに出せそうか。"""
    encoding = getattr(sys.stdout, "encoding", None) or "utf-8"
    try:
        "（´・ω・｀）".encode(encoding)
        return True
    except (UnicodeEncodeError, LookupError):
        return False


def describe_reaction(pitch_type, course, outcome, rng=None):
    """1 球の結果 → 反応(顔文字 + 文 + カテゴリ)の辞書を返す。

    pitch_type は今は使わないが、将来「球種で仕草を変える」余地として受け取る。
    course は実際に来たコース(actual_course)を渡す想定。
    """
    rng = rng or random

    true_cat = _true_category(course, outcome)

    roll = rng.random()
    if roll < REACTION_MISLEAD_RATE:
        category, kind = _OPPOSITE[true_cat], "mislead"
    elif roll < REACTION_MISLEAD_RATE + REACTION_REVEAL_RATE:
        category, kind = true_cat, "reveal"
    else:
        category, kind = rng.choice(_AMBIGUOUS), "ambiguous"

    entry = FACE_TABLE[category]
    return {
        "face": rng.choice(entry["kaomoji"]),
        "ascii_face": rng.choice(entry["ascii"]),
        "text": rng.choice(entry["texts"]),
        "category": category,
        "kind": kind,
    }


def render_reaction_block(reaction, ascii_only=None):
    """CLI 表示用。顔文字が目に飛び込むレイアウトにする。

        BATTER REACTION
            (・_・;)
          一瞬タイミングを取り直した
    """
    if ascii_only is None:
        ascii_only = not _stdout_supports_kaomoji()
    face = reaction["ascii_face"] if ascii_only else reaction["face"]
    color = _CAT_COLOR.get(reaction["category"], "white")
    head = ansi.dim("BATTER REACTION")
    return f"{head}\n    {ansi.paint(face, 'bold', color)}\n  {ansi.paint(reaction['text'], color)}"
