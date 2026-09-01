# HR動画分析ツール — 設計 / ロジックドキュメント

TikTok / Instagram Reels の投稿URLを入力すると、Google Drive上に
「参考動画フォルダ・動画本体・文字起こしDocs・分析Docs・参考動画Slides」を
**1本あたり15〜35円** で自動生成するStreamlitアプリ。

このドキュメントは、他プロジェクトへ **ロジックを組み込むための設計図** として書かれている。
コードの隅々ではなく「なぜこの構造か・どのデータが何処に流れるか・どこを差し替えられるか」を優先して記述。

---

## 0. ひとことで

> 「URL → 動画DL → Whisperで文字起こし → 8枚キーフレーム抽出 → Claudeで構造化分析(JSON) → Drive上に4種の成果物を自動配置」

これを **URL単位で冪等（同じURLは2回目以降スキップ or 失敗ステップから再開）** に処理する。

---

## 1. システム構成図

```
┌─────────────────────────────────────────────────────────┐
│  Streamlit UI (app.py)                                   │
│  ┌──────────────┐  ┌──────────────────────────────────┐  │
│  │ 🎬 動画分析タブ │  │ 📊 実行履歴タブ                    │  │
│  │  URL入力→実行 │  │  一覧・フィルタ・再実行・CSV出力     │  │
│  └──────┬───────┘  └──────────────┬───────────────────┘  │
│         │                          │                     │
│         └─────────┬────────────────┘                     │
│                   ▼                                       │
│          process_single_url()  ← 10ステップの本体          │
└──────────┬──────────────────────────────┬──────────────┘
           │                              │
   ┌───────▼───────┐              ┌──────▼──────────┐
   │  modules/     │              │  history.json    │
   │  ──────────   │              │  (URL単位KV)     │
   │  downloader   │← yt-dlp      └─────────────────┘
   │  transcriber  │← Whisper(OpenAI)
   │  analyzer     │← Claude(Anthropic)
   │  drive/docs/  │← Google API
   │  slides       │  (Service Account)
   │  google_client│
   └───────────────┘
```

---

## 2. 処理パイプライン（10ステップ）

`app.py::process_single_url()` に集約。**各ステップは try/except で分離**されており、
失敗した時点のステップ名を `history.json` に記録して次回リトライ時に再開する。

| # | ステップ | 実装 | 入力 | 出力 | 再開時の扱い |
|---|---------|------|------|------|--------------|
| 1 | 動画DL | `downloader.download_video()` | URL | `filepath, meta` | 常に再ダウンロード |
| 2 | 文字起こし | `transcriber.transcribe()` | mp4 | `str` | `_transcript` があれば再利用 |
| 3 | キーフレーム抽出 | `downloader.extract_key_frames(count=8)` | mp4 | `list[path]` | 常に再抽出 |
| 4 | 構造化分析 | `analyzer.analyze_video()` | 文字起こし+画像+meta | `dict` | `_analysis` があれば再利用 |
| 5 | Driveフォルダ作成 | `drive.create_folder()` | タイトル | `folder_id` | `_folder_id` があれば再利用 |
| 6 | 動画UP | `drive.upload_file()` | mp4 | `video_file_id` | `_video_file_id` があればスキップ |
| 7 | サムネイル生成&UP | `downloader.extract_first_frame()` + `drive.upload_file()` | mp4 | `thumb_url` | `_thumb_url` があればスキップ・失敗しても継続 |
| 8 | 文字起こしDocs化 | `docs.create_doc_with_text()` | str | `transcript_doc_id` | 既存IDあればスキップ |
| 9 | 分析Docs化 | `docs.create_doc_with_text()` | 整形text | `analysis_doc_id` | 既存IDあればスキップ |
| 10 | Slides生成 | `slides.copy_template()` + `replace_placeholders()` | analysis dict | `slides_id` | 既存IDあればスキップ |

