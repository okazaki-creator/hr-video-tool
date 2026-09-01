import os
import re
import tempfile
from datetime import datetime

import streamlit as st
import pandas as pd

import config
from modules import downloader, transcriber, analyzer, drive, docs, slides, history


st.set_page_config(page_title="HR動画分析ツール", page_icon="🎬", layout="wide")
st.title("🎬 HR動画分析ツール")
st.caption("IG / TikTok URLを入れると、Google Driveにフォルダ・動画・文字起こし・分析Docs・スライドを一括生成します。")

# --- 設定チェック ---
errors = config.validate()
if errors:
    st.error("環境設定に不足があります：")
    for e in errors:
        st.write(f"- {e}")
    st.stop()


def sanitize(name: str, max_len: int = 40) -> str:
    name = re.sub(r"[\\/:*?\"<>|\n\r\t]", "_", name).strip()
    return name[:max_len] or "untitled"


def fmt_duration(seconds) -> str:
    if not seconds:
        return "尺不明"
    try:
        seconds = int(seconds)
    except (TypeError, ValueError):
        return "尺不明"
    if seconds < 60:
        return f"約{seconds}秒"
    m, s = divmod(seconds, 60)
    return f"約{m}分{s}秒" if s else f"約{m}分"


def fmt_upload_date(yyyymmdd: str | None) -> str:
    if not yyyymmdd or len(yyyymmdd) != 8:
        return ""
    return f"{yyyymmdd[0:4]}年{yyyymmdd[4:6]}月{yyyymmdd[6:8]}日"


def fmt_count(n) -> str:
    """再生数などを 12,345 / 1.2万 表記に。取得できていなければ「取得不可」。"""
    if n is None or n == "":
        return "取得不可"
    try:
        n = int(n)
    except (TypeError, ValueError):
        return "取得不可"
    if n >= 10000:
        return f"{n / 10000:.1f}万".replace(".0万", "万")
    return f"{n:,}"


def fmt_rate(r) -> str:
    return "取得不可" if r is None else f"{r}%"


def format_metrics_block(meta: dict) -> list[str]:
    """分析Docsに差し込むパフォーマンス指標のブロック。"""
    er = downloader.engagement_rate(meta)
    lines = [
        "",
        "## パフォーマンス指標",
        f"再生数: {fmt_count(meta.get('view_count'))}",
        f"いいね: {fmt_count(meta.get('like_count'))}",
        f"コメント: {fmt_count(meta.get('comment_count'))}",
        f"エンゲージメント率: {fmt_rate(er)}　※(いいね+コメント)÷再生数",
    ]
    if meta.get("follower_count"):
        lines.append(f"アカウントのフォロワー: {fmt_count(meta.get('follower_count'))}")
    if meta.get("view_count") is None:
        lines.append("※ このプラットフォームでは再生数が公開APIから取得できませんでした。")
    return lines


def format_analysis_doc(analysis: dict, meta: dict, url: str, duration_sec, mode: str | None = None) -> str:
    lines = [
        "# 参考動画分析",
        "",
        f"タイトル: {analysis.get('video_title', '')}",
        f"サブタイトル: {analysis.get('summary_title', '')}",
        f"類型: {analysis.get('classification', '')}",
        f"トーン: {analysis.get('tone', '')}",
        f"URL: {url}",
        f"プラットフォーム: {downloader.platform_label(meta)}",
        f"アカウント名: {meta.get('uploader', '')} / @{meta.get('uploader_id', '')}",
        f"尺: {fmt_duration(duration_sec)}",
        f"投稿日: {fmt_upload_date(meta.get('upload_date'))}",
    ]
    lines += format_metrics_block(meta)
    lines += [
        "",
        "## フック（冒頭2秒で何が起きるか）",
    ]
    for i, s in enumerate(analysis.get("hook", []), 1):
        lines.append(f"{i}. {s}")
    lines += ["", "## 構成メモ（本編の展開・編集の特徴）"]
    for i, s in enumerate(analysis.get("structure", []), 1):
        lines.append(f"{i}. {s}")
    lines += ["", f"## {analyzer.get_profile(mode).get('doc_heading', '転用ポイント')}"]
    for i, s in enumerate(analysis.get("adaptation", []), 1):
        lines.append(f"{i}. {s}")
    return "\n".join(lines)


