# QueueLink

Accept matches, pick, ban and swap runes in League of Legends from your phone.

## For players (your friends)

1. Download **QueueLink.exe** and double-click it.
   - If Windows says "Windows protected your PC", click **More info**, then **Run anyway**. The app isn't code-signed yet, which is why Windows warns about it.
   - If Windows asks about network access, tick **Private networks** and click **Allow**. Without this your phone can't connect.
2. A pairing page opens in your browser with a QR code. Scan it with your phone's camera. Your phone must be on the same Wi-Fi as your PC.
3. Start League. That's it.

Keep the black QueueLink window open while you play; closing it stops the app. Your phone stays paired, so next time just open QueueLink and reload the page on your phone.

Lost the pairing page? Open `http://localhost:8080/pair` on the PC.

## For you: building the .exe

### Option A: GitHub builds it (recommended)

1. Put this folder in a GitHub repo, including `.github/workflows/build.yml`.
2. Every push to `main` builds the .exe. Find it under **Actions → latest run → Artifacts**.
3. To publish a version for friends, create a tag such as `v1.0.0` (on GitHub: **Releases → Draft a new release → choose a tag**). The workflow attaches `QueueLink.exe` to the release.
4. Use this permanent link for your Lovable download button. It always points to the newest release:
   `https://github.com/<your-username>/<repo>/releases/latest/download/QueueLink.exe`

### Option B: build it on your PC

Install Python 3.9+ from python.org (tick "Add python.exe to PATH"), then double-click `build.bat`. The result is `dist\QueueLink.exe`.

## Files

| File | What it is |
|---|---|
| `server.py` | The app: connects to League, serves the phone UI and pairing page |
| `index.html` | Phone UI, bundled inside the .exe |
| `icon.ico` | App icon |
| `build.bat` | One-click local build |
| `.github/workflows/build.yml` | Automatic Windows build and releases |
| `requirements.txt` | Python dependencies |

## Under the hood

- **Finding League**: reads the port and token from the running `LeagueClientUx` process, falling back to the `lockfile`. It reconnects automatically if League restarts.
- **Pairing**: the 6-digit PIN is created on first launch and saved in `%APPDATA%\QueueLink\config.json`. Delete that file to reset it, which unpairs every phone. Every phone connection must present the PIN, so other people on the Wi-Fi can't control your client.
- **Ports**: uses 8080, or the next free port up to 8089.
- **Auto-accept** runs on the PC, so it works while your phone screen is off.
- **Phone UI** loads Socket.IO and fonts from public CDNs, so the phone needs internet access. Game icons come straight from the League client.
- **Settings** (environment variables, optional): `QUEUELINK_PORT`, `QUEUELINK_PIN`, `QUEUELINK_NO_BROWSER=1`, and `LEAGUE_PATH` for a non-standard install folder.

### Socket.IO API

| Event | Payload | LCU call |
|---|---|---|
| `accept_match` / `decline_match` | none | `POST /lol-matchmaking/v1/ready-check/accept`, `/decline` |
| `start_queue` / `cancel_queue` | none | `POST` / `DELETE /lol-lobby/v2/lobby/matchmaking/search` |
| `search_and_lock_champion` | `{championId}` or `{query}`, `lock: bool` | `PATCH /lol-champ-select/v1/session/actions/{id}` |
| `set_auto_accept` | `{enabled}` | handled on the PC |
| `set_spells` | `{spell1Id, spell2Id}` | `PATCH /lol-champ-select/v1/session/my-selection` |
| `get_loadout` / `select_rune_page` | `{id}` | `GET /lol-perks/v1/pages`, `PUT /lol-perks/v1/currentpage` |
| `get_snapshot` | none | full current state |

Events sent to phones: `lcu_status`, `gameflow`, `queue`, `ready_check`, `champ_select`, `auto_accept`, `notice`, `snapshot`.

## Notes

- Riot doesn't officially support the League client API this uses. It can change between patches, and Riot's developer policies apply if you distribute the app more widely.
- Code signing removes the SmartScreen warning, but it requires buying a certificate.
