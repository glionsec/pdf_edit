# pdf_edit

PDF のパスワード解除・分割・結合・圧縮・透かし除去を行うスクリプト集。

# unlock

PDF のパスワードを解除し、`<元ファイル名>_decrypted.pdf` に保存する。
引数を省略するとカレントディレクトリの PDF をすべて処理し、パスワードは暗号化された PDF のときだけ尋ねる。

```
uv run python unlock.py                    # カレントディレクトリの PDF をすべて処理
uv run python unlock.py --password secret  # パスワードを引数で指定
uv run python unlock.py --force            # 既存の出力を確認せず上書き
```

Remove the PDF password.

# split

PDF と分割位置（ページ番号をカンマ区切り）を指定し、入力ファイルと同じディレクトリに
`<元ファイル名>_part1.pdf` のような連番ファイルを出力する。
`2` を指定すると「2 ページ目までが part1、3 ページ目以降が part2」になる。

```
uv run python split.py input.pdf 2
uv run python split.py input.pdf 2,5 --password secret
```

Split a PDF at specified pages.

# merge

フォルダ内の PDF をファイル名の自然順（数字は数値として比較）で 1 つの PDF に結合する。

```
uv run python merge.py <folder> merged.pdf
uv run python merge.py <folder> merged.pdf --password secret
```

Merge PDFs in a folder.

# compression

ghostscript で PDF を圧縮する。品質は `/screen`、`/ebook`、`/printer`、`/prepress`、`/default` から指定する。

```
uv run python compression.py input.pdf output.pdf /ebook
```

ghostscript は uv の管理外。ホストに 10 系があるならそのまま実行できる。
無い場合は Docker で固定したものを使う（ghostscript 10.02.1 / python3 3.12.3 を固定。更新時は Dockerfile のバージョンを上げる）。

```
docker build -t pdf-edit-compress .
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/data" pdf-edit-compress input.pdf output.pdf /ebook
```

Compress a PDF with ghostscript.

# strip

ページ直下に描かれたテキスト（透かし・ヘッダー・フッター・ライセンス表記）を除去する。
本文が Form XObject の内側にあり、ページの content stream に重ね文字だけが描かれている PDF 向け
（SANS 教材など）。テキスト描画命令（`Tj` / `TJ` / `'` / `"`）だけを落とし、
画像・図形・本文はそのまま残す。

標準で次のメタデータも削除する（`--keep-metadata` で無効化できる）。

- 文書情報辞書（`/Info`）の全項目（Author / Title / Copyright / Hash / Hash2 など）
- ファイル識別子（`/ID`）を新規生成（元ファイルとの紐付けを切る）
- 各オブジェクトの `/PieceInfo` / `/LastModified` / `/Metadata`

```
uv run python strip.py input.pdf                 # -> input_stripped.pdf（メタデータも削除）
uv run python strip.py input.pdf output.pdf
uv run python strip.py input.pdf --keep-metadata # テキストだけ消してメタデータは残す
uv run python strip.py input.pdf --dry-run       # 対象ページ数・命令数の確認のみ
```

本文のテキストがページ直下に描かれている PDF では本文も消える。適用前に `--dry-run` で確認すること。

Remove overlay text (watermark / header / footer) drawn directly on the page.

# summarize

PDF の内容を章単位で要約した **単一 HTML（図解付き）** を作る。要約文の生成は LLM/エージェントが行い、
スクリプトは「章検出・本文抽出」「指示文の生成」「書式検証」「HTML 結合」を担当する。
出力は外部 CSS・画像・スクリプトを参照しない（図解はインライン SVG）。

```
uv run python summarize.py extract "work/book.pdf"                 # 章検出 + 本文抽出
uv run python summarize.py figures "work/book.pdf" work/summary/<PDF名>/   # 図版を抽出
uv run python summarize.py prompt work/summary/<PDF名>/            # LLM への指示文 PROMPT.md
uv run python summarize.py check  work/summary/<PDF名>/            # 書式・安全性の検証
uv run python summarize.py assemble work/summary/<PDF名>/          # -> <PDF名>_summary.html
```

章検出は次の順に試し、`manifest.json` に方式と確信度を記録する。