def build_slide_replacements(
    analysis: dict,
    meta: dict,
    url: str,
    duration_sec,
    transcript_doc_url: str = "",
    analysis_doc_url: str = "",
    folder_url: str = "",
) -> dict:
    hook = analysis.get("hook", []) + ["", ""]
    structure = analysis.get("structure", []) + ["", "", ""]
    adaptation = analysis.get("adaptation", []) + ["", "", ""]
    return {
        "動画タイトル": analysis.get("video_title", ""),
        "タイトル": analysis.get("summary_title", ""),
        "類型": analysis.get("classification", ""),
        "トーン": analysis.get("tone", ""),
        "URL": url,
        "動画URL": url,
        "プラットフォーム": downloader.platform_label(meta),
        "アカウント名": f"{meta.get('uploader', '')} / @{meta.get('uploader_id', '')}",
        "尺": fmt_duration(duration_sec),
        "投稿日": fmt_upload_date(meta.get("upload_date")),
        "再生数": fmt_count(meta.get("view_count")),
        "いいね": fmt_count(meta.get("like_count")),
        "コメント": fmt_count(meta.get("comment_count")),
        "エンゲージメント率": fmt_rate(downloader.engagement_rate(meta)),
        "フック1": hook[0],
        "フック2": hook[1],
        "構成メモ1": structure[0],
        "構成メモ2": structure[1],
        "構成メモ3": structure[2],
        "転用ポイント1": adaptation[0],
        "転用ポイント2": adaptation[1],
        "転用ポイント3": adaptation[2],
        "文字起こしURL": transcript_doc_url,
        "分析URL": analysis_doc_url,
        "フォルダURL": folder_url,
    }


def build_account_summary(entries: list[dict]):
    """履歴をアカウント単位に集計する。起用候補どうしの比較に使う。

    再生数は Edits実測(_view_count_manual) > 自動取得(_view_count) の優先。
    取得できなかった投稿は平均の母数から除外し、「本数」には分析した全本数を出す。
    保存数はEditsアプリでの実測値のみ（自動取得は不可）。
    """
    rows = {}
    for e in entries:
        if e.get("ステータス") != "成功":
            continue
        acct = e.get("アカウント") or "（不明）"
        r = rows.setdefault(acct, {"views": [], "likes": [], "ers": [], "durs": [], "saves": [], "srs": [], "n": 0})
        r["n"] += 1
        v = history.effective_view_count(e)
        l = e.get("_like_count")
        c, d = e.get("_comment_count"), e.get("_duration_sec")
        sv = e.get("_save_count")
        if v:
            r["views"].append(v)
            if isinstance(l, (int, float)):
                r["ers"].append((l + (c or 0)) / v * 100)
            if isinstance(sv, (int, float)):
                r["srs"].append(sv / v * 100)
        if isinstance(l, (int, float)):
            r["likes"].append(l)
        if isinstance(sv, (int, float)):
            r["saves"].append(sv)
        if isinstance(d, (int, float)) and d:
            r["durs"].append(d)

    if not rows:
        return None

    def avg(xs):
        return sum(xs) / len(xs) if xs else None

    def median(xs):
        if not xs:
            return None
        s = sorted(xs)
        m = len(s) // 2
        return s[m] if len(s) % 2 else (s[m - 1] + s[m]) / 2

    out = []
    for acct, r in rows.items():
        out.append({
            "アカウント": acct,
            "本数": r["n"],
            "平均再生数": fmt_count(round(avg(r["views"])) if r["views"] else None),
            "中央値再生数": fmt_count(round(median(r["views"])) if r["views"] else None),
            "最高再生数": fmt_count(max(r["views"]) if r["views"] else None),
            "平均保存数": fmt_count(round(avg(r["saves"])) if r["saves"] else None),
            "平均保存率": fmt_rate(round(avg(r["srs"]), 2) if r["srs"] else None),
            "平均いいね": fmt_count(round(avg(r["likes"])) if r["likes"] else None),
            "平均EG率": fmt_rate(round(avg(r["ers"]), 2) if r["ers"] else None),
            "平均尺": fmt_duration(round(avg(r["durs"])) if r["durs"] else None),
            "_sort": avg(r["views"]) or -1,
        })
    df = pd.DataFrame(out).sort_values("_sort", ascending=False).drop(columns=["_sort"])
    return df.reset_index(drop=True)


# --- タブ ---
tab_run, tab_history = st.tabs(["🎬 動画分析", "📊 実行履歴"])


def parse_urls(raw: str) -> list[str]:
    """カンマまたは改行で区切られた入力から、URLだけを抽出。"""
    if not raw:
        return []
    parts = re.split(r"[,\n\r]+", raw)
    return [p.strip() for p in parts if p.strip()]


STEP_LABELS = {
    "download": "ダウンロード",
    "transcribe": "文字起こし",
    "keyframes": "キーフレーム抽出",
    "analyze": "分析（Claude）",
    "folder": "フォルダ作成",
    "video_up": "動画UP",
    "thumbnail": "サムネイル生成",
    "transcript_doc": "文字起こしDoc",
    "analysis_doc": "分析Doc",
    "slides": "スライド生成",
}


def _delete_history_key(entry: dict, key: str):
    if key in entry:
        del entry[key]


