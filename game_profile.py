"""Per-game configuration for GameTextSpeaker.

THE SPLIT THIS MODULE EXISTS TO ENFORCE
---------------------------------------
Everything specific to one GAME lives in a profile file (profiles/<slug>.json):
where the dialogue box is, how that game marks who's speaking, and which
character is which. Everything specific to YOUR SETUP stays in the app's own
settings: which speech engine you run and which voices you happen to have
installed.

That split is deliberate. A voice name is engine-specific -- "af_sarah" means
nothing to espeak-ng, and Piper's voices are bare integers -- so if a profile
named actual voices it would break the moment you switched engines, and it
could never be shared with someone who uses a different one. Instead a profile
says "Tommas is male character #0" and your settings say what male #0 sounds
like on the engine you're currently running. Switch from Kokoro to espeak-ng
and every character stays *distinct*; only the actual timbre changes.

NOTHING IN HERE KNOWS ABOUT ANY PARTICULAR GAME. Speaker detection (see
detect_speaker() below) is one general rule -- "the name lives in this
screen region, optionally split on this character" -- and a profile
supplies the region and the (optional) split character. Adding support for
a new game should mean drawing a box and writing a small JSON file, never
editing this module.

A SECOND, NARROWER SPLIT WITHIN A PROFILE ITSELF
-------------------------------------------------
Even within one profile, not everything is equally shareable. `region` and
`name_region` are raw screen-pixel coordinates -- tied to one person's
resolution and window layout, meaningless (or actively wrong) on anyone
else's screen. `cast` (who sounds like what) and `ocr_corrections` (fixes
for a glyph confusion specific to this game's own font) are the opposite:
both travel with the GAME, not the player, so they're exactly what someone
else playing the same game would want from you regardless of their own
screen setup. Profile.export_shared()/import_shared() (see Cast.merge()
and merge_ocr_corrections()) split that portable half out into its own
small file for exactly that reason -- see the main window's Export/Import
buttons and the Cast/OCR Corrections windows in gui.py.
"""

import difflib
import json
import os
import re
import unicodedata
from pathlib import Path

PROFILE_DIR = Path(__file__).with_name("profiles")
PROFILE_VERSION = 1

# Where a shared, portable profile fragment (see Profile.export_shared()/
# import_shared()) defaults to living, relative to PROFILE_DIR -- separate
# from the full profile files themselves, which carry screen coordinates
# and other per-machine settings that don't belong in something meant to
# be handed to someone else or committed for other players of the same
# game to pull from.
SHARED_EXPORT_DIRNAME = "shared"

# Identifies a shared-profile-fragment file as such (as opposed to, say, a
# full profile someone tries to import by mistake) -- checked loosely, not
# enforced as a strict version gate, since the shape has been stable since
# this was added.
SHARED_EXPORT_SCHEMA = "game-text-speaker-shared"

# The narrator is not "no speaker" -- in a lot of games (including second-person
# narration like "You and Tommas spend the days...") it's a voice in its own
# right and deserves to be cast deliberately rather than inheriting whoever
# spoke last. So it's a real, reserved cast member, always present.
NARRATOR = "Narrator"



# --------------------------------------------------------------------------
# Name matching
#
# OCR does not return the same string twice. On the very screenshot this was
# developed against, Tesseract turned "as soon as I get" into "as soon as |
# get" -- and character names corrupt exactly the same way. So a name is never
# used as a dict key directly: every cast member carries the set of spellings
# actually seen for them, and an unrecognized spelling gets one fuzzy chance
# to join an existing member before it's treated as somebody new.
# --------------------------------------------------------------------------

_PUNCT_RE = re.compile(r"[^\w\s]", re.UNICODE)
_WS_RE = re.compile(r"\s+", re.UNICODE)

# Above this, two spellings are considered the same character. Tuned to be
# forgiving of a wrong letter or two in a name of ordinary length while still
# keeping genuinely different names apart -- "Tommas"/"Thomas" match, but
# "Tommas"/"Elena" are nowhere close.
FUZZY_MATCH_THRESHOLD = 0.85

# A name has to be seen this many times before it's written to disk. A one-off
# OCR misread ("Tornmas Guerro") would otherwise permanently litter the cast
# file. It still gets a voice on its FIRST appearance -- see Cast.observe() --
# so nothing sounds wrong while we wait to be sure; it just isn't persisted.
SIGHTINGS_BEFORE_PERSIST = 2

