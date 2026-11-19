"""PDF study mode (the owner's idea, 2026-11-19): "study <file.pdf or folder>" (or "<path> çalış / incele / özetle") makes Umay read PDFs
that are already on this PC (a course's briefs from Brightspace, say) and hand back a study report (.md) in Documents\\Umay Research:
a summary per file and the notes sorted by kind (deadlines, deliverables, marking criteria, learning outcomes, tasks, topics,
definitions), each with a quote and its page number. Fully local, and NO web: nothing of a document goes into a search query,
a cloud model or any address. It runs as a job of research.py (same thread slot, stop/status, restart `resume`), its text is read by
the planner's gpt-oss (planner_server, CPU, 8k window).

How it checks (the model reads, the code decides), the same idea as the web research:
1. pypdf takes the text of each page (line-end hyphens joined). A page with no text layer is counted as "scanned" and listed; a file
   with none at all says "needs OCR" (no OCR here). An encrypted or broken file is named and skipped.
2. The pages are cut into chunks of ~6000 characters on page borders. ONE model call per chunk gives up to 8 notes, each with a kind
   and a quote copied word for word.
3. CODE keeps a note only when its quote is really in the chunk's text (`research.quote_found`) and every number of the note is in its
   quote; the page of the quote is found by code, not by the model. The count of dropped notes is in the report.
4. The model writes a short summary per file from the kept notes only (and an overview of several files); a number in a summary that
   no kept note holds is listed in the report. The report body is built by code.

Limits that are stated in every report: scanned pages can't be read; a quote that is in the file can still be misread by the model;
what the time limit cut off is listed. Settings: UMAY_STUDY_MINUTES (60, of work, not counting a game's pause), UMAY_STUDY_CHUNKS (90),
UMAY_RESEARCH_DIR (where the report goes). A job lives on the disk like a research job (mode "pdf", the files and the notes per chunk).
"""
import hashlib
import os
import re
import time
import unicodedata
from datetime import datetime
from pathlib import Path

import llama_server
import planner_server
import research as R

CHUNK_CHARS = 6000       # ~1,500 tokens: the window is 8,192 with the prompt and the answer
MAX_FILES = 40
PER_CHUNK = 8
KINDS = ("deadline", "deliverable", "criterion", "outcome", "task", "topic", "definition", "fact", "other")
KIND_TITLE = {"deadline": "Deadlines and dates", "deliverable": "What to hand in", "criterion": "Marking criteria and weights",
              "outcome": "Learning outcomes", "task": "Tasks and instructions", "topic": "Key topics and readings",
              "definition": "Definitions and formulas", "fact": "Facts and background", "other": "Other points"}

STUDY = re.compile(r"\b(?:study|summari[sz]e|summary|analy[sz]e|go\s+through|ara[sş]t[ıi]r\w*|çal[ıi][sş]\w*|calis\w*|"
                   r"incele\w*|özetle\w*|ozetle\w*|özet\w*|analiz\w*)", re.I)
DRIVE_PDF = re.compile(r"[A-Za-z]:[\\/][^\"<>|*?\r\n]*?\.pdf\b", re.I)
DRIVE_ANY = re.compile(r"[A-Za-z]:[\\/][^\"<>|*?\r\n]+")
QUOTED = re.compile(r"[\"'“”‘’]([^\"'“”‘’\r\n]{3,300})[\"'“”‘’]")
BARE_PDF = re.compile(r"(?<![\w\\/:])([\w][\w .()+&,-]{0,100}?\.pdf)\b", re.I)


class Unreadable(Exception):
    """A PDF that can't be opened (encrypted, broken)."""


def minutes():
    return R._int("UMAY_STUDY_MINUTES", 60)


def max_chunks():
    return R._int("UMAY_STUDY_CHUNKS", 90)


# --- asking -------------------------------------------------------------------------------------------------------

def _inside_home(path):
    try:
        path.resolve().relative_to(Path.home().resolve())
        return True
    except (ValueError, OSError):
        return False