### 再開ロジックの要点
- `history.json` に **`_` プレフィックスの中間ID** (`_folder_id` `_transcript_doc_id` など) を保存
- 次回同URLを実行すると `history.find_by_url()` で prior_entry を取得
- prior が「成功」 → **完全スキップ**
- prior が「失敗」 → prior_entry を初期stateとして注入し、**失敗ステップ以前は再利用**
- 表示用フィールド（アンダースコアなし）は `st.dataframe` で見え、`_` 始まりは非表示

---

## 3. 主要モジュールの責務

### `modules/downloader.py`
- `yt-dlp` で TikTok / IG Reels をmp4取得
- 失敗時は `tikwm.com` の非公式APIを音声フォールバックとして使用
- `ffmpeg` / `ffprobe` を子プロセスで叩いて尺取得・キーフレーム抽出・サムネ生成
- **キーフレームは動画尺を等分割した中点で8枚** サンプリング（幅720pxにリサイズしトークン節約）

### `modules/transcriber.py`
- `ffmpeg` で音声だけ抽出 (`-b:a 64k`)
- OpenAI Whisper API に投げる（**25MB制限** に注意）
- 音声抽出できない動画（音楽のみ・無音等）は `"（音声なし／文字起こし不可）"` を返し、視覚のみで分析継続

### `modules/analyzer.py` ★このツールの心臓部
- Claude Sonnet 4.6 に対し **文字起こし + 8枚のキーフレーム画像** を同時投入
- **`tools` 経由の tool_use で構造化出力を強制**（`tool_choice={"type":"tool", "name":"output"}`）
- 出力スキーマ（JSON Schema）:

```json
{
  "video_title": "20文字以内のタイトル",
  "classification": "6択から1つ",   // 若手社員の1日密着 / 入社理由・就活ストーリー / キャリアパス可視化 / 内定者・同期の雰囲気 / 経営者・先輩の本音Q&A / 理念・ブランドストーリー
  "tone": "6択から1つ",             // エンタメ・ネタ系 / バラエティ・キャラ系 / ドキュメント・リアル系 / 対話・トーク系 / エモ・シネマ系 / 情報整理・カード系
  "hook": ["45字以内", "45字以内"],           // ちょうど2項目
  "structure": ["45字以内", "×3"],           // ちょうど3項目
  "adaptation": ["45字以内", "×3"],          // ちょうど3項目
  "summary_title": "資料に載せるサブタイトル(1行)"
}
```

**プロンプト設計の意図**（`SYSTEM` プロンプトより）:
- 「頑張ります」等の抽象語禁止 → 「ロケ・演出・数字・行動・視覚要素」を必ず書かせる
- 「体言止めまたは断定形」→ 資料スライドの点線枠にそのまま貼れる密度
- 「意味の重複禁止・1項目1論点」→ 抽出項目のカニバリ防止

### `modules/google_client.py`
- サービスアカウントJSON (`credentials.json`) から Drive/Docs/Slides の3クライアントを生成
- `.env` の `GOOGLE_SERVICE_ACCOUNT_JSON` でパス指定

### `modules/drive.py`
- `create_folder()` / `upload_file()` / `set_anyone_reader()` （リンクを知る全員に閲覧権限）
- 共有ドライブ配下に配置（`supportsAllDrives=True`）

### `modules/docs.py`
- Google Docs を新規作成 → プレーンテキストを一括挿入

### `modules/slides.py`
- **テンプレSlidesをコピー** → プレースホルダー `{{キー}}` を `analysis` の値で置換
- サムネイル画像は特定名の枠 (`{{サムネイル}}` / `サムネイル貼り付け枠`) を画像で差し替え
- URLハイパーリンク化

### `modules/history.py`
- `history.json` （JSON1ファイル）で **URLをキーにした冪等ストア** を実装
- `find_by_url()` / `save_entry()` / `load()` / `clear()`

---

## 4. データフロー詳細

