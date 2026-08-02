#!/usr/bin/env bun
/**
 * voicebridge voicemode channel: hands-free voice INSIDE your live session.
 *
 * A custom Claude Code channel (MCP over stdio). While the mic flag is on
 * (`vb mic on`), it loops: record -> local whisper transcription -> inject
 * into the RUNNING session as a channel event. Claude answers via the
 * `reply` tool, which speaks aloud (macOS say, honoring vb voice/rate/lang).
 * Permission prompts are relayed as speech: say "yes" or "no".
 *
 * This is the real "agent mode": same session, same context, your files,
 * no window focus, no buttons. Say "stop listening" to mute the mic.
 *
 * Launch (research preview needs the dev flag for custom channels):
 *   vb session            # wraps: claude --dangerously-load-development-channels server:voicemode
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync, rmSync, writeFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// ---------- config / state ---------------------------------------------------
const VB = join(homedir(), ".voicebridge");
const MIC_FLAG = join(VB, "mic_on");
const TMP = join(VB, "vm-tmp");
mkdirSync(TMP, { recursive: true });

const VOICE_MAP: Record<string, string> = { hindi: "Lekha", hinglish: "Rishi" };

function bin(name: string): string {
  for (const d of ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]) {
    const p = join(d, name);
    if (existsSync(p)) return p;
  }
  return name;
}
const WHISPER = bin("whisper-cli");
const REC = bin("rec");
const SAY = "/usr/bin/say";
const AFPLAY = "/usr/bin/afplay";
const CURL = bin("curl");
// The Kokoro neural voice server (same one the phone uses). Honor VB_TTS_PORT
// exactly like core.py; otherwise the per-uid port (6000 + uid % 1000). Without
// this override, a user who sets VB_TTS_PORT would have the server bind the
// override while this channel hit the default, so every synth would silently
// fall back to the robotic `say`.
const KOKORO_PORT =
  parseInt(process.env.VB_TTS_PORT || "", 10) ||
  6000 + ((typeof process.getuid === "function" ? process.getuid() ?? 0 : 0) % 1000);
const KOKORO_WAV = join(TMP, "kokoro-say.wav");
// Kokoro voice ids look like af_heart / am_onyx / bf_emma; a `say` voice name
// (e.g. "Samantha") is not one, so fall back to the default in that case.
const KOKORO_ID = /^[a-z][fm]_/;

function readState(file: string): string {
  try {
    return readFileSync(join(VB, file), "utf8").trim();
  } catch {
    return "";
  }
}
function model(): string {
  for (const m of ["ggml-small.en.bin", "ggml-base.en.bin"]) {
    const p = join(VB, "models", m);
    if (existsSync(p)) return p;
  }
  return "";
}

const TINK = "/System/Library/Sounds/Tink.aiff";
const POP = "/System/Library/Sounds/Pop.aiff";
const MORSE = "/System/Library/Sounds/Morse.aiff";

let speaking = false;
let awaitingReply = false;
let replyDeadline = 0;
let pendingPerm = "";

// ---------- audio helpers -----------------------------------------------------
function beep(sound: string) {
  Bun.spawn([AFPLAY, sound], { stdout: "ignore", stderr: "ignore" });
}

function cleanForSpeech(text: string): string {
  let t = text.replace(/```[\s\S]*?```/g, " code block ");
  t = t.replace(/\[([^\]]+)\]\([^)]+\)/g, "$1");
  t = t.replace(/https?:\/\/\S+/g, " a link ");
  t = t.replace(/[`#>*~|]/g, "").replace(/\s+/g, " ").trim();  // keep "_" (identifiers)
  return t.slice(0, 1200);
}

/* A phone call is live if its heartbeat (written by the relay every ~5s) is
   fresh. While it is, the phone OWNS the audio, so this Mac channel stays
   silent instead of double-speaking in a second voice. Mirrors core.call_live
   (15s window) so both speak paths defer to the phone identically. */
function callLive(): boolean {
  try {
    const t = parseFloat(readFileSync(join(VB, "call_heartbeat"), "utf8").trim());
    return Number.isFinite(t) && Date.now() / 1000 - t < 15;
  } catch {
    return false;
  }
}

/* Speak via the Kokoro neural voice (matches the phone). Synthesizes to a wav
   through the local Kokoro server and plays it, blocking to keep half-duplex.
   Returns false if Kokoro is unavailable so the caller can fall back to `say`. */
