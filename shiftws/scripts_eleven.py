"""Script versions with synchronized ElevenLabs-tagged and clean text.

Canonical document (doc_json):
  {"title": str, "hooks": [{"text","formula","score"}...], "selected_hook": int,
   "sections": [{"id","label","tagged","notes","beats":[{"visual","evidence"}], "claims":[...]}],
   "pronunciations": [{"term","say_as","note"}], "editorial_notes": str}

Guarantees enforced here (not left to the writer):
  - audio tags appear by default (channel pref audio_tags_default=true) at hook / emotional shift / payoff;
  - the clean text is the tagged text minus delivery tags — same words, same order;
  - TTS text never contains timestamps, shot notes, speaker headings, URLs or citations;
  - tags known to be read aloud or excessive for this voice are removed and reported;
  - a new version never touches an existing locked narration take.
"""
from __future__ import annotations

import json
import re
from pathlib import Path

from .util import new_id, now

TAG_RE = re.compile(r"\[([^\[\]\n]{1,40})\]")
FORBIDDEN = [
    (re.compile(r"\b\d{1,2}:\d{2}(?::\d{2})?\b"), "timestamp"),
    (re.compile(r"\[(?:ON SCREEN|VISUAL|B-?ROLL|SFX|MUSIC|CUT|SHOT)[^\]]*\]", re.I), "shot/sound note"),
    (re.compile(r"^\s*[A-Z][A-Z ]{2,20}:\s", re.M), "speaker heading"),
    (re.compile(r"https?://\S+"), "URL"),
    (re.compile(r"\((?:source|via|citation|ref)[^)]*\)", re.I), "citation"),
    (re.compile(r"<[^>]+>"), "markup (SSML/HTML is not v3 audio-tag syntax)"),
]
SECTION_ORDER = ["hook", "setup", "progression", "payoff", "ending"]
DEFAULT_TAG_FOR = {"hook": "curious", "payoff": "excited", "ending": "thoughtful"}


def strip_tags(text: str) -> str:
    out = TAG_RE.sub(" ", text)
    out = re.sub(r"[ \t]+", " ", out)
    out = re.sub(r" +([,.!?;:])", r"\1", out)
    return "\n".join(l.strip() for l in out.splitlines()).strip()


def words(text: str) -> list[str]:
    return re.findall(r"[A-Za-z0-9']+", text.lower())


def _feedback(ws) -> tuple[dict, dict]:
    from .projects import narration_preset
    n = narration_preset(ws)
    return n, n["settings"].get("tag_feedback", {})


