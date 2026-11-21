# 現状の課題・粗さ一覧

`df27ca5` 時点のソースを読んで見つかった粗さです。優先度は「このまま運用して痛いか」で付けています。根拠は `file:line`。

## 最重要（ツール外の前提）: 年齢確認 CCV の解除

`GET /api/hatch/ccv-status` が `ccv_required` を返すアカウントは、解除するまで `/api/hatch/*` が全部 403 です。
解除手段は **2 通り**あり、`ccv_mode` で選べます。

| `ccv_mode` | 必要なもの | 内容 |
|---|---|---|
| `card` | 日本発行の**クレジット**カード 1 枚 | `POST /api/checkout/credential` でカードを登録するだけで解除できます。**1 枚で通せるのは 10 アカウントまで**（11 件目から `2078073`）。デビットは `1383254` で拒否。出回っている激安カードは `2078011` で拒否。 |
| `link` | Facebook か Instagram のアカウント 1 つ | **カード不要**。Meta 連携（`hatchagelink:`）だけで解除。 |
| `card_then_link`（既定） | 上記のどちらか | まずカード、デビット等で弾かれたら自動で Meta 連携に切り替えます。 |

> カードが 1 枚も無い場合は `ccv_mode=link` にしてください（WebUI「設定」→ `CCV: 解除方式`）。
> デビットカードしか無い場合も同じです。
> 20 件流すときは `card` だけだと 10 件で止まるので、`link` を併用するか 2 枚目を用意します。

### カード不要の回避策: Meta アカウント連携（`ccv_mode=link`）

年齢確認にはカード登録以外に、**Meta（Facebook / Instagram）アカウントの連携**で通る経路があります。

- 契約: `GET /api/hatch/age-confirmation/linking-options` → `["instagram","facebook"]`（X/Twitter は非対応）。
- 開始: `GET /api/hatch/age-confirmation/linking-web-auth?account_type=facebook`（**GET のみ**、POST は 405）→ `{"url":"https://www.facebook.com/fxauth/?app_id=156749723264603&…","attempt_id":"hatchagelink:<uuid>.<base64>","expires_at":…}`。
  - **`expires_at` = 承認 URL の寿命。実測で発行から約 1,050 秒（約 17.5 分）**（`now=1794937805` に対し `expires_at=1794938857`）。試行を作り直すと新しい値になります。長く放置すると URL が死ぬので、これは「有効期限」として扱ってください。
- 完了確認: `POST /api/hatch/age-confirmation/linking-web-auth-completion` に `{"attempt_id":…}` を送り、`next_action` が `pending` 以外になるまでポーリング。
  - `continue` = 解除成功 ／ `url:null` 付き 200 = 既に連携済み（成功扱い）
  - `use_credit_card` = 連携は成立したが**カードが要る**（＝この経路の行き止まり。別の IG/FB アカウントを試す）
  - `blocked` / `retry_auth` / `restart_linking` = 再試行（`retry_attempt_id` が返る場合はそれを使う）
- 実装: `musepy/linking.py`（クライアント）+ `webui/ccv_link.py`（背景セッション管理）+ `auth_flow.ensure_ccv`。
- 操作: WebUI の **`http://127.0.0.1:8766/link`** を開き、「連携を開始」→ 表示された Meta URL をブラウザで開いて承認 → 画面が 2.5 秒ごとにポーリングして `continue` を検出します。フロー実行中（自動増殖中）に弾かれた場合は、登録フロー自身が URL を出すので同じページにセッションが現れます。
  - まとめて処理するときは **「未承認の URL を全部タブで開く」** が便利です。開いたタブで順に **「許可」/「続行」** を押していくだけで、承認は数十秒でこのページに反映され状態が `continue` に変わります。URL が切れたら **「全部の URL を出し直す」**（`POST /api/ccv/link/rearm_all`）→ もう一度「全部タブで開く」で続行できます。
  - 残り時間が各行に出ます。切れた／放置した行は **「URL 出し直す」**（`POST /api/ccv/link/rearm`）で新しい試行に置き換えられます。まとめてやる場合は `POST /api/ccv/link/rearm_all`。
  - **同じ Meta アカウントを 20 件で使い回せます**（1 件ごとに 1 回の承認が要るだけ）。ブラウザの Meta ログインが残っていれば 2 件目以降は「許可」を押すだけです。
