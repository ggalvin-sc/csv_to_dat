#!/usr/bin/env python3
"""
enrich_volume.py - Build a load-ready Concordance volume from a thin CSV production.

PAGE PURPOSE
------------
Takes a producing-party volume that has Bates + Filename only (no native paths,
no TEXTPATH) and builds a sibling VOL001_LOAD package with:

  * Bates-relative FILEPATH into NATIVES\\0001\\
  * Expanded child Bates for media under Items with Placeholders
  * TEXT\\0001\\{BEGDOC}.txt companions from existing STT .txt sidecars
  * Classic Concordance DAT/DCT + corrected OPT (IMAGES\\0001\\*.pdf)
  * build_report.md (coverage, Bates map, countervoice notes)

FUNCTIONS
---------
  parse_args()              CLI for source/output/repo paths and dry-run.
  bates_number()            Extract trailing integer from a Bates ID.
  format_bates()            Build Bates ID from prefix + number.
  index_natives()           Map Bates stem -> absolute native path.
  index_images()            Map Bates stem -> absolute image path.
  load_csv_rows()           Read thin volume CSV (Bates/Filename columns).
  placeholder_folder_map()  Explicit PLACEHOLDER filename -> media folder.
  list_media_files()        Recurse for mp4/mp3/wav/m4a under a folder.
  sidecar_txt()             Same-basename .txt beside a media file.
  copy_or_link()            Hardlink when possible, else copy2 with retries.
  ensure_dir()              mkdir -p helper.
  build_volume()            Main pipeline: inventory -> expand -> TEXT -> DAT.
  write_build_report()      Markdown report with coverage + countervoice.
  main()                    Entry point.
"""

from __future__ import annotations

import argparse
import csv
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

# ---------------------------------------------------------------------------
# Media / STT constants
# ---------------------------------------------------------------------------

MEDIA_EXTS = {".mp4", ".mp3", ".wav", ".m4a", ".avi", ".mov", ".mkv", ".wmv"}
SCHEMA_FIELDS = [
    "BEGDOC",
    "ENDDOC",
    "BEGATTACH",
    "ENDATTACH",
    "CUSTODIAN",
    "CODED",
    "FILEPATH",
    "FILENAME",
    "FILEEXT",
    "TEXTPATH",
    "DOCTYPE",
    "SOURCEFOLDER",
]

# Explicit map: CSV placeholder Filename (case-insensitive) -> folder under
# Items with Placeholders. Unmapped placeholders get no children.
PLACEHOLDER_FOLDER_MAP: Dict[str, Optional[str]] = {
    "placeholder - snapchat.docx": "SNAPCHAT",
    "placeholder - jail calls.docx": "JAIL CALLS",
    "placeholder - incident 1.21.24.docx": "Incident 1.21.24",
    "placeholder - incident 4.26.24 pt 1.docx": "Incident 4.26.24",
    "placeholder - incident 4.26.24 pt 2.docx": None,  # same folder as pt1; avoid dup
    "placeholder - incident 12.2.24.docx": "Incident 12.2.24",
    "placeholder - incident 12.5.24 body cam.docx": None,  # no media folder
    "placeholder - incident 12.5.24 in car video.docx": None,
    "placeholder - incident 12.5.24 911 recordings.docx": None,
}


@dataclass
class BuildStats:
    source_rows: int = 0
    natives_joined: int = 0
    natives_missing: List[str] = field(default_factory=list)
    images_joined: int = 0
    images_missing: List[str] = field(default_factory=list)
    placeholders: List[str] = field(default_factory=list)
    placeholders_unmapped: List[str] = field(default_factory=list)
    media_children: int = 0
    text_written: int = 0
    text_missing_media: List[str] = field(default_factory=list)
    copy_failures: List[str] = field(default_factory=list)
    new_bates_start: int = 0
    new_bates_end: int = 0
    child_map: List[Tuple[str, str, str, str]] = field(default_factory=list)
    # (child_bates, parent_bates, filename, textpath_or_blank)
    opt_lines: int = 0
    dat_rows: int = 0
    warnings: List[str] = field(default_factory=list)
    errors: List[str] = field(default_factory=list)


def parse_args(argv: Optional[Sequence[str]] = None) -> argparse.Namespace:
    """Parse CLI arguments for the volume enrichment pipeline."""
    p = argparse.ArgumentParser(
        description="Build a load-ready Concordance volume from a thin CSV production.",
    )
    p.add_argument(
        "--source",
        required=True,
        help="Source volume folder (contains VOL001.csv, NATIVES, IMAGES, Items with Placeholders)",
    )
    p.add_argument(
        "--output",
        default=None,
        help="Output VOL001_LOAD folder (default: <source>/VOL001_LOAD)",
    )
    p.add_argument(
        "--repo",
        default=None,
        help="csv_to_dat repo root (default: parent of this scripts/ folder)",
    )
    p.add_argument(
        "--csv-name",
        default="VOL001.csv",
        help="Thin volume CSV filename inside --source (default VOL001.csv)",
    )
    p.add_argument(
        "--volume",
        default="VOL001",
        help="OPT volume identifier (default VOL001)",
    )
    p.add_argument(
        "--skip-media-copy",
        action="store_true",
        help="Do not copy/link media natives (paths still written; for dry inventory)",
    )
    p.add_argument(
        "--dry-run",
        action="store_true",
        help="Inventory + enriched CSV + report only; skip DAT/OPT/file copies",
    )
    p.add_argument(
        "--link-mode",
        choices=("auto", "copy", "hardlink", "symlink"),
        default="auto",
        help="How to place files into NATIVES (auto=symlink/hardlink then copy)",
    )
    p.add_argument(
        "--layout",
        choices=("classic", "relative"),
        default="classic",
        help="classic=NATIVES\\0001\\{BEGDOC}ext under the DAT folder (default); "
        "relative=..\\ paths into the source volume",
    )
    return p.parse_args(argv)


