# 收件箱（文件收集）

收件箱是 FileCodeBox 提供的**文件收集**功能：创建者建立一个收件箱，他人凭**投件码**上传文件，收件人用**管理码**统一管理，取件人凭**取件码**查看下载。三个口令相互独立，权限分离。

## 三码说明

创建收件箱后，系统会生成三个独立的 6 位口令：

| 码 | 字段名 | 用途 | 权限 |
|------|--------|------|------|
| **管理码** | `collection_code` | 收件人管理收件箱 | 查看、下载、打包、删除文件，修改配置 |
| **投件码** | `delivery_code` | 投件人上传文件 | 仅上传；可设置投递次数上限 |
| **取件码** | `retrieve_code` | 取件人查看 | 只读查看与下载，不可删除 |

::: tip 设计理念
三码分离避免"能投递的人也能删除"等越权问题。管理码权限最高，请仅分发给收件人本人。
:::

## 创建收件箱

### 网页端

1. 首页点击「创建收件箱」
2. 填写标题、描述（可选）
3. 设置最大文件数（默认 20，上限由后台 `maxCollectionFiles` 控制）
4. 分别设置收件箱、投件码、取件码的有效期
5. 可选设置访问密码与投递次数上限
6. 创建后获得三个口令，管理页面实时展示投递进度

### API

**POST** `/collection/create/`

```json
{
  "title": "项目文件收集",
  "description": "请上传项目相关文件",
  "password": "",
  "max_files": 20,
  "expire_style": "day",
  "expire_value": 7,
  "delivery_expire_style": "day",
  "delivery_expire_value": 7,
  "retrieve_expire_style": "day",
  "retrieve_expire_value": 7,
  "delivery_count": -1
}
```

- `delivery_count`：最大投递次数，`-1` 表示不限
- 投件码/取件码的过期时间不能超过收件箱过期时间
- 收件箱过期时间创建后不可更改（投件码/取件码有效期可随时延长或修改）

响应返回三个口令及各项过期时间，详见 [API 文档](/api/)。

## 投递文件

### 网页端

1. 打开投递页（输入投件码）
2. 填写署名（可选）
3. 选择要上传的文件
4. 点击投递，上传进度会实时推送到收件箱管理页面

### API

**GET** `/delivery/{delivery_code}`：获取投递页信息（标题、容量、已投递数，不暴露文件列表）。

**POST** `/delivery/upload/`（multipart 表单）：

| 参数名 | 类型 | 必填 | 描述 |
|-------|------|------|------|
| `delivery_code` | string | 是 | 投件码 |
| `file` | file | 是 | 要投递的文件 |
| `uploader_name` | string | 否 | 投递者署名 |

投递的约束：

- 超过投递次数上限（`delivery_count`）将被拒绝
- 达到最大文件数（`max_files`）后收件箱视为已满
- 文件大小不得超过后台 `uploadSize` 限制

## 管理收件箱

### 网页端

输入**管理码**进入管理页面：

- 查看全部投递文件（文件名、大小、投递者、时间）
- 下载单个文件或打包 ZIP 下载全部
- 删除单个文件（或由管理员在后台删除整个收件箱）
- 修改投件码/取件码有效期、调整最大文件数
- 实时查看投递进度（WebSocket 推送）

### API

| 操作 | 接口 |
|------|------|
| 管理页面数据 | `GET /collection/manage/{collection_code}` |
| 投件码查看已投递文件 | `GET /collection/manage-by-delivery/{delivery_code}` |
| 更新配置 | `PATCH /collection/update/{collection_code}` |
| 下载单个文件 | `GET /collection/download/{file_id}?code=管理码或取件码` |
| 打包下载 | `GET /collection/zip/{code}` |
| 删除文件 | `DELETE /collection/delete/{file_id}?code=管理码` |

## 取件

取件人输入**取件码**即可只读查看并下载文件（打包下载同样支持），但**不能删除**文件。

**GET** `/collection/retrieve/{retrieve_code}`：返回文件列表（不暴露管理码与投件码）。

::: warning 注意
取件码过期是独立计算的。即使收件箱未过期，取件码过期后同样无法取件（可通过管理码更新延长）。
:::

## 实时推送（WebSocket）

**WS** `/ws/collection/{code}`：使用管理码或投件码连接。

连接后第一条消息发送 `{"type":"join","nickname":"昵称"}` 注册身份。服务端推送的事件类型：

| 事件 | 说明 |
|------|------|
| `user_joined` / `user_left` | 在线成员变化 |
| `file_uploading` / `file_progress` | 投递开始 / 进度更新 |
| `file_completed` | 投递完成 |
| `file_delivery_failed` | 投递失败 |
| `file_deleted` | 文件被删除 |

## 下一步

- [聊天与直连](/guide/realtime) - 跨设备聊天与直连传输
- [API 文档](/api/) - 收件箱与投递接口详情
- [配置说明](/guide/configuration) - 收件箱相关配置项