- **起点の招待コードが無くても redeem 経路は検証できます**: `ccv_status` が未解除の間は `/api/hatch/invite` も `/api/hatch/invite-code/redeem` も 403 `{"error":"Forbidden"}` を返しますが、これは**ゲートのせいであってコードやプロキシのせいではありません**。未解除の 1 件にデタラメなコード（例 `AAAA-BBBB-CCCC-DDDD`）で `POST /api/registered/redeem` を投げると、カード未設定エラーではなく `403` か `429` が返れば「CCV まで到達した」＝配線は正しいと確認できます。
- **同じ 403 は「CCV 解除後にオンボーディングを踏み残した」ときにも出ます（重要）**: 解除できたのに `/api/hatch/*` が 403 のままなら、それは未解除ではなく **`accept-tos` / `vm/wake` が未実行**なだけです。未活性のアカウントを掘り起こすときは必ず
  `activate_session(access_token)` → `accept-tos` → `GET /?aymh_complete=1` → `POST /api/hatch/vm/wake` → `POST /api/hatch/stateful-sharing` の順で踏んでから `/api/hatch/invite` を撃ってください（`auth_flow.redeem_registered_account` がこの順序です）。
  - 既に `accept-tos` 済みのアカウントはアプリ画面に着地し、HTML から ROW の動的披露文が取れず `disclosure_context_unresolved` になります。これは再同意が不要というだけなので失敗にせず進めます（`client.activate_session(tolerant=True)`、`onboarding["state"] == "disclosure_missing"`）。
- **自分の招待コードは `GET` ではなく `POST /api/hatch/invite` が実体です**: `GET /api/hatch/invite`（`client.invite()`）は発行前は `{"invite_state":"ELIGIBLE","code":null}` を返すため、「コードが空で返ってこない」ように見えます。コードを作るのは `POST`（`client.scratch_code()`）で、`{"code":"XXXXXX","max_uses":30,"use_count":0,"invite_link":"https://muse.ai/join"}` を返します。`GET /api/hatch/invite-code` でも同じコードが読めます。
- **完了チェック `linking-web-auth-completion` は「Meta 側がまだ解決していない」間 HTTP 429 `{"error":"Failed to resolve account linking"}` を返します（これが実質の `pending`。1 本だけを 0.3 req/s で叩いても 429 なので、レート制限ではありません）**: 20 件を並列で待つと開始直後から常時 429 になります。そこで **429 は失敗と数えず、ポーリング間隔どおり淡々と待ち続けます**（ログには `link: まだ未解決（429）、待機中` と出続けます＝正常）。429 以外の失敗だけを粘り、`hard`（429 以外の 4xx＝403 など）は即送出、5xx などは 10 回かつ連続 60 秒で諦めます。バックオフ中に寿命が来た場合は例外ではなく `pending` を返すため、試行が `error` に落ちず承認 URL は寿命（約 17 分）まで生き残ります。`LinkSession.run` は activate を最大 4 回試行し直し、URL が生きているうちは同じ `attempt_id` を使い回すので、人間が既に開いたタブを無効化しません。送信は `linking._slot()` がプロセス全体で「間隔 0.3 秒以上・同時 6 件以下」に抑えます。
  - したがって `/link` の行が何分も `pending` のままなのは**正常**です（429 は解決待ちの合図）。`state=error` になるのは 403 など回復しない応答を受けたときだけです。
  - それでも `state=error` になった行は、その行の「URL 出し直す」を押して開き直してください（URL 自体はまだ生きていても、追跡が切れている可能性があるためです）。
- **`linking-options` の 403 は「失敗」ではなく「もう連携先が無い」か「接続先の一時拒否」です（コード側で吸収済み）**: 解除済みのアカウントに `GET .../linking-options` / `GET .../linking-web-auth` を撃つと 403 `{"error":"Forbidden"}` が返ります。proxy の出口 IP と Cookie がずれたときにも同じ 403 が返ります。そこで `musepy/linking.py` は 403 を「連携対象なし」に畳み（`linking_options()` は空配列、`start()` は `url:null` ＝完了扱い）、`webui/ccv_link.py::_pick_client` は 403 のときだけ次の接続先（代理池 → 直結）に落とし、**全滅したら最後のクライアントを捨てずに返します**（捨てると `state=error` になり、無駄な出し直しを繰り返すだけになります）。加えて `LinkSession.run` は `url:null` / 403 で完了扱いになったとき、`ccv_status` が読めて未解除なら `pending` に戻します（誤って「解除済み」と記録しないため）。
- **`/link` の対象一覧は「未解除のものだけ」です**: 交換（redeem）まで通った `status=redeemed` のアカウントは CCV が既に解除済みなので、`ccv_status` が古い `pending` のまま残っていても一覧から外れます（`webui/ccv_link.py::pending_accounts`）。
- **席を外しても承認 URL が切れないよう、自動で出し直します（既定オン）**: 承認 URL の寿命は約 17 分しかないため、`LinkManager` の監視スレッド（90 秒周期）が「残り寿命 150 秒未満」または `state=error` の試行に新しい URL を出し直します（同一メールは最短 45 秒間隔・1 件ずつ実行して `start` の 429 を避けます）。`/link` の「切れた URL を自動で出し直す」チェックボックスで切り替えでき、`GET/POST /api/ccv/link/auto`（手動実行は `POST /api/ccv/link/auto/tick`）でも操作できます。
  - 承認は常に最新のタブで行う必要はありません。古いタブで承認しても、その試行の裏スレッドが `ccv_status` を再確認して `continue` に落とし、次の出し直しの `start` は `url:null` を返して自然に止まります。
  - 登録フロー側が自分でポーリングしている枠（`external=True`）は、二重に叩かないよう自動出し直しの対象外です。