def bates_number(bates: str) -> int:
    """Return the trailing integer portion of a Bates ID."""
    m = re.search(r"(\d+)$", bates.strip())
    if not m:
        raise ValueError(f"cannot parse Bates number from {bates!r}")
    return int(m.group(1))


def format_bates(prefix: str, number: int, width: int = 6) -> str:
    """Format a Bates ID as prefix + zero-padded number."""
    return f"{prefix}{number:0{width}d}"


def bates_prefix(bates: str) -> str:
    """Return the non-numeric prefix of a Bates ID (including trailing underscore)."""
    m = re.search(r"^(.*?)(\d+)$", bates.strip())
    if not m:
        raise ValueError(f"cannot parse Bates prefix from {bates!r}")
    return m.group(1)


def ensure_dir(path: Path) -> None:
    """Create directory and parents if missing."""
    path.mkdir(parents=True, exist_ok=True)


def index_by_stem(folder: Path) -> Dict[str, Path]:
    """Map file stem (case-sensitive as on disk) -> Path for all files under folder."""
    out: Dict[str, Path] = {}
    if not folder.is_dir():
        return out
    for p in folder.rglob("*"):
        if p.is_file():
            out[p.stem] = p
    return out


def load_csv_rows(csv_path: Path) -> Tuple[List[str], List[Dict[str, str]]]:
    """Load the thin volume CSV; normalize known Bates/Filename column aliases."""
    with csv_path.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        if not reader.fieldnames:
            raise ValueError(f"empty CSV header: {csv_path}")
        fieldnames = list(reader.fieldnames)
        rows = [dict(r) for r in reader]

    # Resolve column names case-insensitively.
    lower = {c.lower(): c for c in fieldnames}

    def col(*aliases: str) -> Optional[str]:
        for a in aliases:
            if a.lower() in lower:
                return lower[a.lower()]
        return None

    beg_col = col("Bates/Control #", "BEGDOC", "BegDoc", "Control Number")
    end_col = col("End Bates/Control #", "ENDDOC", "EndDoc")
    coded_col = col("Coded", "CODED")
    name_col = col("Filename", "FILEPATH", "Native File", "File Name")
    if not beg_col or not name_col:
        raise ValueError(
            f"CSV must have Bates and Filename columns; got {fieldnames!r}"
        )
    normalized: List[Dict[str, str]] = []
    for r in rows:
        beg = (r.get(beg_col) or "").strip()
        end = (r.get(end_col) or "").strip() if end_col else beg
        if not end:
            end = beg
        coded = (r.get(coded_col) or "").strip() if coded_col else ""
        filename = (r.get(name_col) or "").strip()
        normalized.append(
            {
                "BEGDOC": beg,
                "ENDDOC": end,
                "CODED": coded,
                "FILENAME": filename,
            }
        )
    return fieldnames, normalized


def placeholder_folder_map() -> Dict[str, Optional[str]]:
    """Return the explicit placeholder Filename -> folder map (lowercase keys)."""
    return dict(PLACEHOLDER_FOLDER_MAP)


def list_media_files(folder: Path) -> List[Path]:
    """Return media files under folder, sorted by relative path for stable Bates order."""
    if not folder.is_dir():
        return []
    found: List[Path] = []
    for p in folder.rglob("*"):
        if p.is_file() and p.suffix.lower() in MEDIA_EXTS:
            found.append(p)
    found.sort(key=lambda x: str(x).lower())
    return found


def sidecar_transcript(media: Path) -> Optional[Path]:
    """Return preferred STT sidecar: non-empty .txt, else .srt (never .json)."""
    txt = media.with_suffix(".txt")
    if txt.is_file() and txt.stat().st_size > 0:
        return txt
    srt = media.with_suffix(".srt")
    if srt.is_file() and srt.stat().st_size > 0:
        return srt
    # Empty .txt with usable .srt — prefer SRT.
    if txt.is_file() and srt.is_file() and srt.stat().st_size > 0:
        return srt
    return None


# Keep old name as alias for any external callers.
def sidecar_txt(media: Path) -> Optional[Path]:
    """Compatibility wrapper — prefer sidecar_transcript()."""
    return sidecar_transcript(media)


