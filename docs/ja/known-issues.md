# 現状の課題・粗さ一覧

`df27ca5` 時点のソースを読んで見つかった粗さです。優先度は「このまま運用して痛いか」で付けています。根拠は `file:line`。

## 最重要（ツール外の前提）: 年齢確認 CCV の解除

`GET /api/hatch/ccv-status` が `ccv_required` を返すアカウントは、解除するまで `/api/hatch/*` が全部 403 です。
解除手段は **2 通り**あり、`ccv_mode` で選べます。

| `ccv_mode` | 必要なもの | 内容 |
|---|---|---|
| `card` | 日本発行の**クレジット**カード 1 枚 | `POST /api/checkout/*` で 1.00 USD を LIVE 与信（デビットは `1383254` で拒否）。 |
| `link` | Facebook か Instagram のアカウント 1 つ | **カード不要**。Meta 連携（`hatchagelink:`）だけで解除。 |
| `card_then_link`（既定） | 上記のどちらか | まずカード、デビット等で弾かれたら自動で Meta 連携に切り替えます。 |

> カードが 1 枚も無い場合は `ccv_mode=link` にしてください（WebUI「設定」→ `CCV: 解除方式`）。
> デビットカードしか無い場合も同じです。

### カード不要の回避策: Meta アカウント連携（`ccv_mode=link`）

年齢確認にはカード登録以外に、**Meta（Facebook / Instagram）アカウントの連携**で通る経路があります。

- 契約: `GET /api/hatch/age-confirmation/linking-options` → `["instagram","facebook"]`（X/Twitter は非対応）。
- 開始: `GET /api/hatch/age-confirmation/linking-web-auth?account_type=facebook`（**GET のみ**、POST は 405）→ `{"url":"https://www.facebook.com/fxauth/?app_id=156749723264603&…","attempt_id":"hatchagelink:<uuid>.<base64>","expires_at":…}`。`expires_at` は約 1 年後。
- 完了確認: `POST /api/hatch/age-confirmation/linking-web-auth-completion` に `{"attempt_id":…}` を送り、`next_action` が `pending` 以外になるまでポーリング。
  - `continue` = 解除成功 ／ `url:null` 付き 200 = 既に連携済み（成功扱い）
  - `use_credit_card` = 連携は成立したが**カードが要る**（＝この経路の行き止まり。別の IG/FB アカウントを試す）
  - `blocked` / `retry_auth` / `restart_linking` = 再試行（`retry_attempt_id` が返る場合はそれを使う）
- 実装: `musepy/linking.py`（クライアント）+ `webui/ccv_link.py`（背景セッション管理）+ `auth_flow.ensure_ccv`。
- 操作: WebUI の **`http://127.0.0.1:8766/link`** を開き、「連携を開始」→ 表示された Meta URL をブラウザで開いて承認 → 画面が 2.5 秒ごとにポーリングして `continue` を検出します。フロー実行中（自動増殖中）に弾かれた場合は、登録フロー自身が URL を出すので同じページにセッションが現れます。
- 注意: ブラウザは `127.0.0.1:8766` を直接開いてください（この API は SOAX 等の住宅プロキシからだと 403 になることがあるため、WebUI 側は代理池 → 直結の順にフォールバックします）。

### カード経路（`ccv_mode=card` のとき）: **クレジット**カードが 1 枚必要

実カードで通した結果、`461935…`（PayPay 銀行発行 Visa **デビット**）はサーバに**明示的に拒否**されました。

