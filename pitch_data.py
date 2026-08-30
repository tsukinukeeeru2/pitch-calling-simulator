"""球種を「データ」として定義するファイル。

新しい球種を増やしたいときは、この PITCH_LIBRARY に 1 エントリ足すだけでよい。
judge.py に if 文を増やす必要はない ―― judge.py はここの数値を読むだけ。

各フィールドの意味:
    name            : 画面表示名(日本語)
    pitch_class     : ざっくり分類 "fastball" / "breaking" / "offspeed"
                      (打者の「待ち」は速球か変化球かの 2 択で考えるため、
                       fastball 以外はまとめて offspeed 扱いにする → guess_class_of)
    velocity        : 球速の目安(mph)。速いほど差し込みやすい
    movement        : 変化量の目安(0=素直, 大きい=よく曲がる/落ちる)
    whiff_rate      : 振られたときの空振り基礎率
    groundball_rate : フェアで打たれたときゴロになりやすさ(0-1)
    contact_quality : 打たれたとき強い打球になりやすさ(0-1, 低いほど打ち損じ)
    platoon         : 対戦相性。same = 投手と同じ手の打者 / opposite = 逆の手の打者
                      マイナスほど「その打者には有効(打たれにくい)」

将来 MLB データに置き換えるときも、この 1 ファイルの数値を
Statcast の球種別 Whiff% / GB% などで上書きすればよい(下の README 参照)。
"""

PITCH_LIBRARY = {
    "four_seam": {
        "name": "フォーシーム",
        "pitch_class": "fastball",
        "velocity": 95, "movement": 2,
        "whiff_rate": 0.11, "groundball_rate": 0.36, "contact_quality": 0.58,
        "platoon": {"same": 0.00, "opposite": 0.02},
    },
    "two_seam": {
        "name": "ツーシーム",
        "pitch_class": "fastball",
        "velocity": 93, "movement": 4,
        "whiff_rate": 0.08, "groundball_rate": 0.52, "contact_quality": 0.50,
        "platoon": {"same": -0.03, "opposite": 0.03},
    },
    "sinker": {
        "name": "シンカー",
        "pitch_class": "fastball",
        "velocity": 93, "movement": 5,
        "whiff_rate": 0.07, "groundball_rate": 0.58, "contact_quality": 0.47,
        "platoon": {"same": -0.04, "opposite": 0.03},
    },
    "cutter": {
        "name": "カッター",
        "pitch_class": "fastball",
        "velocity": 90, "movement": 4,
        "whiff_rate": 0.13, "groundball_rate": 0.45, "contact_quality": 0.50,
        "platoon": {"same": -0.05, "opposite": 0.02},
    },
    "slider": {
        "name": "スライダー",
        "pitch_class": "breaking",
        "velocity": 85, "movement": 6,
        "whiff_rate": 0.20, "groundball_rate": 0.47, "contact_quality": 0.44,
        "platoon": {"same": -0.07, "opposite": 0.05},
    },
    "sweeper": {
        "name": "スイーパー",
        "pitch_class": "breaking",
        "velocity": 82, "movement": 9,
        "whiff_rate": 0.22, "groundball_rate": 0.43, "contact_quality": 0.42,
        "platoon": {"same": -0.09, "opposite": 0.07},
    },
    "curveball": {
        "name": "カーブ",
        "pitch_class": "breaking",
        "velocity": 78, "movement": 8,
        "whiff_rate": 0.17, "groundball_rate": 0.45, "contact_quality": 0.43,
        "platoon": {"same": -0.04, "opposite": 0.04},
    },
    "changeup": {
        "name": "チェンジアップ",
        "pitch_class": "offspeed",
        "velocity": 84, "movement": 6,
        "whiff_rate": 0.19, "groundball_rate": 0.50, "contact_quality": 0.44,
        "platoon": {"same": 0.05, "opposite": -0.08},
    },
    "splitter": {
        "name": "スプリット",
        "pitch_class": "offspeed",
        "velocity": 86, "movement": 7,
        "whiff_rate": 0.25, "groundball_rate": 0.55, "contact_quality": 0.40,
        "platoon": {"same": 0.02, "opposite": -0.05},
    },
}

# 投手の「持ち球」。ここを差し替えれば投手ごとに球種セットを変えられる。
#   例: {"name": "剛腕", "throws": "R", "repertoire": ["four_seam", "slider", "changeup"]}
DEFAULT_PITCHER = {
    "name": "自軍投手",
    "throws": "R",
    "repertoire": ["four_seam", "sinker", "cutter", "slider", "curveball", "changeup"],
}


def all_pitch_keys():
    """ライブラリにある全球種のキー一覧。"""
    return list(PITCH_LIBRARY)


def get_pitch(key):
    """球種データ(辞書)を返す。"""
    return PITCH_LIBRARY[key]


def pitch_name(key):
    """画面表示名を返す。"""
    return PITCH_LIBRARY[key]["name"]


def guess_class_of(key):
    """打者の待ちは 速球 / 変化球 の 2 択。fastball 系以外はまとめて offspeed。"""
    return "fastball" if PITCH_LIBRARY[key]["pitch_class"] == "fastball" else "offspeed"


def family_of(key):
    """3 分類 "fastball" / "breaking" / "offspeed" をそのまま返す(打者の球種別成績で使う)。"""
    return PITCH_LIBRARY[key]["pitch_class"]


def velocity_of(key):
    return PITCH_LIBRARY[key]["velocity"]


def options_for_keys(keys):
    """球種キーのリストを {key: 表示名} の順序付き辞書にする(メニュー用)。"""
    return {key: PITCH_LIBRARY[key]["name"] for key in keys}


def repertoire_options(pitcher):
    """投手(dict でも Pitcher オブジェクトでも可)の持ち球を {key: 表示名} で返す。"""
    keys = pitcher["repertoire"] if isinstance(pitcher, dict) else pitcher.repertoire
    return options_for_keys(keys)


def platoon_adjust(key, pitcher_throws, batter_bats):
    """対戦相性の補正値。マイナス = その打者には打たれにくい。"""
    same = (pitcher_throws == batter_bats)
    return PITCH_LIBRARY[key]["platoon"]["same" if same else "opposite"]


# 既存コード・テスト用の別名(key -> 表示名)。中身は PITCH_LIBRARY と同じ。
PITCH_TYPES = {key: value["name"] for key, value in PITCH_LIBRARY.items()}
