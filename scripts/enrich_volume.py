#!/usr/bin/env python3
"""
enrich_volume.py - Build a load-ready Concordance volume from a thin CSV production.

PAGE PURPOSE
------------
Takes a producing-party volume that has Bates + Filename only (no native paths,
no TEXTPATH) and builds a sibling VOL001_LOAD package with:

  * Bates-relative FILEPATH into NATIVES\\0001\\
  * Expanded child Bates for media under Items with Placeholders
  * TEXT\\0001\\{BEGDOC}.txt companions from:
      - PDF natives: extracted text (pypdf / pdfminer) with page/paragraph breaks
      - Media children: STT sidecars (prefer JSON utterances, else SRT, else .txt)
  * Classic Concordance DAT/DCT + corrected OPT (IMAGES\\0001\\*.pdf)
  * Optional DATA\\ folder for load files (DAT/DCT/OPT) with paths still
    relative to the volume root (parent of DATA/NATIVES/IMAGES/TEXT)
  * --link-mode copy / --self-contained: real file copies only (no Drive
    symlinks or directory junctions)
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
  sidecar_transcript()      Prefer .json / .srt / .txt beside a media file.
  format_hms()              Seconds -> HH:MM:SS for TEXT line prefixes.
  json_to_formatted_text()  Deepgram JSON -> timed utterance lines.
  srt_to_formatted_text()   SRT -> timed dialogue lines (keep markers).
  extract_pdf_text()        Extract readable text from a PDF native/image.
  write_text_bytes()        Normalize + write UTF-8/CRLF TEXT companion bytes.
  write_text_companion()    STT sidecar -> TEXT companion.
  write_pdf_text_companion() PDF path -> TEXT companion.
  rebuild_text_from_enriched()  Re-write TEXT\\0001 + TEXTPATH from enriched CSV.
  copy_or_link()            Hardlink when possible, else copy2 with retries.
  place_file()              Symlink/hardlink/copy a single file into the package.
  ensure_junction_or_link() Directory junction/symlink helper (not used in copy mode).
  is_self_contained()       True when build must use real copies only.
  write_readme_load()       Write README_LOAD.txt for the volume root.
  ensure_dir()              mkdir -p helper.
  build_volume()            Main pipeline: inventory -> expand -> TEXT -> DAT.
  write_build_report()      Markdown report with coverage + countervoice.
  main()                    Entry point.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import re
import shutil
import sys
import traceback
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple, Union

# ---------------------------------------------------------------------------
# Media / STT constants
# ---------------------------------------------------------------------------

MEDIA_EXTS = {".mp4", ".mp3", ".wav", ".m4a", ".avi", ".mov", ".mkv", ".wmv"}
PDF_EXTS = {".pdf"}
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
# Items with Placeholders (may be a nested subfolder). Unmapped / None = no children.
PLACEHOLDER_FOLDER_MAP: Dict[str, Optional[str]] = {
    "placeholder - snapchat.docx": "SNAPCHAT",
    "placeholder - jail calls.docx": "JAIL CALLS",
    "placeholder - incident 1.21.24.docx": "Incident 1.21.24",
    "placeholder - incident 4.26.24 pt 1.docx": "Incident 4.26.24",
    "placeholder - incident 4.26.24 pt 2.docx": None,  # same folder as pt1; avoid dup
    "placeholder - incident 12.2.24.docx": "Incident 12.2.24",
    # 12.5.24 media lives in Prop # subfolders (not the parent Incident folder alone).
    "placeholder - incident 12.5.24 body cam.docx": (
        "Incident 12.5.24\\Prop #339529 - Body Cam"
    ),
    "placeholder - incident 12.5.24 in car video.docx": (
        "Incident 12.5.24\\Prop #339530 - In Car Video"
    ),
    "placeholder - incident 12.5.24 911 recordings.docx": (
        "Incident 12.5.24\\Prop #339321 - 911 Recordings"
    ),
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
    text_pdf_written: int = 0
    text_pdf_empty: List[str] = field(default_factory=list)
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
        help="How to place files into NATIVES/IMAGES (auto=symlink/hardlink then copy). "
        "Use copy for a self-contained local volume with no Drive links.",
    )
    p.add_argument(
        "--copy-mode",
        choices=("auto", "copy", "hardlink", "symlink"),
        default=None,
        help="Alias for --link-mode (preferred name when forcing real copies).",
    )
    p.add_argument(
        "--layout",
        choices=("classic", "relative"),
        default="classic",
        help="classic=NATIVES\\0001\\{BEGDOC}ext under the volume root (default); "
        "relative=..\\ paths into the source volume",
    )
    p.add_argument(
        "--data-dir",
        default="",
        help="Optional subfolder for DAT/DCT/OPT/enriched CSV (e.g. DATA). "
        "FILEPATH/TEXTPATH/OPT paths stay relative to the volume root.",
    )
    p.add_argument(
        "--self-contained",
        action="store_true",
        help="Force real file copies (no symlinks/junctions) and default "
        "--data-dir DATA when unset. Produces a classic DATA/NATIVES/IMAGES/TEXT set.",
    )
    p.add_argument(
        "--rebuild-text",
        action="store_true",
        help="Regenerate TEXT\\0001 companions (PDF extract + STT) from an "
        "existing VOL001_enriched.csv, update TEXTPATH, and rebuild DAT. "
        "Use --output for the LOAD package; --source for Items with Placeholders.",
    )
    p.add_argument(
        "--skip-pdf-text",
        action="store_true",
        help="Do not extract text from PDF natives into TEXT companions",
    )
    args = p.parse_args(argv)
    if args.copy_mode:
        args.link_mode = args.copy_mode
    if args.self_contained:
        args.link_mode = "copy"
        if not args.data_dir:
            args.data_dir = "DATA"
    return args


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
    """Map file stem (case-sensitive as on disk) -> Path for all files under folder.

    Skips Windows junk (desktop.ini, Thumbs.db) so they are never treated as natives.
    """
    skip_names = {"desktop.ini", "thumbs.db", ".ds_store"}
    out: Dict[str, Path] = {}
    if not folder.is_dir():
        return out
    for p in folder.rglob("*"):
        if p.is_file() and p.name.lower() not in skip_names:
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


def _nonempty_file(path: Path) -> bool:
    """True when path exists as a non-empty file."""
    try:
        return path.is_file() and path.stat().st_size > 0
    except OSError:
        return False


def sidecar_transcript(media: Path) -> Optional[Path]:
    """
    Return preferred STT sidecar for Concordance TEXT.

    Preference: .json (utterances/paragraphs with timing) > .srt (cues) > .txt
    (plain wall-of-text transcript). Deepgram exports often ship all three;
    the .txt is usually an unformatted dump of the same words.
    """
    js = media.with_suffix(".json")
    srt = media.with_suffix(".srt")
    txt = media.with_suffix(".txt")
    if _nonempty_file(js):
        return js
    if _nonempty_file(srt):
        return srt
    if _nonempty_file(txt):
        return txt
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


def is_self_contained(args: argparse.Namespace) -> bool:
    """True when the build must materialize real files (no Drive links)."""
    return bool(getattr(args, "self_contained", False)) or getattr(
        args, "link_mode", "auto"
    ) == "copy"


def place_file(src: Path, dst: Path, mode: str = "auto") -> str:
    """
    Place ``src`` at ``dst`` for a classic Concordance NATIVES/IMAGES tree.

    mode=copy: always shutil.copy2 (self-contained local volume).
    mode=auto: symlink first (local NTFS -> Google Drive), then hardlink, then copy.
    Returns the method used.
    """
    ensure_dir(dst.parent)
    if dst.exists() or dst.is_symlink():
        try:
            if dst.resolve() == src.resolve():
                return "exists"
            if (
                mode == "copy"
                and dst.is_file()
                and not dst.is_symlink()
                and dst.stat().st_size == src.stat().st_size
            ):
                return "exists"
        except OSError:
            pass
        try:
            dst.unlink()
        except OSError:
            raise

    last_err: Optional[BaseException] = None
    # Copy-only builds never create reparse points into Drive.
    if mode == "copy":
        size = src.stat().st_size
        if size >= 100 * 1024 * 1024:
            print(
                f"  copying {src.name} ({size / (1024 ** 3):.2f} GB) -> {dst.name}",
                flush=True,
            )
        shutil.copy2(str(src), str(dst))
        return "copy"

    # Symlink first — useful when source is Google Drive and output is local NTFS.
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
    if mode == "auto":
        try:
            shutil.copy2(str(src), str(dst))
            return "copy"
        except OSError as exc:
            last_err = exc
    assert last_err is not None
    raise last_err


def write_readme_load(
    path: Path,
    *,
    volume: str,
    data_dir: str,
    self_contained: bool,
) -> None:
    """Write README_LOAD.txt describing the classic Concordance folder set."""
    data_label = data_dir.strip("\\/") if data_dir else "(volume root)"
    lines = [
        f"{volume} — Concordance / Relativity load package",
        "",
        "Folder set (volume root = this folder's parent of DATA/NATIVES/IMAGES/TEXT):",
        f"  DATA\\          Load files (DAT/DCT/OPT) — present as: {data_label}",
        "  NATIVES\\0001\\  Bates-named native files",
        "  IMAGES\\0001\\   Bates-named image PDFs",
        "  TEXT\\0001\\     Extracted text companions (PDF + STT)",
        "",
        "Path base: FILEPATH, TEXTPATH, and OPT image paths are relative to the",
        "volume root (this folder), NOT relative to DATA\\.",
        "",
        "Load steps:",
        f"  1. Open {data_label}\\{volume}.dat with companion .dct (cp1252).",
        "  2. Map FILEPATH -> Native File; TEXTPATH -> Extracted Text (file path).",
        f"  3. Load {data_label}\\{volume}.opt (PDF images under IMAGES\\0001\\).",
        "  4. Set the load/image root to this volume folder.",
        "",
    ]
    if self_contained:
        lines.append(
            "Self-contained: all NATIVES/IMAGES/TEXT files are real copies on local "
            "NTFS (no symlinks or junctions into Google Drive)."
        )
    else:
        lines.append(
            "Note: NATIVES/IMAGES may be symlinks/junctions into the source volume; "
            "keep that source reachable when loading."
        )
    lines.append("")
    path.write_text("\n".join(lines), encoding="utf-8")


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
    r"^(\d{1,2}:\d{2}:\d{2})[,\.]\d{1,3}\s*-->\s*\d{1,2}:\d{2}:\d{2}[,\.]\d{1,3}"
)
_SRT_INDEX_RE = re.compile(r"^\d+$")
# Concordance DAT control characters must never appear in extracted-text bodies.
_DAT_CTRL_RE = re.compile("[\x14\xfe\xae]")


def format_hms(seconds: Union[int, float]) -> str:
    """Format a duration in seconds as HH:MM:SS (floor, no millis)."""
    total = max(0, int(float(seconds)))
    hours, rem = divmod(total, 3600)
    minutes, secs = divmod(rem, 60)
    return f"{hours:02d}:{minutes:02d}:{secs:02d}"


def srt_to_formatted_text(text: str) -> str:
    """
    Convert SRT cues to readable timed lines.

    Example: ``[00:00:04] To report sexual assault or harassment, press 0.``
    Sequence numbers are dropped; cue start time is kept as ``[HH:MM:SS]``.
    """
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    out: List[str] = []
    current_ts: Optional[str] = None
    dialogue: List[str] = []

    def flush() -> None:
        nonlocal current_ts, dialogue
        body = " ".join(p for p in dialogue if p).strip()
        if body and current_ts is not None:
            out.append(f"[{current_ts}] {body}")
        elif body:
            out.append(body)
        current_ts = None
        dialogue = []

    for line in lines:
        s = line.strip()
        if not s:
            flush()
            continue
        if _SRT_INDEX_RE.match(s):
            continue
        m = _SRT_TS_RE.match(s)
        if m:
            flush()
            current_ts = m.group(1)
            # Normalize single-digit hours to HH:MM:SS
            parts = current_ts.split(":")
            if len(parts) == 3:
                current_ts = f"{int(parts[0]):02d}:{parts[1]}:{parts[2]}"
            continue
        dialogue.append(s)
    flush()
    return "\n".join(out)


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


def json_to_formatted_text(data: Dict[str, Any]) -> str:
    """
    Build timed transcript lines from a Deepgram-style STT JSON payload.

    Preference order:
      1. results.utterances — one line per utterance, optional Speaker N
      2. results.channels[0].alternatives[0].paragraphs.paragraphs — sentences
      3. results.channels[0].alternatives[0].transcript — plain fallback
    """
    results = data.get("results") or {}
    utterances = results.get("utterances")
    if isinstance(utterances, list) and utterances:
        lines: List[str] = []
        for u in utterances:
            if not isinstance(u, dict):
                continue
            body = str(u.get("transcript") or "").strip()
            if not body:
                continue
            ts = format_hms(u.get("start") or 0)
            speaker = u.get("speaker")
            if speaker is not None and str(speaker).strip() != "":
                lines.append(f"[{ts}] Speaker {speaker}: {body}")
            else:
                lines.append(f"[{ts}] {body}")
        if lines:
            return "\n".join(lines)

    channels = results.get("channels") or []
    if channels and isinstance(channels[0], dict):
        alts = channels[0].get("alternatives") or []
        if alts and isinstance(alts[0], dict):
            alt = alts[0]
            paras_wrap = alt.get("paragraphs")
            if isinstance(paras_wrap, dict):
                paras = paras_wrap.get("paragraphs") or []
                lines = []
                for para in paras:
                    if not isinstance(para, dict):
                        continue
                    for sent in para.get("sentences") or []:
                        if not isinstance(sent, dict):
                            continue
                        body = str(sent.get("text") or "").strip()
                        if not body:
                            continue
                        lines.append(f"[{format_hms(sent.get('start') or 0)}] {body}")
                if lines:
                    return "\n".join(lines)
            transcript = str(alt.get("transcript") or "").strip()
            if transcript:
                return transcript

    # Some exporters put transcript at the top level.
    top = str(data.get("transcript") or "").strip()
    return top


def normalize_extracted_text(text: str) -> str:
    """
    Normalize extracted text for Concordance/Relativity TEXTPATH companions.

    - UTF-8 body (caller writes without BOM)
    - Windows CRLF newlines
    - Strip Concordance control chars 0x14 / 0xFE / 0xAE
    - Collapse runs of blank lines (keep single paragraph breaks)
    """
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    # PDF field padding often uses NBSP; normalize for readable Concordance text.
    text = text.replace("\u00a0", " ").replace("\u2007", " ").replace("\u202f", " ")
    text = _DAT_CTRL_RE.sub("", text)
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


def write_text_bytes(body: str, dst_txt: Path) -> int:
    """
    Normalize ``body`` and write UTF-8 (no BOM) CRLF TEXT companion.

    Returns byte length written. Raises ValueError if empty after normalize.
    """
    ensure_dir(dst_txt.parent)
    normalized = normalize_extracted_text(body)
    if not normalized.strip():
        raise ValueError("empty text after normalize")
    data = normalized.encode("utf-8")  # no BOM
    dst_txt.write_bytes(data)
    return len(data)


def _pdf_page_text_pypdf(pdf_path: Path) -> List[str]:
    """Extract per-page text via pypdf (optional dependency)."""
    from pypdf import PdfReader  # type: ignore

    reader = PdfReader(str(pdf_path))
    pages: List[str] = []
    for page in reader.pages:
        pages.append(page.extract_text() or "")
    return pages


def _pdf_page_text_pdfminer(pdf_path: Path) -> List[str]:
    """Extract per-page text via pdfminer.six (optional dependency)."""
    from pdfminer.high_level import extract_text  # type: ignore

    # pdfminer returns one string; split on form feed when present.
    full = extract_text(str(pdf_path)) or ""
    if "\x0c" in full:
        return full.split("\x0c")
    return [full] if full.strip() else []


def _pdf_page_text_pymupdf(pdf_path: Path) -> List[str]:
    """Extract per-page text via PyMuPDF / fitz (optional dependency)."""
    import fitz  # type: ignore

    doc = fitz.open(str(pdf_path))
    try:
        return [doc.load_page(i).get_text("text") or "" for i in range(doc.page_count)]
    finally:
        doc.close()


def extract_pdf_text(pdf_path: Path) -> str:
    """
    Extract readable text from a PDF, preserving page and paragraph breaks.

    Tries pypdf, then pdfminer.six, then PyMuPDF. Pages are separated by a
    blank line (and a ``--- Page N ---`` marker). Raises ValueError when no
    extractor is installed or the PDF yields no text (image-only / empty).
    """
    if not pdf_path.is_file():
        raise ValueError(f"PDF not found: {pdf_path}")

    errors: List[str] = []
    pages: Optional[List[str]] = None
    for name, fn in (
        ("pypdf", _pdf_page_text_pypdf),
        ("pdfminer", _pdf_page_text_pdfminer),
        ("pymupdf", _pdf_page_text_pymupdf),
    ):
        try:
            pages = fn(pdf_path)
            break
        except ImportError:
            errors.append(f"{name} not installed")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"{name}: {exc}")

    if pages is None:
        raise ValueError(
            "no PDF text extractor available (install pypdf or pdfminer.six); "
            + "; ".join(errors)
        )

    blocks: List[str] = []
    for i, page_text in enumerate(pages, start=1):
        body = (page_text or "").replace("\r\n", "\n").replace("\r", "\n")
        # Soft hyphen / form-feed cleanup; keep real newlines from the extractor.
        body = body.replace("\x0c", "\n").replace("\u00ad", "")
        body = body.strip()
        if not body:
            continue
        blocks.append(f"--- Page {i} ---")
        blocks.append(body)
        blocks.append("")  # blank line between pages

    text = "\n".join(blocks).strip()
    if not text:
        raise ValueError(f"no extractable text in PDF: {pdf_path.name}")
    return text


def resolve_pdf_for_text(
    beg: str,
    filepath: str,
    output: Path,
    native_index: Optional[Dict[str, Path]] = None,
    image_index: Optional[Dict[str, Path]] = None,
) -> Optional[Path]:
    """
    Locate the best PDF to extract text from for a Bates ID.

    Preference: native under FILEPATH / native_index, else IMAGES PDF.
    """
    candidates: List[Path] = []
    if filepath:
        p = output / filepath.replace("\\", os.sep)
        candidates.append(p)
    if native_index and beg in native_index:
        candidates.append(native_index[beg])
    if image_index and beg in image_index:
        candidates.append(image_index[beg])
    # Classic package layout fallbacks.
    candidates.append(output / "NATIVES" / "0001" / f"{beg}.pdf")
    candidates.append(output / "IMAGES" / "0001" / f"{beg}.pdf")

    seen: set = set()
    for c in candidates:
        try:
            key = str(c.resolve())
        except OSError:
            key = str(c)
        if key in seen:
            continue
        seen.add(key)
        if c.is_file() and c.suffix.lower() in PDF_EXTS:
            return c
    return None


def extract_text_from_sidecar(src: Path) -> str:
    """
    Read an STT sidecar and return formatted transcript body (LF newlines).

    .json / .srt produce timed lines; .txt is passed through as-is.
    """
    suffix = src.suffix.lower()
    raw = src.read_bytes()
    for enc in ("utf-8-sig", "utf-8", "cp1252", "latin-1"):
        try:
            text = raw.decode(enc)
            break
        except UnicodeDecodeError:
            continue
    else:
        text = raw.decode("utf-8", errors="replace")

    if suffix == ".json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            raise ValueError(f"invalid STT JSON: {src}: {exc}") from exc
        if not isinstance(data, dict):
            raise ValueError(f"STT JSON root must be object: {src}")
        return json_to_formatted_text(data)

    if suffix == ".srt":
        return srt_to_formatted_text(text)

    return text


def write_text_companion(src_txt: Path, dst_txt: Path) -> int:
    """
    Write a Concordance extracted-text companion from an STT sidecar.

    Prefers timed formatting from .json utterances or .srt cues. Output is
    UTF-8 without BOM, CRLF newlines, no Concordance control characters.

    Returns byte length written. Raises ValueError if the result would be empty
    after normalization (caller should leave TEXTPATH blank).
    """
    body = extract_text_from_sidecar(src_txt)
    try:
        return write_text_bytes(body, dst_txt)
    except ValueError as exc:
        raise ValueError(f"empty transcript after normalize: {src_txt}") from exc


def write_pdf_text_companion(pdf_path: Path, dst_txt: Path) -> int:
    """
    Extract text from ``pdf_path`` and write a Concordance TEXT companion.

    Returns byte length written. Raises ValueError if empty / no extractor.
    """
    body = extract_pdf_text(pdf_path)
    try:
        return write_text_bytes(body, dst_txt)
    except ValueError as exc:
        raise ValueError(f"empty PDF text after normalize: {pdf_path}") from exc


def rebuild_text_from_enriched(
    output: Path,
    placeholders_root: Path,
    *,
    skip_pdf_text: bool = False,
    natives_src: Optional[Path] = None,
    images_src: Optional[Path] = None,
) -> Tuple[List[Dict[str, str]], int, int, List[str]]:
    """
    Re-generate TEXT companions and refresh TEXTPATH on enriched CSV rows.

    - PDF natives (FILEEXT=PDF): extract from NATIVES/IMAGES PDF
    - Media children (SOURCEFOLDER set): STT sidecar beside media

    Returns (updated_rows, stt_written, pdf_written, errors).
    """
    enriched_csv = output / "VOL001_enriched.csv"
    if not enriched_csv.is_file():
        raise FileNotFoundError(f"enriched CSV not found: {enriched_csv}")
    text_out = output / "TEXT" / "0001"
    ensure_dir(text_out)

    native_index = index_by_stem(natives_src) if natives_src else {}
    image_index = index_by_stem(images_src) if images_src else {}
    # Also index package NATIVES/IMAGES when present.
    pkg_natives = output / "NATIVES"
    pkg_images = output / "IMAGES"
    if pkg_natives.is_dir():
        for stem, path in index_by_stem(pkg_natives).items():
            native_index.setdefault(stem, path)
    if pkg_images.is_dir():
        for stem, path in index_by_stem(pkg_images).items():
            image_index.setdefault(stem, path)

    rows: List[Dict[str, str]] = []
    stt_written = 0
    pdf_written = 0
    errors: List[str] = []

    with enriched_csv.open("r", encoding="utf-8-sig", newline="") as fh:
        reader = csv.DictReader(fh)
        for raw in reader:
            row = {k: (raw.get(k) or "") for k in SCHEMA_FIELDS}
            beg = row["BEGDOC"].strip()
            if not beg:
                rows.append(row)
                continue

            src_rel = row["SOURCEFOLDER"].strip()
            fileext = row["FILEEXT"].strip().upper()
            filepath = row["FILEPATH"].strip()
            textpath = ""

            # Media children: STT sidecars
            if src_rel:
                media = placeholders_root / src_rel.replace("\\", os.sep)
                if media.is_file():
                    stt = sidecar_transcript(media)
                    if stt:
                        try:
                            write_text_companion(stt, text_out / f"{beg}.txt")
                            textpath = rel_text_path(beg)
                            stt_written += 1
                        except (OSError, ValueError) as exc:
                            errors.append(f"STT {beg}: {exc}")
                else:
                    errors.append(f"{beg}: media missing: {src_rel}")

            # PDF natives: extract text (skip placeholders / non-PDF)
            elif not skip_pdf_text and fileext == "PDF":
                pdf = resolve_pdf_for_text(
                    beg, filepath, output, native_index, image_index
                )
                if pdf is None:
                    errors.append(f"PDF {beg}: file not found for extraction")
                else:
                    try:
                        write_pdf_text_companion(pdf, text_out / f"{beg}.txt")
                        textpath = rel_text_path(beg)
                        pdf_written += 1
                    except (OSError, ValueError) as exc:
                        errors.append(f"PDF {beg}: {exc}")

            # Prefer newly written companion; else keep prior TEXTPATH if file exists.
            if textpath:
                row["TEXTPATH"] = textpath
            elif row["TEXTPATH"]:
                prior = output / row["TEXTPATH"].replace("\\", os.sep)
                if not prior.is_file():
                    row["TEXTPATH"] = ""
            rows.append(row)

    write_enriched_csv(enriched_csv, rows)
    return rows, stt_written, pdf_written, errors


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
    *,
    data_dir: str = "",
    self_contained: bool = False,
) -> None:
    """Write build_report.md with coverage, Bates map, and countervoice notes."""
    lines: List[str] = []
    lines.append("# VOL001_LOAD build report")
    lines.append("")
    lines.append(f"- Generated: `{datetime.now(timezone.utc).isoformat()}`")
    lines.append(f"- Source: `{source}`")
    lines.append(f"- Output: `{output}`")
    lines.append(
        f"- Layout: classic"
        + (f" with DATA dir `{data_dir}`" if data_dir else "")
        + ("; self-contained copies" if self_contained else "")
    )
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
    lines.append(f"| PDF TEXT extracted | {stats.text_pdf_written} |")
    lines.append(f"| PDF TEXT empty/failed | {len(stats.text_pdf_empty)} |")
    lines.append(f"| Media missing STT | {len(stats.text_missing_media)} |")
    lines.append(f"| New Bates range | {stats.new_bates_start}–{stats.new_bates_end} |")
    lines.append(f"| DAT rows | {stats.dat_rows} |")
    lines.append(f"| OPT lines | {stats.opt_lines} |")
    lines.append(f"| Copy failures | {len(stats.copy_failures)} |")
    lines.append(f"| Self-contained | {self_contained} |")
    lines.append(f"| Data dir | `{data_dir or '(volume root)'}` |")
    lines.append("")

    if stats.media_children:
        stt_n = stats.text_written - stats.text_pdf_written
        pct = (
            100.0 * stt_n / stats.media_children
            if stats.media_children
            else 0.0
        )
        lines.append(
            f"**STT coverage (media children):** {stt_n}/{stats.media_children} ({pct:.1f}%)"
        )
        lines.append("")

    if stats.text_pdf_empty:
        lines.append("## PDF text extraction failures / empty")
        lines.append("")
        for item in stats.text_pdf_empty[:100]:
            lines.append(f"- `{item}`")
        if len(stats.text_pdf_empty) > 100:
            lines.append(f"- … and {len(stats.text_pdf_empty) - 100} more")
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
    lines.append("| Inline STT in cp1252 DAT | TEXTPATH → UTF-8 (no BOM) `TEXT\\0001\\{BEGDOC}.txt`, CRLF, timed transcript, DAT control chars stripped |")
    lines.append("| SRT/JSON sidecars | Prefer `.json` utterances (timestamps + optional Speaker N), else `.srt` with `[HH:MM:SS]` markers, else plain `.txt` |")
    lines.append("| PDF TEXT missing / bunched | Extract from native PDF (pypdf/pdfminer); page markers + paragraph newlines; optional dep in requirements.txt |")
    lines.append("| Parent-relative `..\\` paths | Classic layout uses only paths under the volume root |")
    lines.append("| Long Snapchat filenames | Media natives Bates-renamed under `NATIVES\\0001\\` (original name kept in FILENAME) |")
    if self_contained:
        lines.append(
            "| Multi-GB media / Google Drive | `--self-contained` / `--link-mode copy` "
            "copies all natives+images onto local NTFS (no Drive symlinks/junctions) |"
        )
    else:
        lines.append(
            "| Multi-GB media / Google Drive | Prefer local NTFS output with "
            "symlinks/junctions into the source volume |"
        )
    lines.append("| Incident 4.26.24 pt 2 | Same folder as pt 1 — children attached only to pt 1 to avoid duplicates |")
    lines.append(
        "| 12.5.24 placeholders | Map to Prop # subfolders "
        "(Body Cam / In Car Video / 911 Recordings) under Incident 12.5.24 |"
    )
    lines.append(
        "| Family integrity | Children: BEGATTACH=ENDATTACH=parent; parents blank. "
        "Do not set ENDATTACH=last child (non-contiguous Bates would falsely span "
        "unrelated docs between parent and children) |"
    )
    lines.append("| Case data on GitHub | Output stays under case/local build folder; not committed to csv_to_dat |")
    lines.append("")
    lines.append("## Load checklist")
    lines.append("")
    data_hint = f"`{data_dir}\\VOL001.dat`" if data_dir else "`VOL001.dat`"
    lines.append(
        f"1. Point Relativity/Concordance at {data_hint} "
        "(cp1252, Concordance delimiters); set load root to the volume folder."
    )
    lines.append("2. Map `FILEPATH` → Native File (`NATIVES\\0001\\…`); `TEXTPATH` → Extracted Text as **file path**.")
    lines.append("3. Load Opticon `VOL001.opt` (`IMAGES\\0001\\{BEGDOC}.pdf` — PDFs, not TIFF).")
    lines.append("4. Media children without images are native-only (no OPT line).")
    if self_contained:
        lines.append(
            "5. Package is self-contained — all files under DATA/NATIVES/IMAGES/TEXT "
            "are real copies on local disk."
        )
    else:
        lines.append(
            "5. If NATIVES/IMAGES are symlinks/junctions, keep the source volume "
            "reachable when loading."
        )
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
    # Classic Concordance with DATA-layout / self-contained packages: emit a
    # thorn-wrapped field-name header as the first DAT record (Relativity-
    # friendly). Field names are also written to the companion .dct.
    csv_to_dat(
        str(enriched_csv),
        str(dat_out),
        DatConfig(),
        field_names=field_names,
        emit_dct=True,
        emit_header=True,
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
    Paths are always relative to the volume root (``--output``).

    With ``--self-contained`` / ``--link-mode copy``, NATIVES and IMAGES are
    real file copies (no Drive symlinks/junctions). Optional ``--data-dir DATA``
    places DAT/DCT/OPT under DATA\\ while paths remain volume-root-relative.

    Returns process exit code (0 success, 1 on hard errors).
    """
    source = Path(args.source).resolve()
    layout = getattr(args, "layout", "classic")
    self_contained = is_self_contained(args)
    data_dir_name = (getattr(args, "data_dir", "") or "").strip().strip("\\/")
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
    data_out = output / data_dir_name if data_dir_name else output

    stats = BuildStats()
    ph_map = placeholder_folder_map()

    if not csv_path.is_file():
        print(f"ERROR: CSV not found: {csv_path}", file=sys.stderr)
        return 1
    if not schema_file.is_file():
        print(f"ERROR: schema not found: {schema_file}", file=sys.stderr)
        return 1

    # Self-contained builds need substantial free space (~natives+images+media).
    if self_contained and not args.dry_run and os.name == "nt":
        try:
            usage = shutil.disk_usage(str(output.anchor if output.anchor else output))
            free_gb = usage.free / (1024 ** 3)
            print(f"Free space on output volume: {free_gb:.1f} GB")
            if free_gb < 70:
                print(
                    f"ERROR: need ~70+ GB free for self-contained copy; "
                    f"only {free_gb:.1f} GB available on {output.anchor}",
                    file=sys.stderr,
                )
                return 1
        except OSError as exc:
            stats.warnings.append(f"could not check free space: {exc}")

    ensure_dir(output)
    ensure_dir(data_out)
    text_out = output / "TEXT" / "0001"
    natives_out = output / "NATIVES" / "0001"
    images_out = output / "IMAGES" / "0001"
    ensure_dir(text_out)
    ensure_dir(natives_out)
    ensure_dir(images_out)

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
    # Original natives/images to copy when self-contained / classic.
    native_placements: List[Tuple[str, Path]] = []
    image_placements: List[Tuple[str, Path]] = []

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
            native_placements.append((beg, native))
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
            image_placements.append((beg, img))
        else:
            stats.images_missing.append(beg)

        is_ph = filename.lower().startswith("placeholder")
        if is_ph:
            stats.placeholders.append(f"{beg} | {filename}")

        # PDF extracted-text companion (UTF-8 no BOM, CRLF, page breaks).
        textpath = ""
        if (
            not args.skip_pdf_text
            and not args.dry_run
            and not is_ph
            and ext.lower() in PDF_EXTS
        ):
            pdf_src = native if native and native.is_file() else None
            if pdf_src is None and img is not None and img.suffix.lower() in PDF_EXTS:
                pdf_src = img
            if pdf_src is not None:
                text_dst = text_out / f"{beg}.txt"
                try:
                    write_pdf_text_companion(pdf_src, text_dst)
                    textpath = rel_text_path(beg)
                    stats.text_written += 1
                    stats.text_pdf_written += 1
                except (OSError, ValueError) as exc:
                    stats.text_pdf_empty.append(f"{beg} | {filename}: {exc}")
            else:
                stats.text_pdf_empty.append(f"{beg} | {filename}: PDF not on disk")
        elif (
            not args.skip_pdf_text
            and args.dry_run
            and not is_ph
            and ext.lower() in PDF_EXTS
            and (native or (img and img.suffix.lower() in PDF_EXTS))
        ):
            textpath = rel_text_path(beg)
            stats.text_written += 1
            stats.text_pdf_written += 1

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
                "TEXTPATH": textpath,
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

            folder = placeholders_root.joinpath(
                *folder_name.replace("\\", "/").split("/")
            )
            if not folder.is_dir():
                stats.errors.append(f"mapped folder missing for {filename}: {folder}")
                stats.placeholders_unmapped.append(
                    f"{parent} | {filename} (folder missing)"
                )
                continue

            media_files = list_media_files(folder)
            expanded_folders[folder_name] = parent
            # Parents keep blank BEGATTACH/ENDATTACH. Children get
            # BEGATTACH=ENDATTACH=parent (not last-child — child Bates are
            # non-contiguous with the parent, so a range would falsely span
            # unrelated documents between them).
            pending_children: List[Tuple[str, Path, str, str, str]] = []
            # (child_bates, media, ext, filepath, rel_src)
            for media in media_files:
                child_bates = format_bates(prefix, next_num, width)
                next_num += 1
                stats.media_children += 1
                ext = media.suffix
                rel_src = str(media.relative_to(placeholders_root)).replace("/", "\\")
                if layout == "classic":
                    filepath = rel_native_path(child_bates, ext)
                    media_placements.append((child_bates, media, ext))
                else:
                    filepath = rel_between(output, media)
                pending_children.append((child_bates, media, ext, filepath, rel_src))

            # Parents intentionally left with blank attach fields.

            for child_bates, media, ext, filepath, rel_src in pending_children:
                fileext = ext.lstrip(".").upper()

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
        link_mode = args.link_mode
        use_junctions = not self_contained and link_mode != "copy"
        natives_root_link = output / "NATIVES"
        images_root_link = output / "IMAGES"
        used_junction_natives = False
        used_junction_images = False

        if use_junctions and natives_src.is_dir():
            try:
                kind = ensure_junction_or_link(natives_root_link, natives_src)
                used_junction_natives = True
                print(f"  NATIVES -> {natives_src} ({kind})")
            except OSError as exc:
                stats.warnings.append(
                    f"NATIVES junction failed ({exc}); using per-file links"
                )

        if use_junctions and images_src.is_dir():
            try:
                kind = ensure_junction_or_link(images_root_link, images_src)
                used_junction_images = True
                print(f"  IMAGES -> {images_src} ({kind})")
            except OSError as exc:
                stats.warnings.append(
                    f"IMAGES junction failed ({exc}); using per-file links"
                )

        # When media children need Bates-named files under NATIVES\0001, a
        # junction into source would pollute the producing party folder — rebuild
        # as a real directory with per-file placement.
        if used_junction_natives and media_placements and not args.skip_media_copy:
            try:
                if natives_root_link.is_symlink() or natives_root_link.exists():
                    natives_root_link.rmdir()
            except OSError:
                import subprocess

                subprocess.run(
                    ["cmd", "/c", "rmdir", str(natives_root_link)],
                    capture_output=True,
                )
            used_junction_natives = False
            print("  NATIVES: rebuilding as real dir with Bates-named files")

        if not used_junction_natives:
            ensure_dir(natives_out)
            total_n = len(native_placements)
            for i, (beg, nat) in enumerate(native_placements, 1):
                dst = natives_out / f"{beg}{nat.suffix}"
                if i == 1 or i % 50 == 0 or i == total_n:
                    print(f"  NATIVES originals {i}/{total_n}", flush=True)
                try:
                    place_file(nat, dst, mode=link_mode)
                except OSError as e2:
                    stats.copy_failures.append(f"NATIVE {beg}: {e2}")

        if not used_junction_images:
            ensure_dir(images_out)
            total_i = len(image_placements)
            for i, (beg, img) in enumerate(image_placements, 1):
                dst = images_out / f"{beg}{img.suffix}"
                if i == 1 or i % 50 == 0 or i == total_i:
                    print(f"  IMAGES {i}/{total_i}", flush=True)
                try:
                    place_file(img, dst, mode=link_mode)
                except OSError as e2:
                    stats.copy_failures.append(f"IMAGE {beg}: {e2}")

        # Bates-named media natives (short paths; original name in FILENAME).
        if not args.skip_media_copy and media_placements:
            ensure_dir(natives_out)
            total_m = len(media_placements)
            print(f"  Copying {total_m} media natives (may take a long time)…", flush=True)
            for i, (child_bates, media, ext) in enumerate(media_placements, 1):
                dst = natives_out / f"{child_bates}{ext}"
                if i == 1 or i % 10 == 0 or i == total_m:
                    print(f"  MEDIA natives {i}/{total_m}: {child_bates}{ext}", flush=True)
                try:
                    place_file(media, dst, mode=link_mode)
                except OSError as exc:
                    stats.copy_failures.append(
                        f"MEDIA NATIVE {child_bates}: {exc}"
                    )

        _ = used_junction_images

    enriched_csv = data_out / "VOL001_enriched.csv"
    write_enriched_csv(enriched_csv, enriched)

    # --- DAT + OPT (under DATA\\ when --data-dir is set) ---
    dat_out = data_out / "VOL001.dat"
    opt_out = data_out / "VOL001.opt"
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
                # Validate FILEPATH against volume root (not DATA\\).
                summary = run_validate(repo, dat_out, output, field_names)
                print(summary)
                missing_fp = []
                missing_tp = []
                missing_opt = []
                dotdot = 0
                for row in enriched:
                    fp_raw = row["FILEPATH"]
                    if fp_raw.startswith(".."):
                        dotdot += 1
                    fp = output / fp_raw.replace("\\", os.sep)
                    if fp_raw and not fp.is_file():
                        missing_fp.append(f"{row['BEGDOC']} -> {fp_raw}")
                    tp = row.get("TEXTPATH") or ""
                    if tp:
                        tpp = output / tp.replace("\\", os.sep)
                        if not tpp.is_file():
                            missing_tp.append(row["BEGDOC"])
                if layout == "classic" and opt_out.is_file():
                    for line in opt_out.read_text(encoding="cp1252", errors="replace").splitlines():
                        parts = line.split(",")
                        if len(parts) < 3:
                            continue
                        img_rel = parts[2].strip()
                        if img_rel.startswith(".."):
                            dotdot += 1
                        img_path = output / img_rel.replace("\\", os.sep)
                        if not img_path.is_file():
                            missing_opt.append(img_rel)
                if layout == "classic" and dotdot:
                    stats.errors.append(
                        f"{dotdot} FILEPATHs/OPT paths still use parent-relative '..\\'"
                    )
                if missing_fp:
                    stats.warnings.append(
                        f"{len(missing_fp)} FILEPATH targets missing on disk"
                    )
                    for item in missing_fp[:20]:
                        stats.warnings.append(f"  missing native: {item}")
                if missing_tp:
                    stats.errors.append(f"{len(missing_tp)} TEXTPATH targets missing")
                if missing_opt:
                    stats.warnings.append(
                        f"{len(missing_opt)} OPT image paths missing on disk"
                    )
            except Exception as exc:  # noqa: BLE001
                stats.errors.append(f"validate failed: {exc}")
                traceback.print_exc()

        try:
            write_readme_load(
                output / "README_LOAD.txt",
                volume=args.volume,
                data_dir=data_dir_name,
                self_contained=self_contained,
            )
        except OSError as exc:
            stats.warnings.append(f"README_LOAD.txt write failed: {exc}")

    report_path = output / "build_report.md"
    write_build_report(
        report_path,
        stats,
        source,
        output,
        ph_map,
        data_dir=data_dir_name,
        self_contained=self_contained,
    )

    print(f"Layout:       {layout}")
    print(f"Self-contained: {self_contained}")
    print(f"Data dir:     {data_out}")
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