def _find_named(name):
    """A bare file name ("brief.pdf"): looked up in Downloads, Desktop and Documents, and one folder level below them."""
    for base in ("Downloads", "Desktop", "Documents"):
        root = Path.home() / base
        for pattern in (name, f"*/{name}"):
            try:
                for hit in sorted(root.glob(pattern)):
                    if hit.is_file():
                        return hit
            except OSError:
                pass
    return None


def _as_path(text):
    """The existing file or folder `text` names (inside the home folder), else None."""
    text = text.strip().strip(" .,;:")
    if not text:
        return None
    try:
        path = Path(os.path.expanduser(text))
        if path.is_absolute() and path.exists() and _inside_home(path):
            return path
    except OSError:
        pass
    return None


def _folder_prefix(raw):
    """An unquoted folder path with words after it ("C:\\Users\\me\\Course 1 please"): the longest start that is a folder."""
    words = raw.split(" ")
    while words:
        path = _as_path(" ".join(words))
        if path is not None and path.is_dir():
            return path
        words.pop()
    return None


def pdfs_in(path):
    """The PDFs a file or folder stands for, [Path] in name order (a folder: its tree, at most MAX_FILES)."""
    if path.is_file():
        return [path] if path.suffix.lower() == ".pdf" else []
    if path.is_dir():
        try:
            return sorted((p for p in path.rglob("*") if p.is_file() and p.suffix.lower() == ".pdf"), key=lambda p: str(p).lower())[:MAX_FILES]
        except OSError:
            return []
    return []


def asked(text):
    """[Path of a PDF] (a file, or a folder's PDFs) when `text` asks to study a PDF or a folder of them that exists on this PC (inside
    the home folder), else None. Needs a study word ("çalış", "özetle", "study"...), so "open this pdf" is left alone."""
    text = " ".join((text or "").split()) if "\n" not in (text or "") else (text or "")
    try:
        import voice
        text = text.split(voice.SPOKEN_HINT.strip()[:20])[0].strip()
    except Exception:
        pass
    words = DRIVE_PDF.sub(" ", QUOTED.sub(" ", text))      # a file name ("Assessment Brief.pdf") is not a study word
    if not STUDY.search(words) and not re.search(r"\bpdf\s+mod", words, re.I):
        return None
    found = []
    for match in QUOTED.finditer(text):
        found.append(_as_path(match.group(1)))
    for match in DRIVE_PDF.finditer(text):
        found.append(_as_path(match.group(0)))
    if not [f for f in found if f is not None]:
        for match in DRIVE_ANY.finditer(text):
            found.append(_folder_prefix(match.group(0)))
    if not [f for f in found if f is not None]:
        for match in BARE_PDF.finditer(text):
            words = match.group(1).split()
            for i in range(len(words)):      # "bu brief.pdf": the leading words may not be part of the name
                hit = _find_named(" ".join(words[i:]))
                if hit is not None:
                    found.append(hit)
                    break
    files = []
    for path in found:
        for pdf in pdfs_in(path) if path is not None else []:
            if pdf not in files:
                files.append(pdf)
    return files[:MAX_FILES] or None


# --- reading the files --------------------------------------------------------------------------------------------

def clean_page(text):
    """One page's text with the line-end hyphens joined ("assess-\\nment" -> "assessment") and the spacing collapsed."""
    text = unicodedata.normalize("NFKC", text or "")
    text = re.sub(r"(?<=[a-zçğıöşüA-ZÇĞİÖŞÜ])[-\u00ad]\s*\n\s*(?=[a-zçğıöşü])", "", text)
    return " ".join(text.split())


def page_texts(path):
    """[(page number, text)] of a PDF (pages with no text have ""), from pypdf. Raises Unreadable for an encrypted or broken file."""
    try:
        from pypdf import PdfReader
        reader = PdfReader(str(path))
        if reader.is_encrypted:
            try:
                if not reader.decrypt(""):
                    raise Unreadable("the file is password protected")
            except Unreadable:
                raise
            except Exception as e:
                raise Unreadable(f"the file is encrypted ({type(e).__name__})")
        pages = []
        for number, page in enumerate(reader.pages, 1):
            try:
                pages.append((number, clean_page(page.extract_text() or "")))
            except Exception:
                pages.append((number, ""))
        return pages
    except Unreadable:
        raise
    except Exception as e:
        raise Unreadable(f"can't be opened ({type(e).__name__}: {str(e)[:80]})")


