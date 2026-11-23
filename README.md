# pairwise-skill-cooldown-tick

## 领域

游戏、图形与多媒体

## 主题

技能冷却与持续效果计时

## 说明

帧率相关冷却、暂停时钟与结束事件提交。

初始状态下 `gameplay/skill_cooldown_tick.py` 中所有函数抛出 `NotImplementedError`，测试全部失败。
修复实现后运行：

```bash
python -m pytest -q
```

期望全部通过。
