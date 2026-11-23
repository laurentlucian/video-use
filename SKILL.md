---
name: video-use
description: Edit any video by conversation. Transcribe, cut, color grade, generate overlay animations, burn subtitles — for talking heads, montages, tutorials, travel, interviews. No presets, no menus. Ask questions, confirm the plan, execute, iterate, persist. Production-correctness rules are hard; everything else is artistic freedom.
---

# Video Use

## Principle

1. **LLM reasons from raw transcript + on-demand visuals.** The only derived artifact that earns its keep is a packed phrase-level transcript (`takes_packed.md`). Everything else — filler tagging, retake detection, shot classification, emphasis scoring — you derive at decision time.
2. **Audio is primary, visuals follow.** Cut candidates come from speech boundaries and silence gaps. Drill into visuals only at decision points.
3. **Ask → confirm → execute → iterate → persist.** Never touch the cut until the user has confirmed the strategy in plain English.
4. **Generalize.** Do not assume what kind of video this is. Look at the material, ask the user, then edit.
5. **Artistic freedom is the default.** Every specific value, preset, font, color, duration, pitch structure, and technique in this document is a *worked example* from one proven video — not a mandate. Read them to understand what's possible and why each worked. Then make your own taste calls based on what the material actually is and what the user actually wants. **The only things you MUST do are in the Hard Rules section below.** Everything else is yours.
6. **Invent freely.** If the material calls for a technique not described here — split-screen, picture-in-picture, lower-third identity cards, reaction cuts, speed ramps, freeze frames, crossfades, match cuts, L-cuts, J-cuts, speed ramps over breath, whatever — build it. The helpers are ffmpeg and PIL. They can do anything the format supports. Do not wait for permission.
7. **Verify your own output before showing it to the user.** If you wouldn't ship it, don't present it.

## Hard Rules (production correctness — non-negotiable)

These are the things where deviation produces silent failures or broken output. They are not taste, they are correctness. Memorize them.

1. **Subtitles are applied LAST in the filter chain**, after every overlay. Otherwise overlays hide captions. Silent failure.
2. **Per-segment extract → lossless `-c copy` concat**, not single-pass filtergraph. Otherwise you double-encode every segment when overlays are added.
3. **30ms audio fades at every segment boundary** (`afade=t=in:st=0:d=0.03,afade=t=out:st={dur-0.03}:d=0.03`). Otherwise audible pops at every cut.
4. **Overlays use `setpts=PTS-STARTPTS+T/TB`** to shift the overlay's frame 0 to its window start. Otherwise you see the middle of the animation during the overlay window.
5. **Captions use output-timeline offsets**: `output_time = word.start - segment_start + segment_offset`, with every segment snapped to whole output frames. `render.py --style` does this; hand-built captions must too. Otherwise captions misalign after segment concat.
6. **Never cut inside a word.** Snap every cut edge to a word boundary from the Scribe transcript.
7. **Pad every cut edge.** Working window: 30–200ms. Scribe timestamps drift 50–100ms — padding absorbs the drift. Tighter for fast-paced, looser for cinematic.
8. **Word-level verbatim ASR only.** Never SRT/phrase mode (loses sub-second gap data). Never normalized fillers (loses editorial signal).
9. **Cache transcripts per source.** Never re-transcribe unless the source file itself changed.
10. **Parallel sub-agents for multiple animations.** Never sequential. Spawn N at once via the `Agent` tool; total wall time ≈ slowest one.
11. **Strategy confirmation before execution.** Never touch the cut until the user has approved the plain-English plan.
12. **All session outputs in `<videos_dir>/edit/`.** Never write inside the `video-use/` project directory.

Everything else in this document is a worked example. Deviate whenever the material calls for it.

## Directory layout

The skill lives in `video-use/`. User footage lives wherever they put it. All session outputs go into `<videos_dir>/edit/`.

```
<videos_dir>/
├── <source files, untouched>
└── edit/
    ├── project.md               ← memory; appended every session
    ├── takes_packed.md          ← phrase-level transcripts, the LLM's primary reading view
    ├── edl.json                 ← cut decisions
    ├── transcripts/<name>.json  ← cached raw Scribe JSON
    ├── animations/slot_<id>/    ← per-animation source + render + reasoning
    ├── clips_graded/            ← per-segment extracts with grade + fades (next to the render output)
    ├── captions.ass             ← output-timeline captions (written by render.py --style)
    ├── downloads/               ← yt-dlp outputs
    ├── verify/                  ← debug frames / timeline PNGs
    ├── preview.mp4
    └── final.mp4
```

