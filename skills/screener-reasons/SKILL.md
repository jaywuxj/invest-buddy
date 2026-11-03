---
name: screener-reasons
model: flash
quality: standard
max_tokens: 6000
reconcile_text: true
trigger: screener-weekly（每周六 10:00）/ 选股页「立即更新」
---
选股推荐理由：程序已经算完因子分、估值三把尺子、机构评级与红旗，模型只负责把这些事实写成
普通投资者看得懂的推荐理由、主要风险和证伪条件。一次最多 7 只（超长会截断）。
数字只能来自注入数据（文本数字对账开启）；业务描述可用公开常识但不写数字；必须写反方风险，不下买卖指令。