def process_single_url(
    url: str,
    tmpdir: str,
    log,
    progress,
    prefix: str = "",
    prior_entry: dict | None = None,
) -> dict:
    """1本のURLを処理し、履歴エントリを返す。
    prior_entry が渡された場合、既存アーティファクト（フォルダ・Doc・Slides）を再利用して
    失敗ステップから再開する。成功時も失敗時も履歴エントリを返す（ステータス付き）。
    """
    # 前回の成果物を引き継ぐための入れ物
    state = dict(prior_entry) if prior_entry else {}
    # 過去の失敗情報はいったんクリア（今回リトライで上書きするため）
    for k in ("ステータス", "失敗ステップ", "エラー"):
        _delete_history_key(state, k)

    def _finalize_success() -> dict:
        state.update({"ステータス": "成功", "失敗ステップ": "", "エラー": ""})
        return state

    def _finalize_failure(step_key: str, err: Exception) -> dict:
        state.update({
            "ステータス": "失敗",
            "失敗ステップ": STEP_LABELS.get(step_key, step_key),
            "エラー": str(err),
            "実行日時": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "URL": url,
        })
        return state

    resumed = bool(prior_entry)
    if resumed:
        log.info(f"♻️ 既存エントリから再開します（失敗ステップ: {prior_entry.get('失敗ステップ', '不明')}）")

    # ---- 1. 動画DL ----
    filepath = None
    meta = None
    duration_sec = None
    try:
        progress.progress(10, text=f"{prefix}動画をダウンロード中…")
        filepath, meta = downloader.download_video(url, tmpdir)
        duration_sec = meta.get("duration") or downloader.get_duration_seconds(filepath)
        log.write(f"✅ ダウンロード完了：{os.path.basename(filepath)}（{fmt_duration(duration_sec)}）")
    except Exception as e:
        log.error(f"❌ ダウンロード失敗: {e}")
        return _finalize_failure("download", e)

    # ---- 2. 文字起こし（Whisper） ----
    transcript = state.get("_transcript")
    try:
        progress.progress(30, text=f"{prefix}文字起こし中（Whisper）…")
        if transcript:
            log.write(f"♻️ 文字起こし済み（{len(transcript)}文字）を再利用")
            with log.expander("文字起こし内容"):
                st.text(transcript)
        else:
            transcript_result = transcriber.transcribe(filepath)
            if isinstance(transcript_result, tuple):
                transcript, transcribe_logs = transcript_result
            else:
                transcript = transcript_result
                transcribe_logs = []

            if transcript:
                log.write(f"✅ 文字起こし完了（{len(transcript)}文字）")
                with log.expander("文字起こし内容"):
                    st.text(transcript)
            else:
                log.warning("⚠️ 音声抽出失敗 → 視覚のみで分析継続")
                transcript = "（音声なし／文字起こし不可）"

            if transcribe_logs:
                with log.expander("🔍 文字起こし診断ログ"):
                    for line in transcribe_logs:
                        st.text(line)
            state["_transcript"] = transcript
    except Exception as e:
        log.error(f"❌ 文字起こし失敗: {e}")
        return _finalize_failure("transcribe", e)

    # ---- 3. キーフレーム抽出 ----
    key_frames = []
    try:
        progress.progress(45, text=f"{prefix}キーフレーム抽出中…")
        frames_dir = os.path.join(tmpdir, "frames")
        os.makedirs(frames_dir, exist_ok=True)
        key_frames = downloader.extract_key_frames(filepath, frames_dir, count=8)
        log.write(f"✅ キーフレーム抽出完了（{len(key_frames)}枚）")
    except Exception as e:
        log.error(f"❌ キーフレーム抽出失敗: {e}")
        return _finalize_failure("keyframes", e)

    # ---- 4. 分析（Claude） ----
    analysis = state.get("_analysis")
    video_title = None
    try:
        progress.progress(55, text=f"{prefix}分析中（Claude Sonnet）…")
        if analysis:
            video_title = sanitize(analysis.get("video_title") or "無題動画", max_len=40)
            log.write(f"♻️ 分析済み（{video_title}）を再利用")
            with log.expander("分析結果（JSON）"):
                st.json(analysis)
        else:
            meta_for_ai = dict(meta)
            if not meta_for_ai.get("duration"):
                meta_for_ai["duration"] = duration_sec
            analysis = analyzer.analyze_video(transcript, key_frames, meta_for_ai, mode=st.session_state.get("analysis_mode"))
            video_title = sanitize(analysis.get("video_title") or "無題動画", max_len=40)
            state["_analysis"] = analysis
            log.write(f"✅ 分析完了：**{video_title}**")
            with log.expander("分析結果（JSON）"):
                st.json(analysis)
        # 表示メタデータをこの時点で反映（以降のステップで失敗しても履歴に正しいタイトル等が残る）
        state.update({
            "タイトル": analysis.get("video_title", ""),
            "類型": analysis.get("classification", ""),
            "トーン": analysis.get("tone", ""),
            "プラットフォーム": downloader.platform_label(meta),
            "アカウント": f"{meta.get('uploader', '')} / @{meta.get('uploader_id', '')}",
            "尺": fmt_duration(duration_sec),
            "投稿日": fmt_upload_date(meta.get("upload_date")),
            "再生数": fmt_count(meta.get("view_count")),
            "いいね": fmt_count(meta.get("like_count")),
            "コメント": fmt_count(meta.get("comment_count")),
            "EG率": fmt_rate(downloader.engagement_rate(meta)),
            "_view_count": meta.get("view_count"),
            "_like_count": meta.get("like_count"),
            "_comment_count": meta.get("comment_count"),
            "_duration_sec": duration_sec,
            "URL": url,
        })
    except Exception as e:
        log.error(f"❌ 分析失敗: {e}")
        return _finalize_failure("analyze", e)

    # ---- 5. フォルダ作成（既存があれば再利用） ----
    folder_id = state.get("_folder_id")
    try:
        progress.progress(65, text=f"{prefix}Driveフォルダを準備中…")
        if folder_id:
            log.markdown(f"♻️ 既存フォルダを再利用：[開く]({drive.folder_url(folder_id)})")
        else:
            folder_id = drive.create_folder(f"参考動画_{video_title}", config.SHARED_DRIVE_FOLDER_ID)
            drive.set_anyone_reader(folder_id)
            state["_folder_id"] = folder_id
            state["Driveフォルダ"] = drive.folder_url(folder_id)
            log.markdown(f"✅ フォルダ作成：[開く]({drive.folder_url(folder_id)})")
    except Exception as e:
        log.error(f"❌ フォルダ作成失敗: {e}")
        return _finalize_failure("folder", e)

    # ---- 6. 動画UP（既存があればスキップ） ----
    video_file_id = state.get("_video_file_id")
    try:
        progress.progress(70, text=f"{prefix}動画をDriveにUP中…")
        if video_file_id:
            log.write("♻️ 動画は既にUP済み（スキップ）")
        else:
            video_file_id = drive.upload_file(filepath, folder_id, name=f"{video_title}.mp4")
            drive.set_anyone_reader(video_file_id)
            state["_video_file_id"] = video_file_id
            log.write("✅ 動画UP完了")
    except Exception as e:
        log.error(f"❌ 動画UP失敗: {e}")
        return _finalize_failure("video_up", e)

    # ---- 7. サムネイル抽出＆UP（既存があればスキップ） ----
    thumb_url = state.get("_thumb_url")
    try:
        progress.progress(76, text=f"{prefix}サムネイルを生成・UP中…")
        if thumb_url:
            log.write("♻️ サムネイルは既にUP済み（スキップ）")
        else:
            thumb_local = os.path.join(tmpdir, f"{video_title}_thumb.png")
            try:
                downloader.extract_first_frame(filepath, thumb_local)
                thumb_id = drive.upload_file(thumb_local, folder_id, name=f"{video_title}_thumb.png", mimetype="image/png")
                drive.set_anyone_reader(thumb_id)
                thumb_url = f"https://drive.google.com/uc?export=view&id={thumb_id}"
                state["_thumb_url"] = thumb_url
                log.write("✅ サムネイル生成＆UP完了")
            except Exception as e:
                log.warning(f"⚠️ サムネイル生成失敗（スキップして継続）: {e}")
                thumb_url = None
    except Exception as e:
        log.error(f"❌ サムネイル処理失敗: {e}")
        return _finalize_failure("thumbnail", e)

    # ---- 8. 文字起こしDocs化（既存があればスキップ） ----
    transcript_doc_id = state.get("_transcript_doc_id")
    try:
        progress.progress(82, text=f"{prefix}文字起こしをDocsとしてUP中…")
        if transcript_doc_id:
            log.markdown(f"♻️ 文字起こしDocsは既に生成済み：[開く]({drive.file_url(transcript_doc_id, 'document')})")
        else:
            transcript_doc_id = docs.create_doc_with_text(
                f"文字起こし_{video_title}", transcript, folder_id
            )
            drive.set_anyone_reader(transcript_doc_id)
            state["_transcript_doc_id"] = transcript_doc_id
            state["文字起こしDoc"] = drive.file_url(transcript_doc_id, "document")
            log.markdown(f"✅ 文字起こしDocs：[開く]({drive.file_url(transcript_doc_id, 'document')})")
    except Exception as e:
        log.error(f"❌ 文字起こしDocs失敗: {e}")
        return _finalize_failure("transcript_doc", e)

    # ---- 9. 分析Docs化（既存があればスキップ） ----
    analysis_doc_id = state.get("_analysis_doc_id")
    try:
        progress.progress(90, text=f"{prefix}分析結果をDocsとしてUP中…")
        if analysis_doc_id:
            log.markdown(f"♻️ 分析Docsは既に生成済み：[開く]({drive.file_url(analysis_doc_id, 'document')})")
        else:
            analysis_text = format_analysis_doc(analysis, meta, url, duration_sec, mode=st.session_state.get("analysis_mode"))
            analysis_doc_id = docs.create_doc_with_text(
                f"分析_{video_title}", analysis_text, folder_id
            )
            drive.set_anyone_reader(analysis_doc_id)
            state["_analysis_doc_id"] = analysis_doc_id
            state["分析Doc"] = drive.file_url(analysis_doc_id, "document")
            log.markdown(f"✅ 分析Docs：[開く]({drive.file_url(analysis_doc_id, 'document')})")
    except Exception as e:
        log.error(f"❌ 分析Docs失敗: {e}")
        return _finalize_failure("analysis_doc", e)

    # ---- 10. Slides化（既存があればスキップ） ----
    slides_id = state.get("_slides_id")
    try:
        progress.progress(94, text=f"{prefix}Slidesを生成中…")
        if slides_id:
            log.markdown(f"♻️ Slidesは既に生成済み：[開く]({drive.file_url(slides_id, 'presentation')})")
        else:
            slides_id = slides.copy_template(
                config.TEMPLATE_SLIDES_ID,
                f"参考動画スライド_{video_title}",
                folder_id,
            )
            transcript_doc_url = drive.file_url(transcript_doc_id, "document")
            analysis_doc_url = drive.file_url(analysis_doc_id, "document")
            folder_url = drive.folder_url(folder_id)
            replacements = build_slide_replacements(
                analysis, meta, url, duration_sec,
                transcript_doc_url=transcript_doc_url,
                analysis_doc_url=analysis_doc_url,
                folder_url=folder_url,
            )
            slides.replace_placeholders(slides_id, replacements)

            if thumb_url:
                placeholder_candidates = [
                    "{{サムネイル}}",
                    "サムネイル貼り付け枠",
                    "サムネイル 貼り付け枠",
                ]
                for placeholder in placeholder_candidates:
                    try:
                        slides.replace_shape_with_image(slides_id, placeholder, thumb_url, method="CENTER_INSIDE")
                    except Exception:
                        pass
                log.write("✅ サムネイル画像をスライドに差し込み完了（マッチ枠のみ）")

            try:
                slides.make_urls_clickable(slides_id, [url, transcript_doc_url, analysis_doc_url, folder_url])
                log.write("✅ URLをハイパーリンク化完了")
            except Exception as e:
                log.warning(f"⚠️ URLリンク化失敗（スキップ）: {e}")

            drive.set_anyone_reader(slides_id)
            state["_slides_id"] = slides_id
            state["Slides"] = drive.file_url(slides_id, "presentation")
            log.markdown(f"✅ Slides：[開く]({drive.file_url(slides_id, 'presentation')})")
    except Exception as e:
        log.error(f"❌ Slides生成失敗: {e}")
        return _finalize_failure("slides", e)

    progress.progress(100, text=f"{prefix}完了！")

    # 表示メタデータをまとめて上書き
    state.update({
        "実行日時": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "タイトル": analysis.get("video_title", ""),
        "類型": analysis.get("classification", ""),
        "トーン": analysis.get("tone", ""),
        "プラットフォーム": downloader.platform_label(meta),
        "アカウント": f"{meta.get('uploader', '')} / @{meta.get('uploader_id', '')}",
        "尺": fmt_duration(duration_sec),
        "投稿日": fmt_upload_date(meta.get("upload_date")),
        "再生数": fmt_count(meta.get("view_count")),
        "いいね": fmt_count(meta.get("like_count")),
        "コメント": fmt_count(meta.get("comment_count")),
        "EG率": fmt_rate(downloader.engagement_rate(meta)),
        "_view_count": meta.get("view_count"),
        "_like_count": meta.get("like_count"),
        "_comment_count": meta.get("comment_count"),
        "_duration_sec": duration_sec,
        "URL": url,
    })
    return _finalize_success()