## Setup

First-time install lives in `install.md` (clone, deps, ffmpeg, skill registration, API key). Don't re-run it every session; on cold start just verify:

- Transcription engine: local Whisper (large-v3-turbo; mlx-whisper on Apple Silicon, faster-whisper on CUDA or CPU elsewhere) by default. It needs no key. `ELEVENLABS_API_KEY` in the environment or in `.env` at the repo root switches `--engine auto` to Scribe. Never ask for a key; local is the default path. Local mode has no diarization (every word is `speaker_0`) and no audio-event tags, so read speaker changes and laughter from `timeline_view` when they matter.
- `ffmpeg` + `ffprobe` on PATH.
- Python deps installed (`uv sync` or `pip install -e .` inside the repo).
- Node.js + npm available if the session needs HyperFrames or Remotion slots. HyperFrames currently requires Node.js 22+.
- `yt-dlp`, HyperFrames, Remotion, Manim installed only on first use.
- First-use animation setup happens inside the slot directory, never at the video-use repo root. HyperFrames can be invoked with `npx --yes hyperframes ...`; Remotion can be scaffolded with `npx create-video@latest` or installed as a project-local dependency before using its `remotion render` command.
- This skill vendors `skills/manim-video/`. Read its SKILL.md when building a Manim slot.

Helpers (`helpers/transcribe.py`, `helpers/render.py`, etc.) live alongside this SKILL.md. Resolve their paths relative to the directory containing this file — the skill is typically symlinked at `~/.claude/skills/video-use/` or `~/.codex/skills/video-use/`.

## Helpers

- **`transcribe.py <video>`** — single-file transcription, Scribe-shaped JSON. `--engine auto|local|scribe` (default auto). Pass `--language <code>` (e.g. `pt`, `en`) for local: it picks the filler prompt that keeps "ahn" / "um" in the text; without it there is no filler prompt and Whisper drops most fillers. `--num-speakers N` applies to Scribe only. Cached.
- **`transcribe_batch.py <videos_dir>`** — whole-folder transcription. Scribe runs 4 parallel workers; local runs one file at a time on the GPU. Use for multi-take.
- **`pack_transcripts.py --edit-dir <dir>`** — `transcripts/*.json` → `takes_packed.md` (phrase-level, break on silence ≥ 0.5s).
- **`export_transcript.py <edit_dir>/transcripts/<name>.json`** — writes `<edit_dir>/exports/<name>.words.json` and `<name>.srt` (natural case). Hand these to ffmpeg-skill (`silence.py --words`, `caption.py --srt`) instead of transcribing again. Times follow the transcribed file: after a cut, run `transcribe.py` on the cut file and export again before captioning.
- **`timeline_view.py <video> <start> <end>`** — filmstrip + waveform PNG. On-demand visual drill-down. **Not a scan tool** — use it at decision points, not constantly. Word labels come from `--transcript <file>`, else `<transcripts-dir>/<video_stem>.json` (`--transcripts-dir`, default `<edit-dir>/transcripts`, `--edit-dir` default `<video_parent>/edit`).
- **`onsets.py <video> <transcript.json> [--start S --end E]`** — prints each word's start refined to the real audio onset. Local Whisper often stamps a word at the start of the pause before it (e.g. 16.56 s for a word spoken at 16.89 s). Plan cuts from the refined times. In code: `onsets.refine_range(video, words, start, end)`.
- **`render.py <edl.json> -o <out>`** — frame-snapped per-segment extract (crop on the full-res source → scale, CFR) → concat → overlays (trimmed to their window) → captions LAST → loudnorm. `--style <style.json>` burns word-by-word ASS captions (bare `--style` = `styles/default.json`). `--transcripts-dir <dir>` (default `<edl_dir>/transcripts`). `--no-refine-onsets` keeps raw word starts. `--preview` / `--draft` for faster passes. Intermediates (`clips_graded/`, `base.mp4`, `captions.ass`) go next to `<out>`.
- **`grade.py <in> -o <out>`** — ffmpeg filter chain grade. Presets + `--filter '<raw>'` for custom.