def rebuild_text_package(args: argparse.Namespace) -> int:
    """
    Rebuild TEXT companions + TEXTPATH + DAT for an existing LOAD package.

    Does not re-expand Bates or re-copy natives. Requires VOL001_enriched.csv
    under --output and (for media STT) Items with Placeholders under --source.
    """
    source = Path(args.source).resolve()
    if args.output:
        output = Path(args.output).resolve()
    else:
        output = source / "VOL001_LOAD"
    repo = Path(args.repo).resolve() if args.repo else Path(__file__).resolve().parents[1]
    schema_file = repo / "schemas" / "davila_load.txt"
    placeholders_root = source / "Items with Placeholders"
    natives_src = source / "NATIVES"
    images_src = source / "IMAGES"

    if not (output / "VOL001_enriched.csv").is_file():
        print(f"ERROR: enriched CSV not found under {output}", file=sys.stderr)
        return 1
    if not schema_file.is_file():
        print(f"ERROR: schema not found: {schema_file}", file=sys.stderr)
        return 1

    print(f"Rebuilding TEXT under {output / 'TEXT' / '0001'}")
    rows, stt_n, pdf_n, errors = rebuild_text_from_enriched(
        output,
        placeholders_root,
        skip_pdf_text=bool(args.skip_pdf_text),
        natives_src=natives_src if natives_src.is_dir() else None,
        images_src=images_src if images_src.is_dir() else None,
    )
    print(f"  STT TEXT written: {stt_n}")
    print(f"  PDF TEXT written: {pdf_n}")
    if errors:
        print(f"  TEXT errors: {len(errors)}")
        for e in errors[:30]:
            print(f"    {e}")

    enriched_csv = output / "VOL001_enriched.csv"
    dat_out = output / "VOL001.dat"
    if not args.dry_run:
        try:
            run_csv_to_dat(repo, enriched_csv, dat_out, schema_file)
            print(f"  DAT rebuilt: {dat_out}")
        except Exception as exc:  # noqa: BLE001
            print(f"ERROR: csv2dat failed: {exc}", file=sys.stderr)
            traceback.print_exc()
            return 1

        missing_tp = [
            r["BEGDOC"]
            for r in rows
            if (r.get("TEXTPATH") or "")
            and not (output / r["TEXTPATH"].replace("\\", os.sep)).is_file()
        ]
        if missing_tp:
            print(f"ERROR: {len(missing_tp)} TEXTPATH targets missing", file=sys.stderr)
            return 1

    # Soft-fail on extraction errors (image-only PDFs are expected).
    print(f"Rows: {len(rows)}; TEXTPATH set: {sum(1 for r in rows if r.get('TEXTPATH'))}")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    """CLI entry point."""
    args = parse_args(argv)
    if args.rebuild_text:
        return rebuild_text_package(args)
    return build_volume(args)


if __name__ == "__main__":
    raise SystemExit(main())