- **承認 URL は必ず実ブラウザで開いてください（`curl` だと 400 になります）**: 素の GET（`Sec-Fetch-*` 等が無いリクエスト）では Meta が `HTTP 400` ＋「Sorry, something went wrong.」を返します。ブラウザ相当のヘッダを付けると `302` で `https://www.facebook.com/login/?app_id=156749723264603&next=<fxauth の URL 全体>` に飛び、**`etoken` は `next=` に保持される**のでログイン後にそのまま承認へ戻れます（＝未ログインでもリンクは壊れません）。
  - **Facebook のプロフィール選択画面で「次へ」を押しても先へ進みません（実測・これが連携が永久に終わらない最大の原因）**: 遷移先の `/login/?app_id=…&next=…` は絵だけの `div[role=button]`（`href` 無し・`<form>` 無し・`<input>` 無し）で、「次へ」は実マウスクリック / 強制クリック / JS `.click()` / Enter / `form.submit()` の**どれでも動きません**。動かそうとして別ページへ飛ぶと `next=`（＝`etoken`）が失われ、`login.php?api_key=`（空 api_key・next 無し）→ facebook.com のフィードに着地してトークンが消費されないまま終わり、以後 `linking-web-auth-completion` は 429 を返し続けます。**回避策は「次へ」ではなく「別のプロフィールを使用」を押すこと**です。これを押すと選択画面が本物のログインフォームに差し替わり、`<form action>` が `login/?app_id=…&next=<fxauth 全体>` のまま `next` を保持します。あとはメール・パスワードを入れて Enter するだけで `native_sso`（「〈名前〉としてログイン」）→ `accountscenter.meta.com/add_accounts` まで約 11 秒で到達します（実測）。
  - `add_accounts` が「エラーが発生しました / もう一度 Facebook アカウントにログインしてください」を出した場合は、そのリンク（`app_id=633385687760560&next=…%2Fadd%2F%3Fbackground_page%3D%2Fadd_accounts%2F`）をそのまま踏んで 2 周目に入ります。2 周目も同じ手順（「別のプロフィールを使用」→ メール・パスワード → Enter）で、今度は AC の `add/` ダイアログ（プロフィール選択行）に到達します。
  - **`next_action` に `error` が返ることがあります（フロントの定義に無い実測値）**: 200 で `{"next_action":"error"}` が返ります。以前は「未知の next_action」として例外になっていたため、`musepy/linking.py` の `NEXT_ACTIONS` に `error` を追加し、`REARM_ACTIONS`（＝`retry_auth` / `restart_linking` / `error`）として「承認 URL を出し直せば進める」扱いにしています（`webui/ccv_link.py` の自動出し直し対象も同集合に揃えました）。
  - 一括実行（20 件など）では 1 件ずつ人間の承認が要るので、**先に登録だけ 20 件流して `ccv_link_timeout` を短く（例: 90 秒）し、あとから `/link` で「出し直す」→ 承認**と 2 段階に分けるのが現実的です。
