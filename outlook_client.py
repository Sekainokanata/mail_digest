"""Microsoft Graph API を用いた Outlook 受信箱メール取得モジュール。"""
import json
import os
from datetime import datetime, timedelta, timezone

import msal
import requests

GRAPH_SCOPES = ["Mail.Read"]
GRAPH_ENDPOINT = "https://graph.microsoft.com/v1.0"


class OutlookClient:
    def __init__(self, client_id: str, tenant_id: str, token_cache_path: str):
        self.client_id = client_id
        self.tenant_id = tenant_id
        self.token_cache_path = token_cache_path
        self.access_token = self._authenticate()

    def _load_cache(self) -> msal.SerializableTokenCache:
        cache = msal.SerializableTokenCache()
        if os.path.exists(self.token_cache_path):
            with open(self.token_cache_path, "r", encoding="utf-8") as f:
                cache.deserialize(f.read())
        return cache

    def _save_cache(self, cache: msal.SerializableTokenCache) -> None:
        if cache.has_state_changed:
            with open(self.token_cache_path, "w", encoding="utf-8") as f:
                f.write(cache.serialize())

    def _authenticate(self) -> str:
        cache = self._load_cache()
        app = msal.PublicClientApplication(
            client_id=self.client_id,
            authority=f"https://login.microsoftonline.com/{self.tenant_id}",
            token_cache=cache,
        )

        result = None
        accounts = app.get_accounts()
        if accounts:
            result = app.acquire_token_silent(GRAPH_SCOPES, account=accounts[0])

        if not result:
            # 初回のみデバイスコードフロー: 表示されるURLとコードでブラウザ認証
            flow = app.initiate_device_flow(scopes=GRAPH_SCOPES)
            if "user_code" not in flow:
                raise RuntimeError("デバイスフローの開始に失敗しました: " + json.dumps(flow))
            print(flow["message"])
            result = app.acquire_token_by_device_flow(flow)

        self._save_cache(cache)

        if "access_token" not in result:
            raise RuntimeError(
                f"Outlook認証に失敗しました: {result.get('error_description', result)}"
            )
        return result["access_token"]

    def fetch_recent(self, days: int = 3) -> list[dict]:
        """直近 days 日分の受信メールを取得する。"""
        since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
        headers = {
            "Authorization": f"Bearer {self.access_token}",
            # 本文をHTMLではなくプレーンテキストで受け取る
            "Prefer": 'outlook.body-content-type="text"',
        }
        url = (
            f"{GRAPH_ENDPOINT}/me/mailFolders/inbox/messages"
            f"?$filter=receivedDateTime ge {since}"
            f"&$select=subject,from,receivedDateTime,bodyPreview,body"
            f"&$top=50&$orderby=receivedDateTime desc"
        )

        results = []
        while url:
            resp = requests.get(url, headers=headers, timeout=30)
            resp.raise_for_status()
            data = resp.json()
            for item in data.get("value", []):
                body_content = (item.get("body") or {}).get("content", "") or item.get("bodyPreview", "")
                results.append(
                    {
                        "account": "outlook",
                        "id": item["id"],
                        "subject": item.get("subject") or "(件名なし)",
                        "from": (item.get("from") or {}).get("emailAddress", {}).get("address", "(不明)"),
                        "date": item.get("receivedDateTime", ""),
                        "body": body_content,
                    }
                )
            url = data.get("@odata.nextLink")
        return results