For animations, create `<edit>/animations/slot_<id>/` with `Bash` and spawn a sub-agent via the `Agent` tool.

## The process

1. **Inventory.** `ffprobe` every source. `transcribe_batch.py` on the directory. `pack_transcripts.py` to produce `takes_packed.md`. Sample one or two `timeline_view`s for a visual first impression.
2. **Pre-scan for problems.** One pass over `takes_packed.md` to note verbal slips, obvious mis-speaks, or phrasings to avoid. Plain list, feed into the editor brief.
3. **Converse.** Describe what you see in plain English. Ask questions *shaped by the material*. Collect: content type, target length/aspect, aesthetic/brand direction, pacing feel, must-preserve moments, must-cut moments, animation and grade preferences, subtitle needs. Do not use a fixed checklist — the right questions are different every time.
4. **Propose strategy.** 4–8 sentences: shape, take choices, cut direction, animation plan, grade direction, subtitle style, length estimate. **Wait for confirmation.**
5. **Execute.** Produce `edl.json` via the editor sub-agent brief. Drill into `timeline_view` at ambiguous moments. Build animations in parallel sub-agents. Apply grade per-segment. Compose via `render.py`.
6. **Preview.** `render.py --preview`.
7. **Self-eval (before showing the user).** Run `timeline_view` on the **rendered output** (not the sources) at every cut boundary (±1.5s window). Check each image for:
   - Visual discontinuity / flash / jump at the cut
   - Waveform spike at the boundary (audio pop that slipped past the 30ms fade)
   - Subtitle hidden behind an overlay (Rule 1 violation)
   - Overlay misaligned or showing wrong frames (Rule 4 violation)

   Also sample: first 2s, last 2s, and 2–3 mid-points — check grade consistency, subtitle readability, overall coherence. Run `ffprobe` on the output to verify duration matches the EDL expectation.

   Measure the audio, don't assume it: `ffmpeg -i out.mp4 -af ebur128=peak=true -f null -` for integrated loudness and true peak, plus RMS per section (dialogue, music-only, end card). An end card 15 dB under the dialogue, or effects louder than speech, is a bug. You cannot listen: say so, and report the numbers.

   For anything the user will publish (launch, promo, ad), also spawn one **critic sub-agent** with the rendered file, the EDL, and any reference videos the user gave. Brief it to roast, not to praise: a verdict, ranked problems with timecodes and evidence (frames, levels), and the 5 fixes to do first. Fresh eyes catch what the author stopped seeing — cut-off payoff lines, 0.5s memes, unreadable 28px text at phone size.

   If anything fails: fix → re-render → re-eval. **Cap at 3 self-eval passes** — if issues remain after 3, flag them to the user rather than looping forever. Only present the preview once the self-eval passes.
8. **Iterate + persist.** Natural-language feedback, re-plan, re-render. Never re-transcribe. Final render on confirmation. Append to `project.md`.

## Cut craft (techniques)

- **Audio-first.** Candidate cuts from word boundaries and silence gaps.
- **Preserve peaks.** Laughs, punchlines, emphasis beats. Extend past punchlines to include reactions — the laugh IS the beat.
- **Speaker handoffs** benefit from air between utterances. Common values: 400–600ms. Less for fast-paced, more for cinematic. Taste call.
- **Audio events as signals.** `(laughs)`, `(sighs)`, `(applause)` mark beats. Extend past them.
- **Silence gaps are cut candidates.** Silences ≥400ms are usually the cleanest. 150–400ms phrase boundaries are usable with a visual check. <150ms is unsafe (mid-phrase).
- **Example cut padding** (the launch video shipped with this): 50ms before the first kept word, 80ms after the last. Tighter for montage energy, looser for documentary. Stay in the 30–200ms working window (Hard Rule 7).
- **Never reason audio and video independently.** Every cut must work on both tracks.

## The packed transcript (primary reading view)

`pack_transcripts.py` reads all `transcripts/*.json` and produces one markdown file where each take is a list of phrase-level lines, each prefixed with its `[start-end]` time range. Phrases break on any silence ≥ 0.5s OR speaker change. This is the artifact the editor sub-agent reads to pick cuts — it gives word-boundary precision from text alone at 1/10 the tokens of raw JSON.

