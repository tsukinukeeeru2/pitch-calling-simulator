# 引き継ぎメモ（別チャット / スマホで続きをやるとき用）

このファイルと `README.md` を読めば、新しいチャットでそのまま続きができます。
（Claude のメモリにも要点は保存済み。`C:\Users\tsuki\pitch-calling-simulator` を開いて作業）

最終更新: 2026-08-28（Web版を「メイン作品」として全面刷新。Game Engine を CLI/Web で共通化、MLB実データ Adapter を追加）

---

## この作品は何か

- **SEGA のプログラマ応募ポートフォリオ**用。作者（tsuki）にとって初めての個人開発。
- Python 製の CLI ゲーム「**終盤の守り切りシミュレーター**」。
  毎回ちがう終盤の場面（7〜9回・延長、点差、走者、相手打順）から始まり、
  **その半イニングで 3 アウトを取るまで**が 1 ゲーム。
- 捕手として **球種 / 狙うコース（3×3）/ 配球意図** を選ぶ。
  打者の反応（顔文字）・配球履歴・打球チャート・守備力を読み、**正解は表示されない**。
- 「一番高い確率のボタンを押すゲーム」ではなく、**不完全情報での意思決定**がテーマ。

## 作者の希望（守るべき進め方）

- 実装しながら **日本語で、初心者が追える説明**（何を・なぜ・どのファイル・ロジックの流れ）。
- **作者が自分でコードを説明できること**を最優先。凝った短縮より、読める加算式ロジック。
- **識別子・ファイル名は英語**、コメント・UI テキストは日本語。
- 大きい追加を勝手にしない。スコープを確認するか、自走を指示されたら
  **最後に「仕様判断の一覧」**を出す。
- 既存のモジュール分割（1 ファイル 1 責務）を壊さず、新ファイルで拡張。
- 大きな作業のあとは **自動テスト + 複数ゲームの自動実行** で確認。

`screenshots/` にCLI版・Web版のスクリーンショットあり（README冒頭に埋め込み済み）。

## 動かし方

```bash
cd pitch-calling-simulator
python webapp.py                                 # おすすめ: ブラウザ版（http://127.0.0.1:8765）
python webapp.py --mlb-demo                      # 実在選手のサンプル打線で遊ぶ
python webapp.py --seed 12345                    # 場面・投球判定を再現可能にする
python main.py                                   # CLI版
python main.py --lineup lineups/sample_team.json # 相手打線を差し替え
python main.py --difficulty beginner             # ヒント多め / expert で打球チャートを隠す
python main.py --mlb-demo --mlb-live             # MLB Stats API から実データを取得（失敗時は自動フォールバック）
python tests.py                                  # 自動テスト（末尾に 1000 ゲームのストレス、Game Engine 直通含む）
python stats.py                                  # logs/ を集計した成績ダッシュボード
python replay.py                                 # 直近のゲームを名場面だけリプレイ
```

- 色: 端末なら自動 ON。パイプ時 OFF。`PITCHSIM_COLOR=1` 強制 ON / `NO_COLOR` 強制 OFF。
- リズム QTE: 端末なら自動 ON。非対話なら OFF（確率モデルに委任）。`PITCHSIM_QTE=1/0` で強制。Web版は常に強制OFF。
- CLI操作: `Enter`=投球 / `d`=守備変更 / `m`=読みメモ / `w`=敬遠 / `p`=継投（ブルペンにいる人数だけ） / `q`=終了。
- Web版はクリック→クリックで守備Swap、常時表示の守備図・打者カード・Pitch Call フォームで完結（JavaScriptなし、フォーム送信のみ）。

---

## アーキテクチャ（重要）— Game Engine を CLI と Web で共有する

```
        Game Engine（engine.py）
     resolve_one_pitch() が全部の入口
              ↑              ↑
         main.py        webapp.py
        （CLI版）        （Web版）
```

判定ロジック（球種→コース→意図→打席の結果）は `engine.resolve_one_pitch()` だけで
解決する。`main.py`（CLI）と `webapp.py`（Web）はどちらもこの関数を呼ぶだけで、
**判定ロジックの二重実装はしていない**。`--seed` で乱数を固定すれば CLI/Web どちらでも
同じ場面・同じ結果が再現できる（テスト `test_same_seed_gives_same_engine_result_cli_and_web_share` /
`test_seed_reproducible_over_http` で担保）。

## モジュール地図（23 ファイル）