### 4-1. 分析Docsのテキスト整形（`format_analysis_doc`）

```
# 参考動画分析

タイトル: {video_title}
サブタイトル: {summary_title}
類型: {classification}
トーン: {tone}
URL: {url}
プラットフォーム: {TikTok/Instagram Reels等}
アカウント名: {uploader} / @{uploader_id}
尺: 約XX秒
投稿日: YYYY年MM月DD日

## フック（冒頭2秒で何が起きるか）
1. ...
2. ...

## 構成メモ（本編の展開・編集の特徴）
1. ... 2. ... 3. ...

## 転用ポイント（貴社版で真似る要素）
1. ... 2. ... 3. ...
```

### 4-2. Slidesプレースホルダー対応表（`build_slide_replacements`）

| テンプレ内表記 | 値 |
|---------------|---|
| `{{動画タイトル}}` | `analysis.video_title` |
| `{{タイトル}}` | `analysis.summary_title` |
| `{{類型}}` `{{トーン}}` | 6択の分類結果 |
| `{{URL}}` `{{動画URL}}` | 元動画URL |
| `{{プラットフォーム}}` | TikTok / Instagram Reels |
| `{{アカウント名}}` | `{uploader} / @{uploader_id}` |
| `{{尺}}` | 「約XX秒」or「約Y分Z秒」 |
| `{{投稿日}}` | 「YYYY年MM月DD日」 |
| `{{フック1}}` `{{フック2}}` | hook 2項目 |
| `{{構成メモ1..3}}` | structure 3項目 |
| `{{転用ポイント1..3}}` | adaptation 3項目 |
| `{{文字起こしURL}}` `{{分析URL}}` `{{フォルダURL}}` | 生成物のDrive URL |

> 落とし穴：Slidesエディタのオートコレクトで `{{` と `}}` が分割されると置換されない。プレーンテキスト貼り付け必須。

### 4-3. `history.json` のスキーマ（1エントリ）

```json
{
  "実行日時": "2026-09-01 11:30:55",
  "URL": "https://www.tiktok.com/@example/video/xxx",
  "ステータス": "成功" | "失敗",
  "失敗ステップ": "文字起こし" | "分析（Claude）" | ...,
  "エラー": "...",
  "タイトル": "...",
  "類型": "...",
  "トーン": "...",
  "プラットフォーム": "...",
  "アカウント": "...",
  "尺": "約42秒",
  "投稿日": "2026年08月01日",
  "Driveフォルダ": "https://drive.google.com/...",
  "文字起こしDoc": "https://docs.google.com/...",
  "分析Doc": "https://docs.google.com/...",
  "Slides": "https://docs.google.com/presentation/...",

  "_folder_id": "…",
  "_video_file_id": "…",
  "_thumb_url": "…",
  "_transcript_doc_id": "…",
  "_analysis_doc_id": "…",
  "_slides_id": "…",
  "_transcript": "文字起こし本文",
  "_analysis": { /* Claude 分析結果 dict */ }
}
```
**アンダースコア始まり = 再開用の内部ステート**（UI非表示）。それ以外 = 表示用。

---

## 5. 外部依存

| 種別 | サービス | 用途 | 環境変数 |
|-----|---------|------|---------|
| LLM | Anthropic Claude | 動画分析（マルチモーダル） | `ANTHROPIC_API_KEY` `ANTHROPIC_MODEL` |
| ASR | OpenAI Whisper API | 音声文字起こし | `OPENAI_API_KEY` |
| ストレージ/Docs | Google Drive / Docs / Slides | 成果物配置 | `GOOGLE_SERVICE_ACCOUNT_JSON` `SHARED_DRIVE_FOLDER_ID` `TEMPLATE_SLIDES_ID` |
| CLI | yt-dlp | 動画ダウンロード | — |
| CLI | ffmpeg / ffprobe | 音声抽出・キーフレーム・尺取得 | — |
| フォールバック | tikwm.com API | TikTok音声取得失敗時 | — |

