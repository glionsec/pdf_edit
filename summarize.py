"""PDF の要約版（単一 HTML）を作るための抽出・結合ツール。

サブコマンド:
- extract: 章を検出し、章ごとの本文テキストと manifest.json を書き出す
- prompt:  章一覧から LLM への指示文（PROMPT.md）を生成する
- check:   要約断片と図解の書式・安全性を検証する
- assemble: 要約断片と SVG 図解を 1 つの自己完結 HTML にまとめる

章検出は manual / toc+labels / toc+offset / outline / headings / fixed の順に試す。
要約文そのものの生成は LLM/エージェント側で行い、このスクリプトは決定的な抽出と結合のみを担う。
"""
import argparse
import base64
import json
import os
import re
import sys
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from html.parser import HTMLParser

from pypdf import PdfReader

import figures as figures_module

# ---------------------------------------------------------------- 定数

TOC_PAGE_LIMIT_RATIO = 0.15  # 目次ページを探す範囲（先頭 15%）
FALLBACK_CHUNK = 10
REPEAT_RATIO = 0.3
MAX_CHAPTERS = 200
OUTLINE_MAX_WITHOUT_FILTER = 30
OUTLINE_SECTION_PATTERN = re.compile(
    r"^\s*(exercise|chapter|section|part|lab|module|unit|lesson|演習|第)", re.I
)

PROFILES = {
    "generic": {"header_pattern": None, "outline_filter": None},
    "sans": {
        "header_pattern": re.compile(r"^\s*SEC542\s*\|"),
        "outline_filter": re.compile(r"^\s*exercise", re.I),
    },
}

# 目次行のパターン（数字が先頭 / 数字が末尾）
TOC_NUMBER_FIRST = re.compile(r"^\s*(\d{1,4})\s*[\.\)]?\s*([A-Za-z(].+)$")
TOC_NUMBER_LAST = re.compile(r"^(.+?)[\s.·…]{2,}(\d{1,4})\s*$")
HEADING_NUMBERED = re.compile(r"^\s*((?:\d{1,2})\.?)\s+(\S.{2,80})$")

# 要約断片の許可タグ・属性
ALLOWED_TAGS = {
    "p", "ul", "ol", "li", "strong", "em", "code", "br", "table", "thead",
    "tbody", "tr", "th", "td", "blockquote", "h4", "h5", "span", "dl", "dt", "dd",
}
ALLOWED_ATTRS = {"class"}
FORBIDDEN_SVG_TAGS = {
    "script", "foreignobject", "iframe", "object", "embed", "audio", "video", "image",
}
EXTERNAL_URL = re.compile(r"(?:https?|ftp|javascript):", re.I)
FRAGMENT_EXTERNAL = re.compile(
    r"(?:src|href)\s*=\s*[\"']?\s*(?:https?|ftp|javascript):", re.I
)
VISIBLE_MIN, VISIBLE_MAX = 100, 1200
LI_MIN, LI_MAX = 2, 8
VOID_TAGS = {"br"}


class ExtractError(ValueError):
    pass


ALLOWED_STRATEGIES = {
    "manual", "toc+labels", "toc+offset", "outline", "headings", "fixed", "toc", "unknown",
}


# ---------------------------------------------------------------- 共通


def page_texts(reader):
    return [page.extract_text() or "" for page in reader.pages]


def normalize_title(text):
    return re.sub(r"\s+", " ", text).strip()


def repeated_lines(texts):
    counts = {}
    for text in texts:
        for line in {x.strip() for x in text.splitlines() if x.strip()}:
            counts[line] = counts.get(line, 0) + 1
    threshold = max(2, int(len(texts) * REPEAT_RATIO))
    return {line for line, count in counts.items() if count >= threshold}


def detect_running_headers(texts):
    """全ページの 30% 以上で先頭行の共通接頭辞になっているものを検出する。

    ページ番号だけが違うランニングヘッダー（例: "SEC542 | ... 9"）を汎用に除去する。
    """
    prefixes = {}
    for text in texts:
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        key = re.sub(r"[\s\d\.\-–—]+$", "", lines[0]).strip()
        if len(key) >= 8:
            prefixes[key] = prefixes.get(key, 0) + 1
    threshold = max(2, int(len(texts) * REPEAT_RATIO))
    return [prefix for prefix, count in prefixes.items() if count >= threshold]


def clean_text(text, repeated, header_patterns=None):
    header_patterns = header_patterns or []
    if hasattr(header_patterns, "match"):
        header_patterns = [header_patterns]
    prefixes = [item for item in header_patterns if isinstance(item, str)]
    regexes = [item for item in header_patterns if not isinstance(item, str)]
    out = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            out.append("")
            continue
        if line in repeated:
            continue
        if any(line.startswith(prefix) for prefix in prefixes):
            continue
        if any(pattern.match(line) for pattern in regexes):
            continue
        if re.match(r"^\s*©", line) or re.match(r"^\s*\d{1,4}\s*$", line):
            continue
        out.append(line)
    return re.sub(r"\n{3,}", "\n\n", "\n".join(out)).strip()


# ---------------------------------------------------------------- 目次


