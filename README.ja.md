<div align="center">

# Openmd

**Everything to Markdown — just beginning.**

**見えているのに取り出せないコンテンツを、エージェントが使える Markdown に。**

[![PyPI](https://img.shields.io/pypi/v/openmd-cli?color=3b5bdb)](https://pypi.org/project/openmd-cli/)
[![Python](https://img.shields.io/pypi/pyversions/openmd-cli)](https://pypi.org/project/openmd-cli/)
[![CI](https://github.com/baigao417/openmd/actions/workflows/ci.yml/badge.svg)](https://github.com/baigao417/openmd/actions/workflows/ci.yml)
[![License: MIT](https://img.shields.io/badge/license-MIT-green)](https://github.com/baigao417/openmd/blob/main/LICENSE)

[English](https://github.com/baigao417/openmd/blob/main/README.md) · [简体中文](https://github.com/baigao417/openmd/blob/main/README.zh-CN.md) · 日本語 · [한국어](https://github.com/baigao417/openmd/blob/main/README.ko.md)

</div>

AI の時代、モデルはますます賢くなっています。それでも、モデルが読めるものは、あなたが何を渡せるかで決まります。

本当に価値のある情報の多くは、各プラットフォームの中に閉じ込められています。ログインが必要な記事、動画の中の発言、ポッドキャストの対談、AI と午後いっぱい続けた会話、グループチャットでの議論。見ることはできても、取り出すことはできません。

Openmd が目指すのは、ひとつの礎（いしずえ）です。こうしたコンテンツを、オープンで持ち運びができ、**人とエージェントの両方がそのまま読んで使える** Markdown に変えます。Web 記事、動画プラットフォーム、オフィス文書、チャットの会話。そのすべてが、あなたの専門的なナレッジベースの素材になります。

私たちはまず AI ネイティブな使い方を想定しています。原文は書き換えずに残し、出典と完全性を示すレシートを添え、エラーページやログインウォールははっきり拒否します。エージェントにとって「この資料は信頼できるか」は、「この資料があるか」と同じくらい重要だからです。

すべてを Markdown に。これはまだ始まりにすぎません。

---

## クイックスタート

Openmd はコマンドラインツールと Python モジュール `openmd` を提供します（パッケージ名は `openmd-cli`）。Python 3.10 以降が必要で、Windows・macOS・Linux で動作します。

```bash
# 基本パッケージをインストール（pipx install openmd-cli / uv tool install openmd-cli でも可）
pip install openmd-cli

# ローカル環境とオプション機能の状態を確認
openmd doctor

# リンクを貼り付けて Enter、保存先を選ぶ
openmd

# 1 つの Web ページを指定フォルダへ直接保存
openmd "https://example.com/article" -o ./notes

# エージェントやスクリプト向け：確認なしで実行し、機械可読な JSON レシートを出力
openmd "https://example.com/article" -o ./notes --json

# リンク一覧（1 行に 1 リンク）をまとめて処理。batch コマンドは --out で保存先を指定
openmd batch links.txt --out ./notes
```

各リンクは、選んだフォルダに `タイトル.md` として保存されます。実行ログは `~/.openmd/runs/` に書き込まれ、ノートには混ざりません。

ローカルのオフィス文書を処理したり、ローカルで音声・動画を文字起こししたりする場合は、必要に応じて追加の依存関係をインストールします。

```bash
# 文書と画像の解析
pip install "openmd-cli[document]"

# 画像とスキャン文書の OCR
pip install "openmd-cli[ocr]"

# オプションの Pandoc 変換フォールバック
pip install "openmd-cli[pandoc]"

# ローカル音声認識（ASR）による音声・動画の文字起こし
pip install "openmd-cli[asr]"

# 動画・音声ファイルの保存（Openmd 管理の yt-dlp）
pip install "openmd-cli[media]"
```

> 注：文書解析、OCR、ローカル ASR には、Python パッケージのほかに FFmpeg やローカルモデルが必要になる場合があります。準備状況は `openmd doctor` で確認できます。

Openmd を自分のエージェントや自動化ツールチェーンに組み込みたい場合は、[ツール連携ガイド](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)をご覧ください。

## エージェント向け MCP

シェルが使える場合は CLI + SKILL.md、ツール検出、型付き引数、クライアント側のツール別承認には MCP を使います。オプション extra をインストールして stdio サーバーを起動します。Claude Code：

```bash
pip install "openmd-cli[mcp]"
claude mcp add --transport stdio openmd -- openmd mcp
```

事前インストール不要の `uvx` 一行設定：

```bash
uvx --from "openmd-cli[mcp]" openmd mcp
```

Cursor：**Cursor Settings → MCP → Add new MCP Server** で同じ stdio サーバーを追加します：

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

Windows：実行ファイルは絶対パスを使い、必要なら UTF-8 出力を設定します：

```json
{
  "mcpServers": {
    "openmd": {"command": "C:\\path\\to\\openmd.exe", "args": ["mcp"], "env": {"PYTHONIOENCODING": "utf-8"}}
  }
}
```

Claude Desktop：MCP 設定に以下を追加します。必要に応じて実行ファイルを絶対パスにします。

```json
{
  "mcpServers": {
    "openmd": {"command": "openmd", "args": ["mcp"]}
  }
}
```

ツールは `openmd_save`、`openmd_save_page`、`openmd_job`、`openmd_doctor` の4つです。`openmd_save` は1件または最大20件の `sources` を受け取り、`openmd_job(action="cancel")` でジョブをキャンセルできます。`progressToken` を送るクライアントには進捗を通知し、ポーリングも利用できます。完了した Markdown は `openmd://jobs` と `openmd://job/{job_id}` resources で公開し、読み取り量を制限します。出力先は明示的な絶対パスが必要です。HTML は文字列または絶対ファイルパスで指定し、UTF-8で最大2 MiBです。5つの能力は初期状態では無効です。`network`、`browser`、`media`、`asr`、`cloud` を明示的に許可します。CLI と同じ能力フラグを使いますが、MCP は出力先から日常 CLI の自動許可を推定せず、保存先も記憶しません。stdio はローカルプロセスを起動できるクライアント用です。リモート connector には別途トランスポートブリッジが必要です。

---

## AI とナレッジベースの入口に向いている理由

Openmd 自体はナレッジベースでも RAG 検索システムでもありません。「情報を取り出し、整える」という土台の部分に集中しています。

1. **原文を残し、解釈は後工程に任せる**
   デフォルトでは原文・字幕・文字起こしを完全に残し、段落と空白だけを整えます。要約・翻訳・書き換えは自動では行いません。人は元の言葉を確かめられ、AI のコンテキストに入る前に重要な細部が失われることもありません。

2. **出典・メタデータ・タイムスタンプ付き**
   生成される Markdown には、タイトル、著者、元のリンクなどのメタデータを含められます（ソースが実際に提供する範囲で）。音声・動画の文字起こしにはタイムスタンプと、出典および完全性の説明が付きます。下流のツール向けに、オプションで構造化パッケージも出力できます。

3. **無効なページは保存せずに拒否**
   エラーページやログインウォールを認識すると、Openmd はそれを拒否してレシートで報告します。壊れたページを、普通に見えるノートとして黙って保存することはしません（Web ページの形はさまざまなため、すべての異常ページを見抜けるとは約束できません）。

---

## 現在できること

| カテゴリ | ソース | 状態 | 説明 |
| :--- | :--- | :--- | :--- |
| **Web とコミュニティ** | 一般的な Web 記事 | 利用可 | インストール後すぐに使えます |
| | WeChat 公式アカウント記事、X の投稿 | 利用可 | Openmd ブラウザ拡張機能と、あなたが既に持っているアクセス権が必要 |
| | 現在のページ保存、ログイン通知 | 利用可 | ブラウザ拡張機能が必要 |
| **音声・動画と文字起こし** | YouTube（字幕あり）、小宇宙（Xiaoyuzhou） | 利用可 | 小宇宙は初回利用時に約 20 MB の読み取りコンポーネントを自動で準備します |
| | bilibili、小鵝通（Xiaoe-tech） | 利用可 | ブラウザ拡張機能が必要。bilibili の AI 字幕はログインも必要 |
| | 字幕のない音声・動画、ローカルメディア | 追加依存が必要 | `[asr]` 依存、FFmpeg、ローカル音声モデルが必要 |
| **ローカル文書と画像** | PDF、DOCX、PPTX、XLSX、画像 | 追加依存が必要 | `[document]` 依存が必要。複雑なレイアウトやスキャン文書は追加の OCR ルートを通る場合があります |
| **業務ドキュメント** | Feishu / Lark ドキュメント | 一部対応 | ローカルに `lark-cli` をインストールしてログインし、コマンドに `--enable-network-backends` を付けてネットワーク読み取りを明示的に許可する必要があります |
| **AI チャット** | 豆包、Gemini、千問、元宝、Grok、DeepSeek（開いている会話ページ） | 一部対応 | ブラウザ拡張が必要：会話ページでクリックすると読み込み済みの内容を保存。エージェントは `--as-snapshot` で渡せます。現在表示中のブランチのみ。思考過程は既定で省略 |
| | ChatGPT の会話ページ；ChatGPT / Claude 公式エクスポート ZIP または JSON | 実験的 | ユーザーの質問が表示されない ChatGPT ページは partial として保存。公式エクスポートはブラウザなしでローカル取込 |
| **チャット履歴** | QQ グループのエクスポートファイル（JSON / HTML / TXT） | 一時停止中 | エクスポートファイルの解析は実装済みですが、安定機能としてはまだ公開していません。Openmd がチャットのデータベースを読み取ったり復号したりすることはありません |
| **実験的サポート** | 抖音（Douyin）、小紅書（Xiaohongshu） | 実験的 | ブラウザ拡張機能とログインが必要。これらのサイトは自動読み取りをよくブロックするため、安定性や網羅性は約束できません。小紅書のノートはブラウザで開いて拡張機能をクリック（読み込み済みページを読む）。抖音はメディアのダウンロードとローカル ASR を使います |

### ローカル文書形式

`[document]` はローカル文書解析の基礎依存です。次の表はクリーンインストールで実測し、ネイティブテキストの状態を partial も含めて示します。画像やスキャンには `[ocr]`、任意の変換フォールバックには `[pandoc]` を追加してください。詳しくは[形式チェーンと制限](https://github.com/baigao417/openmd/blob/main/docs/subsystems/document-formats.generated.md)をご覧ください。

| 形式 | `[document]` の状態 | オプション / 制限 |
|---|---|---|
| ネイティブテキスト PDF | 利用可 | スキャンはローカル OCR が必要。表の幾何は保証しません |
| DOCX | 一部対応 | テキストは取得できますが、すべてのサンプルで表形状を保証しません |
| PPTX、XLSX、XLS | 利用可 | 埋め込み画像、ノート、数式キャッシュ、バイナリ画像の制限を報告します |
| HTML、HTM、EPUB、RTF、ODT、ODS、ODP、FB2 | 利用可または一部対応 | ローカルファイル。スクリプトは実行せず、複雑なレイアウトは partial になる場合があります |
| CSV、TSV、JSON、XML、IPYNB、MSG | 利用可 | 大きな表は切り詰める場合があります。ノートブックは実行せず、MSG 添付は欠落として報告します |
| DOC、PPT | 部分的なテキストフォールバック | 推奨変換には LibreOffice が必要。レイアウトと旧式 PPT のアクティブなスライド順は保証しません |
| ZIP | エントリごとの状態付きで一部対応 | 混在メディアは別途 ASR / OCR が必要。アーカイブ資源上限があります |
| MOBI、AZW3 | Calibre が必要 | ツールがない場合は `engine_unavailable`。暗号化電子書籍は拒否し、復号しません |

右クリック変換は `openmd shell-menu install` で現在のユーザーにインストールします。対応文書の隣に `.md` を作成し、フォルダーはサブフォルダー構造を保った `<folder>-md` ミラーに変換します。再実行では変更されていない入力をスキップし、編集済み Markdown を保護します。同じ名前で形式が異なるファイルは、Markdown のファイル名に元の拡張子を残します。Windows ではメニューを追加する前に `uv tool install openmd-cli` または `pipx install openmd-cli` で永続インストールしてください。一時的な `uvx` キャッシュからのインストールは拒否されます。スキャン PDF にはローカル OCR が必要です。Windows 11 では従来型 HKCU メニューが「その他のオプションを表示」に置かれる場合があります。軽量インストーラーはパッケージアプリが必要な最上位メニュー拡張を登録しません。削除は `openmd shell-menu uninstall` です。

あなたのマシンで何が使えるかは `openmd doctor` で確認できます。リンクの処理に失敗すると、Openmd は理由と、[不具合報告](https://github.com/baigao417/openmd/blob/main/.github/ISSUE_TEMPLATE/link-failure.yml)にそのまま貼り付けられる 1 行を表示します。

---

## 出力の例

```markdown
---
title: "読書灯をどうやって作ったか"
author: "Example Studio"
url: "https://www.bilibili.com/video/BV..."
published: "2026-05-24"
platform: "bilibili"
---

# 読書灯をどうやって作ったか

**00:00:00**

今日は 3 か月かけて作ったこのランプと最初の 2 つのバージョンがなぜ失敗したのかをお見せします

**00:00:31**

最初の試作品は夜には明るすぎたのでシェードの下に 2 つ目の光源を加えました

*来源：自动字幕；识别错误保持原样。*
```

- 先頭は YAML front matter で、Obsidian や Logseq などのノートアプリやスクリプトからそのまま読めます。
- 文字起こしは約 30 秒、約 450 文字、または自然な間ごとに段落分けされ、各段落はタイムスタンプで始まります。人はざっと目を通せ、エージェントは特定の瞬間を引用できます。
- 最後の行には、テキストの出典（原文、プラットフォーム字幕、自動字幕、音声認識など）と、内容が完全かどうかが書かれます。このラベルは現在中国語のみです。

---

## ブラウザ拡張機能とローカル実行

- **既存のブラウザのアクセス権を再利用**：WeChat 公式アカウント、X、bilibili などの一部ルートは Openmd ブラウザ拡張機能を通じて動作し、一部のコンテンツはブラウザでのログインが必要です。拡張機能は現在 **Windows の Chrome / Edge** に対応しています。Openmd はあなたが正当に持っているアクセス権だけを使い、ログイン制限、ペイウォール、DRM、その他のアクセス制御の回避は約束しません。
- **ローカル優先とネットワークの境界**：Openmd はローカルで動作しますが、オンラインのコンテンツを読むときは必ず元のサイトにアクセスします。また、一部のコンポーネントは初回利用時にダウンロードされることがあります。クラウドサービスを使う機能は、あなたが明示的に有効にした場合にのみ動作します。

拡張機能のインストール：

1. `openmd browser-setup` を実行します。ローカル接続を登録し、拡張機能のフォルダを開きます。
2. `chrome://extensions`（または `edge://extensions`）を開き、**デベロッパー モード**をオンにして **パッケージ化されていない拡張機能を読み込む** をクリックし、そのフォルダを選びます。
3. 任意のページで Openmd アイコンをクリックすると保存できます。複数のタブを選択すればまとめて保存でき、テキストを選択して右クリックすると、その中のすべてのリンクを保存できます。

公開された `http(s)` ページでは、右クリックから「動画/音声の文字起こし付きで保存」または「動画/音声ファイル付きで保存」も選べます。既存の文字起こしルートでは二重に文字起こしせず、`file` モードでは対応するルートの公開・復号済みメディアを Markdown の隣に保存します。DRM、ペイウォール、暗号化メディアは保存しません。

[Releases](https://github.com/baigao417/openmd/releases) から拡張機能の ZIP をダウンロードし、展開したフォルダを `openmd browser-setup` の実行後に読み込むこともできます。

- **既定のブラウザ動作。** ポップアップを出さず、既存ウィンドウの非アクティブなバックグラウンドタブで読み取ります。Openmd が所有するタブは既定でミュートされ、処理終了時に自動で閉じます。

Chrome に「Openmd がこのブラウザのデバッグを開始しました」というブラウザの通知バーが表示されることがあります。この通知は拡張機能から隠せません。

---

## 管理対象 OpenCLI

OpenCLI は Openmd が管理するブラウザ・プラットフォームコンポーネントです。`openmd opencli list` と
`openmd opencli <site> <command> [args]` は読み取り専用コマンドを透過実行し、出力と終了コードを保持します。
書き込みと未知のコマンドは、明示的に `--allow-write` を付けない限り fail closed です。資格情報の内容は表示しません。

---

## 設定

| 環境変数 | 効果 |
|---|---|
| `OPENMD_AUTO_INSTALL_TOOLS=0` | ブラウザ読み取りコンポーネントを自動でダウンロードしない |
| `OPENMD_NPM_REGISTRY` | 初回ダウンロード時に使う npm ミラー |
| `OPENMD_SHOW_BROWSER=1` | トラブルシューティング用に、バックグラウンドのブラウザウィンドウを表示したままにする |
| `OPENMD_DATA_DIR` | Openmd が設定、ログ、ツールを保存する場所（デフォルトは `~/.openmd`） |

詳しくは：[ツール連携](https://github.com/baigao417/openmd/blob/main/docs/tool-integration.md)、[現在のルート](https://github.com/baigao417/openmd/blob/main/docs/routes.generated.md)、[アーキテクチャマップ](https://github.com/baigao417/openmd/blob/main/docs/architecture-map.html)。

---

## 今後の方向性

**Everything to Markdown** は長期的なビジョンです。以下はまだ提供していない、検討中の方向性です。

- **Web AI チャットのエクスポート**は、すでに開いている会話ページを読み取ります（拡張のクリック、またはエージェント自身のブラウザから `--as-snapshot`）。バックグラウンドでリンクを開き直すことはありません。現在は一部対応で、読み込み済みの表示中ブランチのみ保存します。ChatGPT / Claude の公式エクスポートは `openmd go export.zip -o archive --chatgpt-export` / `--claude-export` でオフライン取込できます（JSON と形式の自動判別にも対応）。Claude は `--claude-title`、`--claude-since`、`--claude-until` で絞り込み可能。非テキストブロックと添付はプレースホルダーと件数のみを保存し、再取込時はユーザーの編集を保護します。
- **電子書籍とその他のテキスト形式**：EPUB、RTF、MOBI、AZW3 などへの対応。
- **より幅広い文書解析**：複雑なレイアウトの文書からの抽出品質を継続的に改善します。

これらは実際のサンプルで検証されるまで、提供済みの機能とは見なしません。

---

## よくある質問

**Openmd はテキストを要約したり整えたりしますか？**
いいえ。空白と段落の区切りだけを調整します。要約、翻訳、推敲は、次に使うツールに任せます。

**データはどこに送られますか？**
どこにも送られません。Openmd はローカルで動作し、あなた自身のネットワークとブラウザでページを読み、選んだフォルダにファイルを書き込みます。Cookie やパスワードを保存することはありません。

**なぜパッケージ名が `openmd-cli` なのですか？**
PyPI の `openmd` という名前は、無関係のプロジェクトが使っています。インストール後のコマンドは `openmd` のままです。

**リンクがうまく処理できません。**
`--verbose` を付けてもう一度実行すると、どの読み取りルートを試したかがわかります。そのうえで、Openmd が表示した 1 行を添えて [リンク失敗の報告](https://github.com/baigao417/openmd/issues/new?template=link-failure.yml)を送ってください。失敗した実際のリンクは、何より役に立つ貢献です。匿名化問題パッケージを有効にすると、`openmd report feedback <ジョブ ID> -o <ディレクトリ>` で問題パッケージを作成できます。リンクルートの失敗は有界な `manifest.json` と `receipt.json` のみ、捕捉済みページルートは後から手動報告できる匿名化 `structure.html`、`doctor.json`、`README.txt` を含みます。拡張機能ポップアップの最近のタスク一覧にも「結果不对？生成问题报告」を表示します。添付前に確認してください。元の本文、cookie、アカウント情報、署名付き URL パラメータは含まれません。

---

## コントリビュート

実際のリンクを添えた不具合報告、新しいプラットフォームの追加、修正を歓迎します。多くの新プラットフォームは、小さな JSON の「ツールカード」だけで追加できます。詳しくは [プラットフォームの追加](https://github.com/baigao417/openmd/blob/main/docs/adding-a-platform.md)をご覧ください。開発手順は Windows・macOS・Linux で共通です。

```bash
git clone https://github.com/baigao417/openmd && cd openmd
python scripts/dev_setup.py     # .venv を作成し、開発用依存をインストールし、ffmpeg / Node を確認
python -m pytest
```

すべての修正には、修正前に失敗していたテストを付けてください。「動くようになった」という主張には、実際に実行した本物のリンクを付けてください。Pull Request の前に [CONTRIBUTING](https://github.com/baigao417/openmd/blob/main/CONTRIBUTING.md) と[コード規約](https://github.com/baigao417/openmd/blob/main/docs/code-standards.md)をお読みください。

---

## 謝辞とライセンス

Openmd は [OpenCLI](https://github.com/jackwener/opencli)（ブラウザ読み取り、同梱）、[trafilatura](https://github.com/adbar/trafilatura)、[youtube-transcript-api](https://github.com/jdepoix/youtube-transcript-api)、Bilibili-Harvest（Lanbin07）、[faster-whisper](https://github.com/SYSTRAN/faster-whisper)、[MarkItDown](https://github.com/microsoft/markitdown) などのオープンソースプロジェクトの上に成り立っています。詳細とライセンスは [THIRD_PARTY_NOTICES](https://github.com/baigao417/openmd/blob/main/THIRD_PARTY_NOTICES.md) をご覧ください。

コミュニティ：[LINUX DO](https://linux.do) コミュニティの交流とフィードバックに感謝します。

本プロジェクトは [MIT](https://github.com/baigao417/openmd/blob/main/LICENSE) ライセンスで公開されています。保存するコンテンツの著作権を尊重してください。Openmd は個人のアーカイブと研究のためのツールです。

**Everything to Markdown — just beginning.**
