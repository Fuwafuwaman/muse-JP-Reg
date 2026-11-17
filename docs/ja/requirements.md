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

## ブラウザのプロキシ設定（Edge + FoxyProxy 等）は効きません

Edge に FoxyProxy を入れて住宅プロキシを設定しても、**このツール自身の通信は一切プロキシされません**。ブラウザと本ツールは別プロセスで、コード側がブラウザ／OS のプロキシ設定を明示的に無効化しているためです。

- `http_client.py:160-168`: `session.trust_env = False` に加え、プロキシ未指定時は `session.proxies = {"http": "", "https": ""}` を代入します。コメントどおり「显式设置空代理，覆盖系统环境变量 (trust_env=False 对 libcurl 不够)」で、`curl_cffi`（libcurl）が `HTTP_PROXY` 等の環境変数を拾うのを潰しています。
- `musepy/client.py:189-192`: 同じく空プロキシを注入し、`trust_env = False` を立てます。
- `start_webui.py:18-21`: `NO_PROXY` / `no_proxy` に `localhost,127.0.0.1,::1` のみを設定し、`HTTP_PROXY` は設定しません（WebUI 自身の疎通をプロキシ経由にしないため）。

つまり FoxyProxy が変えるのは「Edge のタブの経路」だけで、Python の出口 IP は変わりません。プロキシを使うには **WebUI の「代理池」画面に貼り付けてください**。

### 貼り付け先

| 目的 | 場所 |
| --- | --- |
| 自動ループ（推奨） | WebUI の `/proxy`（代理池）画面に 1 行 1 個で貼り、「覆盖保存」。ブラウザの `localStorage` キー `muse_proxy_pool_v1` に保持され（`webui/frontend/src/stores/proxy.js:4`）、`/api/auto/start` の `proxy_pool` として送られます（`app.py:86`） |
| 手動で 1 件だけ | 各画面の proxy 入力欄（`MuseFlow(proxy=...)` にそのまま渡ります） |

代理池画面の「测试全部 / 测试」ボタン（`webui/frontend/src/views/ProxyPool.vue:103`・同 `:147`）は `POST /api/proxy/test`（`app.py:313-351`）を呼び、**出口 IP と遅延**を表示します。直結時と違う IP が出れば経路が切り替わったと確認できます。

### 実測（SOAX 住宅プロキシ・407 の見分け方）

SOAX の JP 住宅プロキシで実際に検証した記録です。同じ症状が出たらここを見てください。

1. `/api/proxy/test` が `407` を返し、レスポンスヘッダに `X-Soax-Detail: invalid or missing package key` が出る場合は、**ユーザー名は正しいがパッケージキー（パスワード側）が違う**という意味です。IP やポート、`country=jp` の指定ミスではありません。
2. キーは SOAX のダッシュボードで確認した値を使ってください。手元の検証では、ダッシュボードの実キーが `MaMHw6F1G1` であるのに対し、コピー元のメモが `MaHMW6F161`（`H` と `w` の位置が入れ替わり、末尾も `1`）になっていて 407 が続いていました。**見た目が近いので目視では気づきにくい**のが罠です。
3. 誤記を直すと `/api/proxy/test` が `ok:true` になり、出口 IP が `14.11.202.129`（岐阜・KDDI）と返りました。直結時の `112.69.27.49`（大阪）とは別 IP なので、経路が切り替わったことを確認できます。
4. その状態で実クライアントを通した結果: `open_home 200` → `restart` で CSRF 取得成功 → `send_otp` が `{"registration_state": "otp_sent"}` を **7.6 秒**で返し、プロキシ経由のフローが最後まで通ることを確認済みです。
5. なお FoxyProxy 側に同じ誤記があっても、Edge のタブは動いてしまうため「FoxyProxy では成功している」ように見えます。プロキシを使うときは **FoxyProxy の文字列ではなく、ダッシュボードの値を代理池画面に貼る**のが確実です。

### 受け付ける形式

`http_client.py:127-147` の `normalize_proxy` を実際に実行した結果です（同じ実装が `musepy/client.py:111-129`・`musepy/runner.py:41-55` にも重複しています）。