MAX_NAME_LENGTH = 40


def normalize_name(raw: str) -> str:
    """Fold a raw OCR'd name to a comparison key: strip accents and
    punctuation, collapse whitespace, casefold. Deliberately lossy -- this is
    only ever used for matching, never for display or speech."""
    if not raw:
        return ""
    decomposed = unicodedata.normalize("NFKD", raw)
    stripped = "".join(c for c in decomposed if not unicodedata.combining(c))
    return _WS_RE.sub(" ", _PUNCT_RE.sub(" ", stripped)).strip().casefold()


def similar(a: str, b: str) -> float:
    return difflib.SequenceMatcher(None, a, b).ratio()


def model_key(engine: str, piper_model=None, kokoro_voices=None) -> str:
    """Identifies one loaded voice set -- the unit a cast member's voice
    assignment (Cast.get_model()/set_model()) is stored against. espeak-ng
    and Windows SAPI don't have a "model" distinct from the voice/name
    itself, so the engine name alone is the key.

    Piper is scoped to its one loaded .onnx file, since that IS the voice
    set for Piper -- there's no separate file defining what speakers a
    given model has. Kokoro is scoped to its VOICES file instead, deliberately
    NOT the model file it's loaded alongside: kokoro-onnx keeps voice
    embeddings (what "af_heart" etc. actually sound like) entirely in a
    separate voices-*.bin, and the .onnx file just runs inference against
    whichever one is loaded. Two quantized variants of the same Kokoro
    model paired with the same voices file genuinely have identical voices
    in every way that matters here -- swapping between them (e.g. to try a
    faster or higher-quality quantization) shouldn't force every
    character's assignment to be redone, the way keying on the model file
    used to.

    Keyed on the filename STEM rather than the full path, on purpose: a
    profile's cast assignments should survive moving the game-text-speaker
    folder, or being handed to someone else whose copy of the same voice
    file lives somewhere else on disk. Voice/model filenames already follow
    a readable naming convention (locale, voice, quality), so the stem
    doubles as a perfectly serviceable label with no extra configuration --
    the full path stays exactly where it already lived, in the app's own
    settings, purely to locate and load the file."""
    if engine == "piper" and piper_model:
        return f"piper:{Path(piper_model).stem}"
    if engine == "kokoro" and kokoro_voices:
        return f"kokoro:{Path(kokoro_voices).stem}"
    return engine


# --------------------------------------------------------------------------
# Per-game OCR corrections
#
# game_text_speaker.clean_ocr_text() already handles OCR/screen-artifact
# noise that's generic to Tesseract, regardless of which game produced it --
# a lone stray punctuation mark, a spurious mid-sentence period. What
# belongs HERE instead is a glyph confusion caused by one particular game's
# FONT -- e.g. an opening quote mark sitting close enough against a capital
# "I" that a font renders them as a single blob Tesseract reads as "T". A
# different game's font might never produce that confusion, or might
# produce a completely different one, so baking a fix like that into the
# app as a blanket rule for every profile is exactly the kind of hack that's
# right for one game and wrong for the next. A profile's own
# "ocr_corrections" list is where a fix like that lives instead.
# --------------------------------------------------------------------------

def apply_ocr_corrections(text, corrections, log=print):
    """Apply one profile's "ocr_corrections" -- a list of {"pattern":
    <regex>, "replace": <replacement, may use \\1-style backreferences>}
    entries, matched with re.fullmatch() against one whitespace-split token
    at a time, in list order.

    Deliberately whole-token (fullmatch), not a substring search: a pattern
    aimed at fixing one specific misread ("T" -> "I") could otherwise clip
    a letter or two out of the middle of an unrelated real word it happens
    to appear inside of. A token gets tested against every entry in order,
    each one seeing whatever the previous entry left behind -- so entries
    can be written to depend on each other, though most profiles will only
    ever need one.

    A bad entry -- an invalid regex, or a replacement string that doesn't
    match the pattern's own group count -- is logged and skipped rather
    than raised, so one broken correction in a profile someone downloaded
    doesn't take down OCR for the whole session."""
    if not corrections or not text:
        return text
    compiled = []
    for c in corrections:
        if not isinstance(c, dict):
            continue
        pattern, replace = c.get("pattern"), c.get("replace")
        if not pattern or replace is None:
            continue
        try:
            compiled.append((re.compile(pattern), replace))
        except re.error as e:
            log(f"[ocr] Skipping invalid ocr_corrections pattern {pattern!r}: {e}")
    if not compiled:
        return text
    out_tokens = []
    for tok in text.split():
        for rx, replace in compiled:
            m = rx.fullmatch(tok)
            if not m:
                continue
            try:
                tok = m.expand(replace)
            except re.error as e:
                log(f"[ocr] Skipping invalid ocr_corrections replacement {replace!r} for pattern "
                    f"{rx.pattern!r}: {e}")
        out_tokens.append(tok)
    return " ".join(out_tokens)


