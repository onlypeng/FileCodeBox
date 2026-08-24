# 聊天与直连传输

FileCodeBox 提供三类实时通信功能，均通过 6 位房间码加入，无需注册：

| 功能 | 用途 | 特点 |
|------|------|------|
| **跨设备聊天** | 实时文本消息 | 支持历史消息同步与在线成员展示 |
| **直连传输** | 点对点文件传输 | WebRTC 直连优先，服务器中转兜底（不落盘） |
| **直连快传** | 消息 + 文件一体 | 多人房间，发送文件前需对方确认 |

## 跨设备聊天

### 创建房间

首页进入「直连」→ 创建聊天房间，设置标题与有效期，获得 6 位房间码。

### 加入聊天

将房间码发给他人，输入房间码即可加入。支持：

- 实时收发文本消息（单条上限 64KB）
- 历史消息同步（新加入/断线重连自动拉取最近 100 条）
- 在线成员展示、昵称自定义
- 打字状态提示

### API

- `POST /chat/create/`：创建房间
- `GET /chat/info/{room_code}`：房间信息
- `GET /chat/history/{room_code}`：历史消息
- `WS /ws/chat/{room_code}`：实时聊天

## 直连传输

面向大文件的点对点传输，数据不经过服务器持久化存储。

### 工作原理

```
发起方 ──WebRTC 信令──▶ 服务器(WebSocket) ──信令──▶ 接收方
   │                                                      │
   └─────────────── P2P 直连（DataChannel） ◀─────────────┘
              （无法直连时自动回退服务器中转，不落盘）
```

- 首个连接者为主机（WebRTC 发起方），其余为访客（应答方）
- 优先通过 WebRTC 建立 P2P 直连
- 对称 NAT 等无法直连的场景下，自动回退到服务器中转（单帧 ≤ 4MB，不落盘）
- 服务器中转受后台 `directRelayEnabled`（开关）与 `directRelaySpeedLimit`（限速）控制

### API

- `POST /transfer/create/`：创建房间
- `GET /transfer/info/{room_code}`：房间信息
- `WS /ws/transfer/{room_code}`：WebRTC 信令转发 + 二进制分片中继

## 直连快传

消息 + 文件一体的多人房间，适合小团队/设备间协作。

### 文件发送流程

1. 发送方发起 `file_offer`（文件名、大小），广播给房间内其他成员
2. 接收方选择接受或拒绝（`file_response`）
3. 对方接受后，发送方通过 P2P 直连或服务器中转传输文件数据
4. 文件数据**定向**转发给接受方，不会广播给其他人

### 技术说明

- 支持 WebRTC 信令（`rtc_offer` / `rtc_answer` / `rtc_ice`）建立 P2P 通道
- 服务器中转不落盘，单帧二进制上限 4MB
- 可通过后台 `directTurnServers` 配置 TURN 服务器，提升对称 NAT 下的直连成功率
- 消息发送有频率限制（每 5 秒最多 10 条），防止刷屏

### API

- `POST /direct/create/`：创建房间
- `GET /direct/info/{room_code}`：房间信息
- `GET /direct/history/{room_code}`：最近聊天消息
- `WS /ws/direct/{room_code}`：多人聊天 + 文件确认与定向中继

## 后台管理

管理员可在管理面板「直连快传房间管理」中查看房间（在线人数、消息数、过期状态），并可强制删除房间、断开在线连接。相关配置见 [配置说明](/guide/configuration) 中的「实时通信设置」。

## 部署注意事项

直连传输依赖 WebSocket，反向代理需开启 WebSocket 支持（`Upgrade` / `Connection` 头），否则信令无法转发。参考 [快速开始](/guide/getting-started) 中的 Nginx 配置。

## 下一步

- [收件箱（文件收集）](/guide/collection) - 文件收集与三码管理
- [API 文档](/api/) - 聊天与直连接口详情
- [配置说明](/guide/configuration) - 实时通信相关配置项