Example line:
```
## C0103  (duration: 43.0s, 8 phrases)
  [002.52-005.36] S0 Ninety percent of what a web agent does is completely wasted.
  [006.08-006.74] S0 We fixed this.
```

## Editor sub-agent brief (for multi-take selection)

When the task is "pick the best take of each beat across many clips," spawn a dedicated sub-agent with a brief shaped like this. The structure is load-bearing; the pitch-shape example is not.

```
You are editing a <type> video. Pick the best take of each beat and 
assemble them chronologically by beat, not by source clip order.

INPUTS:
  - takes_packed.md (time-annotated phrase-level transcripts of all takes)
  - Product/narrative context: <2 sentences from the user>
  - Speaker(s): <name, role, delivery style note>
  - Expected structure: <pick an archetype or invent one>
  - Verbal slips to avoid: <list from the pre-scan pass>
  - Target runtime: <seconds>

Common structural archetypes (pick, adapt, or invent):
  - Tech launch / demo:   HOOK → PROBLEM → SOLUTION → BENEFIT → EXAMPLE → CTA
  - Tutorial:             INTRO → SETUP → STEPS → GOTCHAS → RECAP
  - Interview:            (QUESTION → ANSWER → FOLLOWUP) repeat
  - Travel / event:       ARRIVAL → HIGHLIGHTS → QUIET MOMENTS → DEPARTURE
  - Documentary:          THESIS → EVIDENCE → COUNTERPOINT → CONCLUSION
  - Music / performance:  INTRO → VERSE → CHORUS → BRIDGE → OUTRO
  - Or invent your own.

RULES:
  - Start/end times must fall on word boundaries from the transcript.
  - Pad cut boundaries (working window 30–200ms).
  - Prefer silences ≥ 400ms as cut targets.
  - Unavoidable slips are kept if no better take exists. Note them in "reason".
  - If over budget, revise: drop a beat or trim tails. Report total and self-correct.

OUTPUT (JSON array, no prose):
  [{"source": "C0103", "start": 2.42, "end": 6.85, "beat": "HOOK",
    "quote": "...", "reason": "..."}, ...]

Return the final EDL and a one-line total runtime check.
```

## Color grade (when requested)

Your job is to **reason about the image**, not apply a preset. Look at a frame (via `timeline_view`), decide what's wrong, adjust one thing, look again.

Mental model is ASC CDL. Per channel: `out = (in * slope + offset) ** power`, then global saturation. `slope` → highlights, `offset` → shadows, `power` → midtones.

**Example filter chains** (`grade.py` has `--list-presets`; use them as starting points or mix your own):

- **`warm_cinematic`** — retro/technical, subtle teal/orange split, desaturated. Shipped in a real launch video. Safe for talking heads.
- **`neutral_punch`** — minimal corrective: contrast bump + gentle S-curve. No hue shifts.
- **`none`** — straight copy. Default when the user hasn't asked.

For anything else — portraiture, nature, product, music video, documentary — invent your own chain. `grade.py --filter '<raw ffmpeg>'` accepts any filter string.

Hard rules: apply **per-segment during extraction** (not post-concat, which re-encodes twice). Never go aggressive without testing skin tones.

## Subtitles (when requested)

Captions have three dimensions worth reasoning about: **chunking** (1/2/3/sentence per cue), **case** (UPPER/Title/Natural), and **placement**. The right combo depends on content.

`render.py --style <style.json>` is the one caption path. It writes `captions.ass` from the transcripts and the frame-snapped cuts, then burns it LAST:

