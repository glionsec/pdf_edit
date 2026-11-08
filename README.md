# pdf_edit

PDF のパスワード解除・分割・結合・圧縮を行う対話式スクリプト集。

# unlock

カレントディレクトリ内の PDF をすべて対象に、パスワードを 1 ファイルずつ尋ねて解除し、
`<元ファイル名>_ja.pdf` に保存する。

Remove the PDF password.

# split

PDF と分割位置（ページ番号をカンマ区切り）を尋ね、入力ファイルと同じディレクトリに
`<元ファイル名>_part1.pdf` のような連番ファイルを出力する。
`2` を指定すると「2 ページ目までが part1、3 ページ目以降が part2」になる。

Split a PDF at specified pages.

# merge

フォルダと出力先を尋ね、そのフォルダ内の PDF をファイル名の文字列順に 1 つの PDF へ結合する。

Merge PDFs in a folder.

# compression

入力 PDF・出力先・品質（`/screen`、`/ebook`、`/printer`、`/prepress`、`/default`）を尋ね、
ghostscript で圧縮する。

Compress a PDF with ghostscript.

# preparation

依存関係と Python のバージョンは [uv](https://docs.astral.sh/uv/) で固定している
（`3.12.14` は `.python-version`、依存は `pyproject.toml` と `uv.lock`。取得元は公式 PyPI に固定）。

```
uv sync --locked
```

スクリプトはすべて対話式で引数は取らない。uv 経由で実行する。

```
uv run python merge.py
```

`compression.py` だけは ghostscript が必要で、uv の管理対象外（動作確認は 10.02.1）。

```
sudo apt install ghostscript
```

# notes

- 暗号化された PDF は `unlock.py` で先に解除する（`merge.py` / `split.py` は復号しないため、そのまま渡すとエラーになる）。
- AES-256 で暗号化された PDF は現行の PyPDF2 では復号できない（`PyCryptodome` が必要）。pypdf への移行で解消する予定。