- 注意: ブラウザは `127.0.0.1:8766` を直接開いてください（この API は SOAX 等の住宅プロキシからだと 403 になることがあるため、WebUI 側は代理池 → 直結の順にフォールバックします）。
- **ブラウザ側が facebook.com に到達できないと、承認 URL は何度出し直しても必ず失敗します（プロキシ認証の落とし穴）**: FoxyProxy 等で住宅プロキシを張っている場合、そのプロキシにユーザー名/パスワードが入っていない（または**間違っている**）と Chromium はリクエストを投げっぱなしにできず `ERR_PROXY_AUTH_UNSUPPORTED` のエラーページを表示します。FoxyProxy の該当プロキシに SOAX のユーザー名/パスワードを入力するか、承認作業の間だけ FoxyProxy を Off にしてください。**Meta の承認自体に JP の出口は必須ではありません**（JP が必要なのは Muse API を叩く Python 側で、そちらは `proxy_pool` を使うのでブラウザの設定とは無関係です）。`127.0.0.1` も巻き込まれるため、FoxyProxy の除外に入れておくと `/link` が安定します。
  - **実測した原因（解決済み・再発時の手順）**: 症状は 20 件すべてが `pending` のまま、`/api/ccv/link/sessions` は URL を返し続け、サーバは `429`（未解決）を返し続ける、というものでした。真因は Muse 側ではなく**ブラウザのプロキシ設定**で、FoxyProxy v9.7 の「muse」プロファイルに保存されていた SOAX パスワードが `MaHMW6F161` と誤っており、正しい値 `MaMHw6F1G1` と一致していませんでした（＝SOAX が 407 を返し、Chromium が `ERR_PROXY_AUTH_UNSUPPORTED` を表示、Meta の承認画面が一度も描画されない）。
    - 確認手順: Edge のアドレスバーに `chrome://version` →「プロファイル パス」で、いま使っているウィンドウがどのプロファイルかを先に確定します（本機では「個人」＝ `…\Edge\User Data\Profile 1`。拡張の保存値 `…\Profile 1\Local Extension Settings\gcknhkkoolaabfmlnjonogaaifnjlfnp\000003.log` は追記型なので新旧の値が両方残ります＝新しい方が有効）。
    - 修正手順: 同プロファイルで `chrome-extension://gcknhkkoolaabfmlnjonogaaifnjlfnp/content/options.html` を開く →「プロキシー」タブ → 対象行を展開 → `パスワード` の 👁 を押して値を確認 → 正しい値を入れて `Save`。`テスト` ボタンでも疎通を確かめられます。
    - 検証: 同じウィンドウで `https://www.facebook.com/` が完全描画されれば解消です。エラーページが消えていれば、あとは Meta にログインするだけで承認に戻れます（下記の 302 の項参照）。
- **一括実行では 1 件ごとの run が「失敗」に見えます（正常）**: `ccv_mode=link` では、連携 URL を人間が `ccv_link_timeout`（既定 600 秒）以内に承認しない限り、run は `accept_tos` で失敗し `category=protocol_error` で終わります。アカウント自体は登録済みなので、自動増殖はこれを **「未活性で保留」**として数え、`registered_ok` と同格に `target_count` の到達判定・メール確保枠に含めます（判定は `registered` テーブルへの実在確認。`category` 文字列だけでは判定できません）。管理画面の「未活性で保留」カードがその件数です。
- **保留分はあとから解除できます**: `/link` の「対象」に保留アカウントが出るので、行ごとに「URL 出し直す」→ ブラウザで承認してください。承認後に `ccv_status` が `not_required` へ変われば `/api/registered/redeem` で招待コードを投入できます（＝登録フェーズと解除フェーズを分けるのが最も現実的です）。
- **`/api/registered/redeem` は設定した CCV 方式で解除します**: 以前は `auth_flow.redeem_registered_account` の既定値（`card`）で固定していたため、デビットカードが弾かれる環境では未解除アカウントの redeem が必ず失敗しました。現在は `db.get_ccv_options()` / `db.get_ccv_card()` の設定値と、`ccv_mode` / `ccv_link_timeout` / `ccv_link_account_type` のリクエスト上書きを使います。redeem 中に切った承認 URL は `ccv_link.MANAGER.track` 経由で `/link` に表示されるので、そのまま人間が踏めます。
  - 注意: 未承認のアカウントを含めて redeem すると、その場で連携を待つため 1 件あたり最大 `ccv_link_timeout`（既定 600 秒）ブロックします。**先に `/link` で全部承認してから redeem に掛ける**のが速いです（承認済みなら `ensure_ccv` は走らず即進みます）。