- Chunking: the transcript splits into phrases at punctuation and at pauses ≥ 0.3 s. A phrase shorter than `words_min` merges with its neighbour when the gap is under 0.6 s. Each group then splits evenly into cues of at most `words_max` words, and each cue lays out on at most `lines_max` lines of `max_width` (most balanced break). A cue that cannot fit shrinks.
- Two modes. `active_color` set: the spoken word takes `active_color` and `active_scale` (word-by-word karaoke). `active_color` null or absent: each cue is a plain line, no per-word colour or scale. Each cue pops in from `pop_from` scale over `pop_ms`. Cue centre: `x` (default 0.5), `y`.
- Look: `outline` px; `shadow` (offset down, px), `shadow_blur` (CSS-style px), `shadow_alpha`. The shadow is its own ASS layer under the text, so the blur never softens the fill. Font `tracking` (em fraction) maps to ASS letter spacing.
- Punctuation: quotes and ASS control characters are always removed. `strip_end_punctuation` (default true for `case: "lower"`) removes `. , ; : ! ? …` at the end of each cue; mid-cue commas stay. `end_punctuation_keep: "?"` keeps question marks.
- Overlays hide captions: every EDL overlay clips captions out of its window, unless the overlay sets `"hide_captions": false` (use that for text-free b-roll). Words that start under a caption-hiding overlay are dropped before chunking, so the next cue starts on the first word after the overlay instead of showing late. A cue that runs into an overlay ends at the overlay start. Any cue piece shorter than the style's `min_visible_ms` (default 200) is not shown.
- Word starts are refined to the audio onset (`onsets.py`), so cues land on the voice, not on the pause before it.
- EDL `replacements` fix ASR spellings in captions, whole word, case-insensitive, applied after `case` so proper names keep their case: `{"Laude": "LOUD", "LOUD": "LOUD"}`.
- The caption font file is passed to libass (`fontsdir`), so a font that is not installed still renders. The renderer reads the face name and weight from the file itself.

The style JSON is shared with motion graphics (Remotion overlays). Units are px on the `canvas`, fractions of canvas width/height, `#RRGGBB`, ms. `styles/default.json` is a worked example (Montserrat Black 96, white, 8 px outline, green active word, y = 0.64). Font paths are relative to the style file:

```json
{
  "canvas": {"w": 1080, "h": 1920, "fps": 30},
  "colors": {"accent": "#00EE22", "dark": "#151515", "light": "#F0EFED", "text": "#FFFFFF", "outline": "#000000"},
  "fonts": {"caption": {"family": "Montserrat", "file": "assets/fonts/Montserrat-Black.ttf", "weight": 900}},
  "captions": {
    "size": 96, "case": "upper", "words_max": 3, "max_width": 0.86, "y": 0.64,
    "outline": 8, "shadow": 4, "shadow_alpha": 0.6,
    "active_color": "#00EE22", "active_scale": 1.1, "pop_ms": 100, "pop_from": 0.8
  }
}
```

For sentence captions (paid social, narrative, documentary), copy the style and change it: `case` lower or natural, `words_min` 4, `words_max` 7, `lines_max` 2, `active_color` null, `outline` 0, `shadow` 3, `shadow_blur` 8, `shadow_alpha` 0.5, a tight bold sans at about 2.4% of the height. Use a static TTF; libass renders a variable font at its default instance. Vertical-social UI covers the bottom ~25% of the frame, so keep `y` ≤ ~0.7. Hard rules: captions LAST (Rule 1), output-timeline offsets (Rule 5).

## Animations (when requested)

Animations match the content and the brand. **Get the palette, font, and visual language from the conversation** — never assume a default. If the user hasn't told you, propose a palette in the strategy phase and wait for confirmation before building anything.

**Tool options:**

Pick the engine per animation slot. Do not default to Remotion just because the animation is web-adjacent.

- **HyperFrames** — Browser-native HTML/CSS/GSAP video compositions: product UI motion, website-to-video or mockup-to-video captures, kinetic typography, landing-page/storyboard promos, data-driven UI states, transparent WebM overlays, and clips that need deterministic frame capture plus HyperFrames lint/validate/render checks. Best when the animation should be authored and verified like a web composition instead of a React component tree.
- **Remotion** — React/CSS compositions with component state, reusable React primitives, or an existing Remotion brand system. Best when the user specifically asks for React/Remotion or when React composition is the simpler authoring model.
- **Manim** — formal diagrams, state machines, equation derivations, graph morphs. Read `skills/manim-video/SKILL.md` and its references for depth.
- **PIL + PNG sequence + ffmpeg** — simple overlay cards: counters, typewriter text, single bar reveals, progressive draws. Fast to iterate, any aesthetic you want. The launch video used this.

For HyperFrames slots, scaffold the slot inside `edit/animations/slot_<id>/` with `npx --yes hyperframes init . --example blank --non-interactive --skip-skills`, build the HTML composition there, run the HyperFrames checks that fit the slot (`lint`, `validate`, and a draft render when practical), then produce the final overlay video with `npx --yes hyperframes render . -o render.mp4` or `--format webm -o render.webm` when alpha is required. Point the EDL overlay `file` at the actual rendered path.

