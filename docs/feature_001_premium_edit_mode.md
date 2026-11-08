# feature_001：成品索引批量 / 精品双模式

## 背景

成品索引已有「扫描 → 文案与四段分镜 → 报价 → Seedance/配音 → final.mp4」批量链路。  
精品模式在同一条素材上增加人工账本 + 分镜闸口，通过现有 `guard_plan` 后写入同一 `video_jobs`，下游报价与生成复用。

## 做什么

- `task_mode=batch|premium`（默认 batch）
- 精品：`premium_status=editing|ready`，草稿 `premium_script`，版本 `premium_revision`，本地参考图 `premium_asset`（png/jpg，不进视频网关）
- 接口：`/premium/start|save|submit`，以及模式切换 / 版本列表恢复
- 配置：`premium_enable`、`premium_max_inflight`、`premium_dub_sync_enable`、`premium_ending_enable`
- UI：成品索引卡片单选 + 内嵌编排区（覆盖进度、规则面板、草稿、版本）

## 明确不做什么

- 不改任务队列 / 评论 / 风控 / 通知 / 发布
- 不新接豆包；不把 jpg 传给 Seedance
- 不按账号授权；段数固定 4；不同步口播 / 黑底片尾真实渲染（只降级）

## 验收

```bash
git branch --show-current   # feature/feat-001-premium-edit-mode
pytest tests/test_premium_mode.py -q          # 6 passed
pytest tests/test_premium_guardrails.py -q    # 6 passed
pytest tests/test_content_video_pipeline.py tests/test_content_pipeline.py -q
```

- `premium_enable=false` → premium 接口 HTTP 404
- submit 前不进 quote；满额精品 generate → 429；批量不受影响

## 回滚

配置 `premium_enable: false`；删功能分支回到 `80a7ad7`。新列默认 batch，不删旧表。