1. `--chapters` の手動指定（ページ範囲の配列）
2. 目次 + `/PageLabels`（印刷ページ番号をラベルで解決）
3. 目次 + オフセット自動推定（ページ端の番号表記から。SANS Books はこれ）
4. しおり（既定は上位 30 件まで。`--profile sans` は Exercise のみ）
5. 番号付き見出し（`1. Introduction` など）
6. 10 ページ固定（警告を出してフォールバック）

主なオプション: `--chapters chapters.json` / `--toc-pages 1,2` / `--offset N` / `--profile sans` /
`--dry-run` / `--force`（cwd・ホーム・git リポジトリ・PDF を含むディレクトリは削除しない）。

```
work/summary/<PDF名>/
├── manifest.json     # ページ数・方式・確信度・章一覧（範囲・文字数・前付けフラグ）
├── PROMPT.md         # LLM への指示文（章一覧と出力先つき）
├── chapters/         # 章ごとの本文テキスト
├── summaries/        # 章ごとの要約 HTML 断片（NN.html。LLM/エージェントが作成）
├── diagrams/         # インライン SVG 図解（overview-*.svg, ch-NN.svg）
├── figures/          # PDF から抽出した図版（JPEG/PNG）
├── figures.json      # 図版一覧（章・ページ・サイズ・形式）
└── <名前>_summary.html
```

- `check` は許可タグ・`p.lead`・`li` 数・可視文字数・タグの対応・外部参照・イベント属性・
  SVG の危険要素（script / image / on* / 外部 URL）を検証する
- `assemble` は検証を通らない断片を拒否し、生成した HTML に対しても script / on* / 外部参照を再検査する
- テキストを抽出できない PDF（スキャン画像）はエラーになる。OCR 済みの PDF か `--chapters` を使う
- `figures` は Form XObject の中も含めて画像を探し、サイズ・縦横比・テンプレート部品（同寸法の多用）・
  同一画像の使い回しで装飾を除外する。章あたり 2 枚・合計 5MB を上限に選抜（`--per-chapter` / `--max-total-mb`）
- JPEG はそのまま、FlateDecode の RGB/グレースケールは PNG に変換して抽出する
- `assemble` は既定で図版を**埋め込まない**（`--embed-figures` 指定時のみ base64 の data URI で埋め込む）
- 図の内容を要約に反映する場合は、図ごとの説明を `figures/descriptions.json`
  （`{"figures/ch-03-p036-03.jpg": "…"}`）に置く。`prompt` が章ごとの図版説明として指示文に含める
- この説明を作るには画像を理解できるモデル（vision）か OCR が必要。本文テキストからのみでは図の内容は分からない
- 要約文は LLM 生成のため非決定的。抽出・検証・結合は決定的でテスト対象

# 実 PDF の作業用ディレクトリ

実データの PDF は `work/` に置く（`.gitkeep` 以外は gitignore 済みでコミットされない）。
`work/` の中に好きなサブディレクトリを作ってよい。

```
mkdir -p work/in work/out
cp ~/Downloads/sample.pdf work/in/
uv run python unlock.py work/in/sample.pdf --password PW --force
uv run python merge.py work/in work/out/merged.pdf
uv run python split.py work/in/sample.pdf 2
docker run --rm --user "$(id -u):$(id -g)" -v "$PWD:/data" pdf-edit-compress work/in/sample.pdf work/out/sample.pdf /ebook
```

`work/` をカレントにして実行する場合は、プロジェクトを明示する。

```
cd work
uv run --project .. python ../unlock.py --password PW --force   # work/ 直下の PDF を処理
```

# development

```
uv sync --locked
uv run pytest
```

依存関係と Python のバージョンは [uv](https://docs.astral.sh/uv/) で固定している
（`3.12.14` は `.python-version`、依存は `pyproject.toml` と `uv.lock`。取得元は公式 PyPI に固定）。
スクリプトは引数を省略すると従来どおり対話式で入力を尋ねる。

# notes

- `merge.py` / `split.py` は暗号化 PDF を自動では復号しない。`--password` を渡すか、先に `unlock.py` で解除する。
- AES-128 / AES-256 の暗号化 PDF に対応（`pypdf[crypto]` を導入済み）。
- `strip.py` はページ直下のテキストだけを対象にする。Form XObject 内の本文や画像は消さない。
- `strip.py` は標準でメタデータ（`/Info`、`/ID`、`/PieceInfo` など）も削除する。
