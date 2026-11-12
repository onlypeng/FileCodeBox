# 聊天与直连传输

实时能力都建在「房间」上：一个 6 位房间码 + 一条 WebSocket 连接，服务端只转发信令。
三类房间共用一套连接与成员管理。

| WebSocket 路径 | 用途 |
|---|---|
| `/ws/room/{room_code}` | 跨设备聊天 |
| `/ws/direct/{room_code}` | 直连快传（聊天 + 文件） |
| `/ws/transfer/{room_code}` | 点对点文件传输 |
| `/ws/collection/{code}` | 收件箱实时推送 |

旧客户端仍可用兼容端点 `POST /transfer/create/`、`POST /direct/create/`、
`GET /transfer/info/{room_code}`、`GET /direct/info/{room_code}`。
**`/chat/*` HTTP 端点已下线**，聊天统一走 `/room` 与 `/ws/room/{room_code}`。

## 连接与成员

- 首个连接者为主机（WebRTC 发起方），其余为访客（应答方）
- 写接口（上传房间文件、删缓存）要求「当前在线 **或** 持有成员令牌」；
  令牌在 WS join 时签发，短暂断线也能把动作做完
- 断线重连用 `GET /room/history/{room_code}` 回填最近消息（默认 100 条）
- 文本单条上限 64KB；消息发送限流「每 5 秒最多 10 条」，用于防刷屏
- 中转的文件分片走 ECDH 协商的**分片级加密**：`crypto_setup` 只把公钥定向转发给对端，
  服务端不参与解密

## 直连中转

P2P 优先。连不上时（对称 NAT、防火墙、无 TURN）服务端中转二进制帧，
**且不落盘**——中转是传输兜底，不是存储功能。

- `directRelayEnabled` 中转开关（默认开）
- `directRelayChunkSize` 分片大小 KB（默认 64，范围 16~256）
- `directRelaySpeedLimit` 中转限速 KB/s（0=不限）
- `directMaxRelaySize` 单个中转文件上限 MB（0=不限）
- `directTurnServers` TURN 列表；后台 `/admin/network/turn-check` 可自检连通性

## 文件接收流程

房间有两种交付形态，流程与弹窗规则都不同。以下状态机是避免「重复弹窗、重复下载、
缓存状态不一致」的唯一依据。

**文件态**（键 `transfer_id`）：
`offered → collecting → transferring → done → available`

**每用户态**（键 `transfer_id + client_id`，各人独立互不影响）：
`pending → accepted | declined → downloading → done → holder`

### 房间未开服务缓存

1. 发送方 `file_offer` 广播给其他在线成员
2. **每个在线用户各自弹一次**「是否接收」，互不阻塞；拒绝不影响别人
3. 接受方点开下载才开始传输，边收边写本地（OPFS），完成后落盘并转为 `holder`

### 房间已开服务缓存

1. 文件自动缓存到服务器，**不再向任何人弹「是否接收」**
2. 后进房间的人在文件列表直接看到可用副本
3. **默认不弹窗**：后进的人只在缓存文件列表里看到可用副本，想下再点。房间顶部「静默 / 提示下载」按钮可切成弹窗提醒（该偏好只存在自己浏览器里，不影响别人）
4. 已下载完成的用户转为 `holder`，**不会被再问一次「是否提供下载」**
5. 后来者取源时优先借用在线 `holder` 做 P2P，其次才走服务器缓存

### 取源优先级（固定）

```
在线且已下载的 holder（P2P）  >  服务端缓存  >  无源提示（等发送方上线）
```

进度反馈按用户独立计算：同一文件在 A 的界面上是 62%，在 B 上可以是 0% 或已完成，
互不覆盖。大文件全程流式读取响应体逐片落盘，不会整体读进内存。

相关配置见[配置说明](/guide/configuration)的「直连快传与房间」，
接口见下：

- `GET /room/{room_code}/files`：房间文件列表与缓存状态
- `POST /room/{room_code}/file/upload`：上传/登记房间文件
- `GET /room/{room_code}/file/{transfer_id}/link`：刷新签名下载链接
- `GET /room/{room_code}/file/{transfer_id}/download`：下载
- `DELETE /room/{room_code}/file/{transfer_id}`：删除缓存（先查引用，共享物理文件不会误删）

## 后台管理

管理面板「直连快传房间」可查看房间在线人数、消息数、缓存数、过期状态，
支持强制删除房间与断开在线连接。删除、改房间设置都写审计。

## 部署注意事项

**必须让反向代理转发 WebSocket**（`Upgrade` / `Connection` 头），否则房间页显示
「已断开」而普通页面一切正常。Nginx 配置见[快速开始](/guide/getting-started)。

CDN / 边缘加速默认不转发 `Upgrade` 时，不要换 CDN，改用后台
**系统设置 → WebSocket 独立接入地址**（`wsBaseUrl`）指向直连源站域名即可，
HTTP 仍走 CDN。

### 多实例

文本信令跨实例同步（增量事件 + 每 5 秒全量成员快照自愈）。
**二进制分片刻意不跨实例转发**：单帧最大 256KB，走 pub/sub 会放大约 1.4 倍且延迟不可控。

负载均衡需按房间码做**会话亲和**，否则发送方会收到 `relay_failed`
（不会静默卡住）。纯 P2P 不受影响。完整前置条件见[快速开始](/guide/getting-started)。

## 下一步

- [收件箱（文件收集）](/guide/collection) - 三码机制与投递流程
- [API 参考](/api/) - 聊天与直连接口详情
- [配置说明](/guide/configuration) - 房间缓存与中转参数