def merge_ocr_corrections(existing, incoming):
    """Fold a shared file's OCR corrections into a profile's own list --
    see Profile.export_shared()/import_shared(). Deliberately NOT
    Cast.merge(): this is plain rule data, not cast entries, and there's no
    natural key to overwrite by the way a character's name is one -- just a
    list of rules to have. Each {"pattern", "replace"} pair from `incoming`
    that isn't already present (exact pattern+replace match) is appended,
    in order; an exact repeat of one already there is skipped rather than
    duplicated. Order of the existing list is preserved; new ones land at
    the end."""
    out = [dict(c) for c in (existing or []) if isinstance(c, dict)]
    seen = {(c.get("pattern"), c.get("replace")) for c in out}
    for c in incoming or []:
        if not isinstance(c, dict):
            continue
        key = (c.get("pattern"), c.get("replace"))
        if key in seen:
            continue
        out.append({"pattern": c.get("pattern"), "replace": c.get("replace")})
        seen.add(key)
    return out


# --------------------------------------------------------------------------
# Speaker detection
#
# One mechanism, not a menu of heuristics: a profile points at a SECOND,
# independently-captured screen region -- the "name region" -- where a
# character's name shows up (a nameplate, a portrait label, a bar above the
# text). It's captured and OCR'd on its own every poll, entirely separate
# from the dialogue box, and whatever's in it either reads like a name or it
# doesn't -- see detect_speaker().
#
# A blank crop there IS the answer for a narration line, not a gap to work
# around: nobody's nameplate is showing, so there's nothing to OCR at all
# (see ink_density() -- the ink check happens before OCR even runs, in
# game_text_speaker.run()) and the narrator gets the line. An earlier
# version of this let a name region OVERLAP the dialogue box and split the
# combined text on a configurable character (a quote mark, most often) to
# tell them apart -- useful right up until dialogue length pushed the
# nameplate in or out of a box sized for both. Pointing the name region at
# just the nameplate and letting a blank crop mean "no name" is both
# simpler and doesn't depend on how long any given line runs.
# --------------------------------------------------------------------------


def _looks_like_name(candidate: str) -> bool:
    """Sanity check for whatever detect_speaker() pulled out of the name
    region: does this actually read like a name, or is it a stray mark, a
    page number, or an OCR fragment that happened to land in that box?

    It's empty or absurdly long, it's nothing but digits and punctuation
    ("17)", "1"), it ends mid-sentence ("...intoxicating."), or its words
    aren't Capitalized the way a name (or an ALL-CAPS name, or initials)
    would be ("ife feels positively")."""
    candidate = (candidate or "").strip()
    if not candidate or len(candidate) > MAX_NAME_LENGTH:
        return False
    if candidate[-1] in ".,;:!?":
        return False  # trails off like the end of a sentence, not a label
    words = [w for w in candidate.split() if any(c.isalpha() for c in w)]
    if not words:
        return False  # nothing alphabetic at all -- a number or bare symbol
    if not all(w[0].isupper() for w in words):
        return False  # not Title Case (or ALL CAPS) -- probably prose
    return True


def ink_density(img, threshold: int = 140) -> float:
    """Fraction of pixels dark enough to count as ink. Used to answer "is
    there anything in this box at all" without comparing against a stored
    reference image -- which matters because the answer has to hold for a
    character we've never seen before. Also what lets a blank name-region
    crop (nobody's nameplate showing -- the narrator is talking) skip OCR
    entirely instead of risking a misread on nothing."""
    grey = img.convert("L").point(lambda p: 255 if p < threshold else 0)
    w, h = grey.size
    if not w or not h:
        return 0.0
    return grey.histogram()[255] / float(w * h)


