# 何が必要か（住宅プロキシは必須ではない）

このページが本プロジェクトで最初に読むべきものです。「住宅プロキシが無いと動かないの？」という疑問にコード根拠付きで答えます。

## 結論

**住宅プロキシは必須ではありません。** 必須なのは次の 2 つだけです。

1. **Python 3.10 以上**（`curl_cffi` / `fastapi` / `uvicorn` が動くこと）
2. **メール受信手段**（Muse の OTP を受け取る先。後述の 3 方式から 1 つ）

招待コードは任意です（`redeem_mode=none` で登録のみ可能）。Node.js も不要です（ビルド済みの `webui/static/` が同梱されています）。

## 必須 / 任意 / 不要

| 区分 | 項目 | 理由・根拠 |
| --- | --- | --- |
| 必須 | Python 3.10+ | 実行基盤。`start_webui.py:43-49` が `fastapi`/`uvicorn` を自動 pip インストールしますが、インタプリタ自体は必要 |
| 必須 | メール受信手段 | OTP 取得が登録フローの必須ステップ（`auth_flow.py` の `wait_otp` フェーズ） |
| 任意 | 住宅プロキシ | 空にすると直結で動作。`auto_loop.py:170` の `_pick_proxy` は `_proxy_pool` が空のとき `""` を返し、`MuseFlow(proxy="")` は直結で動作します |
| 任意 | 招待コード | `redeem_mode="none"` でスキップ可。あれば登録後に枠が広がります |
| 任意 | 固定 IP の VPS | 併用可。ただし同一 IP からの大量登録は waitlist 率が上がります |
| 不要 | Node.js / npm | `webui/static/` がリポジトリにコミット済み（`app.py:767` で配信）。フロントを改造したい時だけ必要 |
| 不要 | ブラウザ / Selenium / Playwright | 完全 HTTP 実装（`curl_cffi` で Chrome TLS/HTTP2 を偽装。`musepy/client.py:33-40`） |
| 不要 | 商用の email API | 一時メール Worker を自前で立てるか、手持ちのメールを使えば足ります |

## プロキシ無しで運用する手順

1. 直結で数件だけ流し、`registered_waitlist`（地区受限）の割合を見ます。
2. その割合が高くなければ**そのまま直結運用で構いません**。
3. 直結運用時の推奨設定:
   - `max_per_ip` を小さく（既定 6 → 直結なら **2〜3** を推奨）。同一 IP で打ちすぎると waitlist に落ちます。
   - `concurrency` は **1〜3**。`auto_loop.py` のワーカー数を増やすほど同一 IP への集中度が上がります。
   - `stop_on_reject`（停批保护）を **ON** のまま。waitlist を検知すると自動で一時停止し、ログに理由を出します（`auto_loop.py:262-273` の `_trigger_pause_alert`）。
4. `registered_waitlist` が目立つようになったら初めてプロキシを足します。プロキシプールを空 → 非空にすると、`auto_loop.py:463-470` の `max_per_ip` 到達時に自動で IP を切り替えるロジックが有効になります。
5. プロキシ形式は `http://user:pass@host:port`。3 箇所に重複実装された正規化関数のいずれかが通します（`musepy/client.py:111-129`、`http_client.py:127-147`、`musepy/runner.py:41-55`）。

## メール受信手段の比較

`db.py` の `DEFAULT_MAIL_SOURCE` は `icloud_relay` ですが、**追加サービスが要らないのは `outlook`** です。

| 方式 | `mail_source` 値 | 追加で必要なもの | 使い捨て | 備考 |
| --- | --- | --- | --- | --- |
| Outlook / Hotmail 実アカウント | `outlook` | なし（手持ちのアカウント） | いいえ（プール消費） | `pooled=True`、1 行 4 セグメント `email----password----client_id----refresh_token`。最も手軽 |
| 自前 Cloudflare 一時メール | `cf_temp` | ドメイン + Cloudflare Worker のデプロイ | はい（`ephemeral=True`、`pooled=False`） | 実質無制限。`cf_api_url` / `cf_admin_token` / `cf_domain` を設定画面で登録 |
| iCloud リレー（隠しメール） | `icloud_relay` | 第三者の中継サービス、または `../icloud` 自前サーバ | いいえ（プール消費） | 1 行 2 セグメント `email----relay_url`。既定値だが外部依存あり |

