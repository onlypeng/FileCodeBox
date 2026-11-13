# Chat & Direct Transfer

Realtime features are built on rooms. A room is identified by a room code;
participants connect over WebSocket and the server relays signaling.

## Room types

| WebSocket path | Purpose |
|----------------|---------|
| `/ws/room/{room_code}` | Cross-device chat |
| `/ws/direct/{room_code}` | Direct file transfer (WebRTC P2P) |
| `/ws/transfer/{room_code}` | File transfer room |
| `/ws/collection/{code}` | Inbox live updates |

Older clients may still use the compatibility endpoints `POST /transfer/create/`,
`POST /direct/create/`, `GET /transfer/info/{room_code}`, and
`GET /direct/info/{room_code}`. **The `/chat/*` HTTP endpoints were removed**;
chat now uses `/room` and `/ws/room/{room_code}`.

## Direct transfer

P2P is attempted first. When it is not possible (NAT, firewall, no TURN), the
server relays binary frames — **without writing them to disk**. This is a
deliberate boundary: relay is a transport fallback, not a storage feature.

- `directRelayEnabled` toggles relay (default on)
- `directRelayChunkSize` sets the frame size in KB (default 64)
- `directMaxRelaySize` caps a single relayed file (0 = unlimited)
- `GET /admin/network/turn-check` verifies TURN connectivity from the admin panel

## File receive flow

Two delivery modes, two different flows. This state machine is the single source of
truth for "no duplicate prompts, no duplicate downloads, no inconsistent cache state".

**File state** (key `transfer_id`): `offered → collecting → transferring → done → available`

**Per-user state** (key `transfer_id + client_id`, independent per person):
`pending → accepted | declined → downloading → done → holder`

Once the file reaches `done`, one copy is saved locally. The save only runs after the
data has actually landed: while the OPFS writer is still open there is nothing to read
yet, so the file is not marked as delivered — the save is retried after assembly
completes. Skipping this produced "received/cached but no download". A file is saved at
most once per session and is never downloaded twice.

### Cache off

1. Sender emits `file_offer` to the other online members
2. **Every online member gets exactly one prompt**; declining affects nobody else
3. Transfer starts only when the receiver accepts; bytes are streamed to local storage
   (OPFS) as they arrive, then the file becomes `holder`

### Cache on

1. The file is staged on the server and **nobody is asked to accept**
2. Late joiners see the copy already available in the file list
3. **No prompt by default**: late joiners simply see the copy in the cached file list and download it when they choose. The gear icon ("Room settings") in the room header holds the "Prompt to download existing files when joining" switch (stored per browser, never shared with other members)
4. Anyone who already downloaded becomes a `holder` and is **never asked again** to serve it
5. Late joiners prefer an online `holder` over the server copy
6. **Each file is offered once**: after a prompt, a "room code + file ID" record is kept locally, so refreshing or re-joining never asks again. "Reset download reminders" in Room settings clears the records for that room

### Source priority (fixed)

```
online holder that already downloaded (P2P)  >  server cache  >  no source (wait for sender)
```

Progress is tracked per user: the same file can sit at 62% for A while B sees 0% or
done, without overwriting each other. Large files are read from the response body in
chunks and written straight to disk — never buffered whole in memory.

## Room file cache

P2P requires both peers online. Room cache stages files on the server so a late
joiner can still fetch them.

| Config | Default | Meaning |
|--------|---------|---------|
| `roomCacheEnabled` | `1` | Toggle |
| `roomCacheDefaultHours` | `72` | Retention |
| `roomCacheMaxSize` | `100` | Per-file limit (MB); larger files degrade to pure P2P |
| `roomCacheRoomQuota` | `500` | Per-room quota (MB) |

Related endpoints: `GET /room/{room_code}/files`,
`POST /room/{room_code}/file/upload`,
`GET /room/{room_code}/file/{transfer_id}/link` (refresh a signed link),
`GET /room/{room_code}/file/{transfer_id}/download`,
`DELETE /room/{room_code}/file/{transfer_id}`.

## Membership and history

Room write endpoints require "currently online **or** a member token". The token is
issued by the server on WS join, so a briefly disconnected client can still finish
an action. `GET /room/history/{room_code}` backfills messages after a reconnect.

## Scroll position

On entry the message list lands on the **latest message** (bottom). History is
fetched asynchronously, so sticking to the bottom only happens after those
messages are actually in the DOM — scrolling before that would scroll an empty
list and leave the view at the top once history arrives.

Rules:

- While at the bottom, new messages keep the view pinned to the bottom
- After scrolling up to read earlier messages, new arrivals do **not** yank the
  view back down
- Reaching the top loads one earlier page; the scroll offset is compensated so
  the viewport does not jump
- File cards and fonts grow after the first frame, so the pin is re-applied on
  the next frame

## Multi-instance deployments

Text signaling is synchronized across instances (incremental events plus a 5-second
full membership snapshot that self-heals). **Binary file chunks are deliberately
not forwarded across instances**: a single frame is up to 256KB and pub/sub would
inflate it ~1.4× with unpredictable latency.

Configure session affinity on room code at the load balancer. Without it, the
sender receives a `relay_failed` message rather than a silently stalled transfer.
Pure P2P is unaffected. Rate-limit counters live in process memory, so the
effective threshold under N instances is "configured value × N".
See [Getting Started](/en/guide/getting-started) for the full prerequisites.

## API details

See the [API Reference](/en/api/#realtime-rooms-room-and-ws) for the complete
route table.