def copy_or_link(
    src: Path,
    dst: Path,
    mode: str = "auto",
    retries: int = 3,
    *,
    max_copy_bytes: int = 50 * 1024 * 1024,
) -> str:
    """
    Place src at dst via hardlink, symlink, or copy2.

    Order for mode=auto: hardlink -> symlink -> copy2 (copy skipped when
    src is larger than max_copy_bytes — raises instead so callers can
    fall back to an alternate FILEPATH).

    Returns 'hardlink', 'symlink', 'copy', or 'exists'.
    """
    ensure_dir(dst.parent)
    if dst.exists() or dst.is_symlink():
        try:
            if dst.resolve() == src.resolve():
                return "exists"
            if dst.is_file() and dst.stat().st_size == src.stat().st_size:
                return "exists"
        except OSError:
            pass
        try:
            dst.unlink()
        except OSError:
            if dst.is_dir():
                shutil.rmtree(dst)
            else:
                raise

    last_err: Optional[BaseException] = None
    if mode in ("auto", "hardlink"):
        try:
            os.link(str(src), str(dst))
            return "hardlink"
        except OSError as exc:
            last_err = exc
            if mode == "hardlink":
                raise

    if mode in ("auto", "symlink"):
        try:
            os.symlink(str(src), str(dst))
            return "symlink"
        except OSError as exc:
            last_err = exc
            if mode == "symlink":
                raise

    if mode == "copy" or mode == "auto":
        size = src.stat().st_size
        if mode == "auto" and size > max_copy_bytes:
            raise OSError(
                f"refusing to copy {size} byte file {src.name!r} "
                f"(>{max_copy_bytes}); hardlink/symlink failed: {last_err}"
            )
        for attempt in range(1, retries + 1):
            try:
                shutil.copy2(str(src), str(dst))
                return "copy"
            except OSError as exc:
                last_err = exc
                if attempt == retries:
                    break
    assert last_err is not None
    raise last_err


def rel_between(from_dir: Path, to_path: Path) -> str:
    """Return a backslash relative path from from_dir to to_path."""
    rel = os.path.relpath(str(to_path), str(from_dir))
    return rel.replace("/", "\\")


def ensure_junction_or_link(link: Path, target: Path) -> str:
    """
    Make ``link`` a directory junction (Windows) or symlink to ``target``.

    Returns 'junction', 'symlink', or 'exists'.
    Raises OSError if the filesystem cannot create links (common on Google Drive).
    """
    ensure_dir(link.parent)
    if link.exists() or link.is_symlink():
        try:
            if link.resolve() == target.resolve():
                return "exists"
        except OSError:
            pass
        if link.is_dir() and not link.is_symlink():
            shutil.rmtree(link)
        else:
            link.unlink()

    if os.name == "nt":
        import subprocess

        completed = subprocess.run(
            ["cmd", "/c", "mklink", "/J", str(link), str(target)],
            capture_output=True,
            text=True,
        )
        if completed.returncode == 0:
            return "junction"
        raise OSError(
            completed.stderr.strip()
            or completed.stdout.strip()
            or f"mklink /J failed for {link} -> {target}"
        )
    os.symlink(str(target), str(link), target_is_directory=True)
    return "symlink"


def place_file(src: Path, dst: Path, mode: str = "auto") -> str:
    """
    Place ``src`` at ``dst`` for a classic Concordance NATIVES tree.

    Prefer symlink (works from local NTFS -> Google Drive), then hardlink,
    then copy. Returns the method used.
    """
    ensure_dir(dst.parent)
    if dst.exists() or dst.is_symlink():
        try:
            if dst.resolve() == src.resolve():
                return "exists"
        except OSError:
            pass
        try:
            dst.unlink()
        except OSError:
            raise

    last_err: Optional[BaseException] = None
    # Symlink first — required when source is Google Drive and output is local NTFS.
    if mode in ("auto", "symlink"):
        try:
            os.symlink(str(src), str(dst))
            return "symlink"
        except OSError as exc:
            last_err = exc
            if mode == "symlink":
                raise
    if mode in ("auto", "hardlink"):
        try:
            os.link(str(src), str(dst))
            return "hardlink"
        except OSError as exc:
            last_err = exc
            if mode == "hardlink":
                raise
    if mode in ("auto", "copy"):
        try:
            shutil.copy2(str(src), str(dst))
            return "copy"
        except OSError as exc:
            last_err = exc
    assert last_err is not None
    raise last_err


def rel_native_path(bates: str, ext: str) -> str:
    """Relative Concordance native path (backslash, no leading .\\)."""
    if not ext.startswith("."):
        ext = f".{ext}"
    return f"NATIVES\\0001\\{bates}{ext}"


def rel_text_path(bates: str) -> str:
    """Relative Concordance TEXTPATH."""
    return f"TEXT\\0001\\{bates}.txt"


def rel_image_path(bates: str, ext: str = ".pdf") -> str:
    """Relative Opticon image path."""
    if not ext.startswith("."):
        ext = f".{ext}"
    return f"IMAGES\\0001\\{bates}{ext}"


def doctype_for(filename: str, is_child: bool, media_ext: str = "") -> str:
    """Classify DOCTYPE for the load file."""
    if is_child:
        ext = media_ext.lower()
        if ext in {".mp3", ".wav", ".m4a"}:
            return "AUDIO"
        return "MEDIA"
    if filename.lower().startswith("placeholder"):
        return "PLACEHOLDER"
    return "NATIVE"


_SRT_TS_RE = re.compile(
    r"^\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}"
)
_SRT_INDEX_RE = re.compile(r"^\d+$")
# Concordance DAT control characters must never appear in extracted-text bodies.
_DAT_CTRL_RE = re.compile("[\x14\xfe\xae]")


