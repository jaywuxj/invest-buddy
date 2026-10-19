---
name: panel-events
model: flash
quality: standard
max_tokens: 6000
reconcile_text: true
trigger: panel-build（每日）
---
公司事件流：把东财个股新闻归类为结构化公司事件（面板「事件」Tab 时间线）。
只保留公司层面的实质事件（业绩、回购、股权变动、经营/产品、监管、资本运作、管理层），丢弃行情播报与泛市场新闻。
事件的 code/name/date 由程序按新闻 id 回填，模型只负责分类与改写，杜绝张冠李戴。
