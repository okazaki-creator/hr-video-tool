import json
import os
import subprocess
import urllib.parse
import urllib.request

import yt_dlp


def get_duration_seconds(filepath: str) -> int | None:
    """ffprobeで動画尺(秒)を返す。yt-dlpのメタが欠けている場合のフォールバック。"""
    try:
        result = subprocess.run(
            ["ffprobe", "-v", "error", "-show_entries", "format=duration",
             "-of", "default=noprint_wrappers=1:nokey=1", filepath],
            capture_output=True, text=True, check=True,
        )
        return int(float(result.stdout.strip()))
    except Exception:
        return None


def extract_first_frame(video_path: str, output_path: str) -> str:
    """動画の0秒時点のフレームをPNGとして保存。アスペクト比はそのまま。"""
    subprocess.run(
        ["ffmpeg", "-y", "-i", video_path, "-ss", "0", "-frames:v", "1", output_path],
        capture_output=True, check=True,
    )
    return output_path


def extract_key_frames(video_path: str, output_dir: str, count: int = 8) -> list[str]:
    """動画から等間隔で count 枚のキーフレームを抽出。
    幅720pxにリサイズしてトークン節約。Claude画像分析用。"""
    duration = get_duration_seconds(video_path) or 30
    # 動画を均等分割してその中点でサンプリング
    step = duration / count
    frames = []
    for i in range(count):
        t = step * (i + 0.5)
        out = os.path.join(output_dir, f"frame_{i:02d}.jpg")
        try:
            subprocess.run(
                [
                    "ffmpeg", "-y",
                    "-ss", f"{t:.2f}",
                    "-i", video_path,
                    "-frames:v", "1",
                    "-vf", "scale='min(720,iw)':-2",
                    "-q:v", "3",
                    out,
                ],
                capture_output=True, check=True,
            )
            if os.path.exists(out) and os.path.getsize(out) > 0:
                frames.append(out)
        except subprocess.CalledProcessError:
            continue
    return frames


def _has_video_and_audio_streams(video_path: str) -> tuple[bool, bool]:
    """(has_video, has_audio) を返す。"""
    try:
        result = subprocess.run(
            [
                "ffprobe", "-v", "error",
                "-show_entries", "stream=codec_type",
                "-of", "csv=p=0",
                video_path,
            ],
            capture_output=True, text=True, timeout=30,
        )
        stdout = result.stdout
        return ("video" in stdout, "audio" in stdout)
    except Exception:
        return (False, False)


def _download_with_format(url: str, output_dir: str, fmt: str) -> tuple[str, dict]:
    """指定フォーマットでダウンロード試行。"""
    ydl_opts = {
        "outtmpl": os.path.join(output_dir, "%(id)s.%(ext)s"),
        "format": fmt,
        "quiet": True,
        "no_warnings": True,
        "retries": 3,
        "fragment_retries": 3,
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=True)
        filepath = ydl.prepare_filename(info)
        # 拡張子補正
        if not os.path.exists(filepath):
            base = os.path.splitext(filepath)[0]
            for ext in (".mp4", ".mkv", ".webm", ".m4a"):
                candidate = base + ext
                if os.path.exists(candidate):
                    filepath = candidate
                    break
    return filepath, info


def _download_via_tikwm(url: str, output_dir: str) -> tuple[str, dict] | None:
    """tikwm.com 公開API経由でTikTok動画をダウンロード（音声つき rendition を取れる）。
    yt-dlpが video-only しか取れなかった場合のフォールバック専用。TikTok以外のURLは None を返す。
    Returns: (filepath, info) or None
    """
    if "tiktok.com" not in url.lower():
        return None
    try:
        api_url = "https://tikwm.com/api/"
        payload = urllib.parse.urlencode({"url": url, "hd": "1"}).encode()
        req = urllib.request.Request(
            api_url,
            data=payload,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
        )
        with urllib.request.urlopen(req, timeout=30) as resp:
            body = resp.read()
        api_result = json.loads(body.decode("utf-8"))
        if not isinstance(api_result, dict) or api_result.get("code") != 0:
            return None
        data_obj = api_result.get("data") or {}
        play_url = data_obj.get("hdplay") or data_obj.get("play")
        if not play_url or not isinstance(play_url, str):
            return None

        video_id = str(data_obj.get("id") or "tikwm_video")
        filepath = os.path.join(output_dir, f"{video_id}.mp4")
        dl_req = urllib.request.Request(
            play_url,
            headers={
                "User-Agent": "Mozilla/5.0",
                "Referer": "https://tikwm.com/",
            },
        )
        with urllib.request.urlopen(dl_req, timeout=120) as resp:
            with open(filepath, "wb") as f:
                while True:
                    chunk = resp.read(1024 * 64)
                    if not chunk:
                        break
                    f.write(chunk)

        author = data_obj.get("author") or {}
        info = {
            "id": video_id,
            "title": data_obj.get("title") or "",
            "uploader": author.get("nickname") or "",
            "uploader_id": author.get("unique_id") or "",
            "duration": data_obj.get("duration"),
            "upload_date": None,
            "webpage_url": url,
            "extractor_key": "TikTok",
            "extractor": "TikTok (via tikwm.com)",
        }
        return filepath, info
    except Exception:
        return None


