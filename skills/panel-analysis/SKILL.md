---
name: panel-analysis
model: flash
quality: standard
max_tokens: 8000
reconcile_text: true
trigger: panel-build（每日）
---
组合信号与市场速览：基于程序注入的行情/持仓盈亏/基本面/机构评级/近期财报/新闻标题，
为每只标的给出操作信号（action_level: bullish/normal/neutral/watch-close），写组合级市场速览，
并为每条新闻判定情绪（利好/利空/中性）。框架对齐 STRATEGY.md 价值投资纪律。