def execute_url_batch(urls: list[str]):
    """URLリストを順次処理。tab_run と tab_history どちらからでも呼べる共通関数。
    per-URL の表示は st.container(border=True) を使うため、expander の中から呼んでも安全。"""
    total = len(urls)
    results = {"success": [], "resumed": [], "fail": [], "skipped": []}

    overall_progress = st.progress(0, text=f"0 / {total} 完了")
    overall_status = st.empty()

    for idx, one_url in enumerate(urls, 1):
        prefix = f"[{idx}/{total}] "
        overall_status.info(f"🎬 {prefix}処理中：{one_url}")

        prior = history.find_by_url(one_url)

        # 成功済みならスキップ
        if prior and prior.get("ステータス") == "成功":
            with st.container(border=True):
                st.markdown(f"⏭ **{prefix}{one_url}**（既に成功済みのためスキップ）")
                st.info(f"✅ 前回成功済み：{prior.get('タイトル', '')}")
                if prior.get("Driveフォルダ"):
                    st.markdown(f"📂 フォルダ：{prior.get('Driveフォルダ')}")
            results["skipped"].append(prior)
            overall_progress.progress(idx / total, text=f"{idx} / {total} 完了")
            continue

        with st.container(border=True):
            st.markdown(f"🎬 **{prefix}{one_url}**")
            inner_log = st.container()
            inner_progress = st.progress(0, text=f"{prefix}開始します…")
            entry = None
            try:
                with tempfile.TemporaryDirectory() as tmpdir:
                    entry = process_single_url(
                        one_url, tmpdir, inner_log, inner_progress,
                        prefix=prefix, prior_entry=prior,
                    )
            except Exception as e:
                inner_log.error(f"❌ {prefix}想定外の失敗：{e}")
                entry = {
                    "実行日時": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    "URL": one_url,
                    "ステータス": "失敗",
                    "失敗ステップ": "想定外エラー",
                    "エラー": str(e),
                }
                if prior:
                    for k, v in prior.items():
                        if k.startswith("_") and k not in entry:
                            entry[k] = v

            if entry:
                history.save_entry(entry)
                if entry.get("ステータス") == "成功":
                    was_resumed = bool(prior)
                    (results["resumed"] if was_resumed else results["success"]).append(entry)
                    marker = "♻️ 再開して" if was_resumed else "✅ "
                    inner_log.success(f"{marker}{prefix}完了：{entry.get('タイトル', '')}")
                    inner_log.markdown(f"📂 フォルダ：{entry.get('Driveフォルダ', '')}")
                else:
                    results["fail"].append(entry)
                    inner_log.error(
                        f"❌ {prefix}失敗（{entry.get('失敗ステップ', '不明')}）：{entry.get('エラー', '')}"
                    )

        overall_progress.progress(idx / total, text=f"{idx} / {total} 完了")

    overall_status.empty()
    st.divider()
    col1, col2, col3, col4 = st.columns(4)
    col1.metric("総数", total)
    col2.metric("成功", len(results["success"]) + len(results["resumed"]))
    col3.metric("失敗", len(results["fail"]))
    col4.metric("スキップ", len(results["skipped"]))

    if results["resumed"]:
        st.info(f"♻️ {len(results['resumed'])} 件は既存フォルダを再利用して再開しました。")

    if results["fail"]:
        st.markdown("### ❌ 失敗した動画")
        for f in results["fail"]:
            st.write(
                f"- **URL**: {f.get('URL')}  \n"
                f"  - 失敗ステップ: `{f.get('失敗ステップ', '不明')}`  \n"
                f"  - エラー: `{f.get('エラー', '')[:300]}`"
            )

    if results["success"] or results["resumed"]:
        st.success(
            f"新規 {len(results['success'])} 件 + 再開 {len(results['resumed'])} 件を分析完了。"
            "「実行履歴」タブで一覧確認できます。"
        )
        st.balloons()

    return results