- 症状: `GET /api/hatch/ccv-status` が `{"ccv_status":"ccv_required"}` を返すアカウントは、`hasCompletedActivation` になる前に `/api/hatch/accept-tos` / `/api/hatch/stateful-sharing` / `/api/hatch/invite` / `/api/hatch/invite-code/redeem` が**すべて 403**（`POST /api/auth/check` は `hatchAdmissionInvalidated`）。→ 自分の招待コードが 1 つも出ない＝**裂変（増殖）が止まります**。
- **デビット不可（実測）**: 実カードを入れても `POST /api/checkout/credential` が `code=1383254`「年齢確認のため、VisaまたはMastercardの**クレジットカード**を使用してください。**デビットカードは利用できません**。」を返します。BIN `461935` は PayPay 銀行発行の Visa CLASSIC **DEBIT** なので、この時点で通れません。→ クレジットカードを用意するか、`ccv_mode=link`（上記）に切り替えます。
- 仕様: `POST /api/checkout/*` は `paymentContainerMode:"LIVE"` で、金額は常に **1.00 USD**（JPY 400 相当のダミー）。カードは Meta Pay 側で実在照会されるため、**汎用テスト PAN（`4242…` / `4111…`）は拒否**されます（`2078022` = BIN 単位の一時ブロック、`facebook.com/help/1434403039959381` へ誘導）。
- **診断のコツ（重要）**: `otcSessionId`/`otcType` を付けて撃つと、サーバは失敗理由を汎用コード `2078011` に**丸めて隠します**。理由を見るには otc を**3 呼び出し（init / ptt/encryption-key / checkout/credential）すべてから外した**状態で 1 周させる必要があります（credential だけ外すと `2078136` になります）。`musepy/ccv.py` は `2078011`/`2078132` のとき自動で otc なしの再走を挟み、両方の理由を 1 行の例外メッセージにまとめます。
- 既知コード: `1383254`=クレジット必須／デビット不可、`2078022`=BIN 一時停止、`2078011`=otc 併用時の汎用エラー、`2078136`=otc の片側欠落／不一致、`1353114`=平文送信、`2992007`=Luhn 不一致、`2078132`/`2078135`=PTT 破損。
- 対応: WebUI の「設定」→ メール設定 → `CCV: カード番号` / `有効期限 (MM/YY)` / `セキュリティコード` / `郵便番号` / `国コード` に入力します（`GET/POST /api/settings/ccv`、`db.CCV_FIELDS`）。解除処理は `auth_flow.py` が `accept-tos` の直前に `ensure_ccv` を自動実行します。
- 検証状態: `musepy/ccv.py`（checkout init → credential → finalize、`$e2ee` PTT 暗号化）は実カードで `init` / `encryption-key` まで通り、credential でサーバから**理由付き**の拒否（`1383254`）を取得済み。`musepy/linking.py` は開始〜URL 取得〜ポーリングまで実機で疎通確認済み（`continue` 到達はブラウザ承認待ち）。
- 注意: 解除 1 回につき 1.00 USD の与信が走ります（実課金はされませんが、カード会社側の明細に出ます）。

## P0: 動かす前に直したい

### 1. ~~`httpx` が `requirements.txt` に無い~~ → 対応済み

- 根拠: `test_webui_smoke.py:69` が `from fastapi.testclient import TestClient` を実行しますが、`TestClient` は `httpx` に依存します。当時の `requirements.txt` には `curl-cffi` / `requests` / `fastapi` / `uvicorn` / `pydantic` しかありませんでした。
- 影響: 推奨手順どおりに `pip install -r requirements.txt` だけを行うと、スモークテスト 3 本のうち `test_webui_smoke.py` が ImportError で落ちます。
- 対応: `requirements.txt` に `httpx>=0.27.0` を追加する（本ドキュメント整備と同時に追加済み）。
- 検証: Python 3.12.10 の実環境で `pip install -r requirements.txt` → `python test_webui_smoke.py` を実行し、`WebUI 全链路冒烟通过 3/3`（終了コード 0）を確認済み。`httpx` を uninstall した状態では `RuntimeError: The starlette.testclient module requires the httpx2 package to be installed.` で落ちることも確認済みです（＝この依存は実際に必要）。
- 補足: `httpx` を入れた最新構成（fastapi 0.163.1 / starlette 1.10.0）では `starlette.testclient` が `StarletteDeprecationWarning`（`httpx2` 推奨）を出します。動作に影響はありません。`httpx2` を入れると警告は消えますが、旧 starlette は `httpx` を直接 import するため両方を残すのが無難です。

### 2. 実行環境のツールチェーンが前提化されている