def find_toc_pages(texts):
    limit = max(3, int(len(texts) * TOC_PAGE_LIMIT_RATIO))
    pages = []
    for index, text in enumerate(texts[:limit]):
        upper = text.upper()
        if "TABLE OF CONTENTS" in upper or re.search(r"^\s*CONTENTS\s*$", upper, re.M):
            pages.append(index)
    return pages


def parse_toc_entries(texts, toc_indexes):
    entries = []
    seen = set()
    for index in toc_indexes:
        for line in texts[index].splitlines():
            stripped = line.strip()
            if not stripped or len(stripped) < 4:
                continue
            match = TOC_NUMBER_LAST.match(stripped)
            if match:
                title, number = match.group(1).strip(), int(match.group(2))
            else:
                match = TOC_NUMBER_FIRST.match(stripped)
                if not match:
                    continue
                number, title = int(match.group(1)), match.group(2).strip()
            title = normalize_title(re.sub(r"[.·…]{2,}$", "", title)).strip(" .:·…")
            if len(title) < 2:
                continue
            key = (number, title.lower())
            if key in seen:
                continue
            seen.add(key)
            entries.append((number, title))
    return entries


def chapters_from_entries(entries, total_pages):
    """(ページ番号, タイトル) の列を連続した章範囲にする。"""
    starts = [(number, title) for number, title in entries if 1 <= number <= total_pages]
    starts.sort()
    deduped = []
    for number, title in starts:
        if deduped and deduped[-1][0] == number:
            continue
        deduped.append((number, title))
    if len(deduped) < 2:
        return None
    chapters = []
    for i, (start, title) in enumerate(deduped):
        end = deduped[i + 1][0] - 1 if i + 1 < len(deduped) else total_pages
        chapters.append({"title": title, "start": start, "end": max(start, end)})
    return chapters


def reader_page_labels(reader):
    """/PageLabels が実際に存在する場合だけページラベルを返す。

    pypdf は /PageLabels が無くても 1..N の既定ラベルを返すため、ルート辞書を確認する。
    """
    try:
        root = reader.trailer.get("/Root")
        if root is None or "/PageLabels" not in root:
            return None
        return reader.page_labels
    except Exception:
        return None


def map_entries_with_labels(entries, labels, total_pages):
    """印刷ページ番号を /PageLabels で物理ページに変換する。"""
    if not labels or len(labels) != total_pages:
        return None
    index_by_label = {}
    for index, label in enumerate(labels):
        index_by_label.setdefault(str(label), index)
    mapped = [
        (index_by_label[str(number)] + 1, title)
        for number, title in entries
        if str(number) in index_by_label
    ]
    if len(mapped) < max(2, int(len(entries) * 0.8)):
        return None
    return chapters_from_entries(mapped, total_pages)


def detect_offset(entries, texts):
    """印刷番号と物理ページのズレを、ページ端の番号表記から推定する。

    ヘッダー（先頭行）での一致を優先し、同点なら一致数の合計、最後に |offset| の小ささで選ぶ。
    """
    if not entries:
        return None
    required = max(2, int(len(entries) * 0.8))
    best = None
    for offset in range(-10, 21):
        first_matches = 0
        last_matches = 0
        for number, title in entries:
            index = number + offset - 1
            if not 0 <= index < len(texts):
                continue
            lines = [line.strip() for line in texts[index].splitlines() if line.strip()]
            if not lines:
                continue
            pattern = re.compile(rf"(?<!\d){number}(?!\d)")
            if pattern.search(lines[0]):
                first_matches += 1
            if len(lines) > 1 and pattern.search(lines[-1]):
                last_matches += 1
        total = first_matches + last_matches
        if total < required:
            continue
        key = (first_matches, total, -abs(offset))
        if best is None or key > best[0]:
            best = (key, offset)
    return best[1] if best else None


# ---------------------------------------------------------------- しおり・見出し


def outline_entries(reader, profile_name):
    profile = PROFILES[profile_name]
    entries = []

    def walk(items, depth=0):
        for item in items:
            if isinstance(item, list):
                walk(item, depth + 1)
                continue
            if depth != 0:
                continue
            try:
                page = reader.get_destination_page_number(item) + 1
            except Exception:
                continue
            title = normalize_title(str(item.title))
            if title:
                entries.append((page, title))

    try:
        walk(reader.outline)
    except Exception:
        return []

    if profile["outline_filter"] is not None:
        entries = [e for e in entries if profile["outline_filter"].match(e[1])]
    elif len(entries) > OUTLINE_MAX_WITHOUT_FILTER:
        filtered = [e for e in entries if OUTLINE_SECTION_PATTERN.match(e[1])]
        entries = filtered if len(filtered) >= 2 else []
    return entries


def heading_entries(texts):
    starts = []
    for index, text in enumerate(texts):
        lines = [line.strip() for line in text.splitlines() if line.strip()]
        if not lines:
            continue
        match = HEADING_NUMBERED.match(lines[0])
        if not match:
            continue
        if match.group(1).count(".") > 1:
            continue
        starts.append((index + 1, normalize_title(lines[0])))
    return starts


# ---------------------------------------------------------------- 章検出


