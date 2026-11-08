"""PDF の要約版（単一 HTML）を作るための抽出・結合ツール。

2 つのサブコマンドを持つ:

- extract: 目次やアウトラインから章を検出し、章ごとの本文テキストと manifest.json を書き出す
- assemble: 章ごとの要約 HTML 断片（と任意の SVG 図解）を 1 つの自己完結 HTML にまとめる

要約文そのものの生成は行わない（LLM/エージェント側で `summaries/NN.html` を作る）。
出力 HTML は外部 CSS・画像・スクリプトを参照しない（図解はインライン SVG）。
"""
import argparse
import json
import os
import re
import sys
from datetime import datetime, timezone

from pypdf import PdfReader

HEADER_PATTERN = re.compile(r"^\s*SEC542\s*\|")
FOOTER_PATTERNS = (
    re.compile(r"^\s*©"),
    re.compile(r"^\s*and/or SANS Institute"),
    re.compile(r"^\s*\d{1,3}\s*$"),
)
TOC_LINE_PATTERN = re.compile(r"^\s*(\d{1,4})\s*([A-Za-z(].+)$")
SLIDE_OFFSET = 2  # 目次のスライド番号 + 2 = PDF ページ（Book 1 で検証済み）
FALLBACK_CHUNK = 10
REPEAT_RATIO = 0.3


# ---------------------------------------------------------------- 章検出


def page_texts(reader):
    return [page.extract_text() or "" for page in reader.pages]


def find_toc_pages(texts):
    return [i for i, text in enumerate(texts) if "TABLE OF CONTENTS" in text.upper()]


def parse_toc_entries(texts, toc_indexes):
    entries = []
    seen = set()
    for i in toc_indexes:
        for line in texts[i].splitlines():
            match = TOC_LINE_PATTERN.match(line.strip())
            if not match:
                continue
            slide = int(match.group(1))
            title = re.sub(r"\s+", " ", match.group(2)).strip()
            if (slide, title) in seen:
                continue
            seen.add((slide, title))
            entries.append((slide, title))
    return entries


def _slide_number(line):
    match = re.search(r"(\d+)\s*$", line.strip())
    return int(match.group(1)) if match else None


def toc_ranges(texts, entries):
    """目次エントリから章のページ範囲を作る。検証に失敗したら None。"""
    total = len(texts)
    mapped = []
    for slide, title in entries:
        index = slide + SLIDE_OFFSET - 1  # 0-based
        if not 0 <= index < total:
            continue
        header = texts[index].splitlines()[0] if texts[index].splitlines() else ""
        if _slide_number(header) != slide:
            continue
        mapped.append((index, title))

    if len(entries) == 0 or len(mapped) < len(entries) * 0.8:
        return None

    mapped.sort()
    chapters = []
    for i, (start, title) in enumerate(mapped):
        end = mapped[i + 1][0] if i + 1 < len(mapped) else total
        if end <= start:
            continue
        chapters.append({"title": title, "start": start + 1, "end": end})
    return chapters


def outline_ranges(reader, texts):
    """アウトラインの Exercise 開始ページから章範囲を作る。"""
    starts = []

    def walk(items):
        for item in items:
            if isinstance(item, list):
                walk(item)
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            title = str(item.title).strip()
            if title.lower().startswith("exercise"):
                starts.append((page, re.sub(r"\s+", " ", title)))

    walk(reader.outline)
    starts = sorted(set(starts))
    if not starts:
        return None

    chapters = []
    for i, (start, title) in enumerate(starts):
        end = starts[i + 1][0] - 1 if i + 1 < len(starts) else len(texts)
        if end < start:
            end = start
        chapters.append({"title": title, "start": start, "end": end})
    return chapters


def fallback_ranges(texts):
    chapters = []
    for start in range(1, len(texts) + 1, FALLBACK_CHUNK):
        end = min(start + FALLBACK_CHUNK - 1, len(texts))
        chapters.append({"title": f"p.{start}-{end}", "start": start, "end": end})
    return chapters


