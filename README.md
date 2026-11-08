# unlock

PDFのパスワードを解除する。

Remove the PDF password.

# split

PDFを指定のページで分割する。

Split a PDF at a specified page.

# compression

PDFを圧縮する。

Compress PDFs.

# preparation

依存関係と Python のバージョンは [uv](https://docs.astral.sh/uv/) で固定している
（`3.12.14` は `.python-version`、依存は `pyproject.toml` と `uv.lock`。取得元は公式 PyPI に固定）。

```
uv sync --locked
```

実行は uv 経由で行う。

```
uv run python merge.py
```

`compression.py` だけは ghostscript（10 系）が必要で、uv の管理対象外。