def load_manual_chapters(path, total_pages):
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractError(f"--chapters の JSON を読めません: {exc}") from exc
    raw = data.get("chapters", []) if isinstance(data, dict) else data
    if not isinstance(raw, list):
        raise ExtractError("--chapters の形式が不正です（配列を指定）")
    chapters = []
    for item in raw:
        try:
            title = normalize_title(str(item["title"]))
            start = int(item["start"])
            end = int(item["end"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExtractError(f"--chapters の項目が不正です: {item!r}") from exc
        if not title:
            raise ExtractError("--chapters に空のタイトルがあります")
        if not (1 <= start <= end <= total_pages):
            raise ExtractError(f"--chapters のページ範囲が不正です: {start}-{end}")
        chapters.append({"title": title, "start": start, "end": end})
    if not chapters:
        raise ExtractError("--chapters が空です")
    chapters.sort(key=lambda c: c["start"])
    for prev, cur in zip(chapters, chapters[1:]):
        if cur["start"] <= prev["end"]:
            raise ExtractError("--chapters のページ範囲が重複しています")
    return chapters


def detect_chapters(reader, texts, options=None):
    options = options or {}
    total = len(texts)

    manual_path = options.get("manual")
    if manual_path:
        return {
            "chapters": load_manual_chapters(manual_path, total),
            "strategy": "manual",
            "confidence": 1.0,
            "low_text": sum(len(t) for t in texts) < 20,
        }
    if total == 0:
        raise ExtractError("ページがありません")
    if sum(len(t) for t in texts) < 20:
        raise ExtractError(
            "テキストを抽出できません（スキャン画像の PDF の可能性）。"
            "OCR 済みの PDF か、テキストを持つ PDF を使ってください。"
        )

    toc_pages = options.get("toc_pages") or find_toc_pages(texts)
    toc_pages = [index for index in toc_pages if 0 <= index < total]
    entries = parse_toc_entries(texts, toc_pages) if toc_pages else []

    if entries:
        if options.get("offset") is not None:
            shifted = [(number + options["offset"], title) for number, title in entries]
            chapters = chapters_from_entries(shifted, total)
            if chapters:
                return {"chapters": chapters, "strategy": "toc+offset", "confidence": 0.9}
            raise ExtractError("--offset を適用できませんでした（章が 2 件未満になります）")
        else:
            chapters = map_entries_with_labels(entries, reader_page_labels(reader), total)
            if chapters:
                return {"chapters": chapters, "strategy": "toc+labels", "confidence": 0.95}
            offset = detect_offset(entries, texts)
            if offset is not None:
                shifted = [(number + offset, title) for number, title in entries]
                chapters = chapters_from_entries(shifted, total)
                if chapters:
                    return {"chapters": chapters, "strategy": "toc+offset", "confidence": 0.9}

    profile = options.get("profile", "generic")
    chapters = chapters_from_entries(outline_entries(reader, profile), total)
    if chapters:
        return {"chapters": chapters, "strategy": "outline", "confidence": 0.85}

    chapters = chapters_from_entries(heading_entries(texts), total)
    if chapters:
        return {"chapters": chapters, "strategy": "headings", "confidence": 0.5}

    chapters = [
        {"title": f"p.{start}-{end}", "start": start, "end": end}
        for start in range(1, len(texts) + 1, FALLBACK_CHUNK)
        for end in [min(start + FALLBACK_CHUNK - 1, len(texts))]
    ]
    return {"chapters": chapters, "strategy": "fixed", "confidence": 0.2}


# ---------------------------------------------------------------- extract


def _walk_pdfs(directory):
    try:
        entries = list(os.scandir(directory))
    except OSError:
        return
    for entry in entries:
        try:
            if entry.is_dir(follow_symlinks=False):
                yield from _walk_pdfs(entry.path)
            elif entry.name.lower().endswith(".pdf"):
                yield entry.path
        except OSError:
            continue


def _safe_removal_target(path):
    if os.path.islink(path):
        raise ExtractError(f"シンボリックリンクは削除対象にできません: {path}")
    real = os.path.realpath(path)
    cwd = os.path.realpath(os.getcwd())
    home = os.path.realpath(os.path.expanduser("~"))
    if real in ("/", home) or real == cwd:
        raise ExtractError(f"削除対象として不適切なパスです: {path}")
    if cwd.startswith(real + os.sep):
        raise ExtractError(f"カレントディレクトリの親は削除できません: {path}")
    if os.path.exists(os.path.join(real, ".git")) or (
        os.path.isfile(os.path.join(real, "HEAD"))
        and os.path.isdir(os.path.join(real, "objects"))
    ):
        raise ExtractError(f"git リポジトリは削除できません: {path}")
    if next(_walk_pdfs(real), None) is not None:
        raise ExtractError(f"PDF を含むディレクトリは削除できません: {path}")
    for pattern in ("*.py", "pyproject.toml", "README.md", ".gitignore"):
        if any(
            entry.name == pattern or (pattern.startswith("*.") and entry.name.endswith(pattern[1:]))
            for entry in os.scandir(real)
        ):
            raise ExtractError(f"ソースファイルを含むディレクトリは削除できません: {path}")


def slugify(title):
    slug = re.sub(r"[^A-Za-z0-9]+", "-", title).strip("-").lower()
    return slug[:40] or "chapter"


def extract(pdf_path, out_dir, options=None, dry_run=False):
    options = options or {}
    if not os.path.isfile(pdf_path):
        raise ExtractError(f"ファイルが見つかりません: {pdf_path}")
    if os.path.islink(out_dir):
        raise ExtractError(f"出力先がシンボリックリンクです: {out_dir}")
    real_pdf = os.path.realpath(pdf_path)
    real_out = os.path.realpath(out_dir)
    if real_pdf == real_out or real_pdf.startswith(real_out + os.sep):
        raise ExtractError("PDF が出力先ディレクトリの中にあります")

    try:
        reader = PdfReader(pdf_path)
    except Exception as exc:  # pypdf の壊れた PDF 例外を明示エラーに
        raise ExtractError(f"PDF を読めません: {exc}") from exc
    if getattr(reader, "is_encrypted", False):
        raise ExtractError(
            "暗号化された PDF です。先に unlock.py で解除した PDF を使ってください。"
        )
    texts = page_texts(reader)
    detected = detect_chapters(reader, texts, options)
    chapters = detected["chapters"]
    if len(chapters) > MAX_CHAPTERS:
        raise ExtractError(
            f"章が多すぎます（{len(chapters)}）。--chapters で明示指定してください"
        )
    if detected.get("low_text"):
        print("警告: テキストがほとんど抽出できません（図や画像だけの PDF の可能性）")

    if os.path.isdir(out_dir) and os.listdir(out_dir) and not dry_run:
        if not options.get("force"):
            raise ExtractError(f"出力先が既に存在します（--force で上書き）: {out_dir}")
        _safe_removal_target(out_dir)
        import shutil

        shutil.rmtree(out_dir)

    if dry_run:
        print(
            f"[dry-run] {len(texts)} ページ / {len(chapters)} 章"
            f"（方式: {detected['strategy']}, 確信度: {detected['confidence']}）"
        )
        for number, chapter in enumerate(chapters, start=1):
            print(
                f"  {number:02d} p.{chapter['start']:>3}-{chapter['end']:<3} "
                f"{chapter['title'][:60]}"
            )
        return None

    front_end = chapters[0]["start"] - 1
    covered = set(range(1, front_end + 1))
    for chapter in chapters:
        covered.update(range(chapter["start"], chapter["end"] + 1))
    uncovered = sorted(set(range(1, len(texts) + 1)) - covered)
    if uncovered:
        print(f"警告: 章に含まれないページがあります: {uncovered[:10]}")

    repeated = repeated_lines(texts)
    profile = PROFILES[options.get("profile", "generic")]
    header_patterns = list(detect_running_headers(texts))
    if profile["header_pattern"] is not None:
        header_patterns.append(profile["header_pattern"])
    chapter_dir = os.path.join(out_dir, "chapters")
    os.makedirs(chapter_dir, exist_ok=True)

    manifest_chapters = []
    if front_end > 0:
        front = clean_text("\n".join(texts[:front_end]), repeated, header_patterns)
        with open(os.path.join(chapter_dir, "00_front.txt"), "w", encoding="utf-8") as handle:
            handle.write(f"# 前付け (p.1-{front_end})\n\n{front}\n")
        manifest_chapters.append(
            {"index": 0, "title": "前付け", "front": True, "start": 1, "end": front_end,
             "chars": len(front), "file": "chapters/00_front.txt"}
        )

    for number, chapter in enumerate(chapters, start=1):
        body = clean_text(
            "\n".join(texts[chapter["start"] - 1:chapter["end"]]),
            repeated,
            header_patterns,
        )
        filename = f"{number:02d}_{slugify(chapter['title'])}.txt"
        with open(os.path.join(chapter_dir, filename), "w", encoding="utf-8") as handle:
            handle.write(f"# {chapter['title']} (p.{chapter['start']}-{chapter['end']})\n\n{body}\n")
        manifest_chapters.append(
            {"index": number, "title": chapter["title"], "start": chapter["start"],
             "end": chapter["end"], "chars": len(body), "file": f"chapters/{filename}"}
        )

    manifest = {
        "source": pdf_path,
        "pages": len(texts),
        "strategy": detected["strategy"],
        "confidence": detected["confidence"],
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "uncovered_pages": uncovered,
        "chapters": manifest_chapters,
    }
    with open(os.path.join(out_dir, "manifest.json"), "w", encoding="utf-8") as handle:
        json.dump(manifest, handle, ensure_ascii=False, indent=2)

    os.makedirs(os.path.join(out_dir, "summaries"), exist_ok=True)
    os.makedirs(os.path.join(out_dir, "diagrams"), exist_ok=True)
    return manifest


# ---------------------------------------------------------------- 検証


class _FragmentParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self.text = []
        self.lead = False
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        if tag not in ALLOWED_TAGS:
            self.problems.append(f"許可されていないタグ: <{tag}>")
            return
        if tag not in VOID_TAGS:
            self.stack.append(tag)
        for name, value in attrs:
            name = name.lower()
            if name not in ALLOWED_ATTRS:
                self.problems.append(f"許可されていない属性: {name} (<{tag}>)")
            elif name == "class" and tag == "p" and value == "lead":
                self.lead = True

    def handle_endtag(self, tag):
        tag = tag.lower()
        if tag in VOID_TAGS:
            return
        if not self.stack:
            self.problems.append(f"対応する開始タグがない閉じタグ: </{tag}>")
            return
        if self.stack[-1] == tag:
            self.stack.pop()
            return
        self.problems.append(
            f"閉じタグの不一致: </{tag}>（開いているのは <{self.stack[-1]}>）"
        )
        if tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_data(self, data):
        self.text.append(data)


def _strip_tags(html):
    parser = _FragmentParser()
    parser.feed(html)
    return "".join(parser.text)


def validate_fragment(html, label):
    problems = []
    parser = _FragmentParser()
    parser.feed(html)
    parser.close()
    problems.extend(f"{label}: {p}" for p in parser.problems)
    if parser.stack:
        problems.append(f"{label}: 閉じられていないタグ: {parser.stack}")
    if not parser.lead:
        problems.append(f'{label}: 先頭に <p class="lead"> がありません')
    li_count = len(re.findall(r"<li\b", html, re.I))
    if not LI_MIN <= li_count <= LI_MAX:
        problems.append(f"{label}: <li> の数が {li_count}（{LI_MIN}〜{LI_MAX} の範囲外）")
    visible = _strip_tags(html).strip()
    if not VISIBLE_MIN <= len(visible) <= VISIBLE_MAX:
        problems.append(
            f"{label}: 可視テキストが {len(visible)} 字（{VISIBLE_MIN}〜{VISIBLE_MAX} の範囲外）"
        )
    if FRAGMENT_EXTERNAL.search(html):
        problems.append(f"{label}: 外部参照（http/https/ftp/javascript）が含まれています")
    if re.search(r"<[^>]+\son[a-z]+\s*=", html, re.I):
        problems.append(f"{label}: イベントハンドラ属性が含まれています")
    return problems


SVG_STYLE_UNSAFE = re.compile(r"@import|https?:|//|javascript:|expression\s*\(", re.I)
SVG_STYLE_URL = re.compile(r"url\s*\(\s*([^)]*)\)", re.I)


def _svg_style_unsafe(text):
    """外部参照・危険な CSS を検出する。url(#id) の内部参照は許可する。"""
    if SVG_STYLE_UNSAFE.search(text):
        return True
    for match in SVG_STYLE_URL.finditer(text):
        if not match.group(1).strip().startswith("#"):
            return True
    return False
ALLOWED_SVG_HREF = re.compile(r"#[A-Za-z0-9_-]+")
ALLOWED_SVG_DATA = re.compile(r"data:image/(png|jpeg);base64,[A-Za-z0-9+/=]+")


def validate_svg(svg, label):
    problems = []
    try:
        root = ET.fromstring(svg)
    except ET.ParseError as exc:
        return [f"{label}: SVG の XML が不正です: {exc}"]
    for element in root.iter():
        tag = element.tag.split("}")[-1].lower()
        if tag in FORBIDDEN_SVG_TAGS:
            problems.append(f"{label}: SVG に <{tag}> が含まれています")
        if tag == "style":
            text = "".join(element.itertext())
            if _svg_style_unsafe(text):
                problems.append(f"{label}: SVG の <style> に外部参照・url() が含まれています")
        for name, value in element.attrib.items():
            key = name.split("}")[-1].split(":")[-1].lower()
            text = str(value).strip()
            if key.startswith("on"):
                problems.append(f"{label}: SVG にイベントハンドラ {key} が含まれています")
            elif key == "style" and _svg_style_unsafe(text):
                problems.append(f"{label}: SVG の style 属性に url() 等が含まれています")
            elif key in ("href", "src"):
                if not (ALLOWED_SVG_HREF.fullmatch(text) or ALLOWED_SVG_DATA.fullmatch(text)):
                    problems.append(f"{label}: SVG の {key} が内部参照/data 画像ではありません")
    return problems


def _is_front(chapter):
    """前付け章かどうか（新形式の front フラグ、旧 manifest のタイトルにも対応）。"""
    return bool(chapter.get("front")) or chapter.get("title") == "前付け"


def load_manifest(out_dir):
    path = os.path.join(out_dir, "manifest.json")
    if not os.path.isfile(path):
        raise ExtractError(f"manifest.json がありません: {path}")
    try:
        with open(path, encoding="utf-8") as handle:
            manifest = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ExtractError(f"manifest.json を読めません: {exc}") from exc
    source = manifest.get("source")
    if not isinstance(source, str) or not source.strip():
        raise ExtractError("manifest.json の source が不正です")
    strategy = manifest.get("strategy", manifest.get("method", "unknown"))
    if strategy not in ALLOWED_STRATEGIES:
        raise ExtractError(f"manifest.json の strategy が不正です: {strategy!r}")
    manifest["strategy"] = strategy
    pages = manifest.get("pages")
    chapters = manifest.get("chapters")
    if not isinstance(pages, int) or pages < 1 or not isinstance(chapters, list) or not chapters:
        raise ExtractError("manifest.json の pages / chapters が不正です")
    indexes = set()
    previous_end = 0
    for chapter in chapters:
        index = chapter.get("index")
        if not isinstance(index, int) or index in indexes:
            raise ExtractError(f"manifest の章 index が不正または重複: {index!r}")
        indexes.add(index)
        if not isinstance(chapter.get("title"), str) or not chapter["title"].strip():
            raise ExtractError(f"manifest の章タイトルが不正: {index}")
        if index == 0 and not _is_front(chapter):
            raise ExtractError("manifest の index 0 は前付け（front: true）である必要があります")
        start, end = chapter.get("start"), chapter.get("end")
        if not isinstance(start, int) or not isinstance(end, int):
            raise ExtractError(f"manifest のページ範囲が不正: {index}")
        if not (1 <= start <= end <= pages):
            raise ExtractError(f"manifest のページ範囲が範囲外: {index}")
        if start <= previous_end:
            raise ExtractError(f"manifest の章範囲が重複/順序不正: {index}")
        previous_end = end
    return manifest


def check(out_dir):
    manifest = load_manifest(out_dir)
    problems = []
    for chapter in manifest["chapters"]:
        path = os.path.join(out_dir, "summaries", f"{chapter['index']:02d}.html")
        if not os.path.isfile(path):
            problems.append(f"章 {chapter['index']:02d} の要約がありません: {path}")
            continue
        try:
            with open(path, encoding="utf-8") as handle:
                fragment = handle.read()
        except (OSError, UnicodeDecodeError) as exc:
            problems.append(f"章 {chapter['index']:02d} の要約を読めません: {exc}")
            continue
        problems.extend(validate_fragment(fragment, f"章 {chapter['index']:02d}"))
    problems.extend(figures_module.validate_figures(out_dir))
    diagrams = os.path.join(out_dir, "diagrams")
    if os.path.isdir(diagrams):
        for name in sorted(os.listdir(diagrams)):
            if name.lower().endswith(".svg"):
                try:
                    with open(os.path.join(diagrams, name), encoding="utf-8") as handle:
                        svg = handle.read()
                except (OSError, UnicodeDecodeError) as exc:
                    problems.append(f"{name} を読めません: {exc}")
                    continue
                problems.extend(validate_svg(svg, name))
    return problems


# ---------------------------------------------------------------- prompt


PROMPT_TEMPLATE = """# 章要約の作成指示

あなたは{language}の技術要約を書く担当です。作業ディレクトリは {workdir}。

次の章テキスト（PDF から抽出済み）を読み、それぞれ{language}の要約 {fmt} 断片を書いてください。

入力ディレクトリ: {chapters_dir}
{chapter_list}

出力: {summaries_dir}
{output_list}

書き方（厳守）:
- HTML 断片のみ。<html> <head> <body> <script> <style> <img> <a> は禁止。
- 先頭に `<p class="lead">`（1〜2 文）、続けて `<ul><li>…</li></ul>` を 3〜6 項目。
- 重要語は `<strong>` で囲む。可視テキストは 150〜1000 字程度。
- 外部 URL・画像・インライン style・イベント属性は書かない。
- 特殊文字（& < >）は必ずエスケープする。
- 原文の長い引用はせず、要約に徹する。
- ファイルの読み書き以外の操作はしない。

図解（任意）: {diagrams_dir} に `overview-1.svg` などを置くと全体像の直後に、
`ch-NN.svg` を置くと該当章に埋め込まれます（スクリプト・外部参照は禁止）。

書き終えたら `python summarize.py check {out_dir}` で書式を検証してください。
"""


LANGUAGE_LABELS = {"ja": "日本語", "en": "English"}


def load_figure_descriptions(out_dir):
    """figures/descriptions.json: {ファイル名: 図の説明} を読む（任意）。"""
    path = os.path.join(out_dir, "figures", "descriptions.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except (OSError, UnicodeDecodeError, json.JSONDecodeError):
        return {}


def write_prompt(out_dir, language="ja"):
    language = LANGUAGE_LABELS.get(language, language)
    manifest = load_manifest(out_dir)
    figures_data = figures_module.load_figures(out_dir) or {}
    descriptions = load_figure_descriptions(out_dir)
    figure_pages = {}
    for entry in figures_data.get("figures", []):
        figure_pages.setdefault(entry.get("chapter"), []).append(entry.get("page"))
    chapters_dir = os.path.join(out_dir, "chapters")
    chapter_lines = []
    output_lines = []
    for chapter in manifest["chapters"]:
        pages = figure_pages.get(chapter["index"], [])
        chapter_entries = [
            e for e in figures_data.get("figures", []) if e.get("chapter") == chapter["index"]
        ]
        figure_note = ""
        if chapter_entries:
            notes = []
            for entry in chapter_entries:
                description = descriptions.get(entry["file"], "").strip()
                if description:
                    notes.append(f"p.{entry['page']}: {description[:200]}")
                else:
                    notes.append(f"p.{entry['page']}: （説明なし。図の内容は不明）")
            figure_note = " / 図版 " + "; ".join(notes)
        chapter_lines.append(
            f"- {chapter['index']:02d}: {chapter.get('file', '(file 未設定)')} "
            f"（p.{chapter['start']}-{chapter['end']}, "
            f"{chapter.get('chars', '?')} 字, {chapter['title']}{figure_note}）"
        )
        output_lines.append(f"- {chapter['index']:02d}.html")
    prompt = PROMPT_TEMPLATE.format(
        language=language,
        workdir=os.getcwd(),
        fmt="HTML",
        chapters_dir=chapters_dir,
        chapter_list="\n".join(chapter_lines),
        summaries_dir=os.path.join(out_dir, "summaries"),
        output_list="\n".join(output_lines),
        diagrams_dir=os.path.join(out_dir, "diagrams"),
        out_dir=out_dir,
    )
    path = os.path.join(out_dir, "PROMPT.md")
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(prompt)
    return path


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
  <p class="meta">出典: {source}（{pages} ページ） / 章数: {chapters} / 方式: {strategy} / 作成: {created}</p>
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


def _display_title(source):
    title = os.path.splitext(os.path.basename(source))[0]
    for suffix in ("_decrypted_stripped", "_stripped", "_decrypted"):
        if title.endswith(suffix):
            title = title[: -len(suffix)]
            break
    return re.sub(r"[\x00-\x1f/\\]", "_", title).strip() or "summary"


def assemble(out_dir, embed_figures=False):
    problems = check(out_dir)
    if problems:
        raise ExtractError("要約の検証に失敗しました:\n- " + "\n- ".join(problems))

    manifest = load_manifest(out_dir)
    chapters = manifest["chapters"]
    fragments = {}
    for chapter in chapters:
        with open(
            os.path.join(out_dir, "summaries", f"{chapter['index']:02d}.html"),
            encoding="utf-8",
        ) as handle:
            fragments[chapter["index"]] = handle.read().strip()

    figures_by_chapter = {}
    figures_data = figures_module.load_figures(out_dir) or {} if embed_figures else {}
    for entry in figures_data.get("figures", []):
        path = os.path.join(out_dir, str(entry.get("file", "")))
        if not os.path.isfile(path):
            continue
        with open(path, "rb") as handle:
            payload = handle.read()
        mime = "image/png" if entry.get("format") == "png" else "image/jpeg"
        data_uri = f"data:{mime};base64,{base64.b64encode(payload).decode('ascii')}"
        page = _escape(str(int(entry["page"])))
        width = _escape(str(int(entry["width"])))
        height = _escape(str(int(entry["height"])))
        figure_html = (
            f'  <figure>\n    <img src="{data_uri}" '
            f'alt="p.{page} の図" loading="lazy">\n'
            f'    <figcaption>p.{page} の図（{width}×{height}）</figcaption>\n'
            f'  </figure>\n'
        )
        figures_by_chapter.setdefault(entry.get("chapter"), []).append(figure_html)

    def chapter_figures(index):
        return "".join(figures_by_chapter.get(index, []))

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

    title = _display_title(manifest["source"])
    toc_lines = []
    sections = []

    if 0 in fragments and any(_is_front(c) for c in chapters):
        toc_lines.append('      <li><a href="#overview">全体像</a></li>')
        figures = "\n".join(figure(svg, "全体像") for svg in overview_svgs)
        sections.append(
            f'  <section id="overview">\n    <h2>全体像</h2>\n'
            f"    {fragments[0]}\n{chapter_figures(0)}{figures}  </section>\n"
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
        extra = (
            figure(_read_svg(svg_path), chapter["title"])
            if os.path.isfile(svg_path)
            else ""
        )
        sections.append(
            f'  <section id="{anchor}">\n'
            f'    <h2>{index}. {_escape(chapter["title"])}'
            f' <span class="pages">p.{chapter["start"]}–{chapter["end"]}</span></h2>\n'
            f"    {fragments[index]}\n{chapter_figures(index)}{extra}  </section>\n"
        )

    html = HTML_HEAD.format(
        title=_escape(title),
        source=_escape(os.path.basename(manifest["source"])),
        pages=manifest["pages"],
        chapters=sum(1 for c in chapters if c["index"] > 0),
        strategy=_escape(str(manifest.get("strategy", "unknown"))),
        created=datetime.now(timezone.utc).strftime("%Y-%m-%d"),
        toc="\n".join(toc_lines),
    ) + "\n".join(sections) + HTML_TAIL

    final_problems = validate_final_html(html)
    if final_problems:
        raise ExtractError("生成 HTML の検証に失敗しました:\n- " + "\n- ".join(final_problems))

    output_path = os.path.join(out_dir, f"{title}_summary.html")
    with open(output_path, "w", encoding="utf-8") as handle:
        handle.write(html)
    return output_path


FORBIDDEN_HTML_TAGS = {
    "script", "iframe", "object", "embed", "form", "input", "textarea", "select",
    "button", "video", "audio", "link", "base", "source",
}
ALLOWED_IMG_SRC = re.compile(r"data:image/(png|jpeg);base64,[A-Za-z0-9+/=]+")
ALLOWED_ANCHOR = re.compile(r"#[A-Za-z0-9_-]+")


class _FinalHTMLParser(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.problems = []
        self.stack = []

    def handle_starttag(self, tag, attrs):
        tag = tag.lower()
        inside_svg = "svg" in self.stack
        if tag in FORBIDDEN_HTML_TAGS:
            self.problems.append(f"禁止タグが含まれています: <{tag}>")
        for name, value in attrs:
            key = name.lower()
            text = (value or "").strip()
            if key.startswith("on"):
                self.problems.append(f"イベント属性が含まれています: {key}")
            elif key == "style":
                # SVG 内の style 属性は validate_svg が検証する
                if not inside_svg:
                    self.problems.append("style 属性が含まれています")
            elif key == "src" and not ALLOWED_IMG_SRC.fullmatch(text):
                self.problems.append("src が data:image(png/jpeg) ではありません")
            elif key in ("href", "xlink:href") and not ALLOWED_ANCHOR.fullmatch(text):
                self.problems.append(f"href が内部アンカーではありません: {text[:40]}")
            elif key in ("action", "formaction", "poster", "background", "srcset"):
                self.problems.append(f"危険な属性が含まれています: {key}")
        if tag not in VOID_TAGS:
            self.stack.append(tag)

    def handle_endtag(self, tag):
        tag = tag.lower()
        if self.stack and self.stack[-1] == tag:
            self.stack.pop()
        elif tag in self.stack:
            while self.stack and self.stack.pop() != tag:
                pass

    def handle_comment(self, data):
        self.problems.append("HTML コメントが含まれています")


def validate_final_html(html):
    parser = _FinalHTMLParser()
    parser.feed(html)
    parser.close()
    return parser.problems


# ---------------------------------------------------------------- CLI


def main(argv=None):
    parser = argparse.ArgumentParser(description="PDF の要約版（単一 HTML）を作る")
    sub = parser.add_subparsers(dest="command", required=True)

    extract_parser = sub.add_parser("extract", help="章検出と本文抽出")
    extract_parser.add_argument("pdf", help="入力 PDF")
    extract_parser.add_argument("--out-dir", help="出力先（既定: work/summary/<PDF名>）")
    extract_parser.add_argument("--force", action="store_true", help="既存の出力を上書き")
    extract_parser.add_argument("--dry-run", action="store_true", help="章検出の結果だけ表示")
    extract_parser.add_argument("--profile", choices=sorted(PROFILES), default="generic")
    extract_parser.add_argument("--chapters", help="章定義 JSON（手動指定）")
    extract_parser.add_argument("--toc-pages", help="目次ページ（1 始まり、カンマ区切り）")
    extract_parser.add_argument("--offset", type=int, help="印刷ページ番号と物理ページのズレ")

    prompt_parser = sub.add_parser("prompt", help="LLM への指示文を生成")
    prompt_parser.add_argument("dir", help="extract の出力ディレクトリ")
    prompt_parser.add_argument("--language", default="ja")

    check_parser = sub.add_parser("check", help="要約断片と図解を検証")
    check_parser.add_argument("dir", help="extract の出力ディレクトリ")

    figures_parser = sub.add_parser("figures", help="図版を抽出して figures/ に保存")
    figures_parser.add_argument("pdf", help="入力 PDF")
    figures_parser.add_argument("dir", help="extract の出力ディレクトリ")
    figures_parser.add_argument("--per-chapter", type=int, default=2)
    figures_parser.add_argument("--max-total-mb", type=float, default=5.0)
    figures_parser.add_argument("--force", action="store_true")

    assemble_parser = sub.add_parser("assemble", help="単一 HTML に結合")
    assemble_parser.add_argument("dir", help="extract の出力ディレクトリ")
    assemble_parser.add_argument(
        "--embed-figures", action="store_true", help="抽出した図版を HTML に埋め込む（既定は埋め込まない）"
    )

    args = parser.parse_args(argv)

    try:
        if args.command == "extract":
            out_dir = args.out_dir or os.path.join(
                "work", "summary", os.path.splitext(os.path.basename(args.pdf))[0]
            )
            toc_pages = None
            if args.toc_pages:
                try:
                    toc_pages = [int(x) - 1 for x in args.toc_pages.split(",") if x.strip()]
                except ValueError as exc:
                    raise ExtractError("--toc-pages はカンマ区切りのページ番号で指定してください") from exc
            options = {
                "force": args.force,
                "profile": args.profile,
                "manual": args.chapters,
                "toc_pages": toc_pages,
                "offset": args.offset,
            }
            manifest = extract(args.pdf, out_dir, options, dry_run=args.dry_run)
            if manifest is None:
                return 0
            print(
                f"{manifest['pages']} ページ / 章 {len(manifest['chapters'])} 件"
                f"（方式: {manifest['strategy']}, 確信度: {manifest['confidence']}） -> {out_dir}"
            )
            for chapter in manifest["chapters"]:
                print(
                    f"  {chapter['index']:02d} p.{chapter['start']:>3}-{chapter['end']:<3} "
                    f"{chapter['chars']:>6}字 {chapter['title'][:50]}"
                )
        elif args.command == "figures":
            target = os.path.join(args.dir, "figures.json")
            if os.path.exists(target) and not args.force:
                raise ExtractError("figures.json が既にあります（--force で再抽出）")
            result = figures_module.extract_figures(
                args.pdf,
                args.dir,
                per_chapter=args.per_chapter,
                max_total_bytes=int(args.max_total_mb * 1024 * 1024),
            )
            stats = result["stats"]
            print(
                f"図版 {stats['selected']} 枚 / {stats['bytes']/1e6:.1f}MB を抽出"
                f"（候補 {stats['candidates']} 枚） -> {os.path.join(args.dir, 'figures')}"
            )
        elif args.command == "prompt":
            path = write_prompt(args.dir, args.language)
            print(f"指示文を書き出しました: {path}")
        elif args.command == "check":
            problems = check(args.dir)
            if problems:
                print(f"検証で {len(problems)} 件の問題: ")
                for problem in problems:
                    print(f"  - {problem}")
                return 1
            print("検証 OK")
        else:
            output = assemble(args.dir, embed_figures=args.embed_figures)
            print(f"要約 HTML を書き出しました: {output}")
    except ExtractError as exc:
        print(f"エラー: {exc}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
