"""Gmail API を用いた受信箱メール取得モジュール。"""
import base64
import os
from datetime import datetime, timedelta

from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import InstalledAppFlow
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/gmail.readonly"]


def _extract_body(payload: dict) -> str:
    """メッセージペイロードから text/plain 本文を再帰的に取り出す。"""
    if payload.get("mimeType") == "text/plain" and "data" in payload.get("body", {}):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")

    for part in payload.get("parts", []) or []:
        text = _extract_body(part)
        if text:
            return text

    # text/plain が無ければ text/html でも代用する
    if payload.get("mimeType") == "text/html" and "data" in payload.get("body", {}):
        data = payload["body"]["data"]
        return base64.urlsafe_b64decode(data.encode("utf-8")).decode("utf-8", errors="ignore")

    return ""


class GmailClient:
    def __init__(self, client_secret_path: str, token_path: str):
        self.client_secret_path = client_secret_path
        self.token_path = token_path
        self.service = self._authenticate()

    def _authenticate(self):
        creds = None
        if os.path.exists(self.token_path):
            creds = Credentials.from_authorized_user_file(self.token_path, SCOPES)

        if not creds or not creds.valid:
            if creds and creds.expired and creds.refresh_token:
                creds.refresh(Request())
            else:
                # 初回のみブラウザが開き、Googleアカウントでのログイン・同意が必要
                flow = InstalledAppFlow.from_client_secrets_file(self.client_secret_path, SCOPES)
                creds = flow.run_local_server(port=0)
            with open(self.token_path, "w", encoding="utf-8") as f:
                f.write(creds.to_json())

        return build("gmail", "v1", credentials=creds, cache_discovery=False)

    def fetch_recent(self, days: int = 3) -> list[dict]:
        """直近 days 日分の受信メールを取得する。"""
        after_date = (datetime.now() - timedelta(days=days)).strftime("%Y/%m/%d")
        query = f"after:{after_date}"

        message_ids = []
        page_token = None
        while True:
            resp = (
                self.service.users()
                .messages()
                .list(userId="me", q=query, pageToken=page_token, maxResults=100)
                .execute()
            )
            message_ids.extend(resp.get("messages", []))
            page_token = resp.get("nextPageToken")
            if not page_token:
                break

        results = []
        for m in message_ids:
            msg = (
                self.service.users()
                .messages()
                .get(userId="me", id=m["id"], format="full")
                .execute()
            )
            headers = {h["name"]: h["value"] for h in msg["payload"]["headers"]}
            body = _extract_body(msg["payload"]) or msg.get("snippet", "")
            results.append(
                {
                    "account": "gmail",
                    "id": m["id"],
                    "subject": headers.get("Subject", "(件名なし)"),
                    "from": headers.get("From", "(不明)"),
                    "date": headers.get("Date", ""),
                    "body": body,
                }
            )
        return results
