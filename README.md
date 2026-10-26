# ClipLink for Decky Loader

Copy text between your Steam Deck and your Windows PC over your local network. It pairs with the ClipLink app
for Windows.

- **PC → Deck:** text sent from the PC goes straight onto the Deck's clipboard (in Game Mode and Desktop Mode), with
  a notification.
- **Deck → PC:** Quick Access → ClipLink → **Send clipboard**, or type something and press **Send text**. You can
  also turn on **Send what I copy** so everything you copy on the Deck reaches the PC automatically.
- **Recent:** the last few texts, in both directions. Received texts have **Copy**, which puts the text on the
  clipboard. Sent texts have **Type**, which types the text into the game or app you were using. Every entry has
  **Paste**, which copies the text and presses Ctrl+V in the game or app you were using.
- **On/off switch** at the top. When it's off, ClipLink leaves the network and stops watching the clipboard.

Both devices need to be on the same network. They find each other by themselves. If they don't (for example on a
guest Wi-Fi that isolates devices), enter the PC's address under Settings → PC address.

## Install

Needs [Decky Loader](https://decky.xyz). In Desktop Mode, open Konsole and run this as your normal user:

```sh
curl -fsSL https://raw.githubusercontent.com/JayPrates/cliplink-decky/main/install.sh | bash
```

It downloads the latest release into `~/homebrew/plugins/ClipLink` and restarts Decky. Run it again to update.

Manual alternative: download `ClipLink.zip` from [Releases](https://github.com/JayPrates/cliplink-decky/releases).
Then in Decky, turn on Settings → General → **Developer mode** and use Developer → **Install Plugin from ZIP File**.

On the PC, run ClipLink for Windows. When Windows Firewall asks, allow it on **Private networks**.

## How it works

- `main.py` (backend, runs as your normal user): it announces itself with UDP broadcasts on port 47391 and receives
  text on TCP port 47390, one JSON line per transfer. It reads and writes the clipboard directly on the X displays
  through libX11. That covers Steam's Xwayland in Game Mode and the desktop in Desktop Mode. In a Plasma session it
  also goes through Klipper over D-Bus. Clipboard changes are detected with XFixes, so nothing polls in Game Mode.
- `src/index.tsx` (frontend): the panel and the notifications.
- If the backend can't reach the clipboard, the panel's **Copy** buttons fall back to Steam's browser clipboard.
- History is kept in memory only, never written to disk.
- Optional **pairing code**: when it's set, only devices using the same code can send text here.
- Logs: `~/homebrew/logs/ClipLink/`.

## Build

`npm install && npm run build` builds `dist/index.js`. On Windows, `build.ps1` also packages `out/ClipLink.zip`.