- **同じメールで run を再試行しても、古い検証コードは拾いません**: 前回の run が使った検証メールは受信箱に残り続けます。Graph の `receivedDateTime` と本機の時計には**数十秒のずれ（実測 −25〜−37 秒。つまり必ず過去側にずれている）**があり、時刻だけで新旧を判定できないため、provider 側で「消費済みメール id」をメールボックス単位で記憶し（TTL 1 時間、`data/otp_consumed.json` に永続化）、再試行時はそのメールを飛ばして今回の `send_otp` が起こした新着を待ちます。`confirm_otp` が `invalidCode` で失敗した場合は番号を焼かず `available` に戻すので、そのまま次の run で再試行されます。
- **`confirm_otp` が `invalidCode` を返したら、コードを送り直します（多重防御）**: 消費済みメール id の記憶だけでは、トークン更新後に provider が古い `messages` キャッシュを使い回すケースを塞ぎきれません（古いメールを再び掴んで 400 `invalidCode` になる）。muse.ai の UI 自身と同じ挙動にして、`send_otp → 検証コード待ち → confirm_otp` を `otp_resend_attempts`（既定 3 回）までループします。送り直せば新着メールは必ず未消費・未 `seen` なので、古いメールには負けません。`invalidCode` 以外のエラーはそのまま投げます。さらに `auto_loop` 側でも `confirm_otp` の `server_rejected` を `completion` と同様に「セッションを作り直して再試行（最大 2 回）」に寄せてあり、停批保護でバッチ全体が止まりません（`stop_on_reject` も既定の `true` のままだと 1 件の失敗で一時停止するので、無人で流すなら `false` 推奨）。

- **Meta アカウントが既にアカウントセンター（AC）へ追加済みだと、連携は `add_accounts` で必ず止まります（未解決・最重要）**: 承認の最後は必ず `accountscenter.meta.com/add_accounts/?auth_flow=…&flow=…&token=…&blob=…` に着地しますが、そのプロフィールが既に AC に入っていると画面は「**アカウントは追加済みです / 〈名前〉はすでにこのアカウントセンターに追加されています。**」になり、操作できるのは `キャンセル` と `他のアカウントを追加する` だけ（「次へ」が無い）ので、Muse へ戻る `return_url` が得られず `linking-web-auth-completion` は 429 を返し続けます。FB・IG のどちらでも同じ着地になります。
  - 追加ボタンで別アカウントを足そうとしても、ダイアログの駆動元 `FXAccountsCenterLinkingFlowDialogRouteResolverQuery` が `field_exception` を 2 つ返して `fx_linking_routeable_dialog_flow` / `background` / `return_url` を**すべて null** にし、中身の無い空シェル（＝追加済みの文言）を描画します。例外の mid（`b62de6a0…` / `04b38cc6…`）はページを読み直しても**バイト単位で同一**＝一時的な物ではなくサーバ側で固定です。
  - AC 上でプロフィールを外す導線もありません（`/profiles` に削除ボタンが無い）。`flow=` / `entry_point=` / `used_native_auth=` の付け替えも無効（`flow` は `etoken` から作り直されます）。
  - 応急処置: route 傍受で `for (;;);` を剥がして JSON の `return_url`/`background` を差し込むとダイアログは「エラーが発生しました / もう一度Facebookアカウントにログインしてください」に化けますが、再ログインすると同じ所へ戻ります（`for (;;);` は 9 バイトで、`JSON.parse` 前に必ず剥がし、`route.fulfill` で戻します）。
  - 回避策: **AC に未登録の Meta アカウント**を用意するのが唯一確実です（1 件の Meta アカウントを 20 件で使い回す運用は、この「追加済み」で止まります）。
- **Instagram 経路は AC まで到達できますが、同意画面の「次へ」が反応しません**: `linking-options` は `instagram` も返し、`?account_type=instagram` で URL も出ます。実際に進めると `accounts/login/?force_authentication&next=…` のプロフィール行（「次へ」）は動き、`fxcal` の同意画面（「MetaがInstagramアカウントの情報にアクセスする必要があります。」）まで来ますが、そこで「次へ」が無反応です。
  - **唯一効く操作は「フォーカスして Enter」**: ボタンは `div[role=button] tabindex=0` で、`onClick` は**存在する**（`__reactProps$…onClick` が function）のに実マウスクリック・`.click()`・`force=True` のいずれも不発です。`el.focus()` → `keyboard.press("Enter")` でだけ `accountscenter.meta.com/add_accounts/…` へ進みます（同画面の「別のアカウントでログイン」はクリックでも動くので、画面全体が死んでいるわけではありません）。
  - 進んだ先は FB と同じ「追加済み」なので、IG に切り替えても上記の壁は越えられません。
