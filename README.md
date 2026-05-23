# macOS editor benchmark plan

Zed と VS Code を対象に、記事で挙げられている項目のうちローカルで再現しやすいものを一通り実行します。単にファイルを開くだけでなく、Swift 製の `tools/mac_gui_probe.swift` で macOS Accessibility / CoreGraphics 経由の実ウィンドウ検出、入力、保存を行います。`osascript` はレガシーのフォールバックとして残しています。

## 自動化する項目

| Article benchmark | Local case | Notes |
|---|---|---|
| Open 100K-line JS file | `open_100k_js` | 100,000 行の JS fixture を開き、対象ファイル名がウィンドウタイトルに出るまでを測る。 |
| Open + scroll 100K-line JS file | `scroll_100k_js` | 100,000 行 JS を開き、Page Down を複数回送る。描画負荷時の操作感に近い測定。 |
| Editable after opening small file | `edit_ready_small` | 一時ファイルを開き、実 GUI 入力→Command-S→ディスク反映までを測る。 |
| Editable after opening 100K-line JS file | `edit_ready_100k_js` | 100,000 行 JS の一時コピーを開き、実 GUI 入力→保存→ディスク反映までを測る。 |
| Project search / indexing proxy | `search_large_project` | 大きめのプロジェクトを開き、`Command-Shift-F` で検索語を投入する。インデックス・検索応答の代理測定。 |
| Idle memory folder open | `memory_folder` | 起動後に一定時間待ち、対象プロセス群の RSS を合算。 |
| Idle memory 10 files open | `memory_10_files` | 10 個の小さな JS ファイルを同時に開く。 |
| Memory with large monorepo | `memory_large_project` | 既定では `../zed`、または `--large-project` で指定。 |

## 自動化しない／別計測にする項目

`open_100k_js` は、対象ファイル名がウィンドウタイトルに出るまでを完了条件にします。単に VS Code や Zed の空ウィンドウが出ただけでは完了しません。実際に編集可能になった時点を見たい場合は `edit_ready_small` / `edit_ready_100k_js` を使ってください。これらは `--gui-driver ax` では AppleScript ではなく Accessibility / CoreGraphics 経由で実際に入力し、`Command-S` 後にファイル内容へマーカー文字列が反映されるまでを測ります。

`startup_clean` / `startup_folder` のようなプロセス生成だけに近い測定は、実編集可能性や描画完了を反映しにくいため削除しました。起動に関する値は `open_100k_js` や `edit_ready_*` のように、対象ファイルを開いた後の確認まで含むケースを使います。

以下は単純な CLI 起動時間や RSS では正確に測りにくいため、このハーネスでは直接の数値化を避けます。

- `Input latency`
  - 正確には OS イベント投入から描画完了までを見る必要があります。
  - Instruments、Quartz Debug、ScreenCaptureKit などを使った別計測に分けるのが安全です。
- `Autocomplete latency`
  - LSP、拡張機能、AI 補完、ネットワーク状態の影響が大きいです。
  - 言語・プロジェクト・補完トリガ・拡張構成を固定した専用ベンチが必要です。
- `Memory with AI active`
  - API キー、モデル、ネットワーク、拡張機能、チャット履歴で条件が変わります。
  - AI 機能を有効化する場合は、別プロファイルとして測定条件を明記してください。
- `Large codebase indexing`
  - “indexing 完了”の定義がエディタごとに異なります。
  - このハーネスでは `search_large_project` を代理測定として用意していますが、検索完了そのものをUIから厳密に検出しているわけではありません。

## 初回準備

```sh
python3 bench/bench.py prepare
```

これで以下が生成されます。

- `bench/fixtures/sample_project/`
- `bench/fixtures/ten_files/`
- `bench/fixtures/large_100k_lines.js`

## 実行

```sh
python3 bench/bench.py run --iterations 5 --readiness auto --large-project zed
```

このデフォルト実行には、GUI自動操作を伴う測定も含まれます。初回実行時に `swiftc` で `tmp/mac_gui_probe` をビルドし、アクセシビリティ許可済みの環境ではウィンドウ検出、入力、保存まで実行します。

GUI 操作を伴う case では、起動後・操作前にエディタのウィンドウを既定で `80,80,1400x900` に揃えます。スクロール、検索、編集の比較では表示領域差が結果に混ざるため、この固定サイズを使ってください。変更する場合は `--window-x` / `--window-y` / `--window-width` / `--window-height` を指定します。固定しない確認だけをしたい場合は `--no-fix-window-geometry` を使います。

デフォルト実行ケースは以下です。

```txt
open_100k_js
edit_ready_small
edit_ready_100k_js
scroll_100k_js
search_large_project
memory_folder
memory_10_files
memory_large_project
```

結果は `bench/results/` に出ます。

- `bench-YYYYMMDD-HHMMSS.json`
- `bench-YYYYMMDD-HHMMSS.csv`
- `bench-YYYYMMDD-HHMMSS.md`

GUI操作なしで短く試す場合：

```sh
python3 bench/bench.py run --iterations 1 --cases memory_folder --readiness process --no-fix-window-geometry
```

実編集だけを短く確認する場合：

```sh
python3 bench/bench.py run --iterations 1 --cases edit_ready_small edit_ready_100k_js --readiness window_title
```

スクロールや検索操作も含めて試す場合：

```sh
python3 bench/bench.py run --iterations 1 --cases scroll_100k_js search_large_project --readiness auto --large-project zed
```

## エディタ設定