with tab_run:
    st.subheader("動画分析を実行")
    mode_labels = {k: v["label"] for k, v in analyzer.PROFILES.items()}
    mode_keys = list(mode_labels.keys())
    default_idx = mode_keys.index(config.ANALYSIS_MODE) if config.ANALYSIS_MODE in mode_keys else 0
    chosen = st.selectbox(
        "分析モード",
        mode_keys,
        index=default_idx,
        format_func=lambda k: mode_labels[k],
        help="動画を「何の参考として」分析するかを切り替えます。類型の選択肢と観察の指示が変わります。",
        key="analysis_mode",
    )
    st.caption(
        {"hr": "採用動画として分析します（従来動作）。",
         "gourmet": "店舗集客・グルメ動画として分析します。店舗情報の見せ方まで観察します。"}.get(chosen, "")
    )


    default_urls = st.session_state.pop("prefill_urls", "")
    raw_urls = st.text_area(
        "投稿URL（複数指定可 — カンマ or 改行で区切る）",
        value=default_urls,
        placeholder=(
            "https://www.tiktok.com/@example/video/xxxxxxx\n"
            "https://www.instagram.com/reel/yyyyyy/\n"
            "https://www.tiktok.com/@another/video/zzzzzzz"
        ),
        height=140,
    )

    urls = parse_urls(raw_urls)
    if urls:
        # 履歴と照合して、事前に「スキップ/再開/新規」の内訳を表示
        will_skip = []
        will_resume = []
        will_new = []
        for u in urls:
            p = history.find_by_url(u)
            if p and p.get("ステータス") == "成功":
                will_skip.append(u)
            elif p and p.get("ステータス") == "失敗":
                will_resume.append(u)
            else:
                will_new.append(u)
        st.caption(
            f"📋 検出されたURL：**{len(urls)}件** ／ "
            f"🆕 新規 **{len(will_new)}** ｜ ♻️ 再開 **{len(will_resume)}** ｜ ⏭ スキップ **{len(will_skip)}**"
        )
        if will_resume:
            with st.expander(f"♻️ 再開されるURL（{len(will_resume)}件）", expanded=False):
                for u in will_resume:
                    p = history.find_by_url(u)
                    st.write(f"- `{p.get('失敗ステップ', '?')}` から再開: {u}")
        if will_skip:
            with st.expander(f"⏭ スキップされるURL（{len(will_skip)}件）", expanded=False):
                for u in will_skip:
                    st.write(f"- {u}")

    run = st.button("実行", type="primary", disabled=not urls)

    if run:
        execute_url_batch(urls)


