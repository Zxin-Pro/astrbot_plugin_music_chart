# astrbot_plugin_music_chart

Billboard 音乐榜单查询与每日定时推送（Hot 100 等）。

## 指令

| 指令 | 说明 |
|---|---|
| /音乐 | 当前默认榜单 Top 10 |
| /音乐 <榜单名> | 指定榜单，如 /音乐 华语 |
| /音乐 日期 2026-09-12 | 指定日期榜单（仅公告牌百首单曲榜） |
| /音乐 刷新 | 强制刷新（绕过缓存） |
| /音乐 帮助 | 帮助 |
| /音乐 诊断 | 诊断（连通性/解析/缓存/渲染/调度） |

## 数据源

- 主方案：[mhollingshead/billboard-hot-100](https://github.com/mhollingshead/billboard-hot-100) JSON（免解析 HTML，每日自动更新，仅 hot-100）
- 备选：billboard-charts 库（其他榜单；`pip install billboard-charts`）

## 出图

烛之音乐 t2i 模板（MiSans 深色卡片风）→ 本地 Pillow 深色榜单图 → Markdown 文本，三级降级。

## 定时推送

管理面板配置 push_time（默认 10:00）与 push_target（UMO 或群号，逗号分隔），每日到点自动推送。

## 华语中文歌系列

| 指令 | 榜单 | 数据源 |
|---|---|---|
| /音乐 公告牌 | 公告牌百首单曲榜 | JSON |
| /音乐 华语 | 台湾歌曲榜（国语） | billboard-charts |
| /音乐 粤语 | 香港歌曲榜（粤语） | billboard-charts |
| /音乐 内地 | 华语内地热歌榜 | 网易云音乐 |
| /音乐 飙升 | 华语飙升榜 | 网易云音乐 |
| /音乐 新歌 | 华语新歌榜 | 网易云音乐 |

> Billboard 无中国内地榜（China V Chart 已停更），内地系列走网易云音乐官方榜单接口。