def _split_long(text, size):
    """A page longer than `size` characters in parts that end on a space."""
    parts = []
    while len(text) > size:
        cut = text.rfind(" ", 0, size)
        cut = cut if cut > size // 2 else size
        parts.append(text[:cut].strip())
        text = text[cut:].strip()
    return parts + ([text] if text else [])


def make_chunks(pages, size=CHUNK_CHARS):
    """Pages grouped on their borders: [[(page, text), ...], ...], each chunk about `size` characters. Pages with no text are left out."""
    chunks, current, length = [], [], 0
    for number, text in pages:
        for part in _split_long(text, size) if text else []:
            if current and length + len(part) > size:
                chunks.append(current)
                current, length = [], 0
            current.append((number, part))
            length += len(part) + 1
    if current:
        chunks.append(current)
    return chunks


def chunk_text(chunk):
    """What a quote is checked against (the pages' texts joined by a space, no markers)."""
    return " ".join(text for _, text in chunk)


def page_of(quote, chunk):
    """The page whose text holds the quote, "a-b" when it runs over a border, else the chunk's page range."""
    for number, text in chunk:
        if R.quote_found(quote, text):
            return str(number)
    first, last = chunk[0][0], chunk[-1][0]
    return str(first) if first == last else f"{first}-{last}"


# --- the checks that are code ---------------------------------------------------------------------------------------

def keep_note(item, text):
    """The note as {kind, note, quote} when its quote stands in `text` (the chunk) and every number of the note is in its quote,
    else None."""
    note, quote = str(item.get("note") or "").strip(), str(item.get("quote") or "").strip()
    kind = item.get("kind") if item.get("kind") in KINDS else "other"
    if not note or not quote or not R.quote_found(quote, text):
        return None
    if not R.numbers(note) <= R.numbers(quote):
        return None
    return {"kind": kind, "note": note, "quote": quote}


def unsupported(summary, notes):
    """Numbers of a summary that no kept note or quote holds."""
    held = set()
    for n in notes:
        held |= R.numbers(n["note"]) | R.numbers(n["quote"])
    return sorted(x for x in R.numbers(summary) - held if len(x) > 1 or "." in x)


NOTE_SCHEMA = {"type": "object", "required": ["notes"], "properties": {"notes": {
    "type": "array", "maxItems": PER_CHUNK, "items": {
        "type": "object", "required": ["kind", "note", "quote"], "properties": {
            "kind": {"type": "string", "enum": list(KINDS)}, "note": {"type": "string"}, "quote": {"type": "string"}}}}}}
NOTE_SYSTEM = (
    "You read one part of a university document (a module brief, an assignment brief, lecture notes) for a student who will study it, "
    "and take out the points worth keeping. kind: deadline (a date or time something is due), deliverable (what must be handed in, "
    "its format, length, how), criterion (how it is marked: criteria, weights, grade bands), outcome (a learning outcome or aim), "
    "task (what the student is told to do), topic (a subject, reading or tool the module covers), definition (a term, rule or "
    "formula explained: only when the text itself defines it), fact (a fact or figure the text reports, a case's background), other. "
    "note: one plain sentence in English that says no more than the quote does. quote: copied WORD FOR WORD "
    "from the text, under 40 words, and it must prove the whole note; never copy a [page N] mark. Give only points that matter, at most 8, "
    "and none if the part is only a cover page, contents list or boilerplate.")
SUMMARY_SCHEMA = {"type": "object", "required": ["summary"], "properties": {"summary": {"type": "string"}}}
SUMMARY_SYSTEM = (
    "You write a study summary of a document for the student, 80 to 150 words of plain English, from the notes given and NOTHING "
    "else. Lead with what the document is for, then deadlines, what must be handed in and how it is marked, if the notes have them. "
    "Flowing prose, no list. Copy numbers and dates exactly as the notes give them; never add a fact, name, number or date that "
    "is not there. If the notes are thin, say so.")
