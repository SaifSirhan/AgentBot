"""Label the stored group-GIF library by the emotion each GIF does the work of.

The GIF library holds memes whose meaning is not in their pixels — "facepalm",
"bro is done", "sarcastic clap" are labels a person would apply from having
seen the GIF in a conversation, not from reading a frame. So labeling uses both
signals available: what the frames look like, and the chat context recorded
alongside each occurrence (see telegram_media.save_group_gif).

Labels are written to labels.jsonl, one record per hash, append-only. Labeling
is resumable and idempotent: an already-labeled hash is skipped, so a run that
dies partway through can simply be repeated.

Cost is real — one vision call plus one text call per GIF. Use min_occurrences
to start with the GIFs the group actually reuses.
"""

import json
import os

# Imported lazily inside the functions that need it: this module is safe to
# import in contexts (like the GUI) where the vision stack isn't available.
GROUP_GIFS_DIR = os.path.join(
    os.environ.get("USERPROFILE", ""), "Downloads", "GroupGifs")


def _root():
    """Library root, resolved per call so the path can be redirected in tests."""
    return GROUP_GIFS_DIR or os.path.join(
        os.environ.get("USERPROFILE", ""), "Downloads", "GroupGifs")


def _by_hash_dir():
    return os.path.join(_root(), "by_hash")


def _occurrences_file():
    return os.path.join(_root(), "occurrences.jsonl")


def _labels_file():
    return os.path.join(_root(), "labels.jsonl")


def _read_jsonl(path):
    """Yield parsed objects, skipping malformed lines.

    A partially-written final line is expected after an interrupted append;
    one bad row must not make the whole library unreadable.
    """
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            try:
                yield json.loads(line)
            except Exception:
                continue


def _load_occurrences():
    """Return {hash: [occurrence_dicts]}, in file order."""
    buckets = {}
    for r in _read_jsonl(_occurrences_file()):
        h = r.get("hash")
        if h:
            buckets.setdefault(h, []).append(r)
    return buckets


def _load_labels():
    """Return {hash: label_dict}. Later records win, so relabeling works."""
    labels = {}
    for r in _read_jsonl(_labels_file()):
        h = r.get("hash")
        if h:
            labels[h] = r
    return labels