### Game Engine（CLI・Web 共通、表示に依存しない層）

| ファイル | 責務 |
|---|---|
| `engine.py` | **共通 Game Engine の入口**。`resolve_one_pitch()`（1球の判定+周辺処理をまとめる）・`apply_result`・`log_pitch`・`save_game_log`・`maybe_resolve_sign_steal` |
| `match_state.py` | `MatchState`（試合状況）＋ `generate_random_situation()`。走塁・イニング・盗塁・ブロッキング・継投・敬遠 |
| `lineup.py` | 相手打線 9 人（archetype）／`Lineup`（打順回転）／`from_records` / `load_lineup_file`（CSV・JSON） |
| `pitcher.py` | `Pitcher`（@dataclass）と `execute_pitch()`（要求 → 実際の投球。失投でコースがズレる） |
| `batter.py` | `Batter`（@dataclass。公開: 左右・OPS・タイプ／隠し: 苦手球種・狙い球のクセ 等）と `predict_guess()` |
| `pitch_data.py` | 球種のデータ（`PITCH_LIBRARY`）。1 行足せば球種が増える |
| `strategy.py` | 配球意図・セットアップ（前球が次を活かす）・`catcher_decision_quality`（内部指標）・試合後分析（`build_analysis`）・**Postgame Report集計（`build_postgame_report`）**・読み採点 |
| `pitch_history.py` | 配球履歴（記録とパターン照会のみ） |
| `judge.py` | 1 球の判定。実投→狙い→セットアップ→タイミング→振る/見る→フェアなら打球＋守備＋走塁 |
| `batted_ball.py` | `BattedBall`（@dataclass）。フェア打球の中身（方向 / 種類 / 強さ / 飛距離） |
| `baserunning.py` | 打球後の走塁（長打進塁・併殺・犠飛・タッチアップ・進塁打）を状態を変えずに解決 |
| `fielders.py` | `Fielder`（@dataclass）と `position_fit`（同じ選手でもポジションで守備力が変わる） |
| `defense.py` | `Defense`（7 人の割り当て・守備シフト・EDE 参考値） |
| `fielding.py` | 打球＋守備配置 → アウト/安打種別。加算式・内訳つき。`_hit_extent` が単打〜本塁打 |
| `reaction.py` | 打者の反応（顔文字 + 短文 + カテゴリ 10 種、ASCII フォールバック）。隠し情報は見ない |
| `constants.py` | 9 分割コース（`in_hi`〜`out_lo`）とヘルパー `zone_x/zone_y/is_corner/zone_overlap`、補正値、反応確率 |
| `qte.py` | 送球・ブロッキングのリズム / QTE ミニゲーム（標準ライブラリのみ、非対話なら 0.0 を返す） |

`@dataclass` は「データを保持するのが主目的」な4クラス（Batter/Pitcher/Fielder/BattedBall）だけに
適用（コンストラクタの引数名と属性名を一致させ、`__post_init__` で正規化・派生値を計算）。
`MatchState`/`Defense` のような「振る舞い（メソッドで状態を変える）が主目的」のクラスは対象外。

### 表示層（CLI と Web、それぞれ Game Engine を呼ぶだけ）

| ファイル | 責務 |
|---|---|
| `ansi.py` | 端末の色付け（パイプ時は自動無効） |
| `ballpark_view.py` | 守備位置を「球場を上から見た図」の色付き文字列に |
| `ui.py` | スコアボード・ベース図・打者カード・打球チャート・1 球結果・試合後分析（CLI） |
| `main.py` | **CLI 版**の進行役。入力・`--lineup`/`--mlb-demo`/`--seed` 読込・ゲームログ保存・`ui.py` を呼んで表示 |
| `webapp.py` | **Web 版**（一番のおすすめ）。標準ライブラリの `http.server` のみ、JavaScript不使用でフォーム送信のみ。球場図・ストライクゾーンのTarget/Actual・Postgame Report(CATCHER REPORT)まで完結 |

### ツール

| ファイル | 責務 |
|---|---|
| `stats.py` | `logs/game_*.json` を横断集計する成績ダッシュボード（コース別/意図別の被弾率・decision_quality） |
| `replay.py` | 1 ゲームのログを名場面だけ時系列で振り返るリプレイビューア（`strategy.build_analysis()` の分類を再利用） |
| `mlb_data_adapter.py` | 実データ（MLB Stats API / Savant 風）を Batter 互換 dict に変換する Adapter。`build_demo_lineup()` が `--mlb-demo`/`--mlb-live` の入口 |