class Observation:
    """One poll's worth of what we can see, handed to detect_speaker().

    `text` is the OCR'd dialogue-box text -- what actually gets spoken,
    before any name is split back out of it. `name_text` is the OCR'd
    content of the profile's name region for this same poll (see
    Profile.detect()), or empty when no name region is configured, or its
    crop looked blank this time (see ink_density -- handled by the caller
    in game_text_speaker.run(), before an Observation is even built)."""

    def __init__(self, text, name_text=""):
        self.text = text or ""
        self.name_text = name_text or ""


def detect_speaker(name_text, dialogue_text):
    """The one general way this app locates a speaker: text OCR'd from a
    second, independently-captured region (a profile's `name_region`),
    checked against _looks_like_name(). Nothing splits or parses it out of
    the dialogue text itself -- a blank crop in the name region already
    means "no name here" (see ink_density(), checked before OCR even runs,
    in game_text_speaker.run()), so whatever text does come back from that
    region either reads like a name or it doesn't.

    Returns (name_or_None, dialogue). No name found is a real answer, not a
    failure -- it means the narrator is talking."""
    name_text = (name_text or "").strip()
    if not name_text:
        return None, dialogue_text

    candidate = name_text
    if not _looks_like_name(candidate):
        return None, dialogue_text

    # If the name region happens to overlap the dialogue box (the same
    # words captured twice, once for each region), strip the name back out
    # of dialogue_text so it isn't read/spoken twice. A name region that
    # sits somewhere else on screen entirely just won't match here, and
    # dialogue_text comes back untouched -- which is correct too.
    dialogue = dialogue_text
    if normalize_name(dialogue).startswith(normalize_name(candidate)):
        cut = len(candidate)
        while cut < len(dialogue) and not dialogue[cut].isalnum():
            cut += 1
        stripped = dialogue[cut:].strip()
        if stripped:
            dialogue = stripped
    return candidate, dialogue


# --------------------------------------------------------------------------
# Cast
# --------------------------------------------------------------------------