def download_video(url: str, output_dir: str) -> tuple[str, dict]:
    """IG/TikTok/YouTube等のURLから動画をダウンロード。破損ファイル検知→再試行。
    Returns: (ローカルファイルパス, メタ情報dict)"""
    os.makedirs(output_dir, exist_ok=True)

    # フォーマット候補：まず一番信頼できる `best` で確実にファイルを取得し、
    # それが video-only だった場合のみ音声ありを狙って追加試行する。
    # （TikTokは環境によって `[acodec!=none]` フィルタで全滅することがある）
    format_candidates = [
        "best",                                                       # 単一ベスト（最も安定）
        "best[acodec!=none][vcodec!=none]",                           # 音声+動画両方あり
        "best[ext=mp4][acodec!=none]/best[acodec!=none]",             # mp4かつ音声あり
        "bestvideo[ext=mp4]+bestaudio[ext=m4a]/bestvideo+bestaudio",  # マージ型
    ]

    filepath = None
    info = None
    video_only_fallback = None
    video_only_info = None
    per_format_errors: list[str] = []
    for fmt in format_candidates:
        try:
            candidate_path, candidate_info = _download_with_format(url, output_dir, fmt)
            if not candidate_path or not os.path.exists(candidate_path):
                per_format_errors.append(f"{fmt}: ファイル未生成")
                continue
            has_video, has_audio = _has_video_and_audio_streams(candidate_path)
            if has_video and has_audio:
                filepath = candidate_path
                info = candidate_info
                break
            if has_video and not has_audio:
                # 音声なし。他候補で音声ありを狙うが、全滅時の保険として1つだけ確保。
                # 全フォーマットで同じ出力ファイル名を使うため、上書き回避のためリネームして退避する
                if video_only_fallback is None:
                    base, ext = os.path.splitext(candidate_path)
                    reserved_path = f"{base}.videoonly{ext}"
                    try:
                        os.replace(candidate_path, reserved_path)
                        video_only_fallback = reserved_path
                        video_only_info = candidate_info
                    except OSError:
                        # リネームできなかった場合は元パスを使う（次で上書きされるかもしれないが最善策）
                        video_only_fallback = candidate_path
                        video_only_info = candidate_info
                else:
                    try:
                        os.remove(candidate_path)
                    except OSError:
                        pass
                per_format_errors.append(f"{fmt}: 音声ストリームなし")
                continue
            # 動画すらない → 破損。削除して次へ
            try:
                os.remove(candidate_path)
            except OSError:
                pass
            per_format_errors.append(f"{fmt}: 動画ストリームなし（破損）")
        except Exception as e:
            per_format_errors.append(f"{fmt}: {e.__class__.__name__}: {str(e)[:150]}")
            continue

    # yt-dlpで音声つきが取れなかった場合、tikwm.com経由で音声つきの取得を試みる（TikTokのみ）
    if not filepath:
        tikwm_result = _download_via_tikwm(url, output_dir)
        if tikwm_result:
            candidate_path, candidate_info = tikwm_result
            if os.path.exists(candidate_path):
                has_video, has_audio = _has_video_and_audio_streams(candidate_path)
                if has_video and has_audio:
                    # video-onlyフォールバックはもう不要なので掃除
                    if video_only_fallback and video_only_fallback != candidate_path:
                        try:
                            os.remove(video_only_fallback)
                        except OSError:
                            pass
                    filepath = candidate_path
                    info = candidate_info
                else:
                    per_format_errors.append("tikwm.com: 音声なし")
                    try:
                        os.remove(candidate_path)
                    except OSError:
                        pass
            else:
                per_format_errors.append("tikwm.com: ファイル未生成")
        else:
            per_format_errors.append("tikwm.com: API失敗またはTikTok以外")

    # tikwm.comもダメだったら video-only フォールバックを使う（視覚のみで分析継続）
    if not filepath and video_only_fallback:
        filepath = video_only_fallback
        info = video_only_info

    if not filepath or not os.path.exists(filepath):
        detail = " ｜ ".join(per_format_errors) if per_format_errors else "詳細不明"
        raise RuntimeError(f"ダウンロード失敗: {url}\n試行結果: {detail}")

    meta = {
        "id": info.get("id", ""),
        "title": info.get("title") or info.get("description") or info.get("id") or "",
        "uploader": info.get("uploader") or info.get("channel") or "",
        "uploader_id": info.get("uploader_id") or "",
        "duration": info.get("duration"),
        "upload_date": info.get("upload_date"),  # YYYYMMDD
        "webpage_url": info.get("webpage_url") or url,
        "extractor": info.get("extractor_key") or info.get("extractor") or "",
        # --- パフォーマンス指標（取れないプラットフォームでは None のまま） ---
        # IG Reels は view_count が出ないことがある。その場合 play_count を代替に使う。
        "view_count": info.get("view_count") if info.get("view_count") is not None else info.get("play_count"),
        "like_count": info.get("like_count"),
        "comment_count": info.get("comment_count"),
        "repost_count": info.get("repost_count"),
        "follower_count": info.get("channel_follower_count"),
    }
    return filepath, meta


def engagement_rate(meta: dict):
    """(いいね + コメント) / 再生数 × 100。再生数が取れない場合は None。"""
    views = meta.get("view_count")
    if not views:
        return None
    likes = meta.get("like_count") or 0
    comments = meta.get("comment_count") or 0
    try:
        return round((likes + comments) / views * 100, 2)
    except (TypeError, ZeroDivisionError):
        return None


def platform_label(meta: dict) -> str:
    """extractor から人間可読なプラットフォーム名を推定。"""
    ext = (meta.get("extractor") or "").lower()
    if "instagram" in ext:
        return "Instagram Reels"
    if "tiktok" in ext:
        return "TikTok"
    if "youtube" in ext:
        return "YouTube Shorts / 通常"
    return meta.get("extractor") or "不明"
