---
name: panel-sentiment
model: flash
quality: standard
max_tokens: 8000
trigger: panel-build（每日，先于事件/信号阶段执行）
---
新闻相关性 + 投资者价值筛选 + 情绪判定（一次调用完成，宁缺毋滥）：
1. 相关性：标题点名了公司但主角是别人（如「某公司产品上架腾讯平台」、早报合集里顺带列举）→ 标记不相关，程序剔除。
2. 投资者价值：对持有人/潜在买家无信息增量的（营销软文、无数据泛稿、旧事回顾、八卦花絮）→ 剔除。
3. 情绪：**默认中性**。模型对每条输出 label + basis（硬信息类型 E/R/C/O/L/N/D，或 X=非硬信息）+ confidence；
   程序终判（harness/jobs/panel_build.py `_final_sentiment`）：仅当 basis ∈ 硬信息类且 confidence ≥ 0.8 才保留利好/利空，否则一律中性。
输入含标题 + 正文摘要（≤160 字）；按 7 只股票一批并行，避免长输出截断。
回归测试：tests/sentiment_eval.py（人工标注 tests/sentiment_gold.json）。