### データ

- `lineups/sample_team.json` … 差し替え用の架空サンプル打線（`_note` などの余分キーは `from_records` が無視）
- `mlb_data/sample_lineup_raw.json` … 実在選手9人のサンプル生データ（`--mlb-demo` のフォールバック先。数値は近似・簡略化）
- `logs/game_*.json` … 1 ゲームごとに自動保存（場面・全投球・decision_quality・打球・盗塁等・読み採点）

---

## 実装済みの機能

- [x] ランダムな終盤の場面生成（接戦が出やすい重み）
- [x] 相手打線 9 人（特徴が明確に違う archetype）＋打順回転
- [x] 打者の隠しパラメータ（狙い球のクセ・苦手球種・得意/苦手ゾーン・2 ストライク対応・プレッシャー耐性）
- [x] 狙い球ロジック（カウント・履歴・投手球種割合・前球・性格）
- [x] Pitch Execution（要求→実投のズレ、球の出来 quality、球数による疲労）
- [x] 配球意図 5 種（strike / chase / weak_contact / waste / freeze）
- [x] セットアップ（前球が次を活かす、条件付き・合計 clamp、固定ボーナスなし）
- [x] Catcher Decision Quality（内部のみ。プレイ中は絶対に非表示）
- [x] 打者の反応 = 顔文字 + 文（本音 60% / ミスリード 15% / ぼかし 25%）、ASCII フォールバック
- [x] 守備配置（7 人 Swap・守備シフト・EDE 参考値）、`position_fit`、穴の 1 人を隠す判断
- [x] 加算式のアウト確率＋内訳、俊足打者の内野安打
- [x] 長打（単打/二塁打/三塁打/本塁打）と走塁（併殺・犠飛・タッチアップ・進塁打）
- [x] 9 分割コース（3×3）、`zone_overlap` で苦手/得意ゾーンが部分一致でも効く
- [x] 敬遠（`w`）、継投（`p`、ブルペンの控えが尽きるまで何度でも、球数リセット・持ち球は投げるまで不明）
- [x] 盗塁（一塁のみ・二塁空き・2 アウト未満、速球は刺しやすい）
- [x] 振り逃げ（一塁が空 or 2 アウトでワンバウンド三振 → 後逸なら無条件で生きる、捕手が拾えば一塁へ送球 QTE）
- [x] エラーによる進塁（アウトになるはずの打球を野手が落球/悪送球。総合力・ポジション適性・打球の強さで確率が動く）
- [x] バント警戒シフト（一塁・三塁が前へ。弱い打球は処理しやすく、強い打球には弱くなる）
- [x] サイン交換ミニQTE（二塁走者にサインを覗われるリスク。守れなければ次の1球だけ打者の狙いが実際の球種に強く一致する）
- [x] 打席をまたぐ配球バレ（打者一巡で同じ打者と再戦したとき、前の打席の球種傾向を薄く覚えている）
- [x] 成績ダッシュボード `stats.py`（複数ゲームログをコース別/意図別に集計、被弾率・decision_quality）
- [x] リプレイビューア `replay.py`（1ゲームの名場面を時系列で振り返り、build_analysis()の分類を再利用）
- [x] 難易度設定 `--difficulty beginner/normal/expert`（beginner はヒント表示追加、expert は打球チャート非表示。隠しパラメータには一切触れない）
- [x] リズム / QTE：盗塁刺し（①反応 ②連打 ③送球リズム）、ブロッキング（方向ボタン 内/中/外 ＋ 1・2・3 リズム）、投手の暴投も同じブロッキング
- [x] 試合後分析（結果 と 判断の質 を分けて評価）、読みメモの採点
- [x] 色つき UI、打球チャート（ゲーム中に傾向が溜まる）
- [x] `--lineup` で CSV/JSON 差し替え、ゲームログ保存
- [x] **共通 Game Engine（`engine.py`）に判定ロジックを一本化**。CLI/Web で二重実装なし、`--seed` で両方とも再現可能
- [x] **必要な範囲だけ `@dataclass` 化**（Batter/Pitcher/Fielder/BattedBall。振る舞い中心のクラスは対象外）
- [x] **Web UI 全面刷新**（`webapp.py`）。野球中継のアナリティクス画面に近いレイアウトを「メイン作品」として実装。
      球場図クリック→クリックで守備Swap・捕手メモ・Situation Card導入・ストライクゾーン上のTarget/Actual 2マーカー・
      Postgame Report（CATCHER REPORT、Decision Quality/Pitch Execution/Defenseを別軸で表示）まで、
      **CLI版のすべての機能が Web 版でも使える**（機能差なし）