class Cast:
    """The characters this profile has met, and what voice each got, per model.

    Nothing here is filled in ahead of time. You cannot know an RPG's cast
    before you play it, so the cast list is an OUTPUT of reading -- characters
    are appended as they first speak, and the file is a record of what
    happened rather than something you have to prepare."""

    def __init__(self, entries=None):
        self.entries = []
        for e in entries or []:
            self.entries.append(self._clean(e))
        self._pending = {}  # normalized name -> sightings, in memory only
        self.dirty = False
        if not self.find(NARRATOR):
            # Starts with nothing assigned under any model -- same as any
            # other character nobody's cast yet. See get_model()/set_model().
            self.entries.insert(0, {"name": NARRATOR, "aliases": [], "models": {}})

    @staticmethod
    def _clean(e):
        return {
            "name": e.get("name", ""),
            "aliases": list(e.get("aliases") or []),
            "provisional": bool(e.get("provisional", False)),
            "models": {str(k): dict(v) for k, v in (e.get("models") or {}).items()
                       if isinstance(v, dict)},
        }

    def find(self, name):
        key = normalize_name(name)
        for e in self.entries:
            if normalize_name(e["name"]) == key:
                return e
        return None

    def match(self, raw_name):
        """Resolve an OCR'd name to a cast member, learning the spelling.

        Exact match against every spelling already seen comes first, so the
        common case is cheap. Only a miss falls back to fuzzy comparison --
        and when THAT hits, the new spelling is recorded as an alias, so the
        same misread is an exact match from then on. Matching gets more
        reliable the longer you play."""
        key = normalize_name(raw_name)
        if not key:
            return None
        for e in self.entries:
            if key == normalize_name(e["name"]) or any(key == normalize_name(a) for a in e["aliases"]):
                return e
        best, best_score = None, 0.0
        for e in self.entries:
            if e["name"] == NARRATOR:
                continue
            score = similar(key, normalize_name(e["name"]))
            for a in e["aliases"]:
                score = max(score, similar(key, normalize_name(a)))
            if score > best_score:
                best, best_score = e, score
        if best is not None and best_score >= FUZZY_MATCH_THRESHOLD:
            if raw_name not in best["aliases"]:
                best["aliases"].append(raw_name)
                self.dirty = True
            return best
        return None

    def observe(self, raw_name, freeze=False):
        """Called with whatever a detector produced. Returns (entry, is_new).

        A brand-new name is cast IMMEDIATELY -- it's marked provisional and
        kept out of the saved file until it's been seen
        SIGHTINGS_BEFORE_PERSIST times, so OCR noise costs nothing lasting
        and never pollutes the profile, but it's already sitting in the
        Cast panel, ready for a voice, well before that count is reached
        (game_text_speaker.run() also pauses the main loop right when this
        happens, so that first line waits for one instead of playing in
        whatever the engine already happened to be configured with).

        `freeze=True` (the Cast panel's "Freeze adding new cast members"
        checkbox) turns off that last part: an unrecognized name comes back
        as (None, False) instead of a new provisional entry, and the caller
        falls back to treating that line the same as one with no detected
        speaker at all -- spoken in whatever's already the default, never
        added to the cast, never pausing anything. Someone already met is
        untouched either way; freezing only stops new arrivals."""
        if not raw_name:
            return self.narrator(), False
        existing = self.match(raw_name)
        if existing is not None:
            if existing.get("provisional"):
                key = normalize_name(raw_name)
                self._pending[key] = self._pending.get(key, 1) + 1
                if self._pending[key] >= SIGHTINGS_BEFORE_PERSIST:
                    existing["provisional"] = False
                    self.dirty = True
            return existing, False

        if freeze:
            return None, False

        entry = {"name": raw_name.strip(), "aliases": [], "provisional": True, "models": {}}
        self.entries.append(entry)
        self._pending[normalize_name(raw_name)] = 1
        return entry, True

    def narrator(self):
        return self.find(NARRATOR)

    def remove(self, name):
        """Delete a cast member outright -- for a misread that got confirmed
        before the filtering in detect_speaker() existed to catch it, or one
        you just want to re-meet from scratch. Returns True if someone was
        actually removed.

        The Narrator can't be removed: it's a reserved slot that __init__
        recreates the moment it's missing (see above), so deleting it would
        only bring it right back, for no actual effect."""
        if normalize_name(name) == normalize_name(NARRATOR):
            return False
        e = self.find(name)
        if e is None:
            return False
        self.entries.remove(e)
        key = normalize_name(name)
        self._pending.pop(key, None)
        for alias in e.get("aliases") or []:
            self._pending.pop(normalize_name(alias), None)
        self.dirty = True
        return True

    def get_model(self, name, key):
        """This character's explicit config for one model -- voice, speaker
        (Piper's integer index), speed, whichever of those apply to that
        model's engine -- or an empty dict if nothing's been set, meaning:
        sound like whatever that model is already configured with. See
        model_key() for how `key` identifies a model."""
        e = self.find(name)
        if e is None:
            return {}
        return dict((e.get("models") or {}).get(key) or {})

    def set_model(self, name, key, **fields):
        """Set (or clear) this character's config for one model. A field
        passed as None is removed from just that model's entry; passing no
        fields at all removes the whole entry for this model, which is the
        same as never having set one -- the character goes back to sounding
        like the model's own default.

        This is the entire assignment mechanism now: no class, no pool, no
        index rotation -- a character sounds like exactly what's stored
        here, for exactly the model `key` names, and nothing else."""
        e = self.find(name)
        if e is None:
            return None
        models = e.setdefault("models", {})
        if not fields:
            models.pop(key, None)
            self.dirty = True
            return e
        current = dict(models.get(key) or {})
        for field, value in fields.items():
            if value is None:
                current.pop(field, None)
            else:
                current[field] = value
        if current:
            models[key] = current
        else:
            models.pop(key, None)
        self.dirty = True
        return e

    def to_json(self):
        """Only confirmed members are written. Provisional ones are dropped,
        which is the whole point of them."""
        out = []
        for e in self.entries:
            if e.get("provisional"):
                continue
            item = {"name": e["name"]}
            if e.get("aliases"):
                item["aliases"] = e["aliases"]
            if e.get("models"):
                item["models"] = e["models"]
            out.append(item)
        return out

    def merge(self, entries):
        """Fold in a shared cast -- see Profile.export_shared()/
        import_shared() and the main window's Export/Import buttons.
        `entries` is the same shape to_json() produces: a list of {name,
        aliases?, models?}.

        Matching is by exact name only (case/accent/punctuation-insensitive
        via normalize_name(), same as find()) -- deliberately NOT the fuzzy
        matching match() does for raw OCR spellings, since a shared cast
        file's names are already curated, not noisy screen text, and fuzzy
        matching two different curated lists risks silently merging two
        actually-different characters who just happen to have similar names.

        A name with no existing match becomes a brand-new confirmed entry
        (never provisional -- someone deliberately exported this, so it
        doesn't need to earn its way in the way a freshly-observed OCR name
        does). A name that DOES match an existing entry keeps that entry's
        own name/aliases as the record of what's actually been seen on
        screen, gains any NEW aliases the import brought along, and has the
        imported models folded in per model KEY -- the imported file's
        settings for a given engine/model completely replace this
        character's existing settings for that same key (that's the whole
        point of importing someone else's picks), but any key the import
        doesn't mention -- a different engine you've configured that they
        haven't -- is left exactly as it was.

        Returns (added, updated) counts for a confirmation message."""
        added = updated = 0
        for raw in entries or []:
            if not isinstance(raw, dict):
                continue
            name = (raw.get("name") or "").strip()
            if not name:
                continue
            aliases = [a for a in (raw.get("aliases") or []) if isinstance(a, str)]
            models = {str(k): dict(v) for k, v in (raw.get("models") or {}).items()
                      if isinstance(v, dict)}
            existing = self.find(name)
            if existing is None:
                self.entries.append({
                    "name": name, "aliases": aliases, "provisional": False, "models": models,
                })
                added += 1
                self.dirty = True
                continue
            changed = False
            for alias in aliases:
                if alias not in existing["aliases"] and normalize_name(alias) != normalize_name(existing["name"]):
                    existing["aliases"].append(alias)
                    changed = True
            if models:
                existing_models = existing.setdefault("models", {})
                for key, cfg in models.items():
                    if existing_models.get(key) != cfg:
                        existing_models[key] = cfg
                        changed = True
            if changed:
                updated += 1
                self.dirty = True
        return added, updated