def srt_to_plain_text(text: str) -> str:
    """Strip SRT sequence numbers and timestamps; keep dialogue lines only."""
    out: List[str] = []
    for line in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        s = line.strip()
        if not s:
            continue
        if _SRT_INDEX_RE.match(s):
            continue
        if _SRT_TS_RE.match(s):
            continue
        out.append(s)
    return "\n".join(out)


def normalize_extracted_text(text: str, *, from_srt: bool = False) -> str:
    """
    Normalize transcript text for Concordance/Relativity TEXTPATH companions.

    - UTF-8 body (caller writes without BOM)
    - Windows CRLF newlines
    - Plain dialogue only (SRT timing stripped when from_srt)
    - Strip Concordance control chars 0x14 / 0xFE / 0xAE
    """
    if from_srt:
        text = srt_to_plain_text(text)
    else:
        text = text.replace("\r\n", "\n").replace("\r", "\n")
    text = _DAT_CTRL_RE.sub("", text)
    # Collapse excessive blank lines but keep paragraph breaks.
    lines = [ln.rstrip() for ln in text.split("\n")]
    cleaned: List[str] = []
    blank_run = 0
    for ln in lines:
        if not ln.strip():
            blank_run += 1
            if blank_run <= 1:
                cleaned.append("")
            continue
        blank_run = 0
        cleaned.append(ln)
    text = "\n".join(cleaned).strip()
    if text:
        text += "\n"
    # CRLF for Windows Concordance loaders.
    return text.replace("\n", "\r\n")


def write_text_companion(src_txt: Path, dst_txt: Path) -> int:
    """
    Write a Concordance extracted-text companion from an STT sidecar.

    Returns byte length written. Raises ValueError if the result would be empty
    after normalization (caller should leave TEXTPATH blank).
    """
    ensure_dir(dst_txt.parent)
    raw = src_txt.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    from_srt = src_txt.suffix.lower() == ".srt"
    normalized = normalize_extracted_text(text, from_srt=from_srt)
    if not normalized.strip():
        raise ValueError(f"empty transcript after normalize: {src_txt}")

    # UTF-8 without BOM (Relativity text-file import default).
    data = normalized.encode("utf-8")  # no BOM
    dst_txt.write_bytes(data)
    return len(data)


def write_enriched_csv(path: Path, rows: List[Dict[str, str]]) -> None:
    """Write enriched CSV with SCHEMA_FIELDS header."""
    ensure_dir(path.parent)
    with path.open("w", encoding="utf-8", newline="") as fh:
        writer = csv.DictWriter(fh, fieldnames=SCHEMA_FIELDS, extrasaction="ignore")
        writer.writeheader()
        for r in rows:
            writer.writerow({k: r.get(k, "") for k in SCHEMA_FIELDS})


