"""PDF 内の図版（画像 XObject）を抽出し、単一 HTML に埋め込める形にする。

- ページ直下だけでなく Form XObject の中も再帰的に探す
- JPEG（DCTDecode）はバイト列をそのまま、RGB/グレースケールの FlateDecode は PNG に変換する
- サイズ・縦横比・テンプレート部品（同寸法の多用）・同一画像の使い回しで装飾を除外する
- 抽出結果は figures/ と figures.json に記録し、assemble が base64 で HTML に埋め込む
"""
import hashlib
import json
import os
import struct
import zlib
from collections import Counter

from pypdf import PdfReader

MIN_WIDTH = 80
MIN_HEIGHT = 80
MIN_AREA = 40000
MAX_ASPECT = 8.0
MAX_SAME_DIMENSIONS = 5
MAX_SAME_IMAGE = 3
MAX_FIGURE_BYTES = 2 * 1024 * 1024
SAME_DIMENSIONS_TOLERANCE = True


def iter_image_objects(page, depth_limit=4):
    """ページと入れ子の Form XObject から画像 XObject を列挙する。"""
    found = []

    def walk(obj, depth=0, seen=None):
        if seen is None:
            seen = set()
        if depth > depth_limit or id(obj) in seen:
            return
        seen.add(id(obj))
        resources = obj.get("/Resources") if hasattr(obj, "get") else None
        xobjects = resources.get("/XObject") if resources else None
        if not xobjects:
            return
        for name, reference in xobjects.items():
            sub = reference.get_object()
            subtype = sub.get("/Subtype")
            if subtype == "/Image":
                found.append((str(name), sub))
            elif subtype == "/Form":
                walk(sub, depth + 1, seen)

    walk(page)
    return found


def png_encode_rgb(width, height, rgb):
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + rgb[y * width * 3:(y + 1) * width * 3] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def png_encode_gray(width, height, gray):
    def chunk(tag, data):
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + gray[y * width:(y + 1) * width] for y in range(height))
    header = struct.pack(">IIBBBBB", width, height, 8, 0, 0, 0, 0)
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", header)
        + chunk(b"IDAT", zlib.compress(raw, 6))
        + chunk(b"IEND", b"")
    )


def decode_image(image):
    """画像 XObject を (拡張子, バイト列) に変換する。非対応は None。"""
    width = int(image.get("/Width") or 0)
    height = int(image.get("/Height") or 0)
    if width <= 0 or height <= 0:
        return None
    filters = str(image.get("/Filter") or "")
    if filters == "/DCTDecode":
        raw = getattr(image, "_data", b"") or b""
        if raw[:2] == b"\xff\xd8":
            return "jpg", raw
        return None

    try:
        raw = image.get_data()
    except Exception:
        return None

    if filters == "/FlateDecode":
        color_space = str(image.get("/ColorSpace"))
        bits = int(image.get("/BitsPerComponent") or 8)
        if bits != 8:
            return None
        if color_space in ("/DeviceRGB", "/CalRGB"):
            if len(raw) < width * height * 3:
                return None
            return "png", png_encode_rgb(width, height, raw[: width * height * 3])
        if color_space in ("/DeviceGray", "/CalGray"):
            if len(raw) < width * height:
                return None
            return "png", png_encode_gray(width, height, raw[: width * height])
    return None


def _hash(data):
    return hashlib.sha1(data).hexdigest()


def collect_candidates(reader):
    """画像のメタ情報を集める（デコードは選抜後に行う）。"""
    candidates = []
    for page_index, page in enumerate(reader.pages):
        for name, image in iter_image_objects(page):
            width = int(image.get("/Width") or 0)
            height = int(image.get("/Height") or 0)
            if width <= 0 or height <= 0:
                continue
            data = image._data if hasattr(image, "_data") else b""
            candidates.append(
                {
                    "page": page_index + 1,
                    "name": name,
                    "width": width,
                    "height": height,
                    "bytes": len(data),
                    "filter": str(image.get("/Filter")),
                    "hash": _hash(data),
                    "image": image,
                }
            )
    return candidates


def select_candidates(candidates, chapters, per_chapter=2, max_total_bytes=5 * 1024 * 1024,
                      min_width=MIN_WIDTH, min_height=MIN_HEIGHT, min_area=MIN_AREA):
    """装飾を除外し、章あたりの上限と全体バイト上限で選抜する。"""
    dimensions = Counter((c["width"], c["height"]) for c in candidates)
    hashes = Counter(c["hash"] for c in candidates)

    def page_to_chapter(page):
        for chapter in chapters:
            if chapter["start"] <= page <= chapter["end"]:
                return chapter["index"]
        return None

    filtered = []
    for candidate in candidates:
        width, height = candidate["width"], candidate["height"]
        if width < min_width or height < min_height:
            continue
        if width * height < min_area:
            continue
        if max(width / height, height / width) > MAX_ASPECT:
            continue
        if dimensions[(width, height)] > MAX_SAME_DIMENSIONS:
            continue
        if hashes[candidate["hash"]] > MAX_SAME_IMAGE:
            continue
        if candidate["bytes"] > MAX_FIGURE_BYTES:
            continue
        chapter_index = page_to_chapter(candidate["page"])
        if chapter_index is None:
            continue
        candidate = dict(candidate)
        candidate["chapter"] = chapter_index
        filtered.append(candidate)

    by_chapter = {}
    for candidate in filtered:
        by_chapter.setdefault(candidate["chapter"], []).append(candidate)

    selected = []
    for chapter_index in sorted(by_chapter):
        ranked = sorted(
            by_chapter[chapter_index], key=lambda c: (-c["width"] * c["height"], c["page"])
        )
        selected.extend(ranked[:per_chapter])
    selected.sort(key=lambda c: (c["chapter"], c["page"]))

    total = 0
    capped = []
    for candidate in selected:
        payload = decode_image(candidate["image"])
        if payload is None:
            continue
        extension, data = payload
        if len(data) > MAX_FIGURE_BYTES:
            continue
        if total + len(data) > max_total_bytes:
            break
        total += len(data)
        candidate = dict(candidate)
        candidate["extension"] = extension
        candidate["data"] = data
        capped.append(candidate)
    return capped, {
        "candidates": len(candidates),
        "selected": len(capped),
        "bytes": total,
    }