各方式の詳細な導入手順:

- **`outlook`**: アカウントのメールアドレス、パスワード、`client_id`、`refresh_token` を用意し、`email----password----client_id----refresh_token` の 1 行形式で WebUI の「導入/インポート」から投入します（`/api/import`）。デバッグ用のリフレッシュトークン取得は別途必要です。
- **`cf_temp`**: Cloudflare Worker の一時メール実装を自前で立て、`cf_api_url`（Worker の URL）、`cf_admin_token`（管理トークン）、`cf_domain`（受信用ドメイン）を `/api/settings/mail` から保存します。プールが不要なので、`pooled` 系の枯渇待ちがありません。
- **`icloud_relay`**: 外部の中継 URL を `email----relay_url` 形式で投入します。`import_icloud.py` を使うと、隣の `../icloud` ディレクトリのローカルサーバ（`http://127.0.0.1:4173`）から隠しメールを一括生成して取り込めます（環境変数 `ICLOUD_DB_PATH` で DB パスを上書き可、`import_icloud.py:20`）。

## 注意点

- `/api/settings/mail/test`（テスト受信）は **`cf_temp` でしか使えません**。`pooled=True` の方式では 400 を返します（`webui/app.py:660-666`）。`outlook` を選んだ場合、テスト受信の失敗は異常ではありません。
- `outlook` / `icloud_relay` は**プール消費型**です。1 登録につき 1 行を消費し、行が尽きるとワーカーは「邮箱池已空，等待补充...」と表示して待機→一定回数で終了します（`auto_loop.py:371-390`）。
- 実アカウントを使う方式では、**同じメールを二重登録しない**ため DB の `outlook_accounts`（`status` 列）で排他制御されます。`in_use` のままプロセスが落ちた場合は `/api/accounts/release_stale` で解放してください（起動時にも 1800 秒超の `in_use` は自動解放されます。`app.py:37-42` → `db.py:392`）。
- 直結で `NO_PROXY` 環境変数が効かない場合は `start_webui.py:19-21` の設定を確認してください。

## 検証手順（オフラインでできる）

ネットワークを使わずに、環境が正しいことだけを確認できます。

1. Python をインストール（Windows の Microsoft Store 版エイリアスだけでなく実体が必要）:
   - `winget install Python.Python.3.12`
2. 依存をインストール: `pip install -r requirements.txt`
   - `test_webui_smoke.py:69` の `from fastapi.testclient import TestClient` は `httpx` を要求します。以前は `requirements.txt` に未記載だったため、`httpx>=0.27.0` を追記済みです（追記前のリビジョンを使う場合のみ `pip install httpx` が別途必要）。
3. スモークテスト 3 本を実行（すべてモックで完結、ネットワーク不要。各ファイルに `main()` があるので単体実行できます）:
   - `python test_onboarding_smoke.py`
   - `python test_runner_smoke.py`
   - `python test_webui_smoke.py`
   - pytest を入れるなら `python -m pytest test_onboarding_smoke.py test_runner_smoke.py test_webui_smoke.py -q` でも可（`tests` ではなくリポジトリ直下にあります）。
4. サーバ起動確認: `python start_webui.py --port 8766` → ブラウザで `http://127.0.0.1:8766` を開き、`/api/health` が返ることを確認。
   - `start_webui.py` のヘルプは既定ポートを「8765」と誤表記しますが、**実際の既定値と `webui/app.py:772` のポートは 8766** です。

## つまずきやすいポイント

- **`python` が Microsoft Store のエイリアス（0 バイト）** のままだと、終了コード 9009 で何も起きません。`winget` で実体を入れてください。
- `winget` は `C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\winget.exe` にあります。
- Windows 版の起動用バッチ `启动服务.bat` は、隣の `..\icloud` の Node サーバ（ポート 4173）を先に起動しようとします。`../icloud` が無くても Muse 側（8766）は動きます。
- 停止は `停止服务.bat`（ポート 8766 の LISTENING を `taskkill`）。
