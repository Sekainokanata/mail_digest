"""LM Studio経由でローカルLLMを呼び出し、メールの重要度判定・要約を行うモジュール。
起動時にLM Studioサーバーとモデルを自動起動し、終了時にモデルをアンロードする処理を含む。
"""
import json
import re
import subprocess
import sys
import time

import requests

DEFAULT_MODEL = "google/gemma-4-12b-qat"
DEFAULT_BASE_URL = "http://localhost:1234/v1"

IMPORTANCE_PROMPT = """あなたはメール秘書です。以下のメールが「重要」かどうかを判定してください。

重要と判定する基準の例:
- 締切や期限のある依頼・連絡
- 請求書・契約・支払いに関する連絡
- 緊急対応が必要な内容
- 指導教員・上司・取引先など特定の人物からの直接的な連絡
- 重大なお知らせ(アカウント停止、セキュリティ、面接・選考結果など)

重要でないと判定する基準の例:
- 広告・キャンペーン・メールマガジン
- SNSやサービスからの自動通知(いいね、フォロー等)
- 定型のシステム通知で緊急性がないもの

但し、この条件にあてはまるものは絶対に「重要」と判定してください。
- LINEヤフー株式会社からの連絡
- Pak Jejun (パク チェジュン)氏からのメール


差出人: {sender}
件名: {subject}
日時: {date}
本文:
{body}

以下のJSON形式のみを出力してください。前後に説明文を付けないでください。
{{"important": true または false, "reason": "判定理由を一文で", "summary": "重要な場合のみ本文の要点を2〜3文で要約。重要でない場合は空文字"}}
"""


def _keep_alive_to_seconds(keep_alive) -> int:
    """"10m" 等のOllama形式のkeep_alive表記をLM Studioのttl秒数に変換する。"""
    if isinstance(keep_alive, (int, float)):
        return int(keep_alive)
    match = re.match(r"^(\d+)\s*([smh]?)$", str(keep_alive).strip())
    if not match:
        return 600
    value, unit = match.groups()
    value = int(value)
    if unit == "h":
        return value * 3600
    if unit == "m":
        return value * 60
    return value


class LMStudioJudge:
    def __init__(
        self,
        model: str = DEFAULT_MODEL,
        keep_alive: str = "10m",
        base_url: str = DEFAULT_BASE_URL,
        num_ctx: int = 4096,
    ):
        self.model = model
        self.ttl = _keep_alive_to_seconds(keep_alive)
        self.base_url = base_url
        self.num_ctx = num_ctx

    def _server_running(self) -> bool:
        try:
            requests.get(f"{self.base_url}/models", timeout=2)
            return True
        except requests.RequestException:
            return False

    def _ensure_server_running(self, timeout: float = 60.0, interval: float = 1.0) -> None:
        """LM Studioサーバーが応答するか確認し、応答しなければ起動して待機する。"""
        if self._server_running():
            return

        print("[起動] LM Studioサーバーが起動していないため起動しています...")
        try:
            subprocess.Popen(
                ["lms", "server", "start"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "lmsコマンドが見つかりません。LM Studioがインストールされ、"
                "CLI(lms)がPATHに通っているか確認してください。"
            )

        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if self._server_running():
                return
            time.sleep(interval)
        raise RuntimeError("LM Studioサーバーの起動を待ちましたが接続できませんでした。")

    def _run_lms(self, args: list) -> None:
        subprocess.run(
            ["lms", *args],
            check=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
        )

    def load(self) -> None:
        """アプリ起動時にLM Studioサーバーを起動し、モデルをメモリにロードする。"""
        self._ensure_server_running()
        self._run_lms(
            [
                "load",
                self.model,
                "--ttl",
                str(self.ttl),
                "--context-length",
                str(self.num_ctx),
                "--yes",
            ]
        )
        self._chat([{"role": "user", "content": "ping"}], max_tokens=8)

    def eject(self) -> None:
        """アプリ終了時にモデルをメモリからアンロードする。"""
        self._run_lms(["unload", self.model])

    def _chat(self, messages: list, json_mode: bool = False, max_tokens: int = 256) -> str:
        payload = {
            "model": self.model,
            "messages": messages,
            "temperature": 0,
            "max_tokens": max_tokens,
        }
        if json_mode:
            payload["response_format"] = {
                "type": "json_schema",
                "json_schema": {
                    "name": "mail_judgement",
                    "strict": True,
                    "schema": {
                        "type": "object",
                        "properties": {
                            "important": {"type": "boolean"},
                            "reason": {"type": "string"},
                            "summary": {"type": "string"},
                        },
                        "required": ["important", "reason", "summary"],
                        "additionalProperties": False,
                    },
                },
            }
        resp = requests.post(f"{self.base_url}/chat/completions", json=payload, timeout=120)
        if not resp.ok:
            raise RuntimeError(
                f"LM Studioへのリクエストが失敗しました (status={resp.status_code}): {resp.text}"
            )
        return resp.json()["choices"][0]["message"]["content"]

    def judge(self, mail: dict) -> dict:
        """1件のメールについて重要度判定と要約を行う。"""
        prompt = IMPORTANCE_PROMPT.format(
            sender=mail.get("from", ""),
            subject=mail.get("subject", ""),
            date=mail.get("date", ""),
            body=(mail.get("body", "") or "")[:4000],
        )
        content = self._chat([{"role": "user", "content": prompt}], json_mode=True, max_tokens=256)
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"important": False, "reason": "判定結果のJSON解析に失敗", "summary": ""}
        return data
