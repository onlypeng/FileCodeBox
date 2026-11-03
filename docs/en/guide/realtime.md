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

## Multi-instance deployments

Text signaling is synchronized across instances (incremental events plus a 5-second
full membership snapshot that self-heals). **Binary file chunks are deliberately
not forwarded across instances**: a single frame is up to 256KB and pub/sub would
inflate it ~1.4× with unpredictable latency.

Configure session affinity on room code at the load balancer. Without it, the
sender receives a `relay_failed` message rather than a silently stalled transfer.
Pure P2P is unaffected. See the multi-instance section of the README.

## API details

See the [API Reference](/en/api/#realtime-rooms-room-and-ws) for the complete
route table.