def write_build_report(
    path: Path,
    stats: BuildStats,
    source: Path,
    output: Path,
    placeholder_map_used: Dict[str, Optional[str]],
) -> None:
    """Write build_report.md with coverage, Bates map, and countervoice notes."""
    lines: List[str] = []
    lines.append("# VOL001_LOAD build report")
    lines.append("")
    lines.append(f"- Generated: `{datetime.now(timezone.utc).isoformat()}`")
    lines.append(f"- Source: `{source}`")
    lines.append(f"- Output: `{output}`")
    lines.append("")
    lines.append("## Summary")
    lines.append("")
    lines.append(f"| Metric | Value |")
    lines.append(f"|---|---|")
    lines.append(f"| Source CSV rows | {stats.source_rows} |")
    lines.append(f"| Natives joined | {stats.natives_joined} |")
    lines.append(f"| Natives missing | {len(stats.natives_missing)} |")
    lines.append(f"| Images joined | {stats.images_joined} |")
    lines.append(f"| Images missing | {len(stats.images_missing)} |")
    lines.append(f"| Placeholder parents | {len(stats.placeholders)} |")
    lines.append(f"| Placeholders with no media folder | {len(stats.placeholders_unmapped)} |")
    lines.append(f"| Media children added | {stats.media_children} |")
    lines.append(f"| TEXT files written | {stats.text_written} |")
    lines.append(f"| Media missing STT .txt | {len(stats.text_missing_media)} |")
    lines.append(f"| New Bates range | {stats.new_bates_start}–{stats.new_bates_end} |")
    lines.append(f"| DAT rows | {stats.dat_rows} |")
    lines.append(f"| OPT lines | {stats.opt_lines} |")
    lines.append(f"| Copy failures | {len(stats.copy_failures)} |")
    lines.append("")

    if stats.media_children:
        pct = (
            100.0 * stats.text_written / stats.media_children
            if stats.media_children
            else 0.0
        )
        lines.append(f"**STT coverage (media children):** {stats.text_written}/{stats.media_children} ({pct:.1f}%)")
        lines.append("")

    lines.append("## Placeholder → folder map")
    lines.append("")
    lines.append("| Placeholder Filename | Media folder |")
    lines.append("|---|---|")
    for k, v in sorted(placeholder_map_used.items()):
        lines.append(f"| `{k}` | `{v if v else '(none — no children)'}` |")
    lines.append("")

    if stats.placeholders_unmapped:
        lines.append("### Unmapped / no-media placeholders")
        lines.append("")
        for p in stats.placeholders_unmapped:
            lines.append(f"- `{p}`")
        lines.append("")

    lines.append("## Child Bates map (sample / full)")
    lines.append("")
    lines.append("| Child Bates | Parent | Filename | TEXTPATH |")
    lines.append("|---|---|---|---|")
    for child, parent, fname, tpath in stats.child_map:
        safe_fname = fname.replace("|", "/")
        lines.append(f"| `{child}` | `{parent}` | `{safe_fname}` | `{tpath or ''}` |")
    lines.append("")

    if stats.text_missing_media:
        lines.append("## Media without STT .txt (TEXTPATH blank)")
        lines.append("")
        for item in stats.text_missing_media[:200]:
            lines.append(f"- `{item}`")
        if len(stats.text_missing_media) > 200:
            lines.append(f"- … and {len(stats.text_missing_media) - 200} more")
        lines.append("")

    if stats.natives_missing:
        lines.append("## Missing natives")
        lines.append("")
        for b in stats.natives_missing:
            lines.append(f"- `{b}`")
        lines.append("")

    if stats.images_missing:
        lines.append("## Missing images (no OPT line emitted)")
        lines.append("")
        for b in stats.images_missing[:100]:
            lines.append(f"- `{b}`")
        lines.append("")

    if stats.copy_failures:
        lines.append("## Copy / link failures")
        lines.append("")
        for c in stats.copy_failures:
            lines.append(f"- {c}")
        lines.append("")

    if stats.warnings:
        lines.append("## Warnings")
        lines.append("")
        for w in stats.warnings:
            lines.append(f"- {w}")
        lines.append("")

    if stats.errors:
        lines.append("## Errors")
        lines.append("")
        for e in stats.errors:
            lines.append(f"- {e}")
        lines.append("")

    lines.append("## Countervoice notes")
    lines.append("")
    lines.append("| Risk | Mitigation applied |")
    lines.append("|---|---|")
    lines.append("| Bates expansion vs producing party 342 | Original Bates kept; children start after max existing number |")
    lines.append("| Wrong placeholder→folder map | Explicit map in script; unmapped listed above |")
    lines.append("| OPT wrong folder/ext | OPT regenerated as `IMAGES\\0001\\{BEGDOC}.pdf` only when image exists |")
    lines.append("| FILEPATH was original Filename | Classic layout: `NATIVES\\0001\\{BEGDOC}{ext}` for every row |")
    lines.append("| Inline STT in cp1252 DAT | TEXTPATH → UTF-8 (no BOM) `TEXT\\0001\\{BEGDOC}.txt`, CRLF, plain transcript, DAT control chars stripped |")
    lines.append("| SRT/JSON sidecars | Prefer `.txt`; if only `.srt`, strip timestamps; never load `.json` into TEXT |")
    lines.append("| Parent-relative `..\\` paths | Classic layout uses only paths under the DAT folder |")
    lines.append("| Long Snapchat filenames | Media natives Bates-renamed under `NATIVES\\0001\\` (original name kept in FILENAME) |")
    lines.append("| Multi-GB media / Google Drive | Prefer local NTFS output with symlinks/junctions into the source volume |")
    lines.append("| Incident 4.26.24 pt 2 | Same folder as pt 1 — children attached only to pt 1 to avoid duplicates |")
    lines.append("| 12.5.24 placeholders | No media folders present — parents only, blank TEXTPATH |")
    lines.append("| Family integrity | Children set BEGATTACH/ENDATTACH = parent Bates |")
    lines.append("| Case data on GitHub | Output stays under case/local build folder; not committed to csv_to_dat |")
    lines.append("")
    lines.append("## Load checklist")
    lines.append("")
    lines.append("1. Point Relativity/Concordance at `VOL001.dat` in this folder (cp1252, Concordance delimiters).")
    lines.append("2. Map `FILEPATH` → Native File (`NATIVES\\0001\\…`); `TEXTPATH` → Extracted Text as **file path**.")
    lines.append("3. Load Opticon `VOL001.opt` (`IMAGES\\0001\\{BEGDOC}.pdf` — PDFs, not TIFF).")
    lines.append("4. Media children without images are native-only (no OPT line).")
    lines.append("5. If NATIVES/IMAGES are symlinks/junctions, keep the source volume reachable when loading.")
    lines.append("")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def run_csv_to_dat(
    repo: Path,
    enriched_csv: Path,
    dat_out: Path,
    schema_file: Path,
) -> None:
    """Invoke csv_to_dat csv2dat on the enriched CSV."""
    sys.path.insert(0, str(repo))
    from converter import DatConfig, csv_to_dat  # type: ignore

    field_names = [
        ln.strip()
        for ln in schema_file.read_text(encoding="utf-8").splitlines()
        if ln.strip() and not ln.strip().startswith("#")
    ]
    # Classic Concordance: field names live in the .dct only — no DAT header row.
    csv_to_dat(
        str(enriched_csv),
        str(dat_out),
        DatConfig(),
        field_names=field_names,
        emit_dct=True,
        emit_header=False,
        csv_encoding="utf-8",
        merge_schema=True,
        strict_merge=True,
    )