def prepare(ws, project_id: str, doc: dict) -> dict:
    """Normalize and validate a script document. Returns {doc, warnings, compat, ...}. Mutates nothing in the DB."""
    from .projects import pref
    preset, fb = _feedback(ws)
    tags_on = bool(pref(ws, project_id, "audio_tags_default", True))
    supports = bool(preset["settings"].get("supports_audio_tags"))
    bad_tags = set(fb.get("spoken_aloud", [])) | set(fb.get("excessive", []))
    known = set(preset["settings"].get("starter_tags", [])) | set(fb.get("works", []))
    warnings: list[str] = []
    sections = doc.get("sections") or []
    if not sections:
        raise ValueError("script has no sections")
    for i, s in enumerate(sections):
        s.setdefault("id", f"s{i + 1:02d}-{s.get('label', 'section')}")
        s.setdefault("label", SECTION_ORDER[min(i, len(SECTION_ORDER) - 1)])
        text = s.get("tagged") or s.get("text") or ""
        s.pop("text", None)
        for rx, what in FORBIDDEN:
            if rx.search(text):
                warnings.append(f"{s['id']}: removed {what} from spoken text (kept in notes)")
                s.setdefault("notes", "")
                s["notes"] = (s["notes"] + " " + " ".join(m.group(0) for m in rx.finditer(text))).strip()
                text = rx.sub(" ", text)
        found = TAG_RE.findall(text)
        for t in found:
            tl = t.strip().lower()
            if tl in bad_tags:
                text = text.replace(f"[{t}]", " ")
                warnings.append(f"{s['id']}: removed [{t}] — previously {('read aloud' if tl in fb.get('spoken_aloud', []) else 'excessive')} with this voice")
            elif len(tl.split()) > 3:
                text = text.replace(f"[{t}]", " ")
                warnings.append(f"{s['id']}: removed [{t}] — looks like a note, not a delivery tag")
            elif tl not in known:
                warnings.append(f"{s['id']}: [{t}] is not yet verified with this voice — listen for it")
        if not supports and TAG_RE.search(text):
            warnings.append(f"{s['id']}: the selected model may not support audio tags — use the clean version")
        text = re.sub(r"[ \t]+", " ", text).strip()
        # Strategic default: hook / payoff / ending get one tag if the writer left none.
        if tags_on and supports and not TAG_RE.search(text) and s["label"] in DEFAULT_TAG_FOR:
            choice = _pick_tag(s["label"], fb)
            text = f"[{choice}] {text}"
            warnings.append(f"{s['id']}: added [{choice}] (channel default: audio tags on)")
        s["tagged"] = text
        s["clean"] = strip_tags(text)
        if words(s["clean"]) != [w for w in words(TAG_RE.sub(" ", text))]:
            raise ValueError(f"{s['id']}: clean and tagged versions diverged")
    total_tags = sum(len(TAG_RE.findall(s["tagged"])) for s in sections)
    sentences = sum(max(1, len(re.findall(r"[.!?]+", s["clean"]))) for s in sections)
    if tags_on and sentences and total_tags / sentences > 0.6:
        warnings.append(f"tag density {total_tags}/{sentences} sentences is high — tags work best at hook, shifts and payoff")
    doc["sections"] = sections
    tagged = "\n\n".join(s["tagged"] for s in sections)
    clean = "\n\n".join(s["clean"] for s in sections)
    wc = len(words(clean))
    wpm, wpm_src = learned_wpm(ws)
    est = wc / wpm * 60
    limit = int(preset["settings"].get("max_chars_per_generation") or 5000)
    chunks = chunk_sections(sections, limit)
    compat = {"model": preset["model"], "voice": preset["voice"], "model_confirmed": bool(preset["confirmed"]),
              "supports_audio_tags": supports, "tags_used": sorted({t.lower() for s in sections
                                                                    for t in TAG_RE.findall(s["tagged"])}),
              "char_limit": limit, "char_limit_source": preset["settings"].get("max_chars_source"),
              "chunks": len(chunks)}
    if not preset["confirmed"]:
        warnings.append("ElevenLabs model/voice not confirmed yet — tags assume v3 (provisional)")
    from .projects import get_project
    target = get_project(ws, project_id)["brief"].get("length_seconds") or pref(ws, project_id, "target_seconds")
    gap = None
    if target:
        gap = round(target - est, 1)
        if abs(gap) > max(5, 0.15 * target):
            warnings.append(f"estimated read {est:.0f}s vs target {target}s ({'+' if gap > 0 else ''}{gap}s narration gap)")
    return {"doc": doc, "tagged": tagged, "clean": clean, "word_count": wc, "est_seconds": round(est, 1),
            "wpm": wpm, "wpm_source": wpm_src, "chunks": chunks, "compat": compat, "warnings": warnings,
            "target_seconds": target, "narration_gap_s": gap}


def _pick_tag(label: str, fb: dict) -> str:
    works = fb.get("works", [])
    pref_tag = DEFAULT_TAG_FOR.get(label, "curious")
    return pref_tag if not works or pref_tag in works else works[0]


def learned_wpm(ws) -> tuple[float, str]:
    rows = ws.db.all("SELECT metrics_json FROM narration_takes WHERE status IN ('aligned','locked') "
                     "AND is_synthetic_standin=0 ORDER BY created_at DESC LIMIT 5")
    vals = [json.loads(r["metrics_json"]).get("wpm") for r in rows]
    vals = [v for v in vals if v]
    if vals:
        return round(sum(vals) / len(vals), 1), f"observed from {len(vals)} real take(s)"
    return 150.0, "default 150 wpm (no real takes measured yet)"


def chunk_sections(sections, limit: int) -> list[dict]:
    chunks, cur, size = [], [], 0
    for s in sections:
        n = len(s["tagged"]) + 2
        if cur and size + n > limit:
            chunks.append({"sections": [x["id"] for x in cur], "chars": size})
            cur, size = [], 0
        cur.append(s)
        size += n
    if cur:
        chunks.append({"sections": [x["id"] for x in cur], "chars": size})
    return chunks


