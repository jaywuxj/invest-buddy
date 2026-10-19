---
name: panel-event-brief
model: flash
quality: standard
max_tokens: 4000
trigger: panel-build（每日，事件归类后执行）
---
交易所公告的一句话白话解读：
- 输入：公告列表（id / code / company / date / category / title）。公告只有程式化标题（如「根据全球中期票据计划建议发行票据及财务资料摘要」），没有内容摘要。
- 输出：每条 1-2 句白话解读（≤80 字），说明「这是什么类型的动作 + 标题中的具体事实 + 对股东通常意味着什么」。
- 程序护栏（panel_build.py `_stage_events`）：解读中的数字必须是标题中已有的（子集校验）、≤80 字、不得以承接词开头；不合格丢弃该条解读（公告保留原标题）。
- 缓存：data/panel/event_briefs.json 按公告链接持久缓存，历史公告不重复消耗模型。
