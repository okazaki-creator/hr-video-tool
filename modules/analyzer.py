import base64
import copy

import anthropic

import config


_client = None


def _get_client() -> anthropic.Anthropic:
    global _client
    if _client is None:
        _client = anthropic.Anthropic(api_key=config.ANTHROPIC_API_KEY)
    return _client



# ============================================================
# 分析プロファイル（用途ごとに「類型」と観察の指示を切り替える）
#   hr      : 新卒採用SNS動画の分析（既定・従来動作）
#   gourmet : 店舗集客／グルメ動画の分析（インフルエンサー選定・参考動画調査用）
# ============================================================

HR_CLASSIFICATION = [
    "若手社員の1日密着",
    "入社理由・就活ストーリー",
    "キャリアパス可視化",
    "内定者・同期の雰囲気",
    "経営者・先輩の本音Q&A",
    "理念・ブランドストーリー",
]

GOURMET_CLASSIFICATION = [
    "商品アップ・シズル",
    "価格・コスパ訴求",
    "店舗・空間紹介",
    "店主・スタッフの人物",
    "企画・ネタ・検証",
    "来店体験レポ",
]

CLASSIFICATION_OPTIONS = [
    "若手社員の1日密着",
    "入社理由・就活ストーリー",
    "キャリアパス可視化",
    "内定者・同期の雰囲気",
    "経営者・先輩の本音Q&A",
    "理念・ブランドストーリー",
]

TONE_OPTIONS = [
    "エンタメ・ネタ系",
    "バラエティ・キャラ系",
    "ドキュメント・リアル系",
    "対話・トーク系",
    "エモ・シネマ系",
    "情報整理・カード系",
]


SCHEMA = {
    "type": "object",
    "properties": {
        "video_title": {
            "type": "string",
            "description": "動画の内容を簡潔に要約したタイトル。20文字以内厳守。企業名・職種・切り口・フォーマットなど動画の“何が特徴か”が伝わる短いフレーズ。",
            "maxLength": 20,
        },
        "classification": {
            "type": "string",
            "description": "動画の類型。以下6択から最も当てはまるものを1つだけ選ぶ。",
            "enum": CLASSIFICATION_OPTIONS,
        },
        "tone": {
            "type": "string",
            "description": "動画のトーン。以下6択から最も当てはまるものを1つだけ選ぶ。",
            "enum": TONE_OPTIONS,
        },
        "hook": {
            "type": "array",
            "description": "フック（冒頭2秒で何が起きるか）。ちょうど2項目。各45文字以内。",
            "minItems": 2, "maxItems": 2,
            "items": {"type": "string", "maxLength": 45},
        },
        "structure": {
            "type": "array",
            "description": "構成メモ（本編の展開・編集の特徴）。ちょうど3項目。各45文字以内。テロップ・カット割り・演出など視覚要素も対象。",
            "minItems": 3, "maxItems": 3,
            "items": {"type": "string", "maxLength": 45},
        },
        "adaptation": {
            "type": "array",
            "description": "転用ポイント（貴社版で真似る要素）。ちょうど3項目。各45文字以内。",
            "minItems": 3, "maxItems": 3,
            "items": {"type": "string", "maxLength": 45},
        },
        "summary_title": {
            "type": "string",
            "description": "資料に載せるサブタイトル（1行）。企業名や題材が分かるように。",
        },
    },
    "required": [
        "video_title", "classification", "tone",
        "hook", "structure", "adaptation", "summary_title",
    ],
}


SYSTEM = """あなたは新卒採用SNS動画（TikTok/Reels/Shorts）のクリエイティブ分析家です。
渡された「文字起こし（Whisperによる音声認識）」と「動画のキーフレーム画像」を総合して日本語で分析してください。

【厳守】
- video_title は 20 文字以内。動画の“何が特徴か”を短く。
- classification は 6 択から 1 つ選ぶ：若手社員の1日密着 / 入社理由・就活ストーリー / キャリアパス可視化 / 内定者・同期の雰囲気 / 経営者・先輩の本音Q&A / 理念・ブランドストーリー。
- tone は 6 択から 1 つ選ぶ：エンタメ・ネタ系 / バラエティ・キャラ系 / ドキュメント・リアル系 / 対話・トーク系 / エモ・シネマ系 / 情報整理・カード系。
- hook は 2 項目、structure は 3 項目、adaptation は 3 項目。各 45 文字以内。
- 資料スライドの点線枠に貼り付ける短文。体言止めまたは断定形で密度高く。
- 意味の重複を避ける。1項目1論点。
- 「頑張ります」等の抽象語は禁止。ロケ・演出・数字・行動・視覚要素など固有の観察を書く。
- 画像に写るテロップ・表情・カット割り・場面転換など視覚情報を必ず活用する。
"""


