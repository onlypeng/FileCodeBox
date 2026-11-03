import { defineConfig } from 'vitepress'

// https://vitepress.dev/reference/site-config
export default defineConfig({
  title: 'FileCodeBox',
  description: '简单高效的文件分享工具',
  lang: 'zh-CN',
  lastUpdated: true,
  locales: {
    root: {
      label: '简体中文',
      lang: 'zh-CN',
      title: 'FileCodeBox',
      description: '匿名口令分享文本，文件',
      themeConfig: {
        logo: '/logo_small.png',
        nav: [
          { text: '首页', link: '/' },
          { text: '指南', link: '/guide/getting-started' },
          { text: 'API', link: '/api/' },
          { text: '优秀案例', link: '/showcase' },
          { text: 'Demo', link: 'https://share.lanol.cn' },
          {
            text: '了解更多',
            items: [
              { text: '更新日志', link: '/changelog' },
              { text: '贡献指南', link: '/contributing' },
            ],
          },
        ],

        sidebar: {
          '/guide/': [
            {
              text: '介绍',
              items: [
                {
                  text: '什么是 FileCodeBox',
                  link: '/guide/introduction',
                },
                { text: '快速开始', link: '/guide/getting-started' },
              ],
            },
            {
              text: '基础功能',
              items: [
                { text: '文件上传', link: '/guide/upload' },
                { text: '文件分享', link: '/guide/share' },
                { text: '文件管理', link: '/guide/management' },
                { text: '收件箱（文件收集）', link: '/guide/collection' },
                { text: '聊天与直连传输', link: '/guide/realtime' },
              ],
            },
            {
              text: '高级特性',
              items: [
                { text: '存储配置', link: '/guide/storage' },
                { text: '安全设置', link: '/guide/security' },
                { text: '系统配置', link: '/guide/configuration' },
              ],
            },
          ],
          '/api/': [
            {
              text: 'API 参考',
              items: [
                { text: '约定与鉴权', link: '/api/#约定' },
                { text: '分享与取件', link: '/api/#分享与取件-share' },
                { text: '分片与预签名上传', link: '/api/#分片上传-chunk' },
                { text: '收件箱与投递', link: '/api/#收件箱-collection-与投递-delivery' },
                { text: '实时房间', link: '/api/#实时房间-room-与-ws' },
                { text: '管理后台', link: '/api/#管理后台-admin' },
              ],
            },
          ],
          '/showcase': [
            {
              text: '优秀案例',
              items: [
                { text: '案例展示', link: '/showcase' },
              ],
            },
          ],
        },
        socialLinks: [
          { icon: 'github', link: 'https://github.com/onlypeng/FileCodeBox' },
        ],
        footer: {
          message: '基于 LGPL-3.0 license 发布',
          copyright: 'Copyright © 2022-present FileCodeBox',
        },
        search: {
          provider: 'local',
        },
        outline: {
          level: [2, 3],
          label: '目录',
        },
      },
    },
    en: {
      label: 'English',
      lang: 'en-US',
      title: 'FileCodeBox',
      description: 'Simple and efficient file sharing tool',
      themeConfig: {
        logo: '/logo_small.png',
        nav: [
          { text: 'Home', link: '/en/' },
          { text: 'Guide', link: '/en/guide/getting-started' },
          { text: 'API', link: '/en/api/' },
          { text: 'Showcase', link: '/en/showcase' },
          { text: 'Demo', link: 'https://share.lanol.cn' },
          {
            text: 'More',
            items: [
              { text: 'Changelog', link: '/en/changelog' },
              { text: 'Contributing', link: '/en/contributing' },
            ],
          },
        ],

        sidebar: {
          '/en/guide/': [
            {
              text: 'Introduction',
              items: [
                {
                  text: 'What is FileCodeBox',
                  link: '/en/guide/introduction',
                },
                {
                  text: 'Getting Started',
                  link: '/en/guide/getting-started',
                },
              ],
            },
            {
              text: 'Basic Features',
              items: [
                { text: 'File Upload', link: '/en/guide/upload' },
                { text: 'File Sharing', link: '/en/guide/share' },
                { text: 'File Management', link: '/en/guide/management' },
                { text: 'Inbox (File Collection)', link: '/en/guide/collection' },
                { text: 'Chat & Direct Transfer', link: '/en/guide/realtime' },
              ],
            },
            {
              text: 'Advanced Features',
              items: [
                { text: 'Storage Configuration', link: '/en/guide/storage' },
                { text: 'Security Settings', link: '/en/guide/security' },
                {
                  text: 'System Configuration',
                  link: '/en/guide/configuration',
                },
              ],
            },
          ],
          '/en/api/': [
            {
              text: 'API Reference',
              items: [
                { text: 'Conventions & Auth', link: '/en/api/#conventions' },
                { text: 'Sharing', link: '/en/api/#sharing-share' },
                { text: 'Chunked & Presigned', link: '/en/api/#chunked-upload-chunk' },
                { text: 'Inbox & Delivery', link: '/en/api/#inbox-collection-and-delivery-delivery' },
                { text: 'Realtime Rooms', link: '/en/api/#realtime-rooms-room-and-ws' },
                { text: 'Admin', link: '/en/api/#admin-admin' },
              ],
            },
          ],
          '/en/showcase': [
            {
              text: 'Showcase',
              items: [
                { text: 'Case Studies', link: '/en/showcase' },
              ],
            },
          ],
        },
        socialLinks: [
          { icon: 'github', link: 'https://github.com/onlypeng/FileCodeBox' },
        ],
        footer: {
          message: 'Released under the LGPL-3.0 license',
          copyright: 'Copyright © 2022-present FileCodeBox',
        },
        search: {
          provider: 'local',
        },
        outline: {
          level: [2, 3],
          label: '目录',
        },
      },
    },
  },

  themeConfig: {
    // 语言切换器
    langMenuLabel: '切换语言',

    // 社交链接
    socialLinks: [
      { icon: 'github', link: 'https://github.com/onlypeng/FileCodeBox' },
    ],

    // 页脚
    footer: {
      message: '基于 LGPL-3.0 license 发布',
      copyright: 'Copyright © 2022-present FileCodeBox',
    },

    // 搜索
    search: {
      provider: 'local',
      options: {
        locales: {
          zh: {
            translations: {
              button: {
                buttonText: '搜索文档',
                buttonAriaLabel: '搜索文档',
              },
              modal: {
                noResultsText: '无法找到相关结果',
                resetButtonTitle: '清除查询条件',
                footer: {
                  selectText: '选择',
                  navigateText: '切换',
                },
              },
            },
          },
          en: {
            translations: {
              button: {
                buttonText: 'Search',
                buttonAriaLabel: 'Search docs',
              },
              modal: {
                noResultsText: 'No results found',
                resetButtonTitle: 'Clear query',
                footer: {
                  selectText: 'to select',
                  navigateText: 'to navigate',
                },
              },
            },
          },
        },
      },
    },

    outline: {
      level: [2, 3],
      label: '目录',
    },
  },
})