# --------------------------------------------------------------------------
# Profile
# --------------------------------------------------------------------------

DEFAULT_PROFILE = {
    "version": PROFILE_VERSION,
    "name": "Default",
    "region": None,
    # Where the speaker's name shows up -- a separate screen region from
    # "region" above, in the same {x,y,w,h} pixel format. None disables
    # character detection outright: every line reads as the Narrator.
    "name_region": None,
    # See Cast.observe(): when set, a name nobody's met yet is never added
    # to the cast -- it just falls back to the default voice, same as a
    # line with no detected speaker at all.
    "freeze_cast": False,
    # Whether the Cast window's own checkbox of the same name starts
    # checked for this profile -- see CastWindow._apply_on_top()/
    # _on_keep_on_top_changed() in gui.py. True by default, matching the
    # window's own hardcoded default before this was made per-profile.
    "keep_on_top": True,
    "popup_marker": None,
    "interval": 1.0,
    "similarity": 0.9,
    "popup_threshold": 40,
    "speaker_name_mode": "announce",
    # Fixes for OCR glyph confusions specific to THIS game's font -- e.g. an
    # opening quote mark fused against a capital "I" that a particular font
    # renders close enough together for Tesseract to read as a single "T".
    # That kind of thing depends on the font, not on OCR in general, so it
    # doesn't belong as a blanket rule applied to every game -- see
    # apply_ocr_corrections() below and the module docstring. Empty by
    # default: most games need none of these.
    "ocr_corrections": [],
    "cast": [],
}


def slugify(name: str) -> str:
    s = _WS_RE.sub("-", _PUNCT_RE.sub("", (name or "").strip())).strip("-").lower()
    return s or "profile"


