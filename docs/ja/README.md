# 日本語ドキュメント索引 (docs/ja)

`muse-JP-Reg` の日本語ドキュメント一式です。プロジェクト全体の概要は先に
[README.ja.md](../../README.ja.md) を読んでください。このディレクトリは実務向けの詳細編です。

## 読む順番

1. [requirements.md](requirements.md) — **最初にこれ**。実際に必要なもの／不要なもの（住宅プロキシは必須ではない）、メール受信手段の比較、プロキシ無しでの運用手順。
2. [architecture.md](architecture.md) — 全体構成、スレッドモデル、SSE、スティッキープロキシ、DB スキーマ、設定キー一覧。
3. [registration-flow.md](registration-flow.md) — 登録プロトコル 10 フェーズの詳細（エンドポイント、成果物、失敗カテゴリ）。
4. [invite-farming.md](invite-farming.md) — 招待コードの自己増殖（裂変）の仕組みと上限挙動。
5. [known-issues.md](known-issues.md) — 現状の課題・粗さの一覧（根拠となる `file:line` 付き）と次の一手。

## この一式の前提

- 対象リビジョン: コミット `df27ca5` 時点のソース。
- 本ドキュメントは**コードを読んで書いた静的な解説**です。実行検証は別途 [requirements.md](requirements.md) の手順で行ってください。
- コード内のコメント・UI 文言・ログは中国語、本ドキュメントは日本語です。ログをそのまま引用する箇所は原文（中国語）を併記します。

## 関連リンク

- [README.ja.md](../../README.ja.md) — 日本語の全体概要と最短セットアップ
- [README.md](../../README.md) — 原典（中国語）