- 根拠: `start_webui.py:43-49` は `fastapi`/`uvicorn` の欠如だけを自動 pip インストールします。Python 本体が無い環境は対象外です。
- 影響: Windows で `python` が Microsoft Store の実行エイリアス（0 バイト）のままだと、終了コード 9009 で何も起きません（エラーメッセージも出ません）。
- 対応: `winget install --id Python.Python.3.12 --exact --scope user` などで実体を入れる。README のセットアップに明記済み。
- 検証: このマシンで `winget` から Python 3.12.10 を導入し、`pip install -r requirements.txt` → スモーク 3 本 → `start_webui.py` 起動 → `/api/health` 200 まで通ることを確認しました。なお `winget` はユーザー PATH を更新しますが、**既に起動中のシェルは古い PATH を保持**するため、開き直すまで `python` は 9009 のままです（実測済み）。

### 3. API に認証・CSRF が無い

- 根拠: `webui/app.py` 全体に認証ミドルウェアがありません。待ち受けは `127.0.0.1`（`app.py:772`）。
- 影響: ローカル専用なら問題なし。`0.0.0.0` に開けると、外部から登録実行・アカウント削除が可能です。
- 対応: 当面は `127.0.0.1` 維持。公開するなら Basic 認証 + CSRF トークンを足す。

## P1: 運用中に踏む

### 4. SSE を一度も開かないとキューが残留する

- 根拠: `app.py:422` の `remove_run_queue` は stream の `finally` にしかありません。
- 影響: `/api/runs/{run_id}/stream` を開かずに run を大量に回すと、`_run_queues`（`registrar.py:27`）が肥大します。
- 対応: run 完了時に無条件でキューを捨てる／TTL 付き eviction を入れる。

### 5. 招待コードの同時実行枠がプロセス内メモリ

- 根拠: `db.py:21` の `_invite_inflight` はモジュール変数です。
- 影響: 再起動で消えるため、再起動直後の数秒は在庫を過大評価し、1 枚を 30 回超えて使う可能性があります（超過分はサーバが `used_up` を返すので実害は限定的）。
- 対応: `used_count` を claim 時に DB 上で仮予約（`+1` して commit し、成功時に確定／失敗時に戻す）へ変更。

### 6. プロキシ正規化が 3 箇所に重複（＋フロントの検証が別実装）

- 根拠: `musepy/client.py:111-129`（`normalize_proxy`）、`musepy/runner.py:41-55`（`mkproxy`）、`http_client.py:127-147` がほぼ同一の変換を持ちます。加えて `webui/frontend/src/stores/proxy.js:57-66` が JS 側で独自に検証しています。`auto_loop.py:34-42` は行単位の重複除去で、変換はワーカー側の `_pick_proxy` が継承します。
- 影響: 片方だけ直すと挙動が食い違います（特に `socks5://` → `socks5h://` の扱いと認証付き URL）。`auto_loop.py` 側に `socks5` の記述が無いため、プロキシ変換が誤っていると原因箇所が見えにくくなります。
- 対応: 1 箇所に寄せて他は re-export。

### 7. レスポンス判定が文字列部分一致

- 根拠: `musepy/client.py:514` の `'"success":true' in r.text`。
- 影響: サーバが `"success": true`（空白入り）やフィールド順を変えると偽陰性になります。
- 対応: `json.loads` → `.get("success") is True` に。

### 8. `vmId` を正規表現で HTML から拾う

- 根拠: `musepy/client.py:478` の `re.search(r'vmId[\"]*:[\"]*([0-9a-fA-F-]{36})', r.text)`。
- 影響: 取得失敗しても致命ではありませんが警告が出て、`vm_id` 未設定のまま進みます（`auth_flow.py:382-389`）。
- 対応: スクリプト内 JSON（`__NEXT_DATA__` 等）の構造的パースへ移行。

### 9. メール受信テストが `cf_temp` でしか動かない

- 根拠: `app.py:663-666` が `provider_cls.pooled` のとき 400 を返します。`outlook` / `icloud_relay` は `pooled=True`。
- 影響: `outlook` 利用時に「テスト」ボタンが 400 になり、故障と誤解しがちです。
- 対応: プール先頭行を 1 件借りてテストする実装に。

### 10. `check_live` が例外を握り潰す

- 根拠: `app.py:513-519` が例外を `{"status": "error", "label": "网络错误"}` にして詳細を捨てます。
- 影響: プロキシ死・Cookie 期限切れ・レート制限がすべて同じ表示になり、切り分けできません。
- 対応: `str(e)` を `detail` として返す。

### 11. フロントの文言が中国語ハードコード