OVERVIEW_SYSTEM = (
    "You write the overview of a set of documents (one course) for the student, 100 to 180 words of plain English, from the per-file "
    "summaries given and NOTHING else: what the course asks, the deadlines and hand-ins in time order, how it is marked. Flowing "
    "prose, no list. Copy numbers and dates exactly; never add one that is not there.")


# --- the job ------------------------------------------------------------------------------------------------------

def _out_of_time(job):
    return time.monotonic() - job["began"] - job.get("paused", 0.0) > minutes() * 60


def read_chunk(job, name, chunk):
    """The kept notes of one chunk: {"notes": [{kind, note, quote, page}], "dropped": n}. One model call."""
    text = chunk_text(chunk)
    shown = "\n".join(f"[page {number}] {part}" for number, part in chunk)
    prompt = f"Document: {name}\n\nText:\n{shown}"
    answer = R._ask(job, prompt, NOTE_SCHEMA, NOTE_SYSTEM, 1500)
    kept, dropped, seen = [], 0, set()
    for item in (answer.get("notes") or [])[:PER_CHUNK]:
        if not isinstance(item, dict):
            dropped += 1
            continue
        note = keep_note(item, text)
        key = note and (note["kind"], R.norm(note["note"]))
        if note is None:
            dropped += 1
        elif key not in seen:
            seen.add(key)
            kept.append(dict(note, page=page_of(note["quote"], chunk)))
    return {"notes": kept, "dropped": dropped}


def summarize_file(job, name, notes):
    lines = [f"- ({n['kind']}) {n['note']}" for n in notes][:60]
    prompt = f"Document: {name}\n\nNotes:\n" + "\n".join(lines)[:7000] + "\n\nWrite the summary now: at most 150 words."
    return str(R._ask(job, prompt, SUMMARY_SCHEMA, SUMMARY_SYSTEM, 700).get("summary") or "").strip()


def overview(job, files):
    text = "\n\n".join(f"{f['name']}: {f['summary']}" for f in files if f.get("summary"))[:7000]
    if not text:
        return ""
    prompt = f"Per-file summaries:\n{text}\n\nWrite the overview now: at most 180 words."
    return str(R._ask(job, prompt, SUMMARY_SCHEMA, OVERVIEW_SYSTEM, 900).get("summary") or "").strip()


def label(path, root):
    """The file's name for the report (relative to the folder when the job was a folder)."""
    try:
        return str(Path(path).relative_to(root)).replace("\\", "/")
    except ValueError:
        return Path(path).name


def render(job, files, stats, overview_text):
    now = datetime.now()
    out = [f"# PDF study: {job['topic']}", "",
           f"*{now:%Y-%m-%d %H:%M} · {stats['minutes']:.0f} min · {len(files)} file(s), {stats['pages']} pages, "
           f"{stats['chunks_done']} of {stats['chunks_total']} parts read · {stats['notes']} notes kept, {stats['dropped']} dropped "
           "(quote not found in the text, or a number the quote does not hold)*", ""]
    if overview_text:
        out += ["## Overview", "", overview_text, ""]
        if job.get("overview_check"):
            out += ["**Check:** the overview names numbers that no kept note holds: " + ", ".join(job["overview_check"]) +
                    ". Do not rely on them.", ""]
    for f in files:
        out += [f"## {f['name']}", ""]
        if f.get("error"):
            out += [f"Not read: {f['error']}.", ""]
            continue
        if f.get("scanned_all"):
            out += ["Not read: the pages have no text layer (scanned images). Umay has no OCR, so this file needs to be turned into "
                    "text first.", ""]
            continue
        facts = f"{f['pages']} pages"
        if f.get("blank"):
            facts += f"; no text on page(s) {', '.join(str(p) for p in f['blank'][:20])} (scanned or empty, not read)"
        if f.get("unread"):
            facts += f"; **not reached in the time limit: {f['unread']} of {f['chunks']} parts**"
        out += [f"*{facts}*", ""]
        if f.get("summary"):
            out += [f["summary"], ""]
            if f.get("summary_check"):
                out += ["**Check:** the summary names numbers that no kept note holds: " + ", ".join(f["summary_check"]) +
                        ". Do not rely on them.", ""]
        if not f["notes"]:
            out += ["No point with a quote that stands in the text was found in this file.", ""]
        for kind in KINDS:
            mine = [n for n in f["notes"] if n["kind"] == kind]
            if not mine:
                continue
            out += [f"### {KIND_TITLE[kind]}", ""]
            for n in mine:
                out.append(f"- {n['note']}  ")
                out.append(f"  > \"{n['quote']}\" (p. {n['page']})")
            out.append("")
    out += ["## Method and limits", "",
            "The text of each PDF was read on this PC (pypdf) and cut into parts of about 6000 characters; a local model (gpt-oss) "
            "took out the notes, and nothing was sent to a cloud model or the web. A note is kept only when its quote is found word "
            "for word in that part and every number of the note is in the quote; the page comes from the code. The summaries are "
            "the model's, written from the kept notes only. Scanned pages can't be read, and a quote that is in the file can still "
            "be misread: check what matters in the PDF at the page given.", ""]
    return "\n".join(out)


