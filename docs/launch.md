# voicebridge launch runbook

A complete, date-agnostic playbook for launching voicebridge on Product Hunt,
Hacker News, and X. Everything here is drafted to real platform limits. Fill in
the media where marked, then follow the 7-day checklist.

Author: Krish Ojha. Repo: https://github.com/cc-vb/voicebridge

---

## What only you can do (human punch list)

These steps need a person; the drafts below are ready for you to drop into.

1. **Record the demo video.** 60-90s screen plus audio, following the shot list
   in this doc (keyed to `DEMO.md`). Needs your voice, your mic, two or three
   real sessions open, and your phone on camera for the closing shot.
2. **Capture the GIFs.** Three short clips (fleet moment, barge-in, phone),
   pulled from the same recording session or recorded separately. Specs below.
3. **Upload the video to YouTube** (Product Hunt embeds YouTube only) and paste
   the link into the README hero placeholder and the PH gallery.
4. **Create the Product Hunt listing** (only the maker account can): set the
   name, tagline, gallery, thumbnail, topics, and the maker first comment.
   Schedule for 12:01 AM PT on launch day.
5. **Post to Hacker News** as Show HN, from your account, on launch morning.
6. **Post the X thread** from your account, and pin the first tweet.
7. **Domain / Cloudflare (optional, only if you want a landing page or a stable
   phone link).** voicebridge itself needs no hosting; the phone feature uses a
   quick tunnel or your own Tailscale Funnel. If you register a domain (e.g.
   voicebridge.dev) for a marketing page, that DNS / Cloudflare / hosting setup
   is a manual step. Not required for launch.
8. **Line up 3-5 friendly early upvoters/commenters** who have actually tried
   it, so day-one engagement is genuine (no vote rings, no fake reviews).

Everything else in this doc (copy, specs, checklist, storyboard) is written and
ready.

---

## Asset specs (exact)

Build to these numbers so nothing gets rejected or downscaled.

### Product Hunt gallery images
- Dimensions: **1270 x 760 px** (this is the PH recommended gallery size; it
  renders crisp and keeps a consistent aspect ratio across the carousel).
- Format: PNG or JPG (GIF also allowed, see below).
- File size: **under 3 MB each** (hard limit).
- Count: 4 to 6 images. The **first image is the feed thumbnail card**, so make
  it the strongest single frame (the fleet moment with a caption baked in).
- Keep important content away from the outer ~40px so nothing is clipped.

### Product Hunt thumbnail / logo
- Dimensions: **240 x 240 px** (square).
- Format: PNG (transparent background preferred) or JPG. Animated GIF is
  allowed for the thumbnail and stands out in the feed; keep it subtle.
- File size: **under 3 MB**.

### Hero / demo video
- Host on **YouTube only** (Product Hunt embeds YouTube; a raw mp4 will not
  embed on PH or inline on GitHub).
- Length: **60-90s**. Cut every pause.
- Resolution: record and upload at **1920 x 1080** (1080p), 30 or 60 fps.
- Audio: use headphones while recording so the mic never catches Claude's own
  voice. Normalize levels; the spoken replies must be clearly audible.
- Add baked-in captions for the key moments (see storyboard), because most
  people watch muted on first pass.

### GIFs (for gallery, README, and X)
- Fleet GIF (the money shot): 10s or less, **1270 x 760** for the PH gallery or
  a 16:9 crop, **under 3 MB**. A separate smaller cut (about 800px wide, under
  5 MB) works for the README hero.
- Barge-in GIF: 6-8s, shows a reply stopping mid-sentence when you talk over it.
- Phone GIF: 8-10s, shows the QR scan and the phone speaking a reply.
- Keep GIFs looping and short; trim to the single beat each one is proving.
- Tools: any screen recorder plus `ffmpeg`/`gifski` to size down under the cap.

---

## Demo video shot list / storyboard

Keyed one-to-one to `DEMO.md`. Total target: ~75s. Record screen plus audio
with two or three real Claude Code sessions open (signup, jobhunt, ccdash) so
the fleet is genuine. Font large enough to read on video.