function kokoroSpeak(text: string, rate: string, voice: string): boolean {
  try {
    const kv = KOKORO_ID.test(voice) ? voice : "af_heart";
    const speed = Math.min(1.3, Math.max(0.7, (parseInt(rate, 10) || 175) / 175));
    const body = JSON.stringify({ text, voice: kv, speed });
    const r = spawnSync(
      CURL,
      // -f: fail (non-zero, no body written) on any HTTP >=400 so an error
      // response is never mistaken for audio.
      ["-fsS", "-m", "20", "-o", KOKORO_WAV, "-X", "POST",
       `http://127.0.0.1:${KOKORO_PORT}/synth`,
       "-H", "Content-Type: application/json", "-d", body],
      { timeout: 25000 },
    );
    if (r.status !== 0) return false;
    // Confirm it is really a WAV (RIFF magic), not a >1KB error body, before
    // handing it to afplay, mirroring core._kokoro_wav's guard.
    let head: Buffer;
    try { head = readFileSync(KOKORO_WAV); } catch { return false; }
    if (head.length < 1000 || head.toString("latin1", 0, 4) !== "RIFF") return false;
    spawnSync(AFPLAY, [KOKORO_WAV], { timeout: 240000 }); // blocking: half-duplex
    return true;
  } catch {
    return false;
  }
}

/* Surface a failure from THIS (separate) process to the phone, by appending to
   the same mailbox the relay drains (core.ERROR_MAILBOX). Throttled per message
   so a per-cycle failure can't spam the toast. */
const _lastErr: Record<string, number> = {};
function pushError(where: string, msg: string) {
  const key = where + "|" + msg;
  const now = Date.now();
  if (now - (_lastErr[key] || 0) < 30000) return;
  _lastErr[key] = now;
  try {
    const f = join(VB, "errors_pending.jsonl");
    let lines: string[] = [];
    try { lines = readFileSync(f, "utf8").split("\n").filter(Boolean).slice(-19); } catch {}
    lines.push(JSON.stringify({ ts: now / 1000, where, msg }));
    writeFileSync(f, lines.join("\n") + "\n");
  } catch {}
}

function speak(text: string) {
  const t = cleanForSpeech(text);
  if (!t) return;
  // A live phone call owns the audio: stay silent and let the phone speak.
  if (callLive()) return;
  speaking = true;
  try {
    const rate = readState("rate") || "175";
    const lang = readState("lang").toLowerCase();
    const voice = readState("voice") || VOICE_MAP[lang] || "";
    // Kokoro's voices are English-only, and the user can pick `say` explicitly.
    // Mirror core.speak_chunks_blocking: any Devanagari text or a Hindi/Hinglish
    // session, or a non-kokoro engine preference, MUST use `say` (with the right
    // voice), not Kokoro, otherwise Hindi comes out in a wrong English voice.
    const isHindi = /[ऀ-ॿ]/.test(t) || lang === "hindi" || lang === "hinglish";
    const engine = readState("engine") || "kokoro";
    // Prefer Kokoro (matches the phone) only when it's appropriate; otherwise
    // fall straight to `say`. Fall back to `say` too if the Kokoro server is down.
    if (!isHindi && engine === "kokoro" && kokoroSpeak(t, rate, voice)) return;
    const args = [SAY, "-r", rate];
    if (voice) args.push("-v", voice);
    args.push(t);
    spawnSync(args[0], args.slice(1), { timeout: 240000 }); // blocking: half-duplex
  } finally {
    speaking = false;
  }
}

async function runAsync(
  cmd: string[],
  timeoutMs: number,
): Promise<{ ok: boolean; out: string }> {
  const p = Bun.spawn(cmd, { stdout: "pipe", stderr: "ignore" });
  const timer = setTimeout(() => p.kill(), timeoutMs);
  const out = await new Response(p.stdout).text();
  const code = await p.exited;
  clearTimeout(timer);
  return { ok: code === 0, out: out.trim() };
}

async function record(wav: string): Promise<boolean> {
  // Start on first faint sound, stop after 2s of quiet, level-normalize.
  const r = await runAsync(
    [
      REC, "-q", "-c", "1", "-r", "16000", "-b", "16", wav,
      "trim", "0", "30",
      "silence", "1", "0.05", "0.3%", "1", "2.0", "1.5%",
      "norm", "-1",
    ],
    45000,
  );
  if (!r.ok) return false;
  try {
    return Bun.file(wav).size > 1000;
  } catch {
    return false;
  }
}

async function transcribe(wav: string): Promise<string> {
  const m = model();
  if (!m) {
    // No speech model => every utterance is silently dropped. Say so instead.
    pushError("transcribe", "Speech model missing. Run setup on the Mac.");
    return "";
  }
  const r = await runAsync(
    [WHISPER, "-m", m, "-f", wav, "-nt", "-np", "-l", "en"],
    120000,
  );
  return r.out.replace(/\[[^\]]*\]/g, "").replace(/\s+/g, " ").trim();
}