- [x] **MLB 実データ Adapter**（`mlb_data_adapter.py`）＋ `--mlb-demo`（CLI・Web両方）。
      実在選手9人のサンプル打線、`--mlb-live` で MLB Stats API から実際に取得（失敗時は自動でサンプルにフォールバック、ゲームは絶対に落ちない）
- [x] **継投を複数回可能に**。ブルペンに1〜3人が待機し、控えが尽きるまで何度でも交代できる
      （CLI/Webどちらも複数候補から選べるUI。`match_state.change_pitcher(index)`）
- [x] **Web版のスマホ崩れ修正**。320px幅で守備配置ボタンが重なるバグと、配球履歴表が
      文字単位で折り返されるバグを発見・修正（Playwrightで複数幅のスクリーンショット確認）
- [x] **hot_course/weak_course を実データで上書きできる受け口**（`_hot_weak_from_zone_profile`）。
      `zone_profile` を渡せば選手名からの山勘ではなく実データから計算する。Savant のゾーンデータの
      自動取得は行っていない（下記「まだ」参照）
- [x] 自動テスト 106 件（Game Engine 直通の1000ゲームストレス含む）

## まだ / 次の候補

- [ ] 盗塁で個々の走者の足を使う（今は平均値。走者の identity を持っていない）
- [ ] 中継・カットプレー、犠打・エンドラン
- [ ] 複数イニング / 1 試合フルのモード（**見送り中**。自軍の打撃が今は完全に未実装なので、
      フル実装は「自軍の打撃を作る」か「攻撃側イニングを自動スキップする」かの設計判断が要る、
      規模の大きい新規サブシステムになる）
- [ ] **Baseball Savant のゾーン別成績を自動取得して zone_profile を作る。** 受け口とテストは
      あるが自動取得は未実装 ―― Savant の zone 番号(1〜14, 捕手視点)は打者の左右で意味が変わり、
      このゲームの表記へ正しく変換できているかを実データで検証できる環境が必要（この開発環境は
      組織ポリシーで外部ネットワークが遮断されており検証できなかった）
- [ ] 投手の左右をスコアボードでもっと目立たせる
- [ ] Web版の専用モバイルUI最適化（タップ領域の拡大など。崩れないところまでは対応済み）

---

## 設計の約束（壊さない）

1. **プレイ中に「正解の配球 / 守備 / 内部スコア」を表示しない。** 分析は 3 アウト後だけ。
2. `catcher_decision_quality` は内部専用（`ui` のダッシュボード / 1 球結果に絶対出さない。テスト `test_decision_quality_hidden_from_live_ui` で担保）。
3. `reaction.py` は打者の隠しパラメータを引数で受け取らない（構造的に弱点が透けない）。
4. セットアップ・意図の効果は **条件付き＋合計 clamp**。固定の「＋20%」を作らない。
5. 乱数を使う関数は `rng` を引数で受けられる（テストの再現性）。本編は既定 `random`。
6. QTE は**結果を確定させない**。`throw_bonus` / `block_bonus` は確率をずらすだけ。
7. 色・QTE は非対話環境で自動 OFF（テスト・パイプ実行を壊さない）。
8. コースは 9 分割。文字列直書きせず `zone_x/zone_y/is_corner/zone_overlap` を使う。
9. **判定ロジックは `engine.resolve_one_pitch()` に一本化する。** CLI/Web どちらも同じ関数を呼ぶだけにし、判定ロジックを二重実装しない。

## だいたいの数値感（ランダムプレイ 1000 ゲーム）

- 1 半イニングの平均失点 ≈ 0.79 ／ BABIP ≈ .39 ／ 長打は安打の約 21%
- 盗塁刺（中立送球）≈ 32% ／ 低め暴投の後逸（中立ブロック）≈ 45%

## バランス調整の場所

球種=`pitch_data.py` / 打線=`lineup.py` / 投手=`pitcher.py` / 場面=`match_state.py` /
意図・セットアップ・判断の質=`strategy.py` / 反応と顔文字=`reaction.py` /
守備=`fielders.py`・`defense.py`・`fielding.py` / 打球=`batted_ball.py` /
コース補正=`constants.py`（`COURSE_PROFILE`）/ QTE の難易度=`qte.py`