def score_hooks(ws, hooks: list[str]) -> list[dict]:
    """Score hook candidates with the yt-script skill's hookscore.py (editorial heuristic, not a predictor)."""
    from .skills import run_skill_tool
    out = []
    for h in hooks:
        try:
            r = run_skill_tool("yt-script", "hookscore.py", ["--json", "--hook", h])
            out.append({"text": h, "panel": r.get("properties"), "band": r.get("band"),
                        "formula": r.get("formula"), "score": r.get("total"),
                        "note": "hookscore.py is an editorial heuristic, not a view prediction"})
        except Exception as e:  # noqa: BLE001
            out.append({"text": h, "error": str(e)})
    return out


def save_script(ws, project_id: str, doc: dict, *, change_note: str = "", origin: str = "user",
                thread_id: str | None = None, approve: bool = False, deliverable_id: str | None = None) -> dict:
    from .projects import get_deliverable
    deliverable_id = get_deliverable(ws, deliverable_id, project_id)["id"]
    prep = prepare(ws, project_id, json.loads(json.dumps(doc)))
    last = ws.db.one("SELECT * FROM script_versions WHERE project_id=? ORDER BY version DESC LIMIT 1", (project_id,))
    version = (last["version"] + 1) if last else 1
    sid = new_id("scr")
    status = "approved" if approve else "draft"
    if approve:
        ws.db.x("UPDATE script_versions SET status='superseded' WHERE project_id=? AND deliverable_id IS ? AND "
                "status='approved'", (project_id, deliverable_id))
    ws.db.insert("script_versions", {"deliverable_id": deliverable_id,
        "id": sid, "project_id": project_id, "version": version, "parent_id": last["id"] if last else None,
        "status": status, "title": prep["doc"].get("title", ""), "doc_json": prep["doc"], "tagged_text": prep["tagged"],
        "clean_text": prep["clean"], "word_count": prep["word_count"], "est_seconds": prep["est_seconds"],
        "target_model": prep["compat"]["model"], "compat_json": {**prep["compat"], "warnings": prep["warnings"]},
        "change_note": change_note, "created_at": now()})
    files = export_files(ws, project_id, sid)
    ws.index("script", sid, f"Script v{version}: {prep['doc'].get('title', '')}", prep["clean"],
             project_id=project_id, thread_id=thread_id, locator={"script_id": sid, "version": version})
    ws.remember(kind="decision", subject=f"Script v{version} saved" + (" and approved" if approve else ""),
                content=(change_note or "new script version") + f" — {prep['word_count']} words, ~{prep['est_seconds']:.0f}s",
                project_id=project_id, thread_id=thread_id, ref_type="script", ref_id=sid, provenance=origin)
    affected = _affected_takes(ws, project_id, prep["clean"], prep["tagged"])
    from .projects import set_stage, write_manifest
    set_stage(ws, project_id, "script")
    ws.automation(project_id, "script_exports", "automated", "tagged + clean + sections + pronunciations + beats")
    if approve:
        ws.close_task(project_id, "Write and approve the script")
    write_manifest(ws, project_id)
    return {"script_id": sid, "version": version, "status": status, "word_count": prep["word_count"],
            "est_seconds": prep["est_seconds"], "wpm_source": prep["wpm_source"], "warnings": prep["warnings"],
            "compat": prep["compat"], "files": files, "narration_impact": affected,
            "copy_for_elevenlabs": prep["tagged"]}


def _affected_takes(ws, project_id, clean, tagged) -> list[dict]:
    """A new version never rewrites a locked take — it reports whether the take now differs from the script."""
    out = []
    for t in ws.db.all("SELECT nt.id, nt.take_no, nt.status, sv.clean_text, sv.tagged_text, sv.version FROM narration_takes nt "
                       "JOIN script_versions sv ON sv.id=nt.script_version_id WHERE nt.project_id=? AND "
                       "nt.status IN ('aligned','locked')", (project_id,)):
        if t["clean_text"] != clean:
            out.append({"take_id": t["id"], "take_no": t["take_no"], "status": t["status"], "change": "words changed",
                        "action": "Regenerate in ElevenLabs and import the new take; the locked take stays untouched."})
        elif t["tagged_text"] != tagged:
            out.append({"take_id": t["id"], "take_no": t["take_no"], "status": t["status"], "change": "delivery tags changed",
                        "action": "Optional regeneration for the new delivery; words are unchanged."})
    return out


