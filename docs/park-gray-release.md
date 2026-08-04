# Park 灰度发布策略

## 灰度步骤

### Phase 1: oracle-lite 灰度（3 天）
- 范围：仅 oracle-lite agent 启用 park
- 指标门禁：
  - revive_success_rate > 80% → 扩大范围
  - cold_rate > 50% → 暂停、调查 TTL 设置
  - 无 hot_park 泄露（RSS/FD 正常）

### Phase 2: 全量 oracle 开放
- 增加 oracle + oracle-opus
- 验证多轮 review 场景（最多 5 轮）

### Phase 3: prometheus 短时 park
- 交互式 planner 启用 park-class=short-term
- TTL: 30min（比 oracle 更短）

### Phase 4: 条件 park（reviewer/hephaestus）
- 仅当任务含"修复后复审"时 park
- 由 skill 控制，非默认

## 回滚条件
- revive_success_rate < 60% 持续 2 天
- 内存泄露（RSS 持续增长）
- 并发槽位耗尽（OMP 无法启动新 agent）

## 观测指标
- hot_park_count（活跃 park 实例数）
- avg_park_duration（平均 park 时长）
- revive_success_rate（hot revive 成功率）
- warm_cold_degradation_rate（降级率）
- metrics.jsonl 存放路径：~/.local/share/codeagent/park/metrics.jsonl