| # | Time | On screen | You say / what happens | Caption to bake in |
|---|---|---|---|---|
| 1 | 0:00-0:18 | A session; type `/voice-agent` | "Hey, add input validation to the signup endpoint and run the tests." Claude starts and begins speaking; you cut in: "Actually, also check the rate limiter while you're in there." It stops mid-sentence and takes the new instruction. | "interrupt just by talking" |
| 2 | 0:18-0:33 | Same session still working | "Which agents need me?" It speaks: "3 sessions. Waiting for you: jobhunt. Still working: signup, ccdash." Let the voice play on camera. | "one voice controls every session" |
| 3 | 0:33-0:48 | Focus stays on keyboard-free flow | "Switch to jobhunt." It says "Voice moved to jobhunt." Then: "What's the status, and push if the tests pass." | "jump agents without touching the keyboard" |
| 4 | 0:48-0:58 | You sit back, silent | Another agent finishes and it speaks on its own: "Heads up, signup is ready for you." | "it tells you when an agent needs you" |
| 5 | 0:58-1:10 | Type `/voice-phone`, hold up phone, scan QR, tap Start | "What did signup end up changing?" The phone speaks the answer. | "drive it from your phone, from anywhere" |
| 6 | 1:10-1:15 | End card | To camera: "One voice, every agent, from anywhere. That's voicebridge." | `/plugin marketplace add cc-vb/voicebridge` + repo URL |

Don'ts (from `DEMO.md`): do not demo plain dictation (native `/voice` already
does that); lead with the fleet, that is the delta. Do not walk through install
first; show the magic and put install on the end card.

---

## Product Hunt listing copy

**Name:** voicebridge

**Tagline (<=60 chars).** Pick one:
- `Hands-free voice for Claude Code, from your phone` (49 chars)
- `Drive your Claude Code sessions by voice, anywhere` (50 chars)
- `Talk to Claude Code, and steer a fleet by voice` (47 chars)

Primary recommendation: the first one (leads with the phone superpower).

**Description (drafted to the ~260 char field).**
> Talk to Claude Code hands-free and hear replies in a natural neural voice, at
> your desk or from your phone. Run many coding agents and steer them all with
> one voice: ask which need you, switch between them, get told when one is done.
> Local, free, source-available.

(258 chars.)

**Topics (PH allows up to 3 primary; alternates listed).**
- Developer Tools
- Artificial Intelligence
- Productivity
- Alternates if a slot opens: Accessibility, Privacy

**Maker first comment (post immediately after launch goes live).**
> Hi Product Hunt, Krish here.
>
> I built voicebridge because I wanted to keep working with Claude Code while my
> hands were off the keyboard, walking around, or away from my desk. It does two
> things nothing else quite does together:
>
> 1. You can drive your actual live session from your phone. Scan a QR, tap
> Start, and talk. Your Mac keeps running the work; your phone is the call and
> speaks the replies back.
>
> 2. If you run several agents at once, you steer the whole fleet with one voice.
> Ask "which agents need me?", say "switch to jobhunt", and it even speaks up on
> its own when one finishes: "heads up, signup is ready for you."
>
> Everything runs locally: whisper.cpp for speech-to-text, Kokoro neural TTS for
> the voice. No cloud voice services, no per-minute fees. macOS today; it is
> free to use, and source-available.
>
> It is genuinely useful if typing is the hard part for you (RSI, limited hand
> mobility, low vision); there is an ACCESSIBILITY.md in the repo.
>
> Install is three lines inside Claude Code. Would love your feedback, and I am
> here all day to answer questions.

---

## Show HN

**Title (<=80 chars).** HN convention is "Show HN: <thing>". Options:
- `Show HN: Voicebridge, hands-free voice for Claude Code you drive from your phone` (79 chars)
- `Show HN: Voicebridge, control a fleet of Claude Code agents by voice` (67 chars)

Recommendation: the second (tighter, and the fleet angle is the differentiator).

**URL:** https://github.com/cc-vb/voicebridge

**Body (first comment, keep it plain and honest).**
> I wanted to keep using Claude Code without my hands on the keyboard, so I built
> a local voice layer for it. Two parts I have not seen combined elsewhere:
>
> - You can drive your live session from your phone. It prints a QR, you scan it,
> tap Start, and talk. The Mac runs the session; the phone is just the call and
> speaks replies back.
> - If you run several agents at once, one voice steers all of them. You can ask
> which are waiting on you, switch between them, and it announces when one
> finishes.
>
> Speech-to-text is whisper.cpp and text-to-speech is Kokoro, both running
> locally, so there are no cloud voice services and no per-minute cost. You can
> talk over a reply to interrupt it (barge-in). It works alongside the native
> /voice dictation rather than replacing it.
>
> macOS only for now (it leans on say, osascript, and CoreAudio); Windows/Linux
> are behind an OS layer but need a tester. Source-available.
>
> Honest limits: at the desk the voiced window has to stay focused for text to
> land at the cursor, and headphones are strongly recommended on speakers so the
> mic does not hear Claude. Would appreciate blunt feedback on the fleet-control
> idea specifically.

