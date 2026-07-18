#!/usr/bin/env bun
/**
 * voicebridge telegram-voice channel
 *
 * A custom Claude Code channel (MCP server over stdio) that bridges Telegram
 * to your LIVE session, with voice both ways:
 *   - inbound voice notes  -> whisper.cpp transcription -> injected into session
 *   - inbound text         -> injected as-is
 *   - Claude's reply       -> sent as text AND a spoken voice note (say -> opus)
 *   - permission prompts    -> relayed to Telegram; reply "yes <id>" / "no <id>"
 *
 * Everything (STT + TTS) runs locally on your Mac. Only the audio/text that
 * goes to Telegram leaves the machine, same as any Telegram chat.
 *
 * Run (research preview requires the dev flag for custom channels):
 *   claude --dangerously-load-development-channels server:telegram-voice
 * (registered via the .mcp.json in this directory)
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";
import { z } from "zod";
import { spawnSync } from "node:child_process";
import { existsSync, mkdirSync, readFileSync } from "node:fs";
import { homedir } from "node:os";
import { join } from "node:path";

// ---------- config ----------------------------------------------------------
const HOME = homedir();
const VB = join(HOME, ".voicebridge");
const TG_DIR = join(VB, "telegram");
const TMP = join(TG_DIR, "tmp");
const MODEL = join(VB, "models", "ggml-base.en.bin");
mkdirSync(TMP, { recursive: true });

function fromEnvFile(path: string, key: string): string {
  if (!existsSync(path)) return "";
  for (const line of readFileSync(path, "utf8").split("\n")) {
    const m = line.match(/^\s*([A-Z_]+)\s*=\s*(.*)\s*$/);
    if (m && m[1] === key) return m[2].replace(/^["']|["']$/g, "");
  }
  return "";
}

const TOKEN =
  process.env.TELEGRAM_BOT_TOKEN ||
  fromEnvFile(join(TG_DIR, ".env"), "TELEGRAM_BOT_TOKEN");
if (!TOKEN) {
  console.error(
    "telegram-voice: no TELEGRAM_BOT_TOKEN (set env or ~/.voicebridge/telegram/.env)",
  );
  process.exit(1);
}
const API = `https://api.telegram.org/bot${TOKEN}`;
const FILE_API = `https://api.telegram.org/file/bot${TOKEN}`;

// Resolve Homebrew tools directly (channels may run with a minimal PATH).
function bin(name: string): string {
  for (const d of ["/opt/homebrew/bin", "/usr/local/bin", "/usr/bin"]) {
    const p = join(d, name);
    if (existsSync(p)) return p;
  }
  return name;
}
const WHISPER = bin("whisper-cli");
const FFMPEG = bin("ffmpeg");
const SAY = "/usr/bin/say";

// Sender allowlist: ~/.voicebridge/telegram/allow (one numeric id per line)
// plus optional TELEGRAM_ALLOWED env (comma-separated). Empty = bootstrap mode.
function loadAllow(): Set<string> {
  const s = new Set<string>();
  for (const id of (process.env.TELEGRAM_ALLOWED || "").split(","))
    if (id.trim()) s.add(id.trim());
  const f = join(TG_DIR, "allow");
  if (existsSync(f))
    for (const line of readFileSync(f, "utf8").split("\n"))
      if (line.trim()) s.add(line.trim());
  return s;
}
const allow = loadAllow();
let lastChatId = "";

// ---------- telegram helpers ------------------------------------------------
async function tg(method: string, params: Record<string, unknown>) {
  const r = await fetch(`${API}/${method}`, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: JSON.stringify(params),
  });
  return r.json();
}

async function sendMessage(chatId: string, text: string) {
  await tg("sendMessage", { chat_id: chatId, text });
}

async function sendVoice(chatId: string, oggPath: string) {
  const buf = await Bun.file(oggPath).arrayBuffer();
  const fd = new FormData();
  fd.append("chat_id", chatId);
  fd.append("voice", new Blob([buf], { type: "audio/ogg" }), "reply.ogg");
  await fetch(`${API}/sendVoice`, { method: "POST", body: fd });
}

async function downloadTgFile(fileId: string, out: string): Promise<boolean> {
  const info: any = await tg("getFile", { file_id: fileId });
  const path = info?.result?.file_path;
  if (!path) return false;
  const r = await fetch(`${FILE_API}/${path}`);
  if (!r.ok) return false;
  await Bun.write(out, await r.arrayBuffer());
  return true;
}

// ---------- media pipeline --------------------------------------------------
function run(cmd: string, args: string[]): { ok: boolean; out: string } {
  const r = spawnSync(cmd, args, { encoding: "utf8", timeout: 120000 });
  return { ok: r.status === 0, out: (r.stdout || "").trim() };
}

function decodeToWav(input: string, wav: string): boolean {
  return run(FFMPEG, ["-y", "-i", input, "-ar", "16000", "-ac", "1", wav]).ok;
}

function transcribe(wav: string): string {
  if (!existsSync(MODEL)) return "";
  const r = run(WHISPER, ["-m", MODEL, "-f", wav, "-nt", "-np", "-l", "en"]);
  return r.out.replace(/\[[^\]]*\]/g, "").trim();
}

function tts(text: string, ogg: string): boolean {
  const aiff = ogg.replace(/\.ogg$/, ".aiff");
  if (!run(SAY, ["-o", aiff, text.slice(0, 1200)]).ok) return false;
  return run(FFMPEG, ["-y", "-i", aiff, "-c:a", "libopus", "-b:a", "32k", ogg]).ok;
}

// ---------- MCP server ------------------------------------------------------
const mcp = new Server(
  { name: "telegram-voice", version: "0.1.0" },
  {
    capabilities: {
      experimental: {
        "claude/channel": {},
        "claude/channel/permission": {},
      },
      tools: {},
    },
    instructions:
      'Messages from the user arrive as <channel source="telegram-voice" ' +
      'chat_id="..."> (transcribed from voice when they speak). To answer, ' +
      "call the `reply` tool with the chat_id from the tag and your text; it " +
      "is sent as both text and a spoken voice note. Keep replies concise and " +
      "conversational since they will be heard aloud. For permission prompts, " +
      'the user replies "yes <id>" or "no <id>" from Telegram.',
  },
);

// reply tool: text + spoken voice note back to Telegram
mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "reply",
      description: "Send a reply to the Telegram user (text + spoken voice note)",
      inputSchema: {
        type: "object",
        properties: {
          chat_id: { type: "string", description: "chat_id from the inbound tag" },
          text: { type: "string", description: "the reply, phrased to be heard" },
        },
        required: ["chat_id", "text"],
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== "reply") throw new Error(`unknown tool: ${req.params.name}`);
  const { chat_id, text } = req.params.arguments as { chat_id: string; text: string };
  await sendMessage(chat_id, text);
  try {
    const ogg = join(TMP, `reply-${Date.now()}.ogg`);
    if (tts(text, ogg)) await sendVoice(chat_id, ogg);
  } catch {
    /* voice is best-effort; text already sent */
  }
  return { content: [{ type: "text", text: "sent" }] };
});