| 入力 | 正規化後 |
| --- | --- |
| `jp.proxy.example:8000:user:pass` | `http://user:pass@jp.proxy.example:8000`（`muse-protocol` の `proxies.txt` と同じ 4 段形式） |
| `http://user:pass@1.2.3.4:8080` | そのまま |
| `1.2.3.4:8080` | `http://1.2.3.4:8080`（認証なし） |
| `socks5://u:p@h:1080` | `socks5h://u:p@h:1080`（`h` 付き＝プロキシ側で DNS 解決） |
| `socks5h://h:1080` | そのまま |
| 空行・空白のみ | 直結（`None`）|

1 行 1 個で貼ります。行頭 `#` はコメント、重複行は自動で除去されます（`auto_loop.py:34-42`）。

**日本 (JP) ノードを推奨します。** 地区は `musepy/client.py:296-300` で `lang` が `ja` で始まるとき `region=JP` になるため、JP 出口なら waitlist を避けやすくなります。

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

### 取込（インポート）の書式と挙動

- 取込が必要なのは**プール消費型の `outlook` / `icloud_relay` だけ**です。`cf_temp` は `line_segments = 0` で、毎回自分でアドレスを発行するため号池を使いません（`mail_providers/cf_temp.py:65`）。
- 書式は `----` 区切りで、outlook 4 段（`email----password----client_id----refresh_token`）、icloud_relay 2 段（`email----relay_url`）。画面のプルダウンで種類を**明示**してください。4 段は Outlook と Gmail が同数で自動判別できないため、自動判定はあくまで保険です（`mail_providers/base.py:460-497`）。
- 検証は**全行まとめて**行い、1 行でも不正なら**整批拒否**（HTTP 422）で号池は一切変更されません。行番号と理由が返ります（`base.py:500-545`）。同一メールの重複・メール書式の不正もこの段階で検出。空行と `#` 行はスキップ、BOM は除去。上限は 5,000 行 / 2 MiB（`base.py:87-88`）。
- 既に居るメールは `kind` / `refresh_token` / `relay_url` のいずれかが変わったときだけ更新され、`available` に戻ります。同一内容なら skip です（`db.py:170-215`）。
- ⚠️ **設定の `mail_source` と取込時の `kind` を必ず一致させてください。** `auto_loop.py:343-359` は `mail_source` と同一 `kind` の行しか claim しないため、不一致だと「可用邮箱」の数字だけが増えて 1 件も消費されません。
- 状態は `available` → `in_use` → `done` / `failed`。`failed` は `fail_reason` を残し、**自動ループでは再使用されません**（`claim_next` は `available` のみを対象。`db.py:293-320`）。再挑戦するにはプール画面の「重置」/「重置选中」で `available` に戻します（`db.py:366` `bulk_reset_to_available`）。`in_use` のまま落ちた場合は `release_stale`（`db.py:392`、既定 1800 秒で自動解放、`app.py:37-42`）。
- `../icloud` の 4173 を起動しておけば、取込画面の「ローカル iCloud の隠しメールを一括同期」ボタンで `unused` の隠しメールを自動投入できます（`/api/import/icloud_local` → `import_icloud.py` の `sync_icloud_to_muse`）。

## 注意点

- `/api/settings/mail/test`（テスト受信）は **`cf_temp` でしか使えません**。`pooled=True` の方式では 400 を返します（`webui/app.py:660-666`）。`outlook` を選んだ場合、テスト受信の失敗は異常ではありません。
- `outlook` / `icloud_relay` は**プール消費型**です。1 登録につき 1 行を消費し、行が尽きるとワーカーは「邮箱池已空，等待补充...」と表示して待機し、約 6 巡回で終了します（`auto_loop.py:373-381`）。
- 実アカウントを使う方式では、**同じメールを二重登録しない**ため DB の `outlook_accounts`（`status` 列）で排他制御されます。`in_use` のままプロセスが落ちた場合は `/api/accounts/release_stale` で解放してください（起動時にも 1800 秒超の `in_use` は自動解放されます。`app.py:37-42` → `db.py:392`）。
- 直結で `NO_PROXY` 環境変数が効かない場合は `start_webui.py:19-21` の設定を確認してください。

## 検証手順（オフラインでできる）

ネットワークを使わずに、環境が正しいことだけを確認できます。