class Profile:
    def __init__(self, data=None, path=None):
        merged = dict(DEFAULT_PROFILE)
        merged.update(data or {})
        self.path = Path(path) if path else None
        self.data = merged
        self.cast = Cast(merged.get("cast"))

    # -- attribute-ish access, so callers don't sprinkle dict lookups around
    @property
    def name(self):
        return self.data.get("name") or "Default"

    def get(self, key, default=None):
        return self.data.get(key, default)

    def set(self, key, value):
        if self.data.get(key) != value:
            self.data[key] = value
            self.cast.dirty = True

    @property
    def dirty(self):
        return self.cast.dirty

    # -- detection
    def detect(self, obs):
        return detect_speaker(obs.name_text, obs.text)

    def voice_for(self, entry, key):
        """This character's explicit voice/speaker/speed for one model, or
        None if nothing's been assigned -- meaning: sound like whatever that
        model is already configured with. See model_key() for `key`."""
        if entry is None:
            return None
        return self.cast.get_model(entry["name"], key) or None

    # -- persistence
    def to_json(self):
        out = dict(self.data)
        out["version"] = PROFILE_VERSION
        out["cast"] = self.cast.to_json()
        out.pop("classes", None)  # pre-redesign field; no longer meaningful
        out.pop("name_identifier", None)  # pre-redesign field; no longer meaningful
        return out

    def save(self, path=None):
        """Atomic: written to a temp file in the same directory and then
        renamed over the original. The profile is updated DURING play -- every
        time a character is confirmed -- so a crash halfway through a write
        would otherwise be able to eat a cast list built up over hours."""
        target = Path(path) if path else self.path
        if target is None:
            raise ValueError("no path to save this profile to")
        target.parent.mkdir(parents=True, exist_ok=True)
        tmp = target.with_name(target.name + ".tmp")
        tmp.write_text(json.dumps(self.to_json(), indent=2, ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, target)
        self.path = target
        self.cast.dirty = False

    def save_if_dirty(self):
        """Called once per poll from the main loop. Since a character is only
        added when they first speak, that makes writes rare without needing a
        timer -- the poll interval is the debounce."""
        if self.dirty and self.path is not None:
            try:
                self.save()
                return True
            except OSError:
                return False
        return False

    # -- sharing: just the portable half of this profile -- cast and OCR
    # corrections, neither of which depends on the player's own screen --
    # none of the screen-specific settings. See the module docstring and
    # SHARED_EXPORT_SCHEMA above.
    def export_shared(self, path):
        """Write this profile's cast (character names + their per-model
        voice assignments -- exactly what Cast.to_json() already produces)
        and its ocr_corrections list to their own standalone file at
        `path`, wrapped with just enough context (which profile this came
        from) for import_shared() -- or a curious human -- to make sense
        of it later."""
        target = Path(path)
        target.parent.mkdir(parents=True, exist_ok=True)
        payload = {
            "schema": SHARED_EXPORT_SCHEMA,
            "game": self.name,
            "cast": self.cast.to_json(),
            "ocr_corrections": self.get("ocr_corrections") or [],
        }
        target.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")

    def import_shared(self, path):
        """Read a file written by export_shared() (or an older cast-only
        export from before ocr_corrections was added to it -- a missing
        "ocr_corrections" key is just treated as an empty list) and fold
        both parts into this profile: the cast via Cast.merge(), the OCR
        corrections via merge_ocr_corrections() -- two independent merges,
        since one's got nothing to do with the other. Raises ValueError if
        `path` doesn't look like a shared-profile file at all, so the
        caller can show that message directly rather than a raw JSON/OSError
        traceback.

        Returns (cast_added, cast_updated, corrections_added)."""
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        if isinstance(data, dict):
            cast_entries = data.get("cast")
            correction_entries = data.get("ocr_corrections")
        elif isinstance(data, list):
            # Oldest shape of all, from before this was even wrapped in a
            # dict: a bare list is exactly a "cast" value on its own.
            cast_entries, correction_entries = data, None
        else:
            cast_entries = correction_entries = None
        if not isinstance(cast_entries, list) and not isinstance(correction_entries, list):
            raise ValueError(
                f"{Path(path).name} doesn't look like a shared profile file "
                f"(no \"cast\" or \"ocr_corrections\" list found)."
            )
        cast_added = cast_updated = corrections_added = 0
        if isinstance(cast_entries, list):
            cast_added, cast_updated = self.cast.merge(cast_entries)
        if isinstance(correction_entries, list):
            before = self.get("ocr_corrections") or []
            merged = merge_ocr_corrections(before, correction_entries)
            if merged != before:
                corrections_added = len(merged) - len(before)
                self.set("ocr_corrections", merged)
        return cast_added, cast_updated, corrections_added


def load_profile(path):
    p = Path(path)
    data = json.loads(p.read_text(encoding="utf-8"))
    return Profile(data, path=p)


def list_profiles(directory=None):
    d = Path(directory or PROFILE_DIR)
    if not d.exists():
        return []
    out = []
    for f in sorted(d.glob("*.json")):
        try:
            name = json.loads(f.read_text(encoding="utf-8")).get("name") or f.stem
        except Exception:
            name = f.stem
        out.append((name, f))
    return out


def create_profile(name, directory=None, **overrides):
    d = Path(directory or PROFILE_DIR)
    data = dict(DEFAULT_PROFILE)
    data.update(overrides)
    data["name"] = name
    prof = Profile(data, path=d / f"{slugify(name)}.json")
    prof.save()
    return prof