def run_opt(
    repo: Path,
    dat_path: Path,
    opt_out: Path,
    volume: str,
    image_dir: str,
    image_ext: str,
) -> int:
    """Generate Opticon companion; return lines written."""
    sys.path.insert(0, str(repo))
    from converter import DEFAULT_CONFIG, dat_to_opt  # type: ignore

    return dat_to_opt(
        str(dat_path),
        str(opt_out),
        config=DEFAULT_CONFIG,
        volume=volume,
        image_ext=image_ext,
        pages_per_doc=1,
        image_dir=image_dir,
    )


def run_validate(
    repo: Path,
    dat_path: Path,
    natives_root: Path,
    field_names: Sequence[str],
) -> str:
    """Validate DAT; return report string."""
    sys.path.insert(0, str(repo))
    from converter import DEFAULT_CONFIG, validate_dat_file  # type: ignore

    report = validate_dat_file(
        str(dat_path),
        field_names,
        DEFAULT_CONFIG,
        check_filepath_exists=True,
        natives_root=str(natives_root),
    )
    return report.summary() if hasattr(report, "summary") else str(report)


def build_volume(args: argparse.Namespace) -> int:
    """
    Main pipeline: inventory → expand families → TEXT → enriched CSV → DAT/OPT.

    Classic layout (default): FILEPATH = NATIVES\\0001\\{BEGDOC}{ext},
    OPT = IMAGES\\0001\\{BEGDOC}.pdf, TEXTPATH = TEXT\\0001\\{BEGDOC}.txt.
    Prefer --output on local NTFS with junctions/symlinks into a Google Drive source.

    Returns process exit code (0 success, 1 on hard errors).
    """
    source = Path(args.source).resolve()
    layout = getattr(args, "layout", "classic")
    if args.output:
        output = Path(args.output).resolve()
    else:
        output = source / "VOL001_LOAD"
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]
    schema_file = repo / "schemas" / "davila_load.txt"
    csv_path = source / args.csv_name
    placeholders_root = source / "Items with Placeholders"
    natives_src = source / "NATIVES"
    images_src = source / "IMAGES"

    stats = BuildStats()
    ph_map = placeholder_folder_map()

    if not csv_path.is_file():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 1
    if not schema_file.is_file():
        print(f"ERROR: schema not found: {schema_file}", file=sys.stderr)
        return 1

    ensure_dir(output)
    text_out = output / "TEXT" / "0001"
    natives_out = output / "NATIVES" / "0001"
    images_out = output / "IMAGES" / "0001"
    ensure_dir(text_out)

    _, thin_rows = load_csv_rows(csv_path)
    stats.source_rows = len(thin_rows)
    if not thin_rows:
        print("ERROR: CSV has no data rows", file=sys.stderr)
        return 1

    native_index = index_by_stem(natives_src)
    image_index = index_by_stem(images_src)

    # Determine Bates prefix + next number from existing IDs.
    prefix = bates_prefix(thin_rows[0]["BEGDOC"])
    width = len(re.search(r"(\d+)$", thin_rows[0]["BEGDOC"]).group(1))  # type: ignore
    max_num = max(bates_number(r["BEGDOC"]) for r in thin_rows)
    for r in thin_rows:
        max_num = max(max_num, bates_number(r["ENDDOC"]))
    next_num = max_num + 1
    stats.new_bates_start = next_num

    expanded_folders: Dict[str, str] = {}  # folder -> parent bates
    # Media children pending placement: (child_bates, media_path, ext)
    media_placements: List[Tuple[str, Path, str]] = []

    enriched: List[Dict[str, str]] = []
    used_bates: set = set()

    def add_row(row: Dict[str, str]) -> None:
        b = row["BEGDOC"]
        if b in used_bates:
            stats.errors.append(f"duplicate BEGDOC allocated: {b}")
            return
        used_bates.add(b)
        enriched.append(row)

    # --- Pass 1: existing CSV rows ---
    for r in thin_rows:
        beg = r["BEGDOC"]
        end = r["ENDDOC"]
        filename = r["FILENAME"]
        coded = r["CODED"]
        native = native_index.get(beg)
        if native:
            stats.natives_joined += 1
            ext = native.suffix
            fileext = ext.lstrip(".").upper()
        else:
            stats.natives_missing.append(beg)
            ext = Path(filename).suffix or ".bin"
            fileext = ext.lstrip(".").upper()
            stats.warnings.append(f"no native on disk for {beg}")

        if layout == "classic":
            filepath = rel_native_path(beg, ext)
        else:
            if native:
                filepath = rel_between(output, native)
            else:
                filepath = rel_between(output, natives_src / "0001" / f"{beg}{ext}")

        img = image_index.get(beg)
        if img:
            stats.images_joined += 1
        else:
            stats.images_missing.append(beg)

        is_ph = filename.lower().startswith("placeholder")
        if is_ph:
            stats.placeholders.append(f"{beg} | {filename}")

        add_row(
            {
                "BEGDOC": beg,
                "ENDDOC": end,
                "BEGATTACH": "",
                "ENDATTACH": "",
                "CUSTODIAN": "",
                "CODED": coded,
                "FILEPATH": filepath,
                "FILENAME": filename,
                "FILEEXT": fileext,
                "TEXTPATH": "",
                "DOCTYPE": doctype_for(filename, False),
                "SOURCEFOLDER": "",
            }
        )

    # --- Pass 2: expand media children ---
    if not placeholders_root.is_dir():
        stats.warnings.append(f"Items with Placeholders not found: {placeholders_root}")
    else:
        for r in thin_rows:
            filename = r["FILENAME"]
            key = filename.lower().strip()
            if not key.startswith("placeholder"):
                continue
            parent = r["BEGDOC"]
            folder_name = ph_map.get(key, None)
            if key not in ph_map:
                stats.placeholders_unmapped.append(
                    f"{parent} | {filename} (unknown placeholder)"
                )
                stats.warnings.append(
                    f"unknown placeholder filename not in map: {filename!r}"
                )
                continue
            if folder_name is None:
                stats.placeholders_unmapped.append(f"{parent} | {filename}")
                continue
            if folder_name in expanded_folders:
                stats.warnings.append(
                    f"{filename}: folder {folder_name!r} already expanded under "
                    f"{expanded_folders[folder_name]}; skipping duplicate parent {parent}"
                )
                continue

            folder = placeholders_root / folder_name
            if not folder.is_dir():
                stats.errors.append(f"mapped folder missing for {filename}: {folder}")
                stats.placeholders_unmapped.append(
                    f"{parent} | {filename} (folder missing)"
                )
                continue

            media_files = list_media_files(folder)
            expanded_folders[folder_name] = parent
            for media in media_files:
                child_bates = format_bates(prefix, next_num, width)
                next_num += 1
                stats.media_children += 1
                ext = media.suffix
                rel_src = str(media.relative_to(placeholders_root)).replace("/", "\\")
                fileext = ext.lstrip(".").upper()

                if layout == "classic":
                    filepath = rel_native_path(child_bates, ext)
                    media_placements.append((child_bates, media, ext))
                else:
                    filepath = rel_between(output, media)

                # TEXT companion — UTF-8 no BOM, CRLF, plain transcript
                textpath = ""
                stt = sidecar_transcript(media)
                text_dst = text_out / f"{child_bates}.txt"
                if stt and not args.dry_run:
                    try:
                        nbytes = write_text_companion(stt, text_dst)
                        if nbytes > 0:
                            textpath = rel_text_path(child_bates)
                            stats.text_written += 1
                        else:
                            stats.text_missing_media.append(rel_src)
                    except (OSError, ValueError) as exc:
                        stats.copy_failures.append(f"TEXT {child_bates}: {exc}")
                        stats.text_missing_media.append(rel_src)
                elif stt and args.dry_run:
                    textpath = rel_text_path(child_bates)
                    stats.text_written += 1
                else:
                    stats.text_missing_media.append(rel_src)

                stats.child_map.append((child_bates, parent, media.name, textpath))
                add_row(
                    {
                        "BEGDOC": child_bates,
                        "ENDDOC": child_bates,
                        "BEGATTACH": parent,
                        "ENDATTACH": parent,
                        "CUSTODIAN": "",
                        "CODED": "",
                        "FILEPATH": filepath,
                        "FILENAME": media.name,
                        "FILEEXT": fileext,
                        "TEXTPATH": textpath,
                        "DOCTYPE": doctype_for(media.name, True, ext),
                        "SOURCEFOLDER": rel_src,
                    }
                )

    stats.new_bates_end = next_num - 1
    stats.dat_rows = len(enriched)

    # --- Materialize classic folder tree ---
    if not args.dry_run and layout == "classic":
        # Junction source NATIVES/IMAGES when possible (local NTFS -> Drive).
        # Fall back to per-file symlinks into NATIVES\0001 / IMAGES\0001.
        natives_root_link = output / "NATIVES"
        images_root_link = output / "IMAGES"
        used_junction_natives = False
        used_junction_images = False

        if natives_src.is_dir():
            try:
                kind = ensure_junction_or_link(natives_root_link, natives_src)
                used_junction_natives = True
                print(f"  NATIVES -> {natives_src} ({kind})")
            except OSError as exc:
                stats.warnings.append(
                    f"NATIVES junction failed ({exc}); using per-file links"
                )
                ensure_dir(natives_out)
                for beg, nat in native_index.items():
                    dst = natives_out / nat.name
                    try:
                        place_file(nat, dst, mode=args.link_mode)
                    except OSError as e2:
                        stats.copy_failures.append(f"NATIVE {beg}: {e2}")

        if images_src.is_dir():
            try:
                kind = ensure_junction_or_link(images_root_link, images_src)
                used_junction_images = True
                print(f"  IMAGES -> {images_src} ({kind})")
            except OSError as exc:
                stats.warnings.append(
                    f"IMAGES junction failed ({exc}); using per-file links"
                )
                ensure_dir(images_out)
                for beg, img in image_index.items():
                    dst = images_out / img.name
                    try:
                        place_file(img, dst, mode=args.link_mode)
                    except OSError as e2:
                        stats.copy_failures.append(f"IMAGE {beg}: {e2}")

        # Bates-named media natives (short paths; original name in FILENAME).
        if not args.skip_media_copy and media_placements:
            ensure_dir(natives_out)
            # If NATIVES is a junction into source, writing into NATIVES\0001
            # would pollute the producing party's folder — use a sibling MEDIA
            # natives folder only when junctioned? Prefer: if junctioned, create
            # media links in a local overlay is hard. Safer approach:
            # when junction succeeded, remove junction and rebuild as real dir
            # with per-file links for originals + media (avoids writing into source).
            if used_junction_natives:
                try:
                    # Remove junction (directory junction unlink).
                    if natives_root_link.is_symlink() or natives_root_link.exists():
                        # On Windows, junctions are removed with rmdir / unlink.
                        natives_root_link.rmdir()
                except OSError:
                    # Fall back: cmd rmdir
                    import subprocess

                    subprocess.run(
                        ["cmd", "/c", "rmdir", str(natives_root_link)],
                        capture_output=True,
                    )
                used_junction_natives = False
                ensure_dir(natives_out)
                print("  NATIVES: rebuilding as real dir with Bates-named links")
                for beg, nat in native_index.items():
                    dst = natives_out / nat.name
                    try:
                        place_file(nat, dst, mode=args.link_mode)
                    except OSError as e2:
                        stats.copy_failures.append(f"NATIVE {beg}: {e2}")

            for child_bates, media, ext in media_placements:
                dst = natives_out / f"{child_bates}{ext}"
                try:
                    place_file(media, dst, mode=args.link_mode)
                except OSError as exc:
                    stats.copy_failures.append(
                        f"MEDIA NATIVE {child_bates}: {exc}"
                    )

        _ = used_junction_images  # images stay junctioned when possible

    enriched_csv = output / "VOL001_enriched.csv"
    write_enriched_csv(enriched_csv, enriched)

    # --- DAT + OPT ---
    dat_out = output / "VOL001.dat"
    opt_out = output / "VOL001.opt"
    if not args.dry_run:
        try:
            run_csv_to_dat(repo, enriched_csv, dat_out, schema_file)
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"csv2dat failed: {exc}")
            traceback.print_exc()

        try:
            sys.path.insert(0, str(repo))
            from converter import write_opt  # type: ignore

            if layout == "classic":
                img_rel_dir = "IMAGES\\0001"
            else:
                sample_img = next(iter(image_index.values()), None)
                if sample_img is not None:
                    img_rel_dir = str(Path(rel_between(output, sample_img)).parent)
                else:
                    img_rel_dir = rel_between(output, images_src / "0001")

            opt_records: List[List[str]] = []
            for row in enriched:
                beg = row["BEGDOC"]
                img_src = image_index.get(beg)
                if img_src is not None and img_src.is_file():
                    opt_records.append([beg])
            if opt_records:
                stats.opt_lines = write_opt(
                    str(opt_out),
                    iter(opt_records),
                    volume=args.volume,
                    image_ext=".pdf",
                    pages_per_doc=1,
                    image_dir=img_rel_dir,
                )
            else:
                stats.warnings.append("no images found; OPT not written")
        except Exception as exc:  # noqa: BLE001
            stats.errors.append(f"opt failed: {exc}")
            traceback.print_exc()

        if dat_out.is_file():
            try:
                field_names = [
                    ln.strip()
                    for ln in schema_file.read_text(encoding="utf-8").splitlines()
                    if ln.strip() and not ln.strip().startswith("#")
                ]
                summary = run_validate(repo, dat_out, output, field_names)
                print(summary)
                missing_fp = []
                missing_tp = []
                dotdot = 0
                for row in enriched:
                    fp_raw = row["FILEPATH"]
                    if fp_raw.startswith(".."):
                        dotdot += 1
                    fp = (output / fp_raw.replace("\\", os.sep)).resolve()
                    if fp_raw and not fp.is_file():
                        missing_fp.append(f"{row['BEGDOC']} -> {fp_raw}")
                    tp = row.get("TEXTPATH") or ""
                    if tp:
                        tpp = output / tp.replace("\\", os.sep)
                        if not tpp.is_file():
                            missing_tp.append(row["BEGDOC"])
                if layout == "classic" and dotdot:
                    stats.errors.append(
                        f"{dotdot} FILEPATHs still use parent-relative '..\\'"
                    )
                if missing_fp:
                    stats.warnings.append(
                        f"{len(missing_fp)} FILEPATH targets missing on disk"
                    )
                    for item in missing_fp[:20]:
                        stats.warnings.append(f"  missing native: {item}")
                if missing_tp:
                    stats.errors.append(f"{len(missing_tp)} TEXTPATH targets missing")
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"validate failed: {exc}")
                traceback.print_exc()

    report_path = output / "build_report.md"
    write_build_report(report_path, stats, source, output, ph_map)

    print(f"Layout:       {layout}")
    print(f"Enriched CSV: {enriched_csv}")
    print(f"Report:       {report_path}")
    print(
        f"DAT rows:     {stats.dat_rows} "
        f"(source {stats.source_rows} + media {stats.media_children})"
    )
    print(f"TEXT files:   {stats.text_written}")
    print(f"OPT lines:    {stats.opt_lines}")
    if stats.errors:
        print(f"ERRORS: {len(stats.errors)}", file=sys.stderr)
        for e in stats.errors[:20]:
            print(f"  {e}", file=sys.stderr)
        return 1
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    return build_volume(args)


if __name__ == "__main__":
    raise SystemExit(main())