SYSTEM_GOURMET = """あなたは飲食店・店舗集客のSNS動画（Reels/TikTok）のクリエイティブ分析家です。
渡された「文字起こし（Whisperによる音声認識）」と「動画のキーフレーム画像」を総合して日本語で分析してください。
この分析は、**起用するインフルエンサーの選定**と**自店の動画づくりの参考**に使われます。

【厳守】
- video_title は 20 文字以内。店名・商品・切り口が分かる短いフレーズ。
- classification は 6 択から 1 つ選ぶ：商品アップ・シズル / 価格・コスパ訴求 / 店舗・空間紹介 / 店主・スタッフの人物 / 企画・ネタ・検証 / 来店体験レポ。
- tone は 6 択から 1 つ選ぶ：エンタメ・ネタ系 / バラエティ・キャラ系 / ドキュメント・リアル系 / 対話・トーク系 / エモ・シネマ系 / 情報整理・カード系。
- hook は 2 項目、structure は 3 項目、adaptation は 3 項目。各 45 文字以内。
- 体言止めまたは断定形。意味の重複を避け、1項目1論点。
- 「美味しそう」等の感想語は禁止。**寄り/引き・テロップの位置と内容・価格の出し方・音の有無・尺配分・カット数**など、再現できる具体を書く。
- **店舗情報の見せ方（店名・住所・営業時間・予約導線をどのカットで、どう出しているか）を必ず1項目は含める**。
- adaptation は「自店の動画で真似る要素」として書く。
- 画像に写るテロップ・料理の見せ方・店内の映し方・場面転換など視覚情報を必ず活用する。
"""


PROFILES = {
    "hr": {
        "label": "採用動画（HR）",
        "classification": HR_CLASSIFICATION,
        "system": SYSTEM,
        "adaptation_desc": "転用ポイント（貴社版で真似る要素）。ちょうど3項目。各45文字以内。",
        "doc_heading": "転用ポイント（貴社版で真似る要素）",
    },
    "gourmet": {
        "label": "店舗集客・グルメ動画",
        "classification": GOURMET_CLASSIFICATION,
        "system": SYSTEM_GOURMET,
        "adaptation_desc": "転用ポイント（自店の動画で真似る要素）。ちょうど3項目。各45文字以内。",
        "doc_heading": "転用ポイント（自店で真似る要素）",
    },
}

DEFAULT_MODE = "hr"


def get_profile(mode: str | None = None) -> dict:
    """モード名からプロファイルを取得。未知の値は既定(hr)にフォールバック。"""
    if not mode:
        mode = getattr(config, "ANALYSIS_MODE", DEFAULT_MODE) or DEFAULT_MODE
    return PROFILES.get(mode, PROFILES[DEFAULT_MODE])


def build_schema(mode: str | None = None) -> dict:
    """プロファイルに応じて enum と説明文を差し替えたスキーマを返す。"""
    prof = get_profile(mode)
    schema = copy.deepcopy(SCHEMA)
    schema["properties"]["classification"]["enum"] = prof["classification"]
    schema["properties"]["adaptation"]["description"] = prof["adaptation_desc"]
    return schema


def analyze_video(transcript: str, frames: list[str], meta: dict, mode: str | None = None) -> dict:
    """文字起こし+キーフレーム画像を Claude に送って構造化分析。

    mode: "hr"（既定・採用動画）/ "gourmet"（店舗集客・グルメ動画）
    """
    prof = get_profile(mode)
    client = _get_client()

    # 画像コンテンツを構築
    content_blocks = []
    for path in frames:
        with open(path, "rb") as f:
            b64 = base64.standard_b64encode(f.read()).decode()
        content_blocks.append({
            "type": "image",
            "source": {
                "type": "base64",
                "media_type": "image/jpeg",
                "data": b64,
            },
        })

    user_msg_text = f"""# 動画メタ
- タイトル/説明: {meta.get('title', '')}
- 投稿者: {meta.get('uploader', '')} / @{meta.get('uploader_id', '')}
- 尺(秒): {meta.get('duration')}
- 投稿日: {meta.get('upload_date')}
- URL: {meta.get('webpage_url')}
- プラットフォーム: {meta.get('extractor')}

# 文字起こし（Whisperによる音声認識）
{transcript}

上記の文字起こしと、添付キーフレーム画像({len(frames)}枚・動画の時系列を等間隔サンプリング)を総合して、
指定スキーマ通りに分析結果を output ツール経由で返してください。"""

    content_blocks.append({"type": "text", "text": user_msg_text})

    resp = client.messages.create(
        model=config.ANTHROPIC_MODEL,
        max_tokens=4000,
        system=prof["system"],
        messages=[{"role": "user", "content": content_blocks}],
        tools=[{
            "name": "output",
            "description": "分析結果を構造化して返す",
            "input_schema": build_schema(mode),
        }],
        tool_choice={"type": "tool", "name": "output"},
    )

    for block in resp.content:
        if getattr(block, "type", None) == "tool_use" and getattr(block, "name", None) == "output":
            return block.input
    raise RuntimeError("Claudeが構造化出力を返しませんでした")


# 後方互換
def analyze(transcript: str, meta: dict) -> dict:  # deprecated
    return analyze_video(transcript, [], meta)