def _slug(job):
    return R._slug(job["topic"])


def _file_entries(job):
    """The files of the job as [{path, name}] with names relative to their common folder."""
    paths = [Path(p) for p in job["files"]]
    root = Path(os.path.commonpath([str(p.parent) for p in paths])) if len(paths) > 1 else None
    return [{"path": p, "name": label(p, root) if root else p.name} for p in paths]


def run(job):
    """The whole job (the thread's target, in place of research._run)."""
    began = time.monotonic()
    job["began"] = began
    try:
        R._stage(job, "reading the files")
        entries = _file_entries(job)
        notes = job.setdefault("notes", {})
        # 1. read every file's pages and cut the chunks (cheap: no model)
        plan = []
        for entry in entries:
            try:
                pages = page_texts(entry["path"])
            except Unreadable as e:
                entry["error"] = str(e)
                continue
            entry["pages"] = len(pages)
            entry["blank"] = [n for n, t in pages if not t]
            entry["scanned_all"] = bool(pages) and len(entry["blank"]) == len(pages)
            entry["chunks_list"] = make_chunks(pages)
            entry["chunks"] = len(entry["chunks_list"])
            for index, chunk in enumerate(entry["chunks_list"]):
                plan.append((entry, index, chunk))
        # 2. one model call per chunk, in order, kept on the disk after each
        limit, done_now = max_chunks(), 0
        for entry, index, chunk in plan:
            key = f"{entry['name']}#{index}"
            if key in notes and notes[key].get("done"):
                continue
            if R._job["stop"] or _out_of_time(job) or done_now >= limit:
                break
            R._stage(job, f"{entry['name']}: part {index + 1} of {entry['chunks']}")
            try:
                notes[key] = dict(read_chunk(job, entry["name"], chunk), done=True)
                job["fails"] = 0
            except R.Unavailable:
                job["fails"] = job.get("fails", 0) + 1     # one part that gets no answer is skipped, two in a row end the job
                if R._job["stop"] or job["fails"] >= 2:
                    raise
                notes[key] = {"notes": [], "dropped": 0, "done": True, "failed": True}
            done_now += 1
            R._save(job)
        if R._job["stop"]:
            raise R.Unavailable("stopped")
        # 3. summaries
        for entry in entries:
            entry["notes"] = []
            entry["unread"] = 0
            for index in range(entry.get("chunks", 0)):
                found = notes.get(f"{entry['name']}#{index}")
                if found and found.get("done"):
                    entry["notes"] += found.get("notes", [])
                else:
                    entry["unread"] += 1
            entry["dropped"] = sum(notes.get(f"{entry['name']}#{i}", {}).get("dropped", 0) for i in range(entry.get("chunks", 0)))
        for entry in entries:
            if entry["notes"] and not R._job["stop"]:
                R._stage(job, f"{entry['name']}: summary")
                entry["summary"] = summarize_file(job, entry["name"], entry["notes"])
                entry["summary_check"] = unsupported(entry["summary"], entry["notes"])
        overview_text = ""
        with_summary = [e for e in entries if e.get("summary")]
        if len(with_summary) > 1:
            R._stage(job, "overview")
            overview_text = overview(job, with_summary)
            every = [n for e in entries for n in e["notes"]]
            job["overview_check"] = unsupported(overview_text, every + [{"note": e["summary"], "quote": ""} for e in with_summary])
        stats = {"pages": sum(e.get("pages", 0) for e in entries),
                 "chunks_total": sum(e.get("chunks", 0) for e in entries),
                 "chunks_done": sum(e.get("chunks", 0) - e.get("unread", 0) for e in entries),
                 "notes": sum(len(e["notes"]) for e in entries), "dropped": sum(e.get("dropped", 0) for e in entries),
                 "minutes": (time.monotonic() - began - job.get("paused", 0.0)) / 60}
        report = render(job, entries, stats, overview_text)
        folder = R.out_dir()
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"{datetime.now():%Y-%m-%d_%H-%M}_{_slug(job)}.md"
        path.write_text(report, encoding="utf-8")
        job.update(done=True, report=str(path), stats=stats)
        R._save(job)
        _deliver(job, path, overview_text or next((e["summary"] for e in entries if e.get("summary")), ""), stats, entries)
    except R.Unavailable as e:
        R._tell(f"Study of \"{job['topic'][:80]}\" was stopped." if R._job["stop"] else
                f"Study of \"{job['topic'][:80]}\" stopped: {e}. The parts it had read are kept on the disk.")
    except Exception as e:  # a bug must not leave the owner waiting for a report
        R._tell(f"Study of \"{job['topic'][:80]}\" failed: {type(e).__name__}: {e}"[:400])
    finally:
        R._job.update(thread=None, stage="", stop=False)
        planner_server.forget_restore()   # our prompts took the slot: the next plan puts its own prefix back