with tab_history:
    st.subheader("実行履歴")
    hist = history.load()

    # ステータス欠損の後方互換：古いエントリはすべて成功として補完
    for e in hist:
        if "ステータス" not in e:
            e["ステータス"] = "成功"
            e["失敗ステップ"] = ""
            e["エラー"] = ""

    n_success = sum(1 for e in hist if e.get("ステータス") == "成功")
    n_fail = sum(1 for e in hist if e.get("ステータス") == "失敗")

    col_a, col_b, col_c, col_d = st.columns([1, 1, 1, 3])
    with col_a:
        if st.button("🔄 更新"):
            st.rerun()
    with col_b:
        st.metric("成功", n_success)
    with col_c:
        st.metric("失敗", n_fail)
    with col_d:
        filter_choice = st.radio(
            "表示フィルタ",
            options=["すべて", "成功のみ", "失敗のみ"],
            horizontal=True,
            label_visibility="collapsed",
        )

    if not hist:
        st.info("まだ実行履歴がありません。「動画分析」タブから実行してみてください。")
    else:
        if filter_choice == "成功のみ":
            visible = [e for e in hist if e.get("ステータス") == "成功"]
        elif filter_choice == "失敗のみ":
            visible = [e for e in hist if e.get("ステータス") == "失敗"]
        else:
            visible = hist

        if not visible:
            st.info(f"「{filter_choice}」に該当する履歴はありません。")
        else:
            df = pd.DataFrame(visible)
            # 内部フィールド（アンダースコア始まり）は非表示
            display_cols = [c for c in df.columns if not c.startswith("_")]
            df = df[display_cols]

            st.dataframe(
                df,
                use_container_width=True,
                hide_index=True,
                column_config={
                    "URL": st.column_config.LinkColumn("URL", display_text="🔗 元動画"),
                    "Driveフォルダ": st.column_config.LinkColumn("Driveフォルダ", display_text="📂 開く"),
                    "文字起こしDoc": st.column_config.LinkColumn("文字起こし", display_text="📝 開く"),
                    "分析Doc": st.column_config.LinkColumn("分析", display_text="📊 開く"),
                    "Slides": st.column_config.LinkColumn("Slides", display_text="🎞 開く"),
                    "実行日時": st.column_config.TextColumn("実行日時", width="small"),
                    "タイトル": st.column_config.TextColumn("タイトル", width="medium"),
                    "プラットフォーム": st.column_config.TextColumn("プラットフォーム", width="small"),
                    "アカウント": st.column_config.TextColumn("アカウント", width="medium"),
                    "尺": st.column_config.TextColumn("尺", width="small"),
                    "投稿日": st.column_config.TextColumn("投稿日", width="small"),
                    "ステータス": st.column_config.TextColumn("状態", width="small"),
                    "失敗ステップ": st.column_config.TextColumn("失敗箇所", width="small"),
                    "エラー": st.column_config.TextColumn("エラー", width="medium"),
                    "再生数": st.column_config.TextColumn("再生数", width="small"),
                    "いいね": st.column_config.TextColumn("いいね", width="small"),
                    "コメント": st.column_config.TextColumn("コメント", width="small"),
                    "EG率": st.column_config.TextColumn("EG率", width="small"),
                },
            )

            # ---- アカウント別サマリー（起用候補の比較用） ----
            summary = build_account_summary(visible)
            if summary is not None and not summary.empty:
                st.subheader("👤 アカウント別サマリー")
                st.caption(
                    "起用候補を比較するための集計です。再生数が取得できた投稿のみを平均に含めています。"
                )
                st.dataframe(
                    summary,
                    use_container_width=True,
                    hide_index=True,
                    column_config={
                        "アカウント": st.column_config.TextColumn("アカウント", width="medium"),
                        "本数": st.column_config.NumberColumn("分析本数", width="small"),
                        "平均再生数": st.column_config.TextColumn("平均再生数", width="small"),
                        "中央値再生数": st.column_config.TextColumn("中央値", width="small"),
                        "最高再生数": st.column_config.TextColumn("最高", width="small"),
                        "平均保存数": st.column_config.TextColumn("平均保存数", width="small"),
                        "平均保存率": st.column_config.TextColumn("平均保存率", width="small"),
                        "平均いいね": st.column_config.TextColumn("平均いいね", width="small"),
                        "平均EG率": st.column_config.TextColumn("平均EG率", width="small"),
                        "平均尺": st.column_config.TextColumn("平均尺", width="small"),
                    },
                )
                st.download_button(
                    label="📥 アカウント別サマリーをCSVで保存",
                    data=summary.to_csv(index=False).encode("utf-8-sig"),
                    file_name=f"アカウント別サマリー_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                    mime="text/csv",
                )

            # ---- Edits実測値の入力（再生数・保存数の手動マージ） ----
            ok_entries = [e for e in visible if e.get("ステータス") == "成功"]
            if ok_entries:
                st.subheader("📱 Edits実測値の入力")
                st.caption(
                    "Instagram公式の動画編集アプリ「Edits」では、他人のリールの再生数・保存数を確認できる場合があります"
                    "（アカウントにより未表示のことあり）。スマホのEditsで確認した値を入力して保存すると、"
                    "再生数は自動取得値より優先され、保存数・保存率がアカウント別サマリーに加わります。"
                    "保存数は「あとで行く」の先行指標です。"
                )
                manual_rows = [{
                    "タイトル": e.get("タイトル", ""),
                    "アカウント": e.get("アカウント", ""),
                    "再生数(実測)": e.get("_view_count_manual"),
                    "保存数(実測)": e.get("_save_count"),
                    "URL": e.get("URL", ""),
                } for e in ok_entries]
                edited = st.data_editor(
                    pd.DataFrame(manual_rows),
                    hide_index=True,
                    use_container_width=True,
                    disabled=["タイトル", "アカウント", "URL"],
                    column_config={
                        "再生数(実測)": st.column_config.NumberColumn("再生数(実測)", min_value=0, step=1),
                        "保存数(実測)": st.column_config.NumberColumn("保存数(実測)", min_value=0, step=1),
                        "URL": st.column_config.LinkColumn("URL", display_text="🔗"),
                    },
                    key="manual_metrics_editor",
                )
                if st.button("💾 実測値を保存してサマリーに反映"):
                    n = 0
                    for _, row in edited.iterrows():
                        v = row.get("再生数(実測)")
                        sv = row.get("保存数(実測)")
                        v = int(v) if pd.notna(v) else None
                        sv = int(sv) if pd.notna(sv) else None
                        if v is not None or sv is not None:
                            if history.update_manual_metrics(row["URL"], view_count=v, save_count=sv):
                                n += 1
                    st.success(f"{n}件の実測値を保存しました。")
                    st.rerun()

            csv = df.to_csv(index=False).encode("utf-8-sig")
            st.download_button(
                label="📥 CSVをダウンロード",
                data=csv,
                file_name=f"実行履歴_{datetime.now().strftime('%Y%m%d_%H%M%S')}.csv",
                mime="text/csv",
            )

        # 失敗行の再実行UI
        failed = [e for e in hist if e.get("ステータス") == "失敗"]
        if failed:
            st.markdown("---")
            st.markdown(f"### ❌ 失敗した動画（{len(failed)}件）")

            # 全件再実行ボタン（トップレベルに置く）
            col_rerun_all, col_rerun_desc = st.columns([1, 3])
            with col_rerun_all:
                if st.button(
                    f"▶ 全 {len(failed)} 件をここで再実行",
                    type="primary",
                    key="rerun_all_failed_direct",
                ):
                    st.session_state["_run_now_urls"] = [e.get("URL") for e in failed if e.get("URL")]
                    st.rerun()
            with col_rerun_desc:
                st.caption("失敗ステップから再開。既存フォルダ・Docsは再利用されます。")

            # 個別再実行ボタン
            with st.expander("個別に選んで再実行 / 詳細を確認", expanded=False):
                for idx, e in enumerate(failed):
                    row_url = e.get("URL", "")
                    col_info, col_btn = st.columns([5, 1])
                    with col_info:
                        st.write(
                            f"- **{e.get('タイトル') or '(未分析)'}**  \n"
                            f"  失敗箇所: `{e.get('失敗ステップ', '不明')}`  \n"
                            f"  エラー: `{e.get('エラー', '')[:200]}`  \n"
                            f"  URL: {row_url}"
                        )
                    with col_btn:
                        if st.button("▶ 再実行", key=f"rerun_one_{idx}"):
                            st.session_state["_run_now_urls"] = [row_url]
                            st.rerun()

            # 再実行トリガーがあれば履歴タブの下部で処理を開始
            run_now_urls = st.session_state.pop("_run_now_urls", None)
            if run_now_urls:
                st.markdown("---")
                st.info(f"🔁 {len(run_now_urls)} 件を実行します")
                execute_url_batch(run_now_urls)

        with st.expander("⚠️ 履歴をクリア", expanded=False):
            st.warning("この操作は取り消せません。Drive上のファイルは削除されません。")
            if st.button("履歴をクリア", type="secondary"):
                history.clear()
                st.rerun()
