# 更新日志

本文件记录**当前仓库**的实质性变更。日期为提交日期，按时间倒序。
本项目由两个独立仓库组成（后端 `FileCodeBox`、前端 `FileCodeBoxFronted`），
功能通常需要两侧配合，故合并记录。

---

## 未发布（工作区变更）

### docs

- 全量重写 `readme.md` 与 `readme_en.md`，使中英文覆盖同一组功能。
  修复两处与实现脱节的硬伤：文档里不存在的 `/share/text/` 接口（纯文本分享
  实际走 `/share/file/` 只带 `remark`）、以及「`WORKERS` 建议设为 CPU 核数」
  与同文件多实例章节自相矛盾的建议。
- 重写 `SECURITY.md`（此前是 GitHub 模板占位文本），补充威胁模型、
  安全相关的设计取舍与公网部署加固清单。
- 新建 `docs/contributing.md` 与 `docs/en/contributing.md`，确立注解与文档规范：
  **注解写「为什么」，不写「做了什么」**；改接口/配置必须同提交同步文档。
- 补齐本文件与英文版 changelog（此前均为 0 字节空文件）。

### refactor(comments)

- 后端：为 `core/`、`apps/` 下全部生产模块补写模块头说明与关键函数注解，
  清理 `main.py` 与包初始化文件里 2023 年遗留的 IDE 作者头，
  给 31 个历史迁移补写说明（含「函数名必须是 `migrate()`」这一静默踩坑点）。
- 前端：为 `src/` 下全部 136 个 ts/vue 文件补写模块说明；
  语言包与 `vite-env.d.ts` 为生成/纯数据文件，按规范不加说明。

> 以上均为纯注释/文档改动，不含逻辑变更。

---

## 2026-10-29

### feat(cluster) 多实例部署

- 新增 `core/cluster.py` 作为跨进程协调的唯一收口点：事件广播、成员快照、分布式锁。
- 默认 `LocalHub` 零依赖、行为与改造前等价；配置 `REDIS_URL` / `redisUrl` 后切 `RedisHub`。
- 房间成员状态跨实例同步（增量事件 + 5 秒全量快照自愈），
  房间配额锁改为带续期的分布式锁。
- 二进制分片中继**刻意不跨实例**：改为接入层会话亲和，未亲和时下发 `relay_failed`。
- 能力报告改为「数据库可并发写 **且** 集群已启用」双条件；
  `WORKERS>1` 但条件不齐时启动打 ERROR。
- 前端：收到 `relay_failed` 时标记传输失败并提示改用 P2P 或重新进房，不再静默卡住。

## 2026-10-28

### feat(share) 提取口令（二次口令）

- `FileCodes.access_code_hash`（PBKDF2）+ 迁移 033。
- 创建侧覆盖整包上传（单/多文件/仅备注）、分片完成、直传确认（代理 + 直传）四条路径。
- 取件侧 `/share/select`、`/share/select/info` 校验口令；
  `/share/download/item`、`/share/zip` 对受保护分享强制校验取件阶段下发的短期 token。
- **口令错误不扣取件次数**（避免猜错即成拒绝服务），只计失败限流 + 审计。
- 前端：发件页可选口令输入；取件遇 403 弹窗补输后重试。

### feat(security) 可插拔病毒扫描钩子

- `core/scan.py`：命令 / HTTP 两种形态，参数列表执行、不经 shell。
- 失败按 `virusScanFailOpen` 决定放行或拒绝（默认放行，扫描器挂掉不该拖垮上传）。
- 接入单文件、多文件、分片三条上传路径。

### fix(admin)

- 补齐后台配置白名单缺失的 11 项（审计保留天数、扫描配置、房间缓存、全局配额、主题等）。
- 数据库与 Redis 配置**故意不进白名单**：基础设施配置改了不生效，在线编辑只会误导。

## 2026-10-27

### feat(share) 我的分享与撤销

- `FileCodes.owner_token`（迁移 032）：发起者凭证，用于撤销与查看自己的分享。
- `POST /share/revoke`：403/404 语义区分、删除前检查物理文件引用（秒传共享文件）。
- 前端发件记录支持提前作废；后台审计动作补 `revoke` 展示文案。

### feat(share) 端到端完整性校验

- 服务端落盘时流式计算权威 `file_hash`，不采信客户端声明值（防缓存投毒）。
- 前端下载完成后用 Web Worker 计算 SHA-256 并提示校验结果。

### perf

- 房间配额统计改用 Tortoise `annotate(Sum)` 聚合（跨库安全，带逐行兜底）。
- 两处同步磁盘 I/O 移出事件循环（`asyncio.to_thread`）。
- SQLite WAL 定期 `wal_checkpoint(TRUNCATE)`，并补充备份说明。

### build

- 前端新增 `scripts/clean-dist.mjs`：构建前清理 dist，根治 `emptyOutDir:false`
  造成的构建产物累积（失败时 rename 隔离，不因清理失败中断构建）。

## 2026-10-26

### feat(audit) 审计日志

- `audit_log` 表（迁移 031）与管理后台页面；登录、配置变更、删除、撤销等操作留痕。
- `core/audit.py` 的 `record()` 吞异常：审计失败绝不影响主流程。

### feat(audit) 前端

- 后台审计日志页面；繁体中文语言包。

## 2026-10-25

### feat(core) 数据库类型可配置

- `dbType` 支持 `sqlite` / `postgres` / `mysql`，并做部署能力自适配。

## 2026-10-24

- 默认管理员口令强制修改（首次登录拦截）。
- 全局存储容量统计与配额告警（默认开启）。
- 文件柜秒传：同内容只存一份物理文件。
- TURN 服务器连通性自检接口。
- 大文件哈希计算移入 Web Worker；P2P 失败降级提示。

## 2026-10-23

- 房间成员令牌：写接口要求「当前在线 **或** 持有成员令牌」，解除必须在线的约束。
- 房间消息持久化、文件服务器缓存、多源收取与端到端完整性校验。

## 2026-08-27

- 后端直接 serve 前端构建产物 `frontend_dist`，实现单端口部署。
- GitHub Actions：从仓库内 `frontend_dist` 构建镜像并推送 Docker Hub。
- 文档站点（VitePress）与 Pages 部署。

---

## 说明

- 本项目**没有独立版本号发布线**，以 `master` / `main` 分支当前提交为准。
- 升级前请备份 `data/` 目录（或数据库）以及 `JWT_SECRET` / `ENCRYPTION_KEY`：
  丢失加密密钥会导致已存储的 S3/WebDAV 凭据无法解密。
- 迁移在每次启动时自动执行，且必须幂等；新增迁移的函数名必须是 `migrate()`。
