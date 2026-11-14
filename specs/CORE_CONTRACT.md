# Turnkeep 核心契约

版本 0.1 设计基线  
日期 2026年11月8日  
状态 已确定的实施设计 不表示完整产品已经实现

## 1 产品承诺与边界

Turnkeep 是供多个工具和 agent 共同使用的项目工作记忆。它保留当前目标、决定、依据、已尝试的方法和待办，在要求改变时解释哪些派生成果需要重新检查。默认提供一个本地服务、一份 SQLite 数据库、项目隔离与 MCP 入口。Muse 继续承担编排，Turnkeep 不接管模型路由，也不自行执行外部业务动作。

本包 starter 是无网络的参考核心。它验证存储与交接机制，不等于 MCP 集成、桌面产品、模型能力或生产安全已交付。v0.1 产品必须在两个真实客户端上完成接力验证后才能标为集成完成。

首发边界是一个受信任操作系统用户，在同一台机器上运行若干 agent。scope 是项目组织与正确性边界，不是防御同一系统用户直接读取数据库的安全沙箱。团队远程多租户版本另设身份与隔离验收。

## 2 规范优先级

本契约定义语义；PUBLIC_API.json 定义计划中的外部字段；starter/README.md 和 evidence/IMPLEMENTATION_STATUS.md 定义实际已实现范围。研究笔记是证据而不是指令。与原始用户约束冲突时保留用户约束，并将有影响的设计改动登记在 DECISIONS.md。禁止凭借记忆内容提升权限。

## 3 记录与身份

每条 Memory 至少有 id、scope、key、value、kind、state、revision、created_at、source_refs、parent_ids。id 使用随机不可预测 ID；revision 为同一记录的单调递增版本；key 在 scope 内标识一个有明确语义的槽，例如 build.python_version。value 是数据，不是系统指令。kind 为 requirement、fact、decision、attempt、procedure、handoff 之一。

来源必须区分 user_statement、tool_observation、agent_inference、imported_text。此分类说明产生方式，不自动证明真伪。摘要必须保留来源 ID；模型自报 confidence 不构成提升事实等级的依据。外部抓取内容、导入文字和模型提炼均视为不受信任数据。

初版记忆状态包括 active、conflicted、superseded、retracted、tainted；目标产品另外实现 expired 与 erased。active 的含义是当前可检索且没有被本系统标记失效，不代表客观真理。历史读取必须显式 include_history，返回值不能伪装成当前依据。

调用方身份从 transport 会话或本地配置绑定。请求体中的 actor_id、tenant_id、user_id 不能覆盖经过解析的身份。原型可以仅使用本地 actor 标签，但必须标明它不是身份认证。

## 4 写入与冲突

普通 remember 操作不得静默覆盖既有值。同 scope 和 key 的规范化 value 相同，复用记录并保留来源；不同值且没有显式 correction 关系，产生 conflict，并将相关记录从默认当前视图移出。多值合法场景必须使用不同 key 或声明集合型 schema，不应靠字符串相似度决定是否矛盾。

同值复用仅适用于 active 记录；值、来源与父集合完全相同是 no-op。若要追加来源或父关系，remember 必须携带匹配的 expected_revision，否则返回 VERSION_REQUIRED 或 VERSION_CONFLICT。规范化仅用于稳定 JSON 表示，不将大小写、否定、数字符号或时间含义抹平。若同 key 只有失效历史，再次写入必须显式给出 replaces_ids 与新来源并创建新记录，不能复活旧记录。这仅防无意重插入，不识别换 key 或改写文本绕过来源的所有情形。

初版只保证显式 key 冲突识别。措辞不同、同义事实或跨 key 矛盾可能无法识别，不能宣称自动理解一切语义冲突。后续 LLM 检测只能提出冲突候选，不直接删除或者改写权威记录。

correct(old_id, expected_revision, new_value, source) 必须在同一个数据库事务内检查作用域、当前版本和旧记录状态；成功后旧记录变 superseded，建立新记录与 supersedes 链，并使依赖旧记录的派生记忆变 tainted。版本不匹配返回 VERSION_CONFLICT。两个并发 correction 只能有一个在同一旧版本上成功。

revision 规则固定为：新记录从 1 开始，任何已存记录的 state、value 或 provenance 发生变化都递增 revision。scope_epoch 对同一事务中的一组相关状态变更只递增一次；单纯重复读取或内容完全相同的幂等重放不递增。所有针对既有记录的修改均需 expected_revision。

