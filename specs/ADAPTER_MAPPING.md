# 参考核心与产品接口映射

starter 是独立的内部 Python 参考实现；PUBLIC_API.json 是计划中的产品接口。二者不宣称可以直接逐字段调用。Muse 在产品迁移任务中实现明确的 adapter，并写合同测试。

| 参考核心 | 产品外部字段或行为 | 处理要求 |
| --- | --- | --- |
| source_refs 中的 id | sources 中的 ref | 明确转换；不得丢弃 type 与来源标记 |
| Store 的 actor 配置 | transport 绑定 principal | 本地标签不是认证；远程身份另建 |
| begin 返回 run_id 和 snapshot | Run 中的 id scope_epoch records | 服务端保存读取集合；不能由客户端替换 |
| run status succeeded | accepted | 映射状态名称，保留原提交时间 |
| receipt committed_at commit_epoch | accepted_at epoch | 明确是接受时状态，不表示现在仍可用 |
| CYCLIC_DEPENDENCY | CYCLE_DETECTED | 对外错误保持一个稳定名称 |
| INVALID_SOURCE KIND_MISMATCH 等内部校验 | INVALID_INPUT 加 details | 保留可诊断细节，敏感字段脱敏 |
| ACTOR_MISMATCH | FORBIDDEN | 不把内部标签检查宣称为完整认证 |
| expected_candidates 字典 | id revision 对象数组 | 拒绝重复 ID，转换后验证完整候选集合 |
| history 含完整 before after 正文 | event 元数据和受控 payload 引用 | 产品迁移必须处理隐私擦除与备份策略 |
| 同run finish幂等 | 通用 Idempotency-Key | 通用幂等仍需产品层实现，不能说已经全部具备 |

其他字段以实际 starter README 为准。未来接口出现破坏性变化时更新 schema_version 和迁移说明，而不是在客户端猜测。目标SQL只在新数据库里验证；不能直接拿它覆盖参考数据库。计划中的HTTP路由尚未实现，CLI原型不是HTTP服务。