1. Python をインストール（Windows の Microsoft Store 版エイリアスだけでなく実体が必要）:
   - `winget install --id Python.Python.3.12 --exact --scope user`
   - インストール後は**ターミナルを開き直してください**。`winget` はユーザー PATH を更新しますが、起動中のシェルは古い PATH を保持したままなので、`python` が依然として Store の 0 バイトエイリアスに解決されます（実測: 終了コード 9009 のまま）。
2. 依存をインストール: `python -m pip install -r requirements.txt`
   - `test_webui_smoke.py:69` の `from fastapi.testclient import TestClient` は `httpx` を要求します。以前は `requirements.txt` に未記載だったため、`httpx>=0.27.0` を追記済みです（追記前のリビジョンを使う場合のみ `pip install httpx` が別途必要）。
   - **新しい starlette（1.x 系）を使うと**、`starlette.testclient` は `httpx2` を優先し、`httpx` しか無い場合は `StarletteDeprecationWarning: Using 'httpx' with 'starlette.testclient' is deprecated; install 'httpx2' instead.` を出します。**警告だけで動作します**（実測: httpx 0.28.1 のみで `3/3` 成功）。警告を消したい場合だけ `python -m pip install httpx2` を足してください。旧 starlette は `httpx` を直接 import するため、`httpx` は残しておくのが安全です。
3. スモークテスト 3 本を実行（すべてモックで完結、ネットワーク不要。各ファイルに `main()` があるので単体実行できます）:
   - `python test_onboarding_smoke.py`
   - `python test_runner_smoke.py`
   - `python test_webui_smoke.py`
   - pytest を入れるなら `python -m pytest test_onboarding_smoke.py test_runner_smoke.py test_webui_smoke.py -q` でも可（`tests` ではなくリポジトリ直下にあります）。
4. サーバ起動確認: `python start_webui.py --port 8766` → ブラウザで `http://127.0.0.1:8766` を開き、`/api/health` が返ることを確認。
   - `start_webui.py` のヘルプは既定ポートを「8765」と誤表記しますが、**実際の既定値と `webui/app.py:772` のポートは 8766** です。

### 実測結果（Python 3.12.10 / Windows・ネットワーク接続なし）

上記 1〜4 を通しで実行し、以下のとおり確認できています。手元で同じ出力になれば環境は正常です。

| 手順 | 期待される出力 |
| --- | --- |
| `pip install -r requirements.txt` | `Successfully installed … httpx-0.28.1 …`（`httpx` が含まれること自体が追記修正の検証になります）、`pip check` は `No broken requirements found.` |
| `python test_onboarding_smoke.py` | `冒烟通过 6/6`、終了コード 0 |
| `python test_runner_smoke.py` | `冒烟通过 9/9`、終了コード 0 |
| `python test_webui_smoke.py` | `WebUI 全链路冒烟通过 3/3`、終了コード 0 |
| `curl http://127.0.0.1:8766/api/health` | HTTP 200 と `{"ok":true,"stats":{…},"invites":{…},"registered":{…}}` |

この 3 本が緑でも**外部 API の仕様変更は検出できません**（すべてモック）。実際の登録可否は `outlook` などで 1 件流して確認してください。

## つまずきやすいポイント

- **`python` が Microsoft Store のエイリアス（0 バイト）** のままだと、終了コード 9009 で何も起きません。`winget` で実体を入れてください。
- `winget` で入れた直後に `python -V` が 9009 を返す場合、PATH が古いだけです。**ターミナルを開き直す**か、その場で PATH を読み直してください（実測で解消を確認済み）。
- `python -m pip install` の最後に `… which is not on PATH` という警告が大量に出ますが、`Scripts` ディレクトリの警告なので**無視して構いません**（`pip` / `python` 本体は使えます）。
- `winget` は `C:\Users\<user>\AppData\Local\Microsoft\WindowsApps\winget.exe` にあります。
- Windows 版の起動用バッチ `启动服务.bat` は、隣の `..\icloud` の Node サーバ（ポート 4173）を先に起動しようとします。`../icloud` が無くても Muse 側（8766）は動きます。
- 停止は `停止服务.bat`（ポート 8766 の LISTENING を `taskkill`）。