For Remotion slots, keep the Remotion project isolated inside the same slot directory, scaffold with `npx create-video@latest` or install Remotion locally there, render the composition to `render.mp4` with the project-local `remotion render` command, and verify duration and dimensions with `ffprobe`.

None is mandatory. Invent hybrids if useful (e.g., PIL background with a HyperFrames or Remotion layer on top).

**Duration rules of thumb, context-dependent:**

- **Sync-to-narration explanations.** A viewer needs to parse the content at 1×. Rough floor 3s, typical 5–7s for simple cards, 8–14s for complex diagrams. The launch video shipped at 5–7s per simple card.
- **Beat-synced accents** (music video, fast montage). 0.5–2s is fine — they're visual accents, not information. The "readable at 1×" rule becomes *"recognizable at 1×"*, not *"fully parseable."*
- **Hold the final frame ≥ 1s** before the cut (universal).
- **Over voiceover:** total duration ≥ `narration_length + 1s` (universal).
- **Never parallel-reveal independent elements** — the eye can't track two new things at once. One thing, pause, next thing.

**Animation payoff timing (rule for sync-to-narration):** get the payoff word's timestamp. Start the overlay `reveal_duration` seconds earlier so the landing frame coincides with the spoken payoff word. Without this sync the animation feels disconnected.

**Easing** (universal — never `linear`, it looks robotic):

```python
def ease_out_cubic(t):    return 1 - (1 - t) ** 3
def ease_in_out_cubic(t):
    if t < 0.5: return 4 * t ** 3
    return 1 - (-2 * t + 2) ** 3 / 2
```

`ease_out_cubic` for single reveals (slow landing). `ease_in_out_cubic` for continuous draws.

**Typing text anchor trick:** center on the FULL string's width, not the partial-string width — otherwise text slides left during reveal.

**Example palette** (the launch video — one aesthetic among infinite):
- Background `(10, 10, 10)` near-black
- Accent `#FF5A00` / `(255, 90, 0)` orange
- Labels `(110, 110, 110)` dim gray
- Font: Menlo Bold at `/System/Library/Fonts/Menlo.ttc` (index 1)
- ≤ 2 accent colors, ~40% empty space, minimal chrome
- Result: terminal / retro tech feel

This is one style. If the brand is warm and serif, use that. If it's colorful and playful, use that. If the user handed you a style guide, follow it. If they didn't, propose one and confirm.

**Fonts fail silently.** A web font that didn't load renders in a fallback face with no error — the video ships in "almost Arial". In HyperFrames/Remotion, await the font load and then assert it: `if (!document.fonts.check('700 76px "Inter"')) throw new Error(...)`. In PIL, pass an explicit font path; never rely on the default.

**Worked example — "show the edit" hook** (a launch video for this tool). Instead of a title card, the first 3s visualize the editing itself: each transcript word pops in on its Scribe timestamp, with bars under it drawn from the real audio envelope; a filler ("ummm") grows letter by letter while it is spoken, turns orange and is cut out on screen at the same frame the audio cuts, and the next line lands immediately. It works because the picture is *driven by the same data as the sound* — one composition (Remotion) reads frame-exact word/envelope JSON produced in Python, so nothing can drift. Use the idea whenever the story is "we removed something": make the removal visible.

**Parallel sub-agent brief** — each animation is one sub-agent spawned via the `Agent` tool. Each prompt is self-contained (sub-agents have no parent context). Include:

