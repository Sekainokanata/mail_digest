# Mail Digest — Gmail/Outlook 重要メール自動要約アプリ

Gmail・Outlook(1アカウントずつ)の受信箱から直近3日分のメールを取得し、
ローカルで動くGemma 4 12B(Ollama)に重要度を判定させ、重要なメールだけ要約して表示します。

## 動作の流れ

1. アプリ起動 → Ollamaの `gemma4:12b` を自動ロード
2. 起動直後 → Gmail/Outlookから直近3日分のメールを自動取得
3. 各メールをモデルに判定させ、重要なものだけ要約付きで一覧表示
4. アプリ終了(Ctrl+Cや正常終了) → `gemma4:12b` を自動でEject(メモリ解放)

---

## 0. 事前準備(初回のみ・必須)

このアプリを動かす前に、**Google Cloud** と **Azure** それぞれでアプリ登録が必要です。
どちらを先にやっても構いません。

### 0-1. Ollama とモデルの準備

```bash
# Ollama未インストールの場合
# https://ollama.com/download からインストール

ollama pull gemma4:12b-it-qat
```

### 0-2. Google Cloud Console(Gmail用)

1. [Google Cloud Console](https://console.cloud.google.com/) にアクセスし、新規プロジェクトを作成(既存プロジェクトでも可)
2. 左メニュー「APIとサービス」→「ライブラリ」から **Gmail API** を検索して有効化
3. 「APIとサービス」→「OAuth同意画面」を設定
   - User Type: **外部**(個人Googleアカウントの場合)
   - アプリ名・サポートメール等の必須項目を入力
   - スコープ追加は不要(コード側で `gmail.readonly` を指定)
   - テストユーザーに自分のGmailアドレスを追加(公開審査は不要、テストモードのままでOK)
4. 「認証情報」→「認証情報を作成」→「OAuthクライアントID」
   - アプリケーションの種類: **デスクトップアプリ**
   - 作成後、JSONをダウンロード
5. ダウンロードしたJSONを `mail_digest/client_secret_gmail.json` として配置

### 0-3. Azure Portal(Outlook用)

1. [Azure Portal](https://portal.azure.com/) → 「Microsoft Entra ID」→「アプリの登録」→「新規登録」
2. 名前を入力(例: `MailDigestApp`)
3. サポートされているアカウントの種類:
   - 個人のoutlook.com/hotmail.comアカウントのみで使う場合 → **個人のMicrosoftアカウントのみ**
   - 会社アカウントも使う可能性があるなら → **任意の組織ディレクトリと個人のMicrosoftアカウント**
4. リダイレクトURIは空欄のままでOK(デバイスコードフローを使うため不要)
5. 登録後、「概要」画面の **アプリケーション(クライアント) ID** をコピー → `config.yaml` の `outlook.client_id` に設定
6. 「認証」→「詳細設定」→ **「パブリック クライアント フローを許可する」を「はい」** に変更して保存
   (デバイスコードフローを使うために必須)
7. 「APIのアクセス許可」→「アクセス許可の追加」→「Microsoft Graph」→「委任されたアクセス許可」→ **Mail.Read** を追加
8. 管理者の同意が必要な組織の場合は管理者に付与を依頼(個人アカウントのみの場合は不要)

> Client Secret(クライアントシークレット)の作成は **不要** です。
> このアプリはデスクトップアプリ向けの「パブリッククライアント」として、
> デバイスコードフロー(ブラウザで表示されたコードを入力する方式)で認証するため、
> クライアントIDだけで動作します。

---

## 1. セットアップ

```bash
cd mail_digest
python -m venv venv
source venv/bin/activate  # Windowsは venv\Scripts\activate
pip install -r requirements.txt

cp config.example.yaml config.yaml
# config.yaml を編集し、Outlookのclient_idなどを設定
```

`client_secret_gmail.json` を `mail_digest/` 直下に配置してください。

## 2. 実行

```bash
python main.py
```

- 初回実行時のみ、Gmailはブラウザでログイン画面が開きます。
- 初回実行時のみ、Outlookはターミナルに表示される「https://microsoft.com/devicelogin」とコードでの認証が必要です。
- 2回目以降は `token_gmail.json` / `token_outlook.json` にトークンがキャッシュされ、自動的にサインインします(リフレッシュトークン期限切れ時は再認証が必要な場合があります)。

終了する場合は `Ctrl+C` で問題ありません。終了処理として自動的に `ollama stop gemma4:12b` 相当の処理が実行されます。

## ファイル構成

```
mail_digest/
  main.py              # エントリポイント。起動/取得/判定/表示/終了処理を統括
  gmail_client.py       # Gmail API 認証・メール取得
  outlook_client.py     # Microsoft Graph API 認証・メール取得
  ollama_client.py      # gemma4:12bのロード/判定/Eject
  config.example.yaml   # 設定ファイルのテンプレート
  requirements.txt
```

## 注意事項

- `client_secret_gmail.json` / `token_*.json` / `config.yaml` には認証情報が含まれるため、Gitリポジトリにコミットしないでください(`.gitignore`推奨)。
- メール本文をそのままローカルLLMに渡すため、通信は外部に出ません(Ollamaはローカル推論)。ただし取得自体はGoogle/Microsoftのサーバーと通信します。
- 重要度判定の基準は `ollama_client.py` の `IMPORTANCE_PROMPT` で調整できます。