def get_script(ws, script_id: str | None = None, project_id: str | None = None,
               deliverable_id: str | None = None) -> dict | None:
    if script_id:
        r = ws.db.one("SELECT * FROM script_versions WHERE id=?", (script_id,))
    elif deliverable_id:
        r = ws.db.one("SELECT * FROM script_versions WHERE deliverable_id=? ORDER BY (status='approved') DESC, "
                      "version DESC LIMIT 1", (deliverable_id,))
    else:
        r = ws.db.one("SELECT * FROM script_versions WHERE project_id=? ORDER BY (status='approved') DESC, version DESC "
                      "LIMIT 1", (project_id,))
    if not r:
        return None
    r["doc"] = json.loads(r.pop("doc_json"))
    r["compat"] = json.loads(r.pop("compat_json"))
    return r


def approve(ws, script_id: str, origin: str = "user") -> dict:
    s = get_script(ws, script_id)
    ws.db.x("UPDATE script_versions SET status='superseded' WHERE project_id=? AND status='approved'", (s["project_id"],))
    ws.db.update("script_versions", {"id": script_id}, {"status": "approved"})
    ws.remember(kind="approval", subject=f"Script v{s['version']} approved", content="Approved for narration.",
                project_id=s["project_id"], ref_type="script", ref_id=script_id, provenance=origin)
    ws.close_task(s["project_id"], "Write and approve the script")
    from .projects import write_manifest
    write_manifest(ws, s["project_id"])
    return get_script(ws, script_id)


def export_files(ws, project_id: str, script_id: str) -> dict:
    from .projects import get_project
    s = get_script(ws, script_id)
    d = Path(get_project(ws, project_id)["root_dir"]) / "scripts" / f"v{s['version']:02d}"
    d.mkdir(parents=True, exist_ok=True)
    doc = s["doc"]
    (d / "script_elevenlabs_tagged.txt").write_text(s["tagged_text"] + "\n")
    (d / "script_spoken_clean.txt").write_text(s["clean_text"] + "\n")
    secs = [{"id": x["id"], "label": x["label"], "tagged": x["tagged"], "clean": x["clean"],
             "words": len(words(x["clean"])), "chars": len(x["tagged"])} for x in doc["sections"]]
    prep_chunks = chunk_sections(doc["sections"], int(s["compat"].get("char_limit") or 5000))
    (d / "script_sections.json").write_text(json.dumps({"script_id": script_id, "version": s["version"],
                                                        "model": s["compat"].get("model"), "sections": secs,
                                                        "chunks": prep_chunks}, indent=2))
    pron = ["# Pronunciation notes", ""] + [f"- **{p['term']}** → say \"{p.get('say_as', '')}\"" +
                                            (f" — {p['note']}" if p.get("note") else "")
                                            for p in doc.get("pronunciations", [])]
    if len(pron) == 2:
        pron.append("- (none yet)")
    pron += ["", "Use these in ElevenLabs' pronunciation dictionary or by respelling; they are not part of the TTS text."]
    (d / "pronunciation_notes.md").write_text("\n".join(pron) + "\n")
    beats = [{"section_id": x["id"], "label": x["label"], "beats": x.get("beats", []), "notes": x.get("notes", ""),
              "claims": x.get("claims", [])} for x in doc["sections"]]
    (d / "beat_plan.json").write_text(json.dumps({"script_id": script_id, "beats": beats,
                                                  "editorial_notes": doc.get("editorial_notes", ""),
                                                  "hooks": doc.get("hooks", [])}, indent=2))
    return {k: str(d / k) for k in ("script_elevenlabs_tagged.txt", "script_spoken_clean.txt", "script_sections.json",
                                    "pronunciation_notes.md", "beat_plan.json")}


def record_tag_feedback(ws, *, works=(), excessive=(), spoken_aloud=(), rejected_styles=(), project_id=None) -> dict:
    """Remember which tags work with the chosen voice. Future scripts use this automatically."""
    from .projects import update_narration_preset
    fb = {k: [t.strip("[] ").lower() for t in v] for k, v in
          {"works": works, "excessive": excessive, "spoken_aloud": spoken_aloud, "rejected_styles": rejected_styles}.items() if v}
    p = update_narration_preset(ws, feedback=fb, provenance="user feedback on narration take")
    return p["settings"]["tag_feedback"]