- **AC 未登録の Meta プロフィールを作る＝新規 Instagram サインアップは、携帯 SMS で止まります（実測）**: 「1 件の Meta アカウントを使い回せない」以上、20 件ぶんの未登録プロフィールを作る必要があります。IG のサインアップは メール OTP → 画像キャプチャ → 携帯電話番号 の順で、**前 2 段は自動化できました**（メールは本リポジトリの Outlook provider、キャプチャは OCR）。残る SMS が壁です。
  - メール OTP 画面は `<input>` が 1 つも無く、素の `textarea` だけです（`document.querySelectorAll('input')` が空）。`querySelector('textarea')` に文字を入れ、`次へ` を focus + Enter で送ります。
  - 画像キャプチャは `ddddocr`（`beta=True` ＋画像を 3〜5 倍に拡大、複数条件の多数決）で突破できました。1 回の単発推論だと `79` を `E9` と誤読します。
  - **無料の日本番号は現状ほぼ枯渇**: `text-verification.net/country/JP` は 43 件の番号を出しますが、**表示されるメッセージが固定のサンプル**です（相対時刻が「5 分前」のまま進まず、`Enter 819020` のようなコードも全番号で同じ値）。実際に Instagram の SMS が届いた番号でも、そのコードは `コードが間違っています` で弾かれます。`receive-sms.cc` の Japan は 0 件、`receive-smss.com` / `quackr.io` / `temporary-phone-number.com` / `sms-online.co` に日本は置かれていません。`5sim` の Instagram×Japan は在庫 0 です。
  - `quackr.io` は番号を `+37062027***` のように伏せますが、**遷移先 URL に完全な番号が入ります**（`/temporary-numbers/<国>/<番号>`）。受信箱は「Waiting on incoming messages…」で自動更新される実物です。
  - **Instagram の SMS 上限は 1 日 1 回ぶんしかなく、しかも端末/IP 単位です**: `SMSコードの上限に達しました: リクエストできるSMSコードの上限に達しました。次のコードを受け取るには24時間待つ必要があります` が出ます。`www.instagram.com` のクッキーを全消しして新しいサインアップを開くと**上限表示の無い素のフォームに戻りますが、その新しいセッションで 1 回目を撃った瞬間にまた上限になります**（番号を変えても同じ）。つまり同一ブラウザ＋IP では **1 日 1 通**が限度で、上限後はコードが一切届きません（`receive-smss.com` の受信箱を実測）。
  - 携帯番号画面には**スキップ（あとで）がありません**。要素は `携帯電話番号を入力` / 国ピッカー / `コードを送信` の 3 つだけです。
  - **画像キャプチャは最初は空です**: 「人間であることを確認してください」画面には `次へ` だけがあり、`input`/`textarea`/`img` は 0 件（`約30秒で完了します` の文言のみ）。`次へ` を押すと `/accounts/suspended/` で本物のキャプチャ（`placeholder="画像内のコードを入力してください"` の `textarea` ＋ 画像）が出ます。以後は上記の OCR 手順がそのまま通ります。空の画面で待っても画像は出ません。
  - 国コードの切り替えは、`div[role=button][aria-label="JP +81"]` をクリックすると「国を選択」シート（全 200 国強の**素の `<div>` 行**、`role=option` は無い）が開きます。目的国の行の `span` を `scrollIntoView({block:'center'})` してから実座標クリックします（ピッカー自体を連打しても次の 1 国に進むだけで、一覧は開きません）。国を選んでも入力欄に `+1` が残るので、**国番号を抜いた国内番号で入れ直して**から送信します。
  - 20 件を回すなら、無料番号の枯渇を考えると**有料 SMS（`5sim` / `sms-activate` 等の日本番号、実勢 1 件 数十円）**が現実的です（日本在庫は 5sim が 0 なので sms-activate 側を確認）。
  - 現況: IG は 2 件ともメール認証とキャプチャを突破し、携帯 SMS 画面で上限に当たって 24 時間停止しています。無料番号では 1 日 1 件しか進まないため、20 件は事実上**有料 SMS が必須**です。
  - 代替案: 上限が IP 単位なら、`sms-activate` 等の有料番号＋待機で 1 日 1 件ずつ進めるほか、JP レジデンシャルプロキシの出口 IP を切り替えながら回す手があります（未検証）。
  - 参考（環境）: この環境では Playwright の `connect_over_cdp` が 60 秒で `TimeoutError` になる一方、`GET /json/version` の `webSocketDebuggerUrl` へ**生の WebSocket** で繋ぐと即座に通ります。`Target`/`Runtime.evaluate` を直に撃つ小さなヘルパで足ります（`Page.reload` の後はページ target の id が変わるので、URL から引き直してください）。

### カード経路（`ccv_mode=card` のとき）: **クレジット**カードが 1 枚必要（1 枚 = 10 アカウントまで）

