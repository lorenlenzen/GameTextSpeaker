#!/usr/bin/env python3
"""
preview_piper_speakers.py — generate short audio previews for a spread of
speaker IDs from a multi-speaker Piper voice model, so you can listen and
pick one instead of guessing.

Piper's speaker labels are just anonymous IDs (no gender/accent/quality
info in the model), so there's no way to know a good one in advance —
this just makes listening through a bunch of them fast.

Usage:
    # Preview 16 speakers spread evenly across the model:
    python3 preview_piper_speakers.py --model voices/en_US-libritts-high.onnx

    # Preview specific speaker IDs instead:
    python3 preview_piper_speakers.py --model voices/en_US-libritts-high.onnx --speakers 0,42,100,500

Generates one .wav file per sampled speaker into speaker_previews/ (or
--out-dir), named speaker_<id>.wav — then just double-click through them
in your file manager, or `aplay speaker_previews/speaker_0042.wav`.
Whichever one sounds good, put that ID into the GUI's Speaker ID field
(or --piper-speaker on the command line).
"""

import argparse
import json
import shutil
import subprocess
import sys
from pathlib import Path


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--model", required=True, metavar="PATH", help="Path to the multi-speaker .onnx model.")
    parser.add_argument("--count", type=int, default=16,
                         help="How many speakers to sample, evenly spread across the model (default 16).")
    parser.add_argument("--speakers", default="",
                         help="Comma-separated exact speaker IDs to preview instead of an even spread, e.g. 0,5,42,100.")
    parser.add_argument("--text", default="Hello, this is a preview of this voice.",
                         help="Text spoken in each preview clip.")
    parser.add_argument("--out-dir", default="speaker_previews", metavar="DIR",
                         help="Where to write the .wav files (default ./speaker_previews).")
    args = parser.parse_args()

    if shutil.which("piper") is None:
        sys.exit("Missing 'piper' on PATH — activate your venv first: source venv/bin/activate")

    model = Path(args.model)
    if not model.exists():
        sys.exit(f"Model not found: {model}")
    cfg_path = Path(str(model) + ".json")
    if not cfg_path.exists():
        sys.exit(f"Missing config file: {cfg_path}")
    try:
        cfg = json.loads(cfg_path.read_text())
    except Exception as e:
        sys.exit(f"Couldn't read/parse {cfg_path}: {e}")

    num_speakers = cfg.get("num_speakers", 1)
    if not num_speakers or num_speakers <= 1:
        sys.exit(f"{model.name} isn't a multi-speaker model (num_speakers={num_speakers}) — nothing to preview.")

    if args.speakers:
        try:
            ids = sorted({int(s.strip()) for s in args.speakers.split(",") if s.strip()})
        except ValueError:
            sys.exit("--speakers must be a comma-separated list of integers, e.g. 0,5,42,100")
        bad = [i for i in ids if i < 0 or i >= num_speakers]
        if bad:
            sys.exit(f"Speaker ID(s) out of range (model has {num_speakers} speakers, valid IDs 0-{num_speakers - 1}): {bad}")
    else:
        n = max(1, min(args.count, num_speakers))
        if n == 1:
            ids = [0]
        else:
            ids = sorted({round(i * (num_speakers - 1) / (n - 1)) for i in range(n)})

    out_dir = Path(args.out_dir)
    out_dir.mkdir(exist_ok=True)

    print(f"{model.name}: {num_speakers} speakers total. Generating {len(ids)} preview(s) into {out_dir}/ ...")
    failures = 0
    for sid in ids:
        out_file = out_dir / f"speaker_{sid:04d}.wav"
        text = f"Speaker {sid}. {args.text}"
        cmd = ["piper", "--model", str(model), "--speaker", str(sid), "--output_file", str(out_file)]
        result = subprocess.run(cmd, input=text.encode("utf-8"), stdout=subprocess.DEVNULL, stderr=subprocess.PIPE)
        if result.returncode != 0:
            failures += 1
            err = result.stderr.decode("utf-8", errors="replace").strip()
            print(f"  speaker {sid}: FAILED (code {result.returncode}){': ' + err if err else ''}")
        else:
            print(f"  speaker {sid}: {out_file}")

    if failures:
        print(f"\n{failures} of {len(ids)} preview(s) failed — see errors above.")
    print(
        f"\nDone. Play them back (double-click in your file manager, or "
        f"`aplay {out_dir}/speaker_0042.wav`) and note the ID you like — "
        f"put that number in the GUI's Speaker ID field, or pass "
        f"--piper-speaker <id> on the command line."
    )


if __name__ == "__main__":
    main()