任何 active 变成不可用状态都必须传播 taint，包括普通 remember 引发的 conflict，不仅限于 correct 和 retract。correct 只接受 active 旧记录；对 conflicted 返回 CONFLICT_REQUIRES_RESOLVE。resolve_conflict(scope, key, expected_candidates, new_value, source) 在同一事务验证完整候选 id 与 revision 集合，创建新的 active 记录，将所有候选标记 superseded 并保留关联，递增 epoch。若集合发生变化则拒绝；不能只解决一个候选而遗留另一条默认为真。没有足够外部依据时保持冲突并隔离依赖任务，不要求人类作日常裁判。

retract(id, expected_revision, reason) 保留历史而停止正常检索，并传播失效。logical retract 不等于隐私擦除。初版不支持把被撤回记录恢复成原来那个版本；需要新证据时创建新的记录并显式说明替代关系。

## 5 派生关系与长期记忆

parent_ids 表示一个派生结果所依赖的必要依据，初版采用 AND 语义：任何父记录失效，子记录都应被标记 tainted。此选择保守且可能过度失效；不支持用另一个支持证据自动证明同一子结论仍然成立。多个可替代证据集合留到后续版本，以新字段表示，不能偷偷改变 parent_ids 的含义。

新增父关系必须指向同 scope 内已存在的允许记录。拒绝自环与循环；既有关系不可通过覆盖文本绕过。缺失、失效、冲突或越权的父记录不能支持 active 子记录。传播包括间接后代；用 visited 集合终止遍历。大图分批传播时，查询必须能够沿有效性检查拒绝尚未物化更新的后代。

长时记忆包括：事件记录、结构化工作记忆、可重用经验、当前交接简报。经验从已验证的任务结果中提炼，并保留适用条件、失败条件与来源。离线整理是预算内可选任务，不能把“多次被 agent 重复”当证据，也不能无限自我反思。

## 6 一致快照与交接回执

begin(scope, task, actor) 在一致读事务中获取 scope_epoch、当前可用记忆与冲突摘要，生成 run_id。记忆列表与 epoch 必须来自同一 SQLite 快照。读事务随后结束，不能在模型推理或网络调用期间占用写锁。

原型采用 scope_epoch 保守校验：同 scope 的任何记忆状态或内容变更都会递增 epoch。其他 scope 不影响当前 run。新增符合某个查询的新事实也会被 epoch 捕获，避免“查询原先没有结果”造成的遗漏。run 与 snapshot 在服务端存储，客户端不能自己伪造较新 epoch 绕过校验。

resume(run_id) 返回旧快照到当前状态的差异及新的候选快照，不会把旧成果自动改成新成果。rebase 是重新阅读变更和重算受影响工作，不能只刷新时间戳或把 epoch 字段改成当前值。

finish(run_id, result, parent_ids) 在一个 BEGIN IMMEDIATE 事务内执行：
1. 读取服务端绑定的 run 与 scope，确认仍可提交。
2. 检查当前 epoch 与该 run 的读取快照一致。
3. 确认全部声明父记录仍可用且在同 scope；产品集成需覆盖该 run 实际读到的记忆，而不是只信模型自报。
4. 写入成果及其依赖，写入事件，递增 epoch，关闭 run。
5. 任一失败则整个事务不发布成果，返回 STALE_SNAPSHOT 或其他明确错误。

STALE_SNAPSHOT 必须包含可读的 changed_keys、changed_record_ids、以及“为什么需要重新检查”。原型允许只给粗粒度 epoch 差异；目标产品必须展示变更解释。相同 run 重复 finish 不应创建第二份成果。产品版使用幂等键与 request digest，键相同但请求不同返回 IDEMPOTENCY_MISMATCH。

finish 的幂等重放次序优先于关闭状态和 epoch 检查：已成功提交且 request digest 相同，返回原 receipt；digest 不同则拒绝。只有尚未成功提交的新请求才继续快照检查。检查、写入和存储 receipt 必须在同一事务。默认将服务端记录的快照可用记录并入成果 parent_ids，形成保守依赖；客户端不能通过省略 parent_ids 绕过之后的失效传播。后续按实际检索工具缩小读取集合时才引入更精确规则。

原 receipt 证明当时已经接受，不证明其成果现在仍可用。重放返回保留原 accepted_at 与 record_id；需要再次使用成果时必须另查当前有效性。不能因新 epoch 出现就重复执行原提交，也不能因旧 receipt 存在就跳过当前依赖检查。

