import os
from dotenv import load_dotenv

load_dotenv()


def _get(key: str, default=None):
    """Streamlit Secrets → 環境変数 の順で値を取得。"""
    try:
        import streamlit as st
        if hasattr(st, "secrets") and key in st.secrets:
            return st.secrets[key]
    except Exception:
        pass
    return os.getenv(key, default)


OPENAI_API_KEY = _get("OPENAI_API_KEY")
ANTHROPIC_API_KEY = _get("ANTHROPIC_API_KEY")
GOOGLE_SERVICE_ACCOUNT_JSON = _get("GOOGLE_SERVICE_ACCOUNT_JSON", "./credentials.json")

SHARED_DRIVE_FOLDER_ID = _get("SHARED_DRIVE_FOLDER_ID")
TEMPLATE_SLIDES_ID = _get("TEMPLATE_SLIDES_ID")

ANTHROPIC_MODEL = _get("ANTHROPIC_MODEL", "claude-sonnet-4-6")
WHISPER_MODEL = _get("WHISPER_MODEL", "whisper-1")

# 分析プロファイル: "hr"（採用動画・既定）/ "gourmet"（店舗集客・グルメ動画）
ANALYSIS_MODE = _get("ANALYSIS_MODE", "hr")


def _has_secret_service_account() -> bool:
    """Streamlit Secretsに google_service_account セクションがあるかチェック。"""
    try:
        import streamlit as st
        return hasattr(st, "secrets") and "google_service_account" in st.secrets
    except Exception:
        return False


def validate() -> list[str]:
    """必須設定の抜けをチェックしてエラーメッセージのリストを返す。"""
    errors = []
    if not OPENAI_API_KEY:
        errors.append("OPENAI_API_KEY が未設定です（Whisper文字起こしで必要）")
    if not ANTHROPIC_API_KEY:
        errors.append("ANTHROPIC_API_KEY が未設定です")
    # サービスアカウントは Secrets(google_service_account) か credentials.json のどちらかがあればOK
    if not _has_secret_service_account() and not os.path.exists(GOOGLE_SERVICE_ACCOUNT_JSON):
        errors.append(
            "サービスアカウントが見つかりません（credentials.json か Secretsのgoogle_service_accountが必要）"
        )
    if not SHARED_DRIVE_FOLDER_ID:
        errors.append("SHARED_DRIVE_FOLDER_ID が未設定です")
    if not TEMPLATE_SLIDES_ID:
        errors.append("TEMPLATE_SLIDES_ID が未設定です")
    return errors
