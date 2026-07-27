"""Ollama経由でgemma4:12bを呼び出し、メールの重要度判定・要約を行うモジュール。
起動時にモデルをロードし、終了時にEject(メモリからunload)する処理を含む。
"""
import json
import subprocess
import sys
import time

import ollama

DEFAULT_MODEL = "gemma4:12b-it-qat"

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


class OllamaJudge:
    def __init__(self, model: str = DEFAULT_MODEL, keep_alive: str = "10m"):
        self.model = model
        self.keep_alive = keep_alive

    def _ensure_server_running(self, timeout: float = 30.0, interval: float = 1.0) -> None:
        """Ollamaサーバーが応答するか確認し、応答しなければ起動して待機する。"""
        try:
            ollama.list()
            return
        except Exception:
            pass

        print("[起動] Ollamaサーバーが起動していないため起動しています...")
        try:
            subprocess.Popen(
                ["ollama", "serve"],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                creationflags=subprocess.CREATE_NO_WINDOW if sys.platform == "win32" else 0,
            )
        except FileNotFoundError:
            raise RuntimeError(
                "ollamaコマンドが見つかりません。Ollamaがインストールされているか確認してください。"
            )

        deadline = time.monotonic() + timeout
        last_err = None
        while time.monotonic() < deadline:
            try:
                ollama.list()
                return
            except Exception as e:
                last_err = e
                time.sleep(interval)
        raise RuntimeError(f"Ollamaサーバーの起動を待ちましたが接続できませんでした: {last_err}")

    def load(self) -> None:
        """アプリ起動時にモデルをメモリにロードする。"""
        self._ensure_server_running()
        ollama.chat(
            model=self.model,
            messages=[{"role": "user", "content": "ping"}],
            keep_alive=self.keep_alive,
            options={
                "num_ctx": 4096,      # ← これ。天井を作る
                "num_predict": 256,   # 出力JSONは短いので上限を切る
                "temperature": 0,     # 判定タスクなので決定的に
            },
        )

    def eject(self) -> None:
        """アプリ終了時にモデルをメモリからEject(unload)する。"""
        try:
            ollama.chat(
                model=self.model,
                messages=[{"role": "user", "content": ""}],
                keep_alive=0,
                options={
                    "num_ctx": 4096,      # ← これ。天井を作る
                    "num_predict": 256,   # 出力JSONは短いので上限を切る
                    "temperature": 0,     # 判定タスクなので決定的に
                },
            )
        except Exception:
            # APIでの明示unloadに失敗した場合はCLIでフォールバック
            subprocess.run(["ollama", "stop", self.model], check=False)

    def judge(self, mail: dict) -> dict:
        """1件のメールについて重要度判定と要約を行う。"""
        prompt = IMPORTANCE_PROMPT.format(
            sender=mail.get("from", ""),
            subject=mail.get("subject", ""),
            date=mail.get("date", ""),
            body=(mail.get("body", "") or "")[:4000],
        )
        resp = ollama.chat(
            model=self.model,
            messages=[{"role": "user", "content": prompt}],
            format="json",
            keep_alive=self.keep_alive,
            options={
                "num_ctx": 4096,      # ← これ。天井を作る
                "num_predict": 256,   # 出力JSONは短いので上限を切る
                "temperature": 0,     # 判定タスクなので決定的に
            },
        )
        content = resp["message"]["content"]
        try:
            data = json.loads(content)
        except json.JSONDecodeError:
            data = {"important": False, "reason": "判定結果のJSON解析に失敗", "summary": ""}
        return data