実カードで通した結果、`461935…`（PayPay 銀行発行 Visa **デビット**）はサーバに**明示的に拒否**されました。
逆に `4549240653688285`（Visa **クレジット**）は **20 件のうち 10 件まで通し、11 件目から上限で止まりました**。

- 症状: `GET /api/hatch/ccv-status` が `{"ccv_status":"ccv_required"}` を返すアカウントは、`hasCompletedActivation` になる前に `/api/hatch/accept-tos` / `/api/hatch/stateful-sharing` / `/api/hatch/invite` / `/api/hatch/invite-code/redeem` が**すべて 403**（`POST /api/auth/check` は `hatchAdmissionInvalidated`）。→ 自分の招待コードが 1 つも出ない＝**裂変（増殖）が止まります**。
- **上限は「そのカード」の話で、未解除の全件で一貫して再現（実測・再確認済み）**: 11 件目以降の未解除アカウントに同じクレジットカードを撃ち直すと、必ず `code=2078073 step=write` で止まります（例外なし＝BIN 一時停止ではありません）。逆にデビットは 1 件目から `1383254` なので、**残りの解除は `ccv_mode=link`（カード不要・Meta 連携）か 2 枚目のクレジットカードが必須**です。ここで上限に当たっても `ccv-status` は `ccv_required` のまま変わらないため、失敗は繰り返し再現します。
- **デビット不可（実測）**: 実カードを入れても `POST /api/checkout/credential` が `code=1383254`「年齢確認のため、VisaまたはMastercardの**クレジットカード**を使用してください。**デビットカードは利用できません**。」を返します。BIN `461935` は PayPay 銀行発行の Visa CLASSIC **DEBIT** なので、この時点で通れません。→ クレジットカードを用意するか、`ccv_mode=link`（上記）に切り替えます。
- **1 枚あたりの上限は 10 アカウント（実測・重要）**: BIN `454924` のクレジットカードで 10 件目までは `success:true`、**11 件目から `code=2078073 step=write`「このカードが関連付けられている広告アカウントが多すぎます。別の支払い方法を選択して、もう一度実行してください。」**。20 件流すなら **2 枚目**（クレジット）が必要です。BIN や期限を変えても同じカードなら上限は共通で、時間を置いての回復も確認できていません。
- **与信（オーソリ）は撃たなくても解除できます（実測）**: 解除に必要なのは `POST /api/checkout/credential`（カード登録）までで、この時点で `ccv-status` は `not_required` に変わります。`POST /api/checkout/finalize` は 1.00 USD の LIVE 与信を作るだけなので、**`musepy/ccv.py` の `activate(..., finalize=False)` を使えばカード明細に載りません**（`auth_flow.ensure_ccv` は既定でこの与信なしパスを通り、それでも解除されないカードだけ finalize 付きで撃ち直します）。課金（実売上）はどのみち発生しません。
- 仕様: `POST /api/checkout/*` は `paymentContainerMode:"LIVE"` で、金額は常に **1.00 USD**（JPY 400 相当のダミー）。カードは Meta Pay 側で実在照会されるため、**汎用テスト PAN（`4242…` / `4111…`）は拒否**されます（`2078022` = BIN 単位の一時ブロック、`facebook.com/help/1434403039959381` へ誘導）。
- **診断のコツ（重要）**: `otcSessionId`/`otcType` を付けて撃つと、サーバは失敗理由を汎用コード `2078011` に**丸めて隠します**。理由を見るには otc を**3 呼び出し（init / ptt/encryption-key / checkout/credential）すべてから外した**状態で 1 周させる必要があります（credential だけ外すと `2078136` になります）。`musepy/ccv.py` は `2078011`/`2078132` のとき自動で otc なしの再走を挟み、両方の理由を 1 行の例外メッセージにまとめます。
  - **ただし `2078011` は「隠された理由」だけではありません**: otc を外した再走でも同じ `2078011` が返ることがあり、その場合は `2078011` 自体が**真の理由**です（「このカードは Meta 側で write できなかった」の汎用表現）。otc なしの再走が具体的コード（`1383254` / `2078022`）を返したなら丸められていた証拠、両方 `2078011` なら丸めではなく本物の拒否と判断します。