- 根拠: `webui/frontend/src/router/index.js:6-77` をはじめ全ビュー。
- 影響: 日本語話者には敷居が高い。ただし本ドキュメントでカバー可能。
- 対応: i18n（vue-i18n）導入は任意。

## P2: 整理したい

### 12. 未使用モジュール

- 根拠: `config.py` はどのモジュールからも import されていません（同名の dataclass は各所に散在）。
- `fingerprint.py`（27KB）: 未使用。クライアントは `musepy/client.py:33-40` に Chrome 142 の UA を直書きしています（`http_client.py:24` のコメントだけが言及）。
- `http_client.py`: `app.py:315` のプロキシテストからのみ利用。
- 対応: 削除するか、意図をコメントで残す。

### 13. `mail_outlook.py` / `mail_cf.py` は re-export シム

- 根拠: 両ファイルとも本体 `mail_providers/outlook.py` / `cf_temp.py` を再輸出しているだけです（単体実行用の `__main__` 付き）。
- 影響: どちらが正なのか分かりにくい。新規 provider 追加は `mail_providers/__init__.py` に import 行を足す方式です。
- 対応: シムは残しつつ、`mail_providers/` 配下へ一本化するか README で役割を明記。

### 14. API パスとテーブル名の不一致

- `/api/accounts*` が扱うテーブルは `outlook_accounts` です（`db.py` の各種関数）。`outlook` 以外の方式も入るため名称が実態と合っていません。
- 対応: 将来 `mail_accounts` にリネーム（マイグレーションは `db.py:113-141` の冪等 ALTER 方式に倣う）。

### 15. 起動ヘルプのポート誤記

- 根拠: `start_webui.py` は docstring（5 行目・7 行目）と `--port` ヘルプ（34 行目）で既定を「8765」と表示しますが、実際の既定値と `app.py:772` は 8766 です。
- 対応: ヘルプ文字列を 8766 に修正。

### 16. `../icloud` への外部依存

- 根拠: `import_icloud.py:20` が `../icloud/data/workbench.db` を既定参照し、`启动服务.bat` は `../icloud/src/server.mjs` をポート 4173 で起動しようとします。
- 影響: リポジトリ単体では iCloud 方式が完結しません（`outlook` なら不要）。
- 対応: README に「iCloud 方式は別リポジトリ同梱が前提」と明記。

### 17. `webui/static` のビルド鮮度が未検証

- 根拠: `vite.config.js:20-22` は `emptyOutDir: true` で `../static` を出力します。コミット済み `webui/static/index.html` が現在の `src` と一致するかは未検証です。
- 対応: `cd webui/frontend && npm ci && npm run build` して diff を見る（Node が必要）。

### 18. `base.py` の `NotImplementedError` は抽象既定

- 根拠: `base.py:329-331` の `from_config` は既定で `NotImplementedError` を投げるだけで、抽象メソッド強制ではありません。
- 影響: `from_config` を実装し忘れた provider は、実行時になって初めて気付きます。
- 対応: `abc.abstractmethod` 化、または `create_mail_provider` 時に検証。

### 19. 地域の扱いが言語に従属

- 根拠: `musepy/client.py:298-300` が `lang` の先頭 `ja` を見て `region="JP"` を暗黙に送ります。`auth_flow.py:260` は `send_otp(email)` と呼ぶだけで region を渡しません。
- 影響: 「言語は ja だが IP は日本以外」という構成で、意図せず JP 指定になります（waitlist の一因）。
- 対応: `region` をオプション化して UI から指定可能に。

### 20. `musepy/runner.BatchRunner` は実質旧世代

- 根拠: WebUI は `AutoLoopController` を使い、`BatchRunner` はスモークテスト（`test_runner_smoke.py`）でしか触られていません。
- 対応: 位置づけを明記するか、将来統合。

## 参考: 拡張のしかた（メール方式を足す）

`mail_providers/__init__.py` のコメントに手順があります。

1. `mail_providers/xxx.py` で `MailProvider` を継承し `create_mailbox()` / `wait_for_otp()` / `from_config()` を実装。
2. クラスに `@register` を付け、`mail_providers/__init__.py` の末尾に `from . import xxx` を 1 行足す。
3. 設定項目が必要なら `config_fields` で宣言し、`db.get_mail_settings()` から受け取る。