---

## X / Twitter thread

Each tweet is under 280 chars. Post as a thread; pin tweet 1. Attach the fleet
GIF to tweet 1 and the phone GIF to tweet 4.

**1/6**
> You can now control Claude Code with your voice, and drive your live coding
> session from your phone.
>
> Say "which agents need me?" and it tells you. Say "switch to jobhunt" and it
> does. One voice, every agent.
>
> Local, free, source-available. A thread:

**2/6**
> The part nothing else does: run many coding agents at once and steer them all
> by voice.
>
> "Which agents need me?" -> it reads out who is waiting vs working. It even
> speaks up on its own when one finishes: "heads up, signup is ready for you."

**3/6**
> Talk hands-free at your desk with /voice-agent. Everything you say goes to
> Claude; replies are read aloud in a natural neural voice.
>
> Talk over a reply and it stops and takes your new words. That is real barge-in,
> not a mute button.

**4/6**
> The superpower: /voice-phone prints a QR. Scan it, tap Start, and talk to your
> live session from anywhere.
>
> Your Mac keeps running the work. Your phone is the call and speaks the answers
> back. No app store, no account.

**5/6**
> It all runs locally: whisper.cpp for speech, Kokoro neural TTS for the voice.
> No cloud voice services, no per-minute fees.
>
> And if typing is the hard part for you (RSI, low vision), that is exactly who
> this is built for.

**6/6**
> Three lines inside Claude Code to try it:
>
> /plugin marketplace add cc-vb/voicebridge
> /plugin install voicebridge@voicebridge
> /voicebridge:setup
>
> macOS, source-available. Repo and demo: https://github.com/cc-vb/voicebridge

(Note: tweet 2 uses "->" only as shorthand inside a spoken example; replace with
"then" if you prefer to keep the post arrow-free.)

---

## 7-day launch checklist

Day counts are relative; slot them against your chosen launch date.

**Day 1 (T-6): record.**
- Record the demo video per the shot list above. Do a dry run first so the
  barge-in and fleet beats land cleanly.
- Capture the three GIFs (fleet, barge-in, phone).

**Day 2 (T-5): edit and export.**
- Cut the video to under 90s, bake in the captions, normalize audio.
- Upload to YouTube (unlisted for now). Export the GIFs under their size caps.

**Day 3 (T-4): assets.**
- Build the PH gallery images at 1270x760 (first frame = fleet moment with a
  baked caption). Build the 240x240 thumbnail. Confirm every file is under 3 MB.
- Paste the YouTube link into the README hero placeholder and commit the GIFs
  into `docs/media/`, updating the README GIF placeholder.

**Day 4 (T-3): draft the listings.**
- Create the PH listing in draft: name, tagline, description, gallery, thumbnail,
  topics, maker first comment. Schedule for 12:01 AM PT on launch day.
- Paste the Show HN title/body and the X thread into a scratch note, final pass
  for length and typos.

**Day 5 (T-2): dry run and reviewers.**
- Ask 2-3 people who have actually used it to be ready to comment honestly on
  launch day. No vote rings, no scripted reviews.
- Re-read every draft for hype you cannot back up; cut it.

**Day 6 (T-1): clean-Mac install dogfood (gating).**
- On a Mac (or a fresh user account) that has never had voicebridge, install
  exactly as a stranger would, from the marketplace only:
  ```
  /plugin marketplace add cc-vb/voicebridge
  /plugin install voicebridge@voicebridge
  /voicebridge:setup
  ```
- Grant the Microphone and Accessibility prompts. Run `vb doctor` and confirm a
  clean pass.
- Confirm **`/voice-agent`** actually listens and speaks a reply.
- Confirm **`/voice-phone`** prints a scannable QR and the phone can talk to the
  session and hear the answer.
- If any step needs a machine-specific path fix or a missing dependency, fix it
  in the repo and re-test from scratch. Launch is blocked until a stranger's
  install works end to end, because the demo and the launch both depend on it.

**Day 7 (T-0): launch.**
- 12:01 AM PT: PH listing goes live; post the maker first comment immediately.
- Morning: post Show HN and the X thread; pin tweet 1.
- Throughout the day: reply to every PH and HN comment quickly and honestly.
  Answer questions, log bugs, thank people. Do not argue; note real issues as
  follow-ups.
- End of day: capture feedback and file the top issues in the repo for the next
  version.