def extract_figures(pdf_path, out_dir, per_chapter=2, max_total_bytes=5 * 1024 * 1024):
    manifest_path = os.path.join(out_dir, "manifest.json")
    if not os.path.isfile(manifest_path):
        raise ValueError(f"manifest.json がありません: {manifest_path}")
    with open(manifest_path, encoding="utf-8") as handle:
        manifest = json.load(handle)
    chapters = manifest["chapters"]
    reference = PdfReader(pdf_path)
    candidates = collect_candidates(reference)
    selected, stats = select_candidates(
        candidates, chapters, per_chapter=per_chapter, max_total_bytes=max_total_bytes
    )

    figures_dir = os.path.join(out_dir, "figures")
    os.makedirs(figures_dir, exist_ok=True)
    entries = []
    for number, candidate in enumerate(selected, start=1):
        filename = (
            f"ch-{candidate['chapter']:02d}-p{candidate['page']:03d}-{number:02d}."
            f"{candidate['extension']}"
        )
        path = os.path.join(figures_dir, filename)
        with open(path, "wb") as handle:
            handle.write(candidate["data"])
        entries.append(
            {
                "chapter": candidate["chapter"],
                "page": candidate["page"],
                "file": f"figures/{filename}",
                "format": candidate["extension"],
                "width": candidate["width"],
                "height": candidate["height"],
                "bytes": len(candidate["data"]),
            }
        )

    figures = {"source": manifest["source"], "stats": stats, "figures": entries}
    with open(os.path.join(out_dir, "figures.json"), "w", encoding="utf-8") as handle:
        json.dump(figures, handle, ensure_ascii=False, indent=2)
    return figures


def load_figures(out_dir):
    path = os.path.join(out_dir, "figures.json")
    if not os.path.isfile(path):
        return None
    try:
        with open(path, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"figures.json を読めません: {exc}") from exc
    if not isinstance(data, dict) or not isinstance(data.get("figures"), list):
        raise ValueError("figures.json の形式が不正です")
    for entry in data["figures"]:
        if not isinstance(entry, dict):
            raise ValueError("figures.json の要素が不正です")
    return data


def validate_figures(out_dir):
    """figures.json と実ファイルの整合性・安全性を検証する。"""
    problems = []
    try:
        figures = load_figures(out_dir)
    except ValueError as exc:
        return [str(exc)]
    if figures is None:
        return problems
    chapter_indexes = set()
    manifest_path = os.path.join(out_dir, "manifest.json")
    if os.path.isfile(manifest_path):
        with open(manifest_path, encoding="utf-8") as handle:
            chapter_indexes = {c["index"] for c in json.load(handle)["chapters"]}
    for entry in figures.get("figures", []):
        if not isinstance(entry.get("chapter"), int):
            problems.append(f"figures.json の chapter が不正です: {entry.get('chapter')!r}")
            continue
        if not isinstance(entry.get("page"), int) or entry["page"] < 1:
            problems.append(f"figures.json の page が不正です: {entry.get('page')!r}")
            continue
        if not isinstance(entry.get("width"), int) or entry["width"] < 1:
            problems.append(f"figures.json の width が不正です: {entry.get('width')!r}")
            continue
        if not isinstance(entry.get("height"), int) or entry["height"] < 1:
            problems.append(f"figures.json の height が不正です: {entry.get('height')!r}")
            continue
        if not isinstance(entry.get("bytes"), int) or entry["bytes"] < 0:
            problems.append(f"figures.json の bytes が不正です: {entry.get('bytes')!r}")
            continue
        relative = str(entry.get("file", ""))
        if not relative or relative.startswith("/") or ".." in relative.split("/"):
            problems.append(f"figures.json の file が不正です: {relative!r}")
            continue
        path = os.path.join(out_dir, relative)
        if not os.path.isfile(path):
            problems.append(f"図版ファイルがありません: {relative}")
            continue
        if entry.get("chapter") not in chapter_indexes:
            problems.append(f"図版の章番号が manifest にありません: {relative}")
        extension = str(entry.get("format"))
        if extension not in ("png", "jpg"):
            problems.append(f"図版の形式が不正です: {relative} ({extension})")
            continue
        with open(path, "rb") as handle:
            head = handle.read(8)
        expected = b"\x89PNG\r\n\x1a\n" if extension == "png" else b"\xff\xd8\xff"
        if not head.startswith(expected):
            problems.append(f"図版の内容が形式と一致しません: {relative}")
        elif os.path.getsize(path) != entry["bytes"]:
            problems.append(f"図版のサイズが figures.json と一致しません: {relative}")
    return problems