---

## 6. コスト構造（1本あたり）

| 項目 | 単価 | 目安 |
|-----|------|------|
| Whisper | $0.006 / 分 | 1〜5円 |
| Claude Sonnet 4.6（画像8枚+テキスト） | 入出力数千〜数万トークン | 10〜30円 |
| Google APIs | 無料枠内 | 0円 |
| **合計** | | **15〜35円 / 本** |

---

## 7. 他プロジェクトへの組み込みポイント

このツールから **切り出して再利用しやすい単位** は以下：

### A. 「動画→構造化分析JSON」のコア
最小構成:
```
downloader.download_video()          # yt-dlp ラッパ
+ downloader.extract_key_frames()    # ffmpeg で8枚
+ transcriber.transcribe()           # Whisper
+ analyzer.analyze_video()           # Claude tool_use で構造化
```
戻り値の JSON スキーマ（第4節）はそのまま契約として使える。

### B. 「Claude tool_use で構造化出力を強制」パターン
`analyzer.py` の下記は他ドメインにも転用可：
- `tools=[{"name": "output", "input_schema": SCHEMA}]`
- `tool_choice={"type": "tool", "name": "output"}`
- レスポンスから `block.type == "tool_use"` を拾って `block.input` を返す

**プロンプト側の締めゼリフ**を「output ツール経由で返してください」にすることで
自由記述→JSON変換のパースエラーがゼロになる。

### B'. スキーマ設計の勘所
- `maxLength` / `minItems` / `maxItems` / `enum` を **全項目に付ける** ことでハルシネーション削減
- SYSTEMプロンプトに **禁止語リスト**（「頑張ります」等）を明記
- **観察の粒度** を「ロケ・演出・数字・行動・視覚要素」と具体列挙で指定

### C. Google Slides テンプレ差し込みパターン
- テンプレスライドに `{{キー}}` を置いておく → 辞書で一括置換
- 特定名の枠を画像に差し替える（サムネイル）
- URL自動ハイパーリンク化

### D. URL単位の冪等パイプライン
- **1ファイル(JSON)を状態ストアにする** 素朴な設計
- ステップごとに `try/except` → 失敗ステップ名を記録 → 次回prior_entryから復元
- 成功済み中間IDを保持しておくことで **リトライ時にDrive上のファイルを重複作成しない**

---

## 8. 制約と拡張候補

### 現在の制約
- 非公開IG投稿はyt-dlpがログイン要求 → 未対応
- Whisperの25MB制限 → 長尺動画は音質を落とす必要あり
- SlidesのプレースホルダーはSlidesオートコレクトで壊れやすい
- サービスアカウントは共有ドライブ・テンプレSlidesに個別共有が必要

### 拡張候補
- 過去分析の一覧化・全文検索
- SlackやChatwork通知連携
- 分析結果を DB (Postgres / BigQuery) に永続化して横断集計
- スキーマを類型別に切り替え（他業種展開）

---

## 9. ディレクトリ構成

```
hrbunseki-tool/
├── app.py                 # Streamlit UI + パイプライン本体
├── config.py              # .env読み込み・設定バリデーション
├── history.json           # URL単位KV（成果物ID・状態）
├── requirements.txt
├── .env.example
├── credentials.json       # サービスアカウント鍵（要gitignore）
├── docs/
│   └── design.md          # ← このファイル
└── modules/
    ├── downloader.py      # yt-dlp / ffmpeg / tikwm フォールバック
    ├── transcriber.py     # Whisper
    ├── analyzer.py        # Claude（tool_use で構造化）
    ├── google_client.py   # サービスアカウント認証
    ├── drive.py           # フォルダ作成・UP・権限付与
    ├── docs.py            # Docs作成＋テキスト挿入
    ├── slides.py          # テンプレコピー＋プレースホルダー置換
    └── history.py         # history.json 読み書き
```