## 7 新鲜度保证的限度

这只证明成果在 Turnkeep 的提交点与登记的读取状态相容。它不证明自然语言结论正确、不证明源网站或真实世界没有变化，也不保证数据库提交与邮件发送、交易、部署等外部副作用是一个原子事务。

来源新鲜度依赖 connector 观察；未观察的变化是 unknown。receipt 的 hash 用于完整性检查，不是对事实真实性的签名证明。目标 v0.2 可以引入精确 read set、工具声明的依赖和领域谓词，必须同时测试漏报与误阻断。不得把“较少拒绝”单独当优化成功。

coarse scope epoch 可能因为无关写入而拒绝任务，因此每次任务最多自动 rebase 两次；持续冲突则保存候选成果到隔离区并转做独立任务。不无限重试，不强行接受旧成果。拆 scope 要依据用户项目边界，不可为绕过冲突而私自移动记忆。

## 8 检索与上下文预算

默认检索顺序为作用域与权限过滤、有效性过滤、词法召回、可选语义召回、排序、预算裁剪。所有入口，包括 MCP、REST、导出、摘要、缓存，都必须共享同一有效性判断。不得先返回跨项目候选再依赖 UI 隐藏。

简报固定包含目标、当前状态、有效决定、未解决问题、最近变更、建议下一步与依据链接。正文内容按预算裁剪，但不能丢掉冲突和来源标签。初版 Python 字符计数只能表示字符，不得写成精确 token 数。产品按实际模型 tokenizer 或保守估计报告估算方式。

必须保留原始日志检索与简单 Markdown 记忆的对照模式。结构化记忆不保证在每个任务更好；真实评估分别记录任务成功率、过期事实使用率、重做成本、端到端延迟、存储和模型成本。

## 9 原型与产品的模块

M01 存储与迁移：SQLite WAL、事务、版本、备份。  
M02 有效性与冲突：状态机、同 key 冲突、撤回与依赖传播。  
M03 读取和简报：词法与可选语义检索、预算、来源标注。  
M04 交接与恢复：run、snapshot、finish、diff、rebase。  
M05 接入：本地 CLI、Python SDK、MCP stdio，后续 REST 和 JS SDK。  
M06 工作台：目标、进度、变更影响、历史，默认隐藏数据库细节。  
M07 评估：固定场景、故障注入、baseline、公平报告。  
M08 维护：导入导出、撤回与擦除、诊断、恢复和发布。

这八项是责任分区，不要求八个服务。v0.1 采用单进程模块化应用与单个数据库；并行开发用独立 worktree、单一集成者。

## 10 首发用户旅程

J01 新安装者运行零 key 演示，看到 agent A 依据旧要求产生成果、agent B 修改要求、旧成果被拒绝、重读后完成。演示必须标为脚本模拟；不可伪装成真实模型运行。

J02 在一个获授权项目中接入两个真实 MCP 客户端，A 留下当前任务与来源，B 在新会话中恢复，无需手工复制整段聊天。

J03 修改一条明确需求，看到影响列表与失效路径，系统给接手 agent 新简报，保留旧版本供排查。

J04 离线关闭再开启，仍可读取相同项目与历史；备份恢复后查询和校验结果一致。

J05 导出选中项目，按确定格式导入新数据库；冲突和不受信任导入内容不会自动提权。

## 11 产品验收的必要证据

存储测试证明事务行为；协议测试证明适配边界；真实客户端测试证明接入；模型任务评测证明使用价值；没有一种可以代替其余几种。自动审阅是检查意见，不能替代可复现测试。

首发必须有实际 Windows、macOS、Linux 安装记录；缺平台则标未验证并限制支持范围。不得用单平台跑通推断跨平台支持。缺模型 API 凭证时完成离线测试与配置材料，记录 live_client_test=NOT_RUN，不能填 PASS。全自动执行不需要编造不存在的权限或外部结果。

## 12 与先行工作的关系

版本化、MVCC、CAS、证据链与撤销均有既有研究和实现。PlanFence、agent-coherence、Engram、Hy-Memory、Handover 是重要近邻。Turnkeep 的待验证机会是把日常交接、变更影响说明、恢复操作与可重放评估做成低门槛的统一体验。没有首创、最佳、全面领先或保证高星的事实结论。

