"""重要メールダイジェスト アプリケーション。

起動時:
  1. Ollamaのgemma4:12b-it-qatを自動ロード
  2. 直近3日分のGmail/Outlookメールを自動取得
  3. 各メールの重要度をモデルで判定し、重要なものだけ要約して表示

終了時:
  4. Ollamaのgemma4:12b-it-qatを自動でEject(メモリからunload)
"""
import atexit
import signal
import sys

import yaml

from gmail_client import GmailClient
from ollama_client import OllamaJudge
from outlook_client import OutlookClient


def load_config(path: str = "config.yaml") -> dict:
    with open(path, "r", encoding="utf-8") as f:
        return yaml.safe_load(f)


def main() -> None:
    config = load_config()
    days = config.get("fetch_days", 3)
    ollama_cfg = config.get("ollama", {})
    model = ollama_cfg.get("model", "gemma4:12b-it-qat")
    keep_alive = ollama_cfg.get("keep_alive", "10m")
    restart_every = ollama_cfg.get("restart_every", 10)

    judge = OllamaJudge(model=model, keep_alive=keep_alive)
    ejected = {"done": False}

    def cleanup():
        if ejected["done"]:
            return
        ejected["done"] = True
        print(f"\n[終了処理] {model} をEjectしています...")
        judge.eject()
        print("[終了処理] 完了")

    atexit.register(cleanup)
    signal.signal(signal.SIGINT, lambda *_: sys.exit(0))
    signal.signal(signal.SIGTERM, lambda *_: sys.exit(0))

    print(f"[起動] {model} をロードしています...")
    judge.load()
    print("[起動] 完了")

    print(f"[取得] 直近{days}日分のメールを取得しています...")
    mails = []

    gmail_cfg = config.get("gmail", {})
    if gmail_cfg.get("enabled", True):
        gmail = GmailClient(
            client_secret_path=gmail_cfg["client_secret_path"],
            token_path=gmail_cfg.get("token_path", "token_gmail.json"),
        )
        gmail_mails = gmail.fetch_recent(days=days)
        mails += gmail_mails
        print(f"  Gmail: {len(gmail_mails)}件取得")

    outlook_cfg = config.get("outlook", {})
    if outlook_cfg.get("enabled", True):
        outlook = OutlookClient(
            client_id=outlook_cfg["client_id"],
            tenant_id=outlook_cfg.get("tenant_id", "common"),
            token_cache_path=outlook_cfg.get("token_cache_path", "token_outlook.json"),
        )
        outlook_mails = outlook.fetch_recent(days=days)
        mails += outlook_mails
        print(f"  Outlook: {len(outlook_mails)}件取得")

    print(f"[判定] 合計{len(mails)}件のメールを判定しています...")
    important_mails = []
    for i, mail in enumerate(mails, 1):
        result = judge.judge(mail)
        label = "重要" if result.get("important") else "通常"
        print(f"  ({i}/{len(mails)}) [{label}] {mail['subject'][:40]}")
        if result.get("important"):
            mail["reason"] = result.get("reason", "")
            mail["summary"] = result.get("summary", "")
            important_mails.append(mail)

        if restart_every and i % restart_every == 0 and i != len(mails):
            print(f"  [メモリ解放] {model} を再ロードしています...")
            judge.eject()
            judge.load()

    print("\n" + "=" * 60)
    print(f"重要メール一覧 ({len(important_mails)}件 / 全{len(mails)}件)")
    print("=" * 60)
    if not important_mails:
        print("重要と判定されたメールはありませんでした。")
    for mail in important_mails:
        print(f"\n[{mail['account'].upper()}] {mail['subject']}")
        print(f"差出人: {mail['from']}")
        print(f"日時: {mail['date']}")
        print(f"判定理由: {mail['reason']}")
        print(f"要約: {mail['summary']}")
    print()


if __name__ == "__main__":
    main()