1. One-sentence goal: *"Build ONE animation: [spec]. Nothing else."*
2. Absolute output path (`<edit>/animations/slot_<id>/render.mp4`)
3. Exact technical spec: resolution, fps, codec, pix_fmt, CRF, duration
4. Style palette as concrete values (RGB tuples, hex, or reference to a design system)
5. Font path with index
6. Frame-by-frame timeline (what happens when, with easing)
7. Anti-list ("no chrome, no extras, no titles unless specified")
8. Code pattern reference (copy helpers inline, don't import across slots)
9. Deliverable checklist (script, render, verify duration via ffprobe, report)
10. **"Do not ask questions. If anything is ambiguous, pick the most obvious interpretation and proceed."**

One sub-agent = one file (unique filenames, parallel agents don't overwrite each other).

## Music and sound effects (when requested)

Sound is where generated videos sound cheap. Worked rules from launch edits:

- **Fewer effects.** Every effect is tied to something visible (a cut, a landing, a click). ~20 stock whooshes/risers/impacts in 18s reads as generic; ~8 reads as designed.
- **Hit on the frame.** Most effects have an attack (silence or a build before the transient). Measure it (first sample above ~-30 dBFS of the peak) and start the file `attack` seconds *before* the visible contact frame.
- **Duck music under speech** (roughly -12 to -15 dB relative to its music-only level), and ramp it out before a stinger or end card instead of letting its own tail decay under your CTA.
- **Master once:** mix to PCM, then two-pass loudnorm (-14 LUFS, true peak ≤ -1 dBTP) on the final mix. Then measure per section (see Self-eval).
- **Music taste is the user's call.** Generated music defaults to "hype"; offer two contrasting beds and let the user listen. Don't claim a mix sounds good — you can only measure it.

## Output spec

Match the source unless the user asked for something specific. Common targets: `1920×1080@24` cinematic, `1920×1080@30` screen content, `1080×1920@30` vertical social, `3840×2160@24` 4K cinema, `1080×1080@30` square. `render.py` outputs the first source's aspect at 1920 on the long side (1280 in `--draft`), constant frame rate: 30 fps unless the EDL sets `"fps"`. Phone footage is variable frame rate (e.g. 29.83); the CFR output fixes that. Worth asking the user which delivery format matters.

## EDL format

```json
{
  "version": 1,
  "fps": 30,
  "sources": {"C0103": "/abs/path/C0103.MP4", "C0108": "/abs/path/C0108.MP4"},
  "ranges": [
    {"source": "C0103", "start": 2.42, "end": 6.85,
     "beat": "HOOK", "quote": "...", "reason": "Cleanest delivery, stops before slip at 38.46."},
    {"source": "C0108", "start": 14.30, "end": 28.90, "zoom": 1.25, "anchor": [0.5, 0.4],
     "beat": "SOLUTION", "quote": "...", "reason": "Only take without the false start."},
    {"source": "C0108", "start": 31.00, "end": 36.20, "anchor": [0.5, 0.42],
     "reframes": [{"at_s": 0.5, "zoom": 1.12, "ms": 450}, {"at_s": 3.0, "zoom": 1.0, "ms": 450}]}
  ],
  "grade": "warm_cinematic",
  "audio_filter": "highpass=f=80,acompressor=threshold=-20dB:ratio=2.5",
  "replacements": {"Laude": "LOUD"},
  "overlays": [
    {"file": "animations/slot_1/render.mov", "start_in_output": 0.0, "duration": 5.0},
    {"file": "animations/logo.png", "start_in_output": 80.0, "position": [0.5, 0.15], "scale": 0.5},
    {"file": "animations/cta.mov", "start_in_output": 88.0, "blur_base": {"px": 30, "ramp_ms": 250}}
  ],
  "music": {"file": "audio/bed.mp3", "start_s": 14.79, "gain_db": -16, "fade_in_ms": 100, "fade_out_ms": 400,
            "duck": {"under_voice_db": 6, "attack_ms": 20, "release_ms": 300},
            "automation": [{"at_s": 40.0, "gain_db": -16}, {"at_s": 40.4, "gain_db": -12}]},
  "sfx": [{"file": "audio/pop.wav", "at_s": 12.345, "gain_db": -6}]
}
```

- `fps`: output frame rate (default 30, CFR). Each range's length snaps to whole frames (`round((end - start) * fps)`), so total duration is exact and A/V sync holds to the last segment.
- `zoom` (default 1.0) punches in per range; it crops the full-res source, then scales, so a 1.35 zoom on 4K footage never upscales. `anchor` is the source point (fractions, default `[0.5, 0.5]`) that the crop centres on, clamped to the frame.
- `grade` is a preset name, a raw ffmpeg filter, or `auto`. `audio_filter` is an ffmpeg audio chain applied per segment before the fades.
- `reframes` (optional, per range) are smooth zoom moves inside the range: each eases (cubic in-out) from the current zoom to `zoom` over `ms` (default 450), starting `at_s` seconds into the range. The range `zoom` is the starting value. Moves must not overlap. Use them for reframes and emphasis pushes; keep snap `zoom` changes for jump cuts.
- `freeze_head_s` (optional, per range) holds the frame at `start + freeze_head_s` for the first `freeze_head_s` seconds while the audio plays from `start`. Use it when the first frames are motion-blurred but trimming the head would clip the first word; sync is exact after the hold.
- `replacements` applies to caption text only. A key with spaces replaces a run of words (same word count on both sides), e.g. `{"streamer do zero": "Streamer do Zero"}`.
- `overlays` are rendered clips or stills. Each one is trimmed to its window (`duration` defaults to the end of the output), so a long clip never extends the output. Alpha works for ProRes 4444 / qtrle `.mov`, VP9 `.webm`, and PNG. `position` is where the overlay's centre lands (fractions of the output, default centre); `scale` multiplies its own size. `hide_captions` (default `true`) clips captions out of the overlay window; set `false` for text-free b-roll. A video overlay without `duration` lasts its own clip length. `blur_base: {px, ramp_ms}` blurs the base video (gaussian sigma `px`) under that overlay's window, crossfading in and out over `ramp_ms`, before any overlay is drawn.
- `music` is a bed mixed under the voice: `file`, `start_s` (offset into the track), `gain_db`, `fade_in_ms`, `fade_out_ms`. `automation` is a list of `{at_s, gain_db}` keyframes on the output timeline; the gain ramps linearly between them, starting from `gain_db` at 0 s, and holds after the last one (use it for swells). `duck: {under_voice_db, attack_ms, release_ms}` sidechain-ducks the bed by about `under_voice_db` while the voice plays.
- `sfx` is a list of `{file, at_s, gain_db, pan?, duration_s?, fade_out_ms?}` one-shots placed sample-accurately at `at_s` on the output timeline. `pan` (−1..1, constant power) moves a mono-centred effect off the voice so it is not masked. `duration_s` cuts the file; `fade_out_ms` fades its last milliseconds (use it on risers that end abruptly). Align the transient, not the file start: subtract the file's attack (first sample above peak − 30 dB) from the visual event time; for whooshes and risers align the peak.
- With `music` or `sfx` the composite mixes voice, bed and effects to PCM, and loudnorm runs once on the full mix (−14 LUFS, −1 dBTP).

## Memory — `project.md`

Append one section per session at `<edit>/project.md`:

```markdown
## Session N — YYYY-MM-DD

**Strategy:** one paragraph describing the approach
**Decisions:** take choices, cuts, grades, animations + why
**Reasoning log:** one-line rationale for non-obvious decisions
**Outstanding:** deferred items
```

On startup, read `project.md` if it exists and summarize the last session in one sentence before asking whether to continue.

## Anti-patterns

Things that consistently fail regardless of style:

- **Hierarchical pre-computed codec formats** with USABILITY / tone tags / shot layers. Over-engineering. Derive from the transcript at decision time.
- **Hand-tuned moment-scoring functions.** The LLM picks better than any heuristic you'll write.
- **Whisper SRT / phrase-level output.** Loses sub-second gap data. Always word-level verbatim.
- **Running Whisper locally on CPU.** Slow. Use the GPU path (`helpers/local_stt.py` picks it). The disfluency prompt keeps most fillers, but check `timeline_view` for unlabeled ums before a tight filler pass.
- **Burning subtitles into base before compositing overlays.** Overlays hide them. (Hard Rule 1.)
- **Single-pass filtergraph when you have overlays.** Double re-encodes. Use per-segment extract → concat.
- **Linear animation easing.** Looks robotic. Always cubic.
- **Unverified web fonts.** A failed load silently falls back to a system face. Assert the font loaded before rendering.
- **Stock SFX on every transition.** Tie each effect to a visible event; cap the count.
- **Hard audio cuts at segment boundaries.** Audible pops. (Hard Rule 3.)
- **Typing text centered on the partial string.** Text slides left as it grows.
- **Sequential sub-agents for multiple animations.** Always parallel.
- **Editing before confirming the strategy.** Never.
- **Re-transcribing cached sources.** Immutable outputs of immutable inputs.
- **Assuming what kind of video it is.** Look first, ask second, edit last.
