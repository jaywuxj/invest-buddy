---
name: deep-diligence
model: flash
quality: deep
max_tokens: 4000
trigger: api
---
个股深度研究：对单只标的做「业务-财务-估值-风险」四维分析，输出含人审位的结构化结论。
随时经 POST /api/analyze/<code> 触发，30-90s 内产出。
