#!/usr/bin/env bun
/**
 * voicebridge vapi relay
 *
 * Bridges a real-time Vapi phone call to your LIVE Claude Code session.
 *
 * Vapi handles the call, speech-to-text, and text-to-speech. It talks to a
 * "custom LLM" over an OpenAI-compatible /chat/completions endpoint. This
 * relay IS that endpoint, and it is ALSO a Claude Code channel: each user
 * turn is injected into your running session, and Claude's reply (via the
 * channel reply tool) is returned for Vapi to speak.
 *
 *   phone --call--> Vapi --(STT)--> POST /chat/completions --> [this relay]
 *        <--speak-- Vapi <--(TTS)-- reply text <-------------- your session
 *
 * Run (custom channel needs the dev flag during research preview):
 *   claude --dangerously-load-development-channels server:vapi-relay
 * then expose the HTTP port with a tunnel and point Vapi's custom LLM at it.
 *
 * NOTE: this is the heavier, less-battle-tested path. Claude Code turns can
 * take seconds; configure Vapi filler/patience. Needs a Vapi account, a
 * phone number, and a public tunnel. See VAPI.md.
 */

import { Server } from "@modelcontextprotocol/sdk/server/index.js";
import { StdioServerTransport } from "@modelcontextprotocol/sdk/server/stdio.js";
import {
  ListToolsRequestSchema,
  CallToolRequestSchema,
} from "@modelcontextprotocol/sdk/types.js";

const PORT = Number(process.env.VAPI_RELAY_PORT || 8790);
const TURN_TIMEOUT_MS = Number(process.env.VAPI_TURN_TIMEOUT_MS || 120000);
const SHARED_SECRET = process.env.VAPI_SHARED_SECRET || ""; // optional bearer

// One phone call = one sequential conversation, so a single in-flight turn
// is enough: the HTTP handler parks a resolver, the reply tool resolves it.
let pending: ((text: string) => void) | null = null;

const mcp = new Server(
  { name: "vapi-relay", version: "0.1.0" },
  {
    capabilities: { experimental: { "claude/channel": {} }, tools: {} },
    instructions:
      'Turns from a live phone call arrive as <channel source="vapi-relay" ' +
      'chat_id="call">. Answer by calling the `reply` tool with chat_id ' +
      '"call" and a short, spoken-style sentence (it will be read aloud on a ' +
      "phone, so be brief and conversational, no code or markdown).",
  },
);

mcp.setRequestHandler(ListToolsRequestSchema, async () => ({
  tools: [
    {
      name: "reply",
      description: "Speak a reply back to the caller",
      inputSchema: {
        type: "object",
        properties: {
          chat_id: { type: "string" },
          text: { type: "string", description: "short, spoken-style reply" },
        },
        required: ["chat_id", "text"],
      },
    },
  ],
}));

mcp.setRequestHandler(CallToolRequestSchema, async (req) => {
  if (req.params.name !== "reply") throw new Error(`unknown tool: ${req.params.name}`);
  const { text } = req.params.arguments as { chat_id: string; text: string };
  if (pending) {
    pending(text);
    pending = null;
  }
  return { content: [{ type: "text", text: "spoken" }] };
});

await mcp.connect(new StdioServerTransport());

function openAIResponse(text: string) {
  return {
    id: `chatcmpl-${Date.now()}`,
    object: "chat.completion",
    created: Math.floor(Date.now() / 1000),
    model: "voicebridge-claude-code",
    choices: [
      {
        index: 0,
        message: { role: "assistant", content: text },
        finish_reason: "stop",
      },
    ],
  };
}

Bun.serve({
  port: PORT,
  hostname: "127.0.0.1", // expose via tunnel, not directly
  idleTimeout: 0,
  async fetch(req) {
    const url = new URL(req.url);
    if (url.pathname === "/health") return new Response("ok");
    if (req.method !== "POST" || !url.pathname.endsWith("/chat/completions"))
      return new Response("not found", { status: 404 });

    if (SHARED_SECRET) {
      const auth = req.headers.get("authorization") || "";
      if (auth !== `Bearer ${SHARED_SECRET}`)
        return new Response("unauthorized", { status: 401 });
    }

    let body: any;
    try {
      body = await req.json();
    } catch {
      return new Response("bad request", { status: 400 });
    }
    const msgs = Array.isArray(body?.messages) ? body.messages : [];
    const lastUser = [...msgs].reverse().find((m: any) => m.role === "user");
    const userText = (lastUser?.content ?? "").toString().trim();
    if (!userText) return Response.json(openAIResponse("Sorry, I didn't catch that."));

    // Park a resolver, inject the turn, wait for Claude's reply tool.
    const reply = await new Promise<string>((resolve) => {
      pending = resolve;
      const timer = setTimeout(() => {
        if (pending === resolve) {
          pending = null;
          resolve("Still working on that, give me a moment.");
        }
      }, TURN_TIMEOUT_MS);
      mcp
        .notification({
          method: "notifications/claude/channel",
          params: { content: userText, meta: { chat_id: "call" } },
        })
        .catch(() => {
          clearTimeout(timer);
          if (pending === resolve) {
            pending = null;
            resolve("I couldn't reach the session.");
          }
        });
    });

    return Response.json(openAIResponse(reply));
  },
});

console.error(`vapi-relay: channel connected, HTTP on 127.0.0.1:${PORT}`);