def detect_chapters(reader, texts):
    toc_indexes = find_toc_pages(texts)
    if toc_indexes:
        entries = parse_toc_entries(texts, toc_indexes)
        chapters = toc_ranges(texts, entries)
        if chapters:
            return chapters, "toc"
    chapters = outline_ranges(reader, texts)
    if chapters:
        return chapters, "outline"
    return fallback_ranges(texts), "fallback"


# ---------------------------------------------------------------- 整形


def repeated_lines(texts):
    counts = {}
    for text in texts:
        for line in {x.strip() for x in text.splitlines() if x.strip()}:
            counts[line] = counts.get(line, 0) + 1
    threshold = max(2, int(len(texts) * REPEAT_RATIO))
    return {line for line, count in counts.items() if count >= threshold}


def clean_text(text, repeated):
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if HEADER_PATTERN.match(line) or line in repeated:
            continue
        if any(pattern.match(line) for pattern in FOOTER_PATTERNS):
            continue
        out.append(line)
    cleaned = "\n".join(out)
    cleaned = re.sub(r"\n{3,}", "\n\n", cleaned)
    return cleaned.strip()


# ---------------------------------------------------------------- extract


def slugify(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    return slug[:40] or "chapter"


def extract(pdf_path, out_dir):
    if not os.path.isfile(pdf_path):
        raise ValueError(f"ファイルが見つかりません: {pdf_path}")
    if os.path.isdir(out_dir) and os.listdir(out_dir):
        raise ValueError(f"出力先が既に存在します（--force で上書き）: {out_dir}")

    reader = PdfReader(pdf_path)
    texts = page_texts(reader)
    chapters, method = detect_chapters(reader, texts)
    if not chapters:
        raise ValueError("章を検出できませんでした")

    repeated = repeated_lines(texts)
    chapter_dir = os.path.join(out_dir, "chapters")
    os.makedirs(chapter_dir, exist_ok=True)

    manifest_chapters = []
    front_end = chapters[0]["start"] - 1
    if front_end > 0:
        front_text = clean_text("\n".join(texts[:front_end]), repeated)
        with open(os.path.join(chapter_dir, "00_front.txt"), "w", encoding="utf-8") as handle:
            handle.write(f"# 前付け (p.1-{front_end})\n\n{front_text}\n")
        manifest_chapters.append(
            {"index": 0, "title": "前付け", "start": 1, "end": front_end,
             "chars": len(front_text), "file": "chapters/00_front.txt"}
        )

    for number, chapter in enumerate(chapters, start=1):
        part = texts[chapter["start"] - 1:chapter["end"]]
        body = clean_text("\n".join(part), repeated)
        filename = f"{number:02d}_{slugify(chapter['title'])}.txt"
        with open(os.path.join(chapter_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(
                f"# {chapter['title']} (p.{chapter['start']}-{chapter['end']})\n\n{body}\n"
            )
        manifest_chapters.append(
            {"index": number, "title": chapter["title"], "start": chapter["start"],
             "end": chapter["end"], "chars": len(body), "file": f"chapters/{filename}"}
        )

    manifest = {
        "source": pdf_path,
        "pages": len(texts),
        "method": method,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "chapters": manifest_chapters,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    os.makedirs(os.path.join(out_dir, "summaries"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "diagrams"), exist_ok=True)
    return manifest


# ---------------------------------------------------------------- assemble

HTML_HEAD = """<!DOCTYPE html>
<html lang="ja">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>{title} 要約</title>
<style>
  :root {{ --fg:#1f2430; --muted:#5b6475; --line:#d8dee9; --bg:#ffffff;
           --accent:#1f6feb; --chip:#eef4ff; --warn:#fff6e5; }}
  * {{ box-sizing: border-box; }}
  body {{ margin:0; padding:0 16px 48px; color:var(--fg); background:var(--bg);
         font-family: -apple-system, BlinkMacSystemFont, "Hiragino Sans",
                      "Noto Sans JP", "Yu Gothic", Meiryo, sans-serif;
         line-height:1.8; font-size:16px; }}
  header, main, footer {{ max-width: 920px; margin: 0 auto; }}
  header {{ padding: 32px 0 8px; border-bottom: 2px solid var(--line); }}
  h1 {{ font-size: 1.7em; margin: 0 0 8px; line-height: 1.4; }}
  .meta {{ color: var(--muted); font-size: .9em; margin: 0 0 16px; }}
  h2 {{ font-size: 1.3em; margin: 40px 0 12px; padding-bottom: 4px;
        border-bottom: 1px solid var(--line); }}
  h3 {{ font-size: 1.05em; margin: 18px 0 6px; }}
  .pages {{ color: var(--muted); font-size: .85em; font-weight: normal; margin-left: .5em; }}
  nav.toc {{ background: var(--chip); border-radius: 10px; padding: 12px 20px; margin: 24px 0; }}
  nav.toc h2 {{ border: 0; font-size: 1.05em; margin: 0 0 8px; }}
  nav.toc ol {{ margin: 0; padding-left: 1.4em; columns: 2; column-gap: 32px; }}
  nav.toc a {{ color: var(--accent); text-decoration: none; }}
  nav.toc a:hover {{ text-decoration: underline; }}
  section {{ margin: 8px 0; }}
  ul {{ margin: 6px 0 12px; padding-left: 1.4em; }}
  li {{ margin: 2px 0; }}
  .lead {{ color: var(--muted); }}
  figure {{ margin: 20px 0; padding: 12px; background: #fbfcfe;
            border: 1px solid var(--line); border-radius: 10px; }}
  figure svg {{ display: block; width: 100%; height: auto; }}
  figcaption {{ color: var(--muted); font-size: .85em; margin-top: 6px; text-align: center; }}
  .note {{ background: var(--warn); border-left: 4px solid #e0a400;
           padding: 8px 12px; border-radius: 6px; font-size: .9em; }}
  footer {{ margin-top: 48px; padding-top: 12px; border-top: 1px solid var(--line);
            color: var(--muted); font-size: .85em; }}
  @media print {{ nav.toc {{ background: none; }} body {{ font-size: 11pt; }} }}
</style>
</head>
<body>
<header>
  <h1>{title} 要約</h1>
  <p class="meta">出典: {source}（{pages} ページ） / 章数: {chapters} / 作成: {created}</p>
  <nav class="toc">
    <h2>目次</h2>
    <ol>
{toc}
    </ol>
  </nav>
</header>
<main>
"""

HTML_TAIL = """</main>
<footer>
  <p>本要約は PDF から抽出したテキストを LLM が要約したものです。正確な内容・条文・仕様は原本を参照してください。</p>
</footer>
</body>
</html>
"""


def _escape(text):
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def _read_svg(path):
    with open(path, encoding="utf-8") as handle:
        svg = handle.read()
    svg = re.sub(r"<\?xml[^>]*\?>", "", svg)
    svg = re.sub(r"<!DOCTYPE[^>]*>", "", svg)
    return svg.strip()


def assemble(out_dir):
    manifest_path = os.path.join(out_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ValueError(f"manifest.json がありません: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)

    chapters = manifest["chapters"]
    missing = []
    fragments = {}
    for chapter in chapters:
        path = os.path.join(out_dir, "summaries", f"{chapter['index']:02d}.html")
        if not os.path.isfile(path):
            missing.append(f"{chapter['index']:02d}.html")
            continue
        with open(path, encoding="utf-8") as handle:
            fragments[chapter["index"]] = handle.read().strip()
    if missing:
        raise ValueError("章の要約が不足しています: " + ", ".join(missing))

    diagrams_dir = os.path.join(out_dir, "diagrams")
    overview_svgs = []
    if os.path.isdir(diagrams_dir):
        for name in sorted(os.listdir(diagrams_dir)):
            if name.startswith("overview") and name.endswith(".svg"):
                overview_svgs.append(_read_svg(os.path.join(diagrams_dir, name)))

    def figure(svg, caption):
        return (
            f'  <figure>\n{svg}\n'
            f"    <figcaption>{_escape(caption)}</figcaption>\n  </figure>\n"
        )

    title = os.path.splitext(os.path.basename(manifest["source"]))[0]
    for suffix in ("_decrypted_stripped", "_stripped", "_decrypted"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            break
    toc_lines = []
    sections = []

    if 0 in fragments:
        toc_lines.append('      <li><a href="#overview">全体像</a></li>')
        figures = "\n".join(figure(svg, "全体像") for svg in overview_svgs)
        sections.append(
            f'  <section id="overview">\n    <h2>全体像</h2>\n'
            f"    {fragments[0]}\n{figures}  </section>\n"
        )

    for chapter in chapters:
        index = chapter["index"]
        if index == 0:
            continue
        anchor = f"ch-{index}"
        toc_lines.append(
            f'      <li><a href="#{anchor}">{index}. {_escape(chapter["title"])}</a></li>'
        )
        svg_path = os.path.join(diagrams_dir, f"ch-{index:02d}.svg")
        extra = figure(_read_svg(svg_path), chapter["title"]) if os.path.isfile(svg_path) else ""
        sections.append(
            f'  <section id="{anchor}">\n'
            f'    <h2>{index}. {_escape(chapter["title"])}'
            f' <span class="pages">p.{chapter["start"]}–{chapter["end"]}</span></h2>\n'
            f"    {fragments[index]}\n{extra}  </section>\n"
        )

    html = HTML_HEAD.format(
        title=_escape(title),
        source=_escape(os.path.basename(manifest["source"])),
        pages=manifest["pages"],
        chapters=sum(1 for c in chapters if c["index"] > 0),
        created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        toc="\n".join(toc_lines),
    ) + "\n".join(sections) + HTML_TAIL

    external = re.search(r'(?:src|href)\s*=\s*["\']\s*https?:', html, re.I)
    if external:
        raise ValueError("外部参照が含まれています（単一 HTML になっていません）")

    output_path = os.path.join(out_dir, f"{title}_summary.html")
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return output_path


# ---------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description="PDF の要約版（単一 HTML）を作る")
    sub = parser.add_subparsers(dest="command", required=True)

    extract_parser = sub.add_parser("extract", help="章検出と本文抽出")
    extract_parser.add_argument("pdf", help="入力 PDF")
    extract_parser.add_argument("--out-dir", help="出力先（既定: work/summary/<PDF名>）")
    extract_parser.add_argument("--force", action="store_true", help="既存の出力を上書き")

    assemble_parser = sub.add_parser("assemble", help="要約断片と図解を単一 HTML に結合")
    assemble_parser.add_argument("dir", help="extract の出力ディレクトリ")

    args = parser.parse_args(argv)

    try:
        if args.command == "extract":
            out_dir = args.out_dir or os.path.join(
                "work", "summary", os.path.splitext(os.path.basename(args.pdf))[0]
            )
            if args.force and os.path.isdir(out_dir):
                import shutil

                shutil.rmtree(out_dir)
            manifest = extract(args.pdf, out_dir)
            print(
                f"{manifest['pages']} ページ / 章 {len(manifest['chapters'])} 件（方式: {manifest['method']}）"
                f" -> {out_dir}"
            )
            for chapter in manifest["chapters"]:
                print(
                    f"  {chapter['index']:02d} p.{chapter['start']:>3}-{chapter['end']:<3} "
                    f"{chapter['chars']:>6}字 {chapter['title'][:50]}"
                )
        else:
            output = assemble(args.dir)
            print(f"要約 HTML を書き出しました: {output}")
    except ValueError as exc:
        print(f"エラー: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