// permission relay: forward tool-approval prompts to Telegram
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
  if (!lastChatId) return;
  await sendMessage(
    lastChatId,
    `Claude wants to run ${params.tool_name}: ${params.description}\n` +
      `${params.input_preview}\n\nReply "yes ${params.request_id}" or "no ${params.request_id}"`,
  );
});

// ---------- inbound long-poll loop ------------------------------------------
const VERDICT = /^\s*(y|yes|n|no)\s+([a-km-z]{5})\s*$/i;

async function handleMessage(msg: any) {
  const fromId = String(msg?.from?.id ?? "");
  const chatId = String(msg?.chat?.id ?? "");
  if (!fromId || !chatId) return;

  // Bootstrap: if nobody is allowed yet, tell them their id and drop.
  if (allow.size === 0) {
    await sendMessage(
      chatId,
      `voicebridge: your Telegram id is ${fromId}. Add it to ` +
        `~/.voicebridge/telegram/allow (one line) or set TELEGRAM_ALLOWED, ` +
        `then restart the channel.`,
    );
    return;
  }
  if (!allow.has(fromId)) return; // gate on sender, drop silently
  lastChatId = chatId;

  // Permission verdict?
  if (typeof msg.text === "string") {
    const m = VERDICT.exec(msg.text);
    if (m) {
      await mcp.notification({
        method: "notifications/claude/channel/permission",
        params: {
          request_id: m[2].toLowerCase(),
          behavior: m[1].toLowerCase().startsWith("y") ? "allow" : "deny",
        },
      });
      return;
    }
  }

  // Resolve the message to text (voice -> transcript, or plain text).
  let text = "";
  const voice = msg.voice || msg.audio;
  if (voice?.file_id) {
    const raw = join(TMP, `in-${Date.now()}.oga`);
    const wav = raw.replace(/\.oga$/, ".wav");
    if ((await downloadTgFile(voice.file_id, raw)) && decodeToWav(raw, wav)) {
      text = transcribe(wav);
    }
    if (!text) {
      await sendMessage(chatId, "(couldn't transcribe that, try again?)");
      return;
    }
    await sendMessage(chatId, `heard: ${text}`); // echo so they trust it
  } else if (typeof msg.text === "string") {
    text = msg.text;
  } else {
    return;
  }

  await mcp.notification({
    method: "notifications/claude/channel",
    params: { content: text, meta: { chat_id: chatId } },
  });
}

async function poll() {
  let offset = 0;
  // drain any backlog so we start fresh
  const first: any = await tg("getUpdates", { timeout: 0, offset: -1 });
  if (first?.result?.length) offset = first.result.at(-1).update_id + 1;
  for (;;) {
    try {
      const res: any = await tg("getUpdates", { timeout: 30, offset });
      for (const u of res?.result ?? []) {
        offset = u.update_id + 1;
        if (u.message) await handleMessage(u.message);
      }
    } catch {
      await new Promise((r) => setTimeout(r, 1000));
    }
  }
}

await mcp.connect(new StdioServerTransport());
poll(); // fire-and-forget; connect stays alive over stdio
console.error("telegram-voice channel: connected, polling Telegram");
