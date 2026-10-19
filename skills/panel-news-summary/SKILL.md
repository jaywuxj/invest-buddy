---
name: panel-news-summary
model: flash
quality: standard
max_tokens: 8000
trigger: panel-build（每日，情绪判定后执行）
---
新闻中文摘要生成（动态页正文一律是它）：
- 输入：保留的新闻列表（id / title / summary / lang）。
- 输出：每条 summary_cn 100-300 字（目标约 200 字）——提取「发生了什么 + 关键数字 + 对投资者的意义」；英文条目另输出 title_cn（≤40 字）。
- 程序护栏（panel_build.py `_stage_summary`）：数字必须是原文子集、长度 60-320 字，否则丢弃该条保留原文摘要。
- 缓存：data/panel/news_i18n.json 按新闻链接持久，历史新闻不重复消耗。
