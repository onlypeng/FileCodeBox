# 介绍

<div align="center">

<img src="https://fastly.jsdelivr.net/gh/vastsa/FileCodeBox@V1.6/static/banners/img_1.png" alt="FileCodeBox Logo">

<p><em>匿名口令分享文本和文件，像拿快递一样取文件</em></p>

[![GitHub stars](https://img.shields.io/github/stars/vastsa/FileCodeBox)](https://github.com/vastsa/FileCodeBox/stargazers)
[![GitHub forks](https://img.shields.io/github/forks/vastsa/FileCodeBox)](https://github.com/vastsa/FileCodeBox/network)
[![GitHub issues](https://img.shields.io/github/issues/vastsa/FileCodeBox)](https://github.com/vastsa/FileCodeBox/issues)
[![GitHub license](https://img.shields.io/github/license/vastsa/FileCodeBox)](https://github.com/vastsa/FileCodeBox/blob/master/LICENSE)
[![QQ Group](https://img.shields.io/badge/QQ%20Group-739673698-blue.svg)](https://qm.qq.com/q/PemPzhdEIM)
[![Python Version](https://img.shields.io/badge/Python-3.8+-blue.svg)](https://www.python.org)
[![FastAPI](https://img.shields.io/badge/FastAPI-0.128+-green.svg)](https://fastapi.tiangolo.com)
[![Vue Version](https://img.shields.io/badge/Vue.js-3.x-brightgreen.svg)](https://v3.vuejs.org)
</div>

## 📝 项目简介

FileCodeBox 是一个基于 **FastAPI + Vue3** 开发的轻量级文件分享工具。用户无需注册，输入口令（提取码）即可匿名分享与获取文本和文件，就像从驿站取件一样简单。

除了基础的文本/文件分享，当前版本还内置了以下功能模块：

- **收件箱（文件收集）**：创建收件箱，他人凭投件码上传文件，收件人用管理码统一管理，取件人凭取件码查看下载。
- **跨设备聊天**：通过 6 位房间码实时收发文本消息。
- **直连传输**：WebRTC P2P 直传大文件，服务器中转兜底且不落盘。
- **直连快传**：消息 + 文件一体的多人房间。

## 🎯 应用场景

<table>
<tr>
<td align="center">
<h4>📁 临时文件分享</h4>
快速分享单个文件，无需注册登录
</td>
<td align="center">
<h4>📝 文本快速分享</h4>
分享代码片段、文本内容等
</td>
<td align="center">
<h4>📬 文件收集</h4>
创建收件箱收集他人投递的文件
</td>
</tr>
<tr>
<td align="center">
<h4>💬 跨设备聊天</h4>
房间码加入，实时收发消息
</td>
<td align="center">
<h4>⚡ 点对点传输</h4>
WebRTC 直传，服务器中转兜底不落盘
</td>
<td align="center">
<h4>🌐 小型分享服务</h4>
搭建私有的文件分享服务
</td>
</tr>
</table>

## ✨ 核心特性

<table>
<tr>
<td align="center">
<h4>🚀 轻量简洁</h4>
基于 FastAPI + SQLite3 + Vue3，部署简单，性能出色
</td>
<td align="center">
<h4>📤 便捷上传</h4>
支持复制粘贴、拖拽、多文件批量上传
</td>
<td align="center">
<h4>📦 多种类型</h4>
支持文本、单文件、多文件分享
</td>
</tr>
<tr>
<td align="center">
<h4>🔒 安全机制</h4>

- IP 限制上传/查询次数
- 提取码错误次数限制
- 文件自动过期清理
- 管理员口令哈希存储
- 敏感配置加密存储
</td>
<td align="center">
<h4>🎫 提取码分享</h4>
随机提取码，可自定义次数及有效期
</td>
<td align="center">
<h4>🔁 大文件上传</h4>
分片上传支持断点续传；S3 预签名直传
</td>
</tr>
<tr>
<td align="center">
<h4>🎭 匿名分享</h4>
无需注册登录，保护隐私
</td>
<td align="center">
<h4>🛠 管理面板</h4>
仪表盘、文件/收件箱/直连房间管理、系统配置
</td>
<td align="center">
<h4>🐳 容器部署</h4>
支持 Docker 一键部署
</td>
</tr>
<tr>
<td align="center">
<h4>💾 存储扩展</h4>
本地 / S3 / OneDrive / OpenDAL / WebDAV
</td>
<td align="center">
<h4>📱 响应式设计</h4>
支持移动端访问与深色模式
</td>
<td align="center">
<h4>🌍 国际化</h4>
支持简体中文与英文
</td>
</tr>
</table>

## 🚀 快速开始

### Docker 部署

```bash
docker run -d --restart=always -p 12345:12345 -v /opt/FileCodeBox/:/app/data --name filecodebox onlypeng/filecodebox:latest
```

### 手动部署

```bash
git clone https://github.com/vastsa/FileCodeBox.git
cd FileCodeBox
pip install -r requirements.txt
python main.py
```

详细部署方式见 [快速开始](/guide/getting-started)。

## 📖 使用说明

### 分享文件 / 文本
1. 打开网页，选择「发件」
2. 选择或拖拽文件 / 粘贴文本
3. 设置过期时间和次数
4. 获取提取码

### 获取文件
1. 打开网页，在首页「查码」输入提取码
2. 点击查询
3. 下载文件或查看文本

### 收件箱（文件收集）
1. 创建收件箱，获得管理码、投件码、取件码
2. 将投件码发给投递人上传文件
3. 用管理码管理文件，取件码供他人只读取件

### 管理面板
1. 访问 `/#/admin`
2. 输入管理员口令登录
3. 管理文件、收件箱、直连房间和系统配置

## 🔗 下一步

- [快速开始](/guide/getting-started) - 部署与基本使用
- [文件上传](/guide/upload) - 普通/分片/预签名上传
- [收件箱](/guide/collection) - 文件收集与三码管理
- [聊天与直连](/guide/realtime) - 跨设备聊天与直连传输
- [API 文档](/api/) - 接口参考
