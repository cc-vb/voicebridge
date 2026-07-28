# Stable phone endpoint (issue #5)

Quick tunnels (the default) mint a new random hostname every run and the edge
drops them after a few hours. A stable URL means: you enter the access key
once (never again), a home-screen install keeps working, and there is a
permanent address for the native app and push later.

The code side is DONE: once you have a stable public URL, run

    vb call link <https://your-stable-url>

and `vb phone` uses that URL forever (it verifies it responds, then prints the
same QR every time). `vb call link` with no argument shows the current one;
`vb call link off` goes back to quick tunnels. Everything below is the
one-time, account-specific setup only you can do, pick ONE option.

First, note THIS machine's per-user relay port (it is derived from your uid):

    python3 -c 'import os; print(8790 + os.getuid() % 1000)'

Call that PORT below (e.g. 9291).

## Option A, Tailscale Funnel (recommended: free, no domain)

No domain to buy, a stable `https://<machine>.<tailnet>.ts.net` URL.

1. Install + sign in (free account):
       brew install tailscale
       sudo tailscale up
2. Expose the relay's port publicly over Funnel:
       tailscale funnel PORT
   It prints a stable https URL like `https://your-mac.tailXXXX.ts.net`.
3. Wire it into voicebridge (once):
       vb call link https://your-mac.tailXXXX.ts.net
4. Start the relay and it is served at that URL every time:
       vb call on
   `vb phone` now prints the same QR forever.

Caveat: the Mac must be awake and the relay running for the URL to answer
(Funnel forwards to your Mac; it does not survive sleep). That is fine for a
personal phone-control setup; surviving sleep needs a hosted relay (later).

## Option B, Cloudflare named tunnel (custom domain)

Nicer URL (e.g. `phone.yourdomain.com`), needs a domain on Cloudflare
(~$10/yr via Cloudflare Registrar). cloudflared is already installed.

1. Log in (interactive, opens a browser, YOUR step):
       cloudflared tunnel login
2. Create the tunnel and route your subdomain to it:
       cloudflared tunnel create voicebridge
       cloudflared tunnel route dns voicebridge phone.yourdomain.com
3. Point it at the local relay. Create ~/.cloudflared/config.yml:
       tunnel: voicebridge
       credentials-file: /Users/YOU/.cloudflared/<TUNNEL-ID>.json
       ingress:
         - hostname: phone.yourdomain.com
           service: http://127.0.0.1:PORT
         - service: http_status:404
4. Run it (and install as a login service so it persists):
       cloudflared tunnel run voicebridge      # to test
       sudo cloudflared service install         # to keep it running
5. Wire it in:
       vb call link https://phone.yourdomain.com

## Keeping the relay up across reboots (optional)

The tunnel needs the relay (`vb call on`) running. To auto-start it at login,
create ~/Library/LaunchAgents/com.voicebridge.relay.plist:

    <?xml version="1.0" encoding="UTF-8"?>
    <!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
      "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
    <plist version="1.0"><dict>
      <key>Label</key><string>com.voicebridge.relay</string>
      <key>ProgramArguments</key>
      <array>
        <string>/usr/bin/python3</string>
        <string>/Users/YOU/voicebridge/bin/vb</string>
        <string>call</string><string>on</string>
      </array>
      <key>RunAtLoad</key><true/>
      <key>KeepAlive</key><true/>
    </dict></plist>

then `launchctl load ~/Library/LaunchAgents/com.voicebridge.relay.plist`.

## What only you can do

- Pick Option A or B and run its steps (Tailscale login, or the Cloudflare
  login + domain). These need your account and cannot be automated from here.
- Run `vb call link <url>` with the URL it gives you.
That is the whole remaining task; `vb phone` and the whole relay already know
how to use a saved permanent URL.