- 既知コード: `1383254`=クレジット必須／デビット不可、**`2078073`=カード 1 枚あたりの登録アカウント上限（10 件で到達）**、`2078022`=BIN 一時停止、`2078011`=汎用 write エラー（otc 併用で理由が丸められた場合と、カード自体が理由の場合の両方）、`2078136`=otc の片側欠落／不一致、`1353114`=平文送信、`2992007`=Luhn 不一致、`2078132`/`2078135`=PTT 破損。
- **X（Twitter）で拾った「1 ドルカード」30 枚は通らない（実測）**: 出回っている無料/激安カード番号（30 枚すべてが BIN `53179006` を共有、期限 `2031/05`〜`2031/06`）は、**8 アカウントすべてで一様に `code=2078011 step=write`** になります。otc あり・なしの両方で `2078011` なので、丸めではなくカード側の拒否です。30 枚が同一 BIN なので、BIN 単位で弾かれれば 30 枚全部が同時に死にます。
- **実測したカード別の応答（8 アカウントで完全に再現）**: `5317900654522646`→`2078011`、`4619352535263644`（PayPay デビット）→`1383254`、`4242424242424242`→`2078022`。加えて Luhn 不正の `1234567890123456`、非対応ブランドの `6011…`（Discover）/ `3782…`（Amex）も `2078011`。**コードはアカウントではなくカードで決まる**＝ write 段自体は健全です。
- **実ブラウザとの一致を確認済み（重要）**: 実際の Muse UI（Edge + CDP、同じアカウント・同じカード）にカードを打ち込んで送信しても、`musepy/ccv.py` と**完全に同一の** `POST /api/checkout/credential` 本文・同一の `{"code":"2078011","failureStep":"write"}` が返ります。フロントは失敗時に otc を付けたまま**同じ本文で 1 回だけ自動リトライ**します（こちらの実装は otc を外して撃ち直す点だけが違います）。したがって `2078011` はクライアント実装の不具合ではなく、カード/Meta 側の結果です。
  - この実測でフロントの `zip` は `""` でした。JP の `countryPostalPolicy.visibleFields` は `country` のみで郵便番号欄が無いためです。こちらは `1000001` を送りますが結果は同じなので、`zip` は判定に影響しません。
- **アカウント単位の例外も観測**: 1 アカウント（`axtellstorie64@outlook.com`）だけは実カードでも `1383254` ではなく `2078011` を返しました（他 8 アカウントは `1383254`）。そのアカウントは検証で何度も撃っているため、試行の積み重ねで一時的に汎用エラーへ落ちていると考えられます。**切り分けは「別アカウントで同じカードを撃つ」**が有効です。
- 対応: WebUI の「設定」→ メール設定 → `CCV: カード番号` / `有効期限 (MM/YY)` / `セキュリティコード` / `郵便番号` / `国コード` に入力します（`GET/POST /api/settings/ccv`、`db.CCV_FIELDS`）。解除処理は `auth_flow.py` が `accept-tos` の直前に `ensure_ccv` を自動実行します。
- 検証状態: `musepy/ccv.py`（checkout init → credential → finalize、`$e2ee` PTT 暗号化）は **BIN `454924` の実クレジットカードで 10 アカウントを `not_required` まで通し済み**（11 件目は `2078073` の上限）。`musepy/linking.py` は開始〜URL 取得〜ポーリングまで実機で疎通確認済み（`continue` 到達はブラウザ承認待ち）。
- 注意: 既定（`finalize=False`）では**与信は走らず、カード会社の明細に 1.00 USD は出ません**。`finalize=True` を選んだときだけ 1.00 USD の LIVE 与信が作られ、明細に仮計上されます（いずれも実課金ではありません）。
- 既に登録だけ済んでいるアカウントを掘り起こす `auth_flow.redeem_registered_account` も `no_charge=True`（既定）を `_ensure_ccv` に通すので、この経路でカードが使われても与信は走りません（`no_charge=False` で 1.00 USD 与信になります）。

### 実測サマリ（20 件運用の現在地）

- 20 件の登録自体は完了済み（`registered_unactivated`）。うち **12 件が解除済みで自分の招待コード（`own_code`）を保持**、うち何件かは既にプール経由の連鎖交換（親コード → 子コード）まで通っています。
- 残り **10 件がカード上限 `2078073` でブロック**中（`kolbyherms471` ほか）。2 枚目のクレジットカードを設定すれば同じ手順で通ります。
- 403 は**地理のせいではありません**: 住宅プロキシの出口は JP（`60.153.200.97` / 東京 / SoftBank・bbtec）で確定済みです。`ccv_required` とオンボーディング未完了の両方が 403 の原因になります。
- 起動時 `activate_session` が `region_waitlist` を投げるのは、`ccv_required` のアカウントが待機ページに居るためで、これも地理判定のメッセージではありません。`ccv_status` が `not_required` になった後は通ります。

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
