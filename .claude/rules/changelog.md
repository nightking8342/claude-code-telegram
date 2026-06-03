---
paths:
  - CHANGELOG.md
  - CHANGELOG.zh-CN.md
---

# CHANGELOG 维护规范

修改 CHANGELOG.md、CHANGELOG.zh-CN.md 或 release notes 时遵循以下规则。

## 格式标准

基于 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.0.0/)，遵循 [语义化版本](https://semver.org/lang/zh-CN/)。

## 分类

条目放入以下分区之一，按此顺序排列：

| 分区 | 何时使用 |
|---|---|
| **Added** | 新的用户可见功能或命令 |
| **Changed** | 对现有功能的行为变更、性能改善、依赖升级 |
| **Deprecated** | 即将移除的功能 |
| **Removed** | 已移除的功能 |
| **Fixed** | Bug 修复 |
| **Security** | 安全相关的变更 |

## 条目格式

```
- **标题 / 标题**：中英文并列的描述
```

- 标题用加粗，中英文用 `/` 分隔
- 描述简洁，一句话说明"做了什么"和"为什么"
- 关联 issue/PR 时加 `(#123)`
- 不要把多个不相关改动合并在同一条里

## 版本管理

- 未发布的改动全部写在 `[Unreleased]` 下
- 发版时（`make bump-*`）将 `[Unreleased]` 内容移到具体版本号（如 `[1.9.0] - 2026-xx-xx`）
- 不要修改历史版本的条目内容
- 保持 `[Unreleased]` 在文件最上方（`# Changelog` 和格式声明之后）

## 什么该记

- 新命令、新工具、新的用户交互方式
- 行为变更（包括中间件顺序、配置默认值、安全规则的变更）
- SDK / 主要依赖版本升级
- 对上游 Claude Code 内置功能的桥接或适配（如 Plan mode、AskUserQuestion）
- 修复了影响用户体验或稳定性的 bug

## 什么不该记

- 内部重构、代码清理、变量重命名
- 单纯的测试或文档改动（除非是重大文档重组）
- CI 配置微调
- 依赖的小版本补丁升级
