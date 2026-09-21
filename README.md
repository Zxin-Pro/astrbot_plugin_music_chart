# astrbot_plugin_music_chart

Billboard 音乐榜单查询与每日定时推送（Hot 100 等）。

## 指令

| 指令 | 说明 |
|---|---|
| /music | 当前 Billboard Hot 100 Top 10 |
| /music <slug> | 指定榜单，如 /music billboard-200 |
| /music date 2026-09-12 | 指定日期榜单（仅 hot-100） |
| /music refresh | 强制刷新（绕过缓存） |
| /music help | 帮助 |
| /music debug | 诊断（连通性/解析/缓存/渲染/调度） |

## 数据源

- 主方案：[mhollingshead/billboard-hot-100](https://github.com/mhollingshead/billboard-hot-100) JSON（免解析 HTML，每日自动更新，仅 hot-100）
- 备选：billboard-charts 库（其他榜单；`pip install billboard-charts`）

## 出图

烛之音乐 t2i 模板（MiSans 深色卡片风）→ 本地 Pillow 深色榜单图 → Markdown 文本，三级降级。

## 定时推送

管理面板配置 push_time（默认 10:00）与 push_target（UMO 或群号，逗号分隔），每日到点自动推送。