def _save_label(record):
    os.makedirs(_root(), exist_ok=True)
    with open(_labels_file(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _find_gif_file(digest):
    """Path to the stored file for a hash, or None. The extension varies —
    animations are stored as .mp4, GIFs as .gif."""
    for ext in (".gif", ".mp4", ".webm"):
        p = os.path.join(_by_hash_dir(), digest[:16] + ext)
        if os.path.exists(p):
            return p
    return None


_LABEL_PROMPT = (
    "These are evenly-spaced frames from one GIF, in order.\n\n"
    "Give 3 to 5 SHORT emotion/context labels describing WHEN this GIF would "
    "be sent, not what it shows. Lowercase, comma-separated, no explanation.\n"
    "Examples: celebration, facepalm, bro is done, sarcastic clap, "
    "peak comedy, mocking."
)


def label_gifs(min_occurrences=1, limit=None):
    """Label unlabeled GIFs in the library. Returns a summary string.

    One vision call per GIF carrying all frames, rather than one per frame:
    fewer requests, and the model sees the motion instead of a series of
    stills. Thinking stays ON here (unlike the video path) because the answer
    is a judgement about meaning, which is exactly what reasoning buys.
    """
    import agent
    from telegram_media import extract_gif_frames, cleanup_frames

    occurrences = _load_occurrences()
    existing = _load_labels()
    candidates = [
        (h, occs) for h, occs in occurrences.items()
        if h not in existing and len(occs) >= min_occurrences
    ]
    # Most-reused first: the group's own behaviour is the best signal about
    # which GIFs are worth spending money on.
    candidates.sort(key=lambda x: -len(x[1]))
    if limit:
        candidates = candidates[:limit]

    if not candidates:
        return (f"Nothing to label — {len(existing)} already labeled, "
                f"{len(occurrences)} unique GIFs known.")

    labeled = failed = 0
    for h, occs in candidates:
        gif_path = _find_gif_file(h)
        if not gif_path:
            print(f"[label] {h[:8]}: stored file missing, skipped")
            failed += 1
            continue

        try:
            paths, frames_dir = extract_gif_frames(gif_path, num_frames=4)
        except Exception as e:
            print(f"[label] {h[:8]}: decode failed: {type(e).__name__}: {e}")
            failed += 1
            continue

        try:
            if not paths:
                print(f"[label] {h[:8]}: no frames decoded")
                failed += 1
                continue

            # What it looks like.
            try:
                tags = agent.describe_images(paths, _LABEL_PROMPT)
            except Exception as e:
                print(f"[label] {h[:8]}: vision raised: {type(e).__name__}: {e}")
                tags = ""
            if not tags or tags.startswith("ERROR"):
                print(f"[label] {h[:8]}: vision failed: {str(tags)[:150]}")
                failed += 1
                continue

            # Where it was actually used — the stronger signal when present.
            sample_contexts = []
            for o in occs[:3]:
                ctx = o.get("recent_messages") or []
                if ctx:
                    sample_contexts.append(" | ".join(ctx[-3:]))

            prompt = (f"GIF frames (tags): {tags}\n\n")
            if sample_contexts:
                prompt += "Sample chat contexts where this GIF was sent:\n"
                for c in sample_contexts:
                    prompt += f"- {c}\n"
                prompt += ("\nLabel by what the GIF was communicating in those "
                           "contexts.\n\n")
            prompt += _LABEL_PROMPT

            try:
                raw = agent.ask_llm_direct(prompt, max_tokens=200)
            except Exception as e:
                print(f"[label] {h[:8]}: label call raised: {type(e).__name__}: {e}")
                raw = ""

            labels = []
            if raw and not raw.startswith(("Error:", "ERROR")):
                labels = [l.strip().lower().rstrip(".")
                          for l in raw.split(",") if l.strip()][:5]
            if not labels:
                print(f"[label] {h[:8]}: no labels produced: {str(raw)[:150]}")
                failed += 1
                continue

            _save_label({
                "hash": h,
                "labels": labels,
                "occurrences": len(occs),
                "sample_context": sample_contexts[0] if sample_contexts else "",
                # Keep the tags: they cost a vision call already, and a later
                # relabel without them would have to pay for it again.
                "frame_tags": tags,
            })
            labeled += 1
            print(f"[label] {h[:8]} -> {labels}")
        finally:
            cleanup_frames(frames_dir)

    return f"Labeled {labeled} GIFs. Failed: {failed}. " + library_stats()


def library_stats():
    """Unique GIFs, total occurrences, and how many are labeled."""
    occs = _load_occurrences()
    labels = _load_labels()
    total_occ = sum(len(v) for v in occs.values())
    if not occs:
        return "Library empty."
    return (f"{len(occs)} unique GIFs, {total_occ} occurrences, "
            f"{len(labels)} labeled.")


def find_by_emotion(emotion, max_results=3):
    """Return hashes whose labels match an emotion, most-used first.

    Substring match in both directions, so 'celebrate' finds 'celebration' and
    'sarcastic clap' is found by 'clap'.
    """
    emotion = (emotion or "").strip().lower()
    if not emotion:
        return []
    occs = _load_occurrences()
    labels = _load_labels()
    matches = []
    for h, rec in labels.items():
        for label in rec.get("labels") or []:
            if emotion in label or label in emotion:
                matches.append((h, len(occs.get(h, []))))
                break
    matches.sort(key=lambda x: -x[1])
    return [h for h, _ in matches[:max_results]]


def get_gif_path(digest):
    """Public path lookup for a stored GIF, or None."""
    return _find_gif_file(digest)