// ---------- exit / verdict phrase handling -----------------------------------
const MUTE_RE =
  /^\s*(stop listening|mute|mic off|turn (the )?mic off|stop the mic|go to sleep)[.!\s]*$/i;
const YES_RE = /^\s*(yes|yeah|yep|sure|allow|approve|go ahead|do it)[.!\s]*$/i;
const NO_RE = /^\s*(no|nope|deny|don't|do not|cancel)[.!\s]*$/i;

// ---------- MCP server --------------------------------------------------------
const mcp = new Server(
  { name: "voicemode", version: "0.1.0" },
  {
    capabilities: {
      experimental: {
        "claude/channel": {},
        "claude/channel/permission": {},
      },
      tools: {},
    },
    instructions:
      'Spoken messages from the user arrive as <channel source="voicemode">. ' +
      "This is a LIVE VOICE conversation: always answer by calling the " +
      "voicemode `reply` tool with short, natural, spoken-style text (first " +
      "person, no markdown, no code, no URLs; summarize instead). Reply " +
      "BEFORE starting long work (say what you're about to do), and reply " +
      "again with the outcome when done. The user hears your reply aloud.",
  },
);

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "reply",
      description:
        "Speak a reply aloud to the user (live voice conversation). Short, spoken-style text only.",
      inputSchema: {
        type: "object",
        properties: {
          text: { type: "string", description: "what to say, conversational" },
        },
        required: ["text"],
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== "reply")
    throw new Error(`unknown tool: ${req.params.name}`);
  const { text } = req.params.arguments as { text: string };
  awaitingReply = false; // reply arrived; mic can resume after speech
  speak(text);
  return { content: [{ type: "text", text: "spoken" }] };
});

// Permission relay: speak the prompt, answer by voice.
const PermReq = z.object({
  method: z.literal("notifications/claude/channel/permission_request"),
  params: z.object({
    request_id: z.string(),
    tool_name: z.string(),
    description: z.string(),
    input_preview: z.string(),
  }),
});
mcp.setNotificationHandler(PermReq, async ({ params }) => {
  pendingPerm = params.request_id;
  speak(
    `I need permission to use ${params.tool_name}. ${params.description}. ` +
      `Say yes to allow, or no to deny.`,
  );
});

await mcp.connect(new StdioServerTransport());

// ---------- mic loop ----------------------------------------------------------
const sleep = (ms: number) => new Promise((r) => setTimeout(r, ms));

async function micLoop() {
  let announced = false;
  for (;;) {
    if (!existsSync(MIC_FLAG)) {
      announced = false;
      await sleep(400);
      continue;
    }
    // Phone owns the audio while a call is live: yield the Mac mic to it so we
    // don't capture the same speech twice.
    if (callLive()) {
      announced = false;
      await sleep(500);
      continue;
    }
    if (!announced) {
      speak("Voice mode on. I'm listening.");
      announced = true;
    }
    if (speaking) {
      await sleep(200);
      continue;
    }
    if (awaitingReply) {
      if (Date.now() > replyDeadline) {
        awaitingReply = false;
        speak("Still working. Listening again in the meantime.");
      } else {
        await sleep(300);
        continue;
      }
    }

    beep(TINK);
    await sleep(350); // don't record the Tink as the first word
    const wav = join(TMP, "mic.wav");
    try {
      rmSync(wav, { force: true });
    } catch {}
    const got = await record(wav);
    beep(POP);
    if (!got) continue;
    const text = await transcribe(wav);
    if (!text) continue;

    if (pendingPerm) {
      if (YES_RE.test(text) || NO_RE.test(text)) {
        const allow = YES_RE.test(text);
        await mcp.notification({
          method: "notifications/claude/channel/permission",
          params: {
            request_id: pendingPerm,
            behavior: allow ? "allow" : "deny",
          },
        });
        pendingPerm = "";
        speak(allow ? "Allowed." : "Denied.");
        continue;
      }
      // anything else falls through as a normal message
    }

    if (MUTE_RE.test(text)) {
      try {
        rmSync(MIC_FLAG, { force: true });
      } catch {}
      speak("Okay, mic off. Run vb mic on when you want me back.");
      continue;
    }

    console.error(`voicemode <- "${text}"`);
    beep(MORSE);
    await mcp.notification({
      method: "notifications/claude/channel",
      params: { content: text, meta: { via: "voice" } },
    });
    awaitingReply = true;
    replyDeadline = Date.now() + 180000; // resume listening after 3 min anyway
  }
}

micLoop();
console.error("voicemode channel: connected (mic follows ~/.voicebridge/mic_on)");
