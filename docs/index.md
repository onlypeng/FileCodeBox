---
layout: home

hero:
  name: "FileCodeBox"
  text: "驿码"
  tagline: 匿名口令分享文本与文件，无需注册，输入提取码即可分享与取件
  image:
    src: /logo_small.png
    alt: FileCodeBox
  actions:
    - theme: brand
      text: 快速开始
      link: /guide/getting-started
    - theme: alt
      text: API 参考
      link: /api/
    - theme: alt
      text: 在 GitHub 上查看
      link: https://github.com/onlypeng/FileCodeBox

features:
  - icon: 🚀
    title: 单端口部署
    details: 后端同时提供 API 与前端静态产物，Docker 一条命令即可运行，SQLite 开箱即用、零外部依赖
  - icon: 🔒
    title: 取件码 + 二次口令
    details: 提取码之外可再加一条独立信道传递的口令；口令只存哈希，猜错不扣取件次数
  - icon: 📬
    title: 收件箱
    details: 投件码与取件码分离，收集作业/材料时提交者只能上传、看不到别人的文件
  - icon: ⚡
    title: 直连快传
    details: WebRTC P2P 优先，服务器中转兜底且不落盘；房间缓存让离线的一方上线后仍能取到
  - icon: 📦
    title: 可插拔存储
    details: 本地 / S3 兼容 / OneDrive / WebDAV / OpenDAL；服务端权威哈希秒传，同内容只存一份
  - icon: 🛠️
    title: 完整管理面
    details: 仪表盘、在线配置、审计日志、TURN 自检；PostgreSQL/MySQL + Redis 可水平扩展
---

> 本项目基于 [vastsa/FileCodeBox](https://github.com/vastsa/FileCodeBox) 继续开发。
> 文档以当前仓库实现为准；细节见 [README](https://github.com/onlypeng/FileCodeBox)、
> [API 参考](/api/)、[参与贡献](/contributing) 与 [安全政策](https://github.com/onlypeng/FileCodeBox/blob/master/SECURITY.md)。
