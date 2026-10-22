# QueueLink

Accept matches, pick, ban and swap runes in League of Legends from your phone.

QueueLink is a remote control for the League client. The League app on your PC runs a small hidden server that the client itself uses to do things like accept matches and lock champions, and QueueLink sends it those same commands from your phone.

The pieces:

QueueLink.exe on your PC connects to that hidden server. It watches for events like “match found” or “it’s your turn to pick,” and passes them to your phone instantly.
Your phone opens a web page that the .exe serves over your Wi-Fi, so there’s no app to install. When you tap Accept or Lock In, the phone tells the .exe, and the .exe tells League.
The QR code is just the address of your PC plus a 6-digit PIN. The PIN stops anyone else on your Wi-Fi from controlling your game. Phones only pair once.

## For players

1. Download **QueueLink.exe** and double-click it.
   - If Windows says "Windows protected your PC", click **More info**, then **Run anyway**. The app isn't code-signed yet, which is why Windows warns about it.
   - If Windows asks about network access, tick **Private networks** and click **Allow**. Without this your phone can't connect.
2. A pairing page opens in your browser with a QR code. Scan it with your phone's camera. Your phone must be on the same Wi-Fi as your PC.
3. Start League. That's it.

Keep the black QueueLink window open while you play; closing it stops the app. Your phone stays paired, so next time just open QueueLink and reload the page on your phone.

Lost the pairing page? Open `http://localhost:8080/pair` on the PC.

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