def _deliver(job, path, summary, stats, entries):
    unread = [e["name"] for e in entries if e.get("error") or e.get("scanned_all")]
    note = f" Not readable: {', '.join(unread[:5])}." if unread else ""
    text = (f"PDF study done: {job['topic'][:100]}\n\n{summary.strip()}\n\n{stats['pages']} pages, {stats['notes']} notes kept.{note} "
            f"Report: {path}")
    R._tell(text[:3500])
    try:
        import telegram_listener
        telegram_listener.send_document(path.read_bytes(), path.name, f"PDF study: {job['topic'][:80]}")
    except Exception:
        pass


def start(files, source="other"):
    """Starts the job on research's thread slot and answers at once. One job (of either kind) at a time."""
    with R._lock:
        if R._job["thread"] is not None and R._job["thread"].is_alive():
            return f"I'm already working on \"{R._job['topic'][:60]}\", dear. It's at: {R._job['stage'] or 'starting'}."
        if not planner_server.available():
            return "I can't study the PDFs right now, dear: the local planner's model isn't there."
        note = " A game has the machine, so it waits until the game ends." if llama_server.hold["on"] else ""
        stamp = datetime.now()
        paths = [str(p) for p in files]
        topic = Path(paths[0]).stem[:80] if len(paths) == 1 else f"{len(paths)} PDFs in {Path(paths[0]).parent.name}"[:80]
        job = {"id": f"{stamp:%Y%m%d-%H%M%S}-{hashlib.sha1(chr(10).join(paths).encode('utf-8', 'replace')).hexdigest()[:6]}",
               "topic": topic, "created": stamp.isoformat(timespec="seconds"), "source": source, "mode": "pdf", "files": paths}
        R._launch(job)
    return (f"On it, dear. I'll study {len(paths)} PDF{'s' if len(paths) != 1 else ''} here on the PC, nothing goes to the web. "
            f"It can take a while for a long file. The report goes to Documents\\Umay Research and to Telegram.{note}")
