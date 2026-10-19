---
name: news-sentinel
model: flash
quality: standard
max_tokens: 3000
trigger: cron(工作日 21:00)
---
重大事件哨兵：扫描全部标的近期新闻，筛出「可能改变投资结论」的重大事件（财报、监管、管理层变动、大额回购/减持）。