既定設定は `bench/editors.example.json` です。手元のアプリ名やプロセス名が違う場合はコピーして編集します。

```sh
cp bench/editors.example.json bench/editors.local.json
```

`editors.local.json` は `.gitignore` 済みです。

設定例：

```json
{
  "label": "editor_a",
  "enabled": true,
  "app_name": "Zed",
  "bundle_id": "dev.zed.Zed",
  "process_name": "Zed",
  "process_regex": "Zed\\.app|/Zed(?: |$)|\\bZed\\b",
  "extra_args": []
}
```

`command` を使うと CLI から起動できます。

```json
{
  "label": "editor_b",
  "enabled": true,
  "process_name": "Code",
  "process_regex": "Visual Studio Code\\.app|/Code Helper|\\bCode\\b",
  "target_arg_mode": "app_args",
  "command": ["code"],
  "extra_args": ["--disable-extensions", "--new-window"]
}
```

`target_arg_mode` は、対象ファイルやフォルダを `open` の document として渡すか、アプリ引数として渡すかを指定します。VS Code のように `--new-window` と対象パスを同じ CLI 引数列で扱うアプリでは `app_args` を使います。これを指定しないと、空の新規ウィンドウに入力して Save As ダイアログが出ることがあります。

## readiness の意味

`--readiness` は起動完了判定の方法です。

- `process`: 対象プロセスが見えた時点
- `window`: 対象プロセスの window 数が 1 以上になった時点
- `window_title`: window title に対象ファイル名が出た時点
- `auto`: `window_title` / `window` を試し、不可なら `process` にフォールバック

`--gui-driver ax` がデフォルトです。このモードでは Swift ヘルパーが Accessibility で対象ウィンドウを観測し、CoreGraphics でクリック、ペースト、保存キーを送ります。`--gui-driver osascript` を指定すると従来の `System Events` 経由に戻せます。

`window` / `window_title` と `edit_ready_*` は macOS のアクセシビリティ権限が必要です。権限ダイアログが出たら Terminal / 実行元のシェル / `tmp/mac_gui_probe` のいずれか表示された対象に許可を与えてください。許可がない場合、`auto` readiness は `process` にフォールバックできますが、`edit_ready_*` は実入力を検証できないため失敗します。

固定ウィンドウサイズは `--gui-driver ax` のときだけ有効です。結果 JSON / CSV には `window_geometry_seconds` と、実際に設定後に観測された `window_geometry` が出ます。エディタや macOS が指定サイズを微調整することがあるため、比較前にこの値が Zed / VS Code で同じか確認してください。

`edit_ready_*` では、入力前に前面ウィンドウ中央をクリックし、クリップボード経由でマーカー文字列をペーストしてから `Command-S` します。VS Codeなどで初回表示直後に入力が取りこぼされる場合は、`--edit-delay 2.0` のように待ち時間を伸ばしてください。

`edit_ready_*` の主指標 `editable_saved_seconds` は、ファイル起動からマーカー文字列がディスクに保存されるまでの総時間です。結果 JSON / CSV には補助列として `input_started_seconds` も出力します。これはファイル起動から GUI 入力イベントを投げた時点までの時間で、保存反映で成功確認された iteration の「入力開始可能」の代理指標として使えます。

## 測定時の推奨条件

- AC 電源接続
- 低電力モード off
- 可能なら外部ディスプレイ構成を固定
- Spotlight indexing、Time Machine、クラウド同期などを避ける
- 測定前に対象エディタをすべて終了
- 同じ順序で複数回実行し、中央値を使う
- VS Code 系は拡張有効／無効で結果が大きく変わるため、設定を明記する

## 原稿での使い方

本文では、次のように測定条件を明記すると安全です。

```txt
測定は同一のmacOSマシン上で実施した。各エディタを未起動状態に戻してから起動し、
100,000行のJavaScriptファイル、10ファイル同時オープン、小規模プロジェクト、
Zedリポジトリ相当の大型プロジェクトを対象に、起動時間とRSSメモリ使用量を記録した。
編集可能になるまでの時間は、一時ファイルを開いた後にmacOS Accessibility / CoreGraphics経由で文字入力と保存を行い、
入力したマーカー文字列がディスク上のファイルに反映されるまでとして測定した。
大きなファイルの操作性については、100,000行ファイルを開いた後にAppleScriptでPage Downを複数回送り、
プロジェクト検索についてはCommand-Shift-Fで検索語を投入する操作を測定した。
比較対象は匿名化し、エディタA/B/C/D/Eとして示す。
```

## 公開時の注意

GitHub に公開する場合、生成物は含めないでください。`.gitignore` では以下を除外しています。

- `fixtures/`: `prepare` で再生成できるベンチ用 fixture
- `results/`: 実行結果。絶対パス、マシン構成、macOSビルド番号などのローカル情報を含みます
- `tmp/`: 一時ファイル、Swiftビルド成果物、モジュールキャッシュ
- `editors.local.json`: 手元環境向けのエディタ設定

公開対象は基本的に以下だけで十分です。

```txt
README.md
bench.py
editors.example.json
run_local.sh
tools/mac_gui_probe.swift
.gitignore
```

## 注意

このハーネスは、記事に掲載された数値の真偽を検証するためのものではなく、手元の macOS で同じ種類の測定を再現するためのものです。`startup` は「アプリプロセス／ウィンドウが確認できるまで」であり、すべての拡張、LSP、AI、インデックスが完了した時刻とは限りません。編集可能性まで含めたい場合は `edit_ready_*` の結果を優先してください。
