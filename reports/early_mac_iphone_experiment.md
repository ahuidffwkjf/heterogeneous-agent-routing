# 早期 Mac/iPhone 异构 Agent 路由实验报告

## 1. 实验定位

本报告记录项目早期的工程验证实验。实验关注的是一个收敛后的问题：用户只提交自然语言任务，Controller 不接收用户指定的 Agent，而是根据任务需求从注册表中自动选择合适的执行单元。

这一阶段不是最终论文实验，也没有声称已经完成真实 MicroVM 性能评测。它的作用是验证后续系统所依赖的控制面闭环：任务解析、能力匹配、执行单元注册、心跳、轮询、结果回传、失败重试以及一次性沙箱生命周期。

## 2. 研究问题

早期实验验证以下问题：

1. 能否从任务描述中推断 `mobile`、`local_file_access`、`document_generation` 等需求？
2. 能否只依据推断结果和注册表状态，自动选择 Mac Agent 或 iPhone Agent？
3. iPhone 位于 NAT 或移动网络环境时，能否通过主动轮询而不是 Controller 回调完成任务派发？
4. Agent 掉线、租约过期或 Controller 重启时，任务状态能否保持一致并触发重试？
5. MicroVM 预热、租用、销毁和补池是否能与任务生命周期协调？

## 3. 系统组成

```text
用户任务
   |
   v
Controller
   ├── TaskParser / Router：推断需求并自动选择执行单元
   ├── SQLite Registry：能力、平台、硬件、心跳和在线状态
   ├── Job Store：任务状态、attempt、lease 和失败历史
   ├── Mac Agent：HTTP 接收任务并执行本地文件测试
   ├── iPhone Agent：主动轮询、执行移动端测试、回传结果
   └── Mock MicroVM Pool：预热、租用、销毁和补充
```

早期的 Mac/iPhone Agent 是两个独立的执行端。它们用于验证跨设备路由，不代表最终的 Harness 抽象。后续设计把用户可见的执行单元提升为 Harness：全局 Router 只选择 Harness，Harness 内部的 Agent 或 Agent Team 对外隐藏。

## 4. 执行单元注册

每个执行单元在注册表中包含：

- `unit_id`、`unit_type`：执行单元身份和类型；
- `platforms`：例如 `macos`、`ios`；
- `capabilities`：例如 `mobile`、`local_file_access`、`document_generation`；
- `tools`：可用工具；
- `metadata`：硬件信息、端点、心跳要求和调试信息；
- `status`、`last_heartbeat`：在线性和最近心跳时间。

配置文件中的执行单元会在 Controller 启动时写入 SQLite。运行中的 Agent 还可以通过带 token 的注册接口加入，并持续发送心跳。Controller 的健康监视线程会将超时执行单元标记为不可用，Router 不会把新任务派给它。

## 5. 路由流程

一次任务请求经历以下流程：

```text
POST /tasks
  ↓
TaskParser 从描述和显式字段推断需求
  ↓
Registry 提供当前在线执行单元快照
  ↓
Router 先检查硬约束，再按软约束评分
  ↓
Controller 为选中的执行单元创建任务租约
  ↓
目标 Agent 接收任务并回传结果
  ↓
成功：完成任务并销毁任务 MicroVM
失败/掉线：记录失败，选择可用单元重试并创建新的租约
```

路由的关键约束是用户不能通过请求体指定 `agent_id`、`selected_unit` 或类似字段绕过 Router。换句话说，实验验证的是自动选择，而不是“API 帮用户选择”。

## 6. 实验场景与观察结果

### 6.1 Mac 本地文件与报告任务

提交任务时只提供自然语言描述和输入文件路径：

```json
{
  "task_id": "mac-file-001",
  "description": "读取本地文件并生成一份报告",
  "input_path": "data/sample_input.txt"
}
```

TaskParser 推断出：

- `local_file_access`；
- `document_generation`。

Router 自动选择 `mac_agent_01`，没有要求用户指定平台。已观察到的返回结果包括：文件大小 170 bytes、3 行、84 个字符，以及内容摘录。任务最终状态为 `completed`，结果中记录执行者为 `mac_agent_01`。

该场景证明了“任务语义 → 能力需求 → 执行单元”的基本闭环，也证明了本地文件访问这类平台相关需求可以被纳入路由依据。

### 6.2 移动端任务

提交移动端任务时只提供：

```json
{
  "task_id": "iphone-test-003",
  "description": "移动端测试",
  "required_capabilities": ["mobile"]
}
```

Router 自动选择 `iphone_agent_01`。由于手机通常不能被 ECS 直接回调，iPhone App 采用主动轮询 Controller 的方式：

```text
iPhone 注册
  → 定时 heartbeat
  → GET /mobile/tasks/next
  → 在手机端执行或标记完成
  → POST /mobile/tasks/<job_id>/result
```

在实验中任务经历了 `queued → running → completed` 状态变化，最终结果中包含执行者、质量、延迟和成功标记。这个结果验证了移动端 Agent 的连接模式，但“执行”部分仍以测试 App 的手动完成动作作为占位实现，不等价于真实移动端自动化基准。

### 6.3 掉线与重试

Controller 为每次分配生成独立 `lease_id`，并记录 `attempt` 和失败历史。Agent 掉线或心跳超时后，监视线程将任务重新置为可重试状态；重试时可以选择新的在线执行单元。旧 Agent 即使恢复并提交旧租约结果，也不能覆盖新 attempt 的结果。

这部分验证的是控制面一致性和故障语义，而不是大规模可靠性统计。早期单元测试覆盖了：动态注册、心跳超时、任务重试、旧租约拒绝以及 Controller 重启后的任务恢复。

### 6.4 MicroVM 预热池

早期实现使用 Mock Backend 表示 MicroVM，不启动真实 KVM 虚拟机。池的语义是：

```text
启动 Controller → 预热 N 个 ready MicroVM
任务到达 → reserve 一个 MicroVM
任务成功/失败 → destroy 任务 MicroVM
池中 ready 数不足 → 创建新 MicroVM 补足余额
```

这里采用“一次任务一个沙箱”的策略，任务结束后不归还原沙箱，以降低任务间状态泄漏风险。早期测试验证了预热、租用、销毁和补池，以及逻辑节点之间的 pause/resume 和快照恢复语义；但后者仍然是 Mock 的控制面模拟。

## 7. 测试与证据

早期开发过程中，Router 的基础单元测试曾得到以下结果：

```text
Ran 3 tests in 0.000s
OK
```

随后扩展到可靠性、Harness Runtime 和 MicroVM Pool 后，核心非网络测试达到 12 个通过。测试主要覆盖：

- 任务需求解析和硬约束选择；
- 平台、能力、工具和 GPU 约束；
- 动态注册与状态更新；
- 心跳超时与掉线；
- 重试、租约和过期结果拒绝；
- Controller 重启后的任务恢复；
- MicroVM 预热、租用、销毁、补池和逻辑跨节点恢复。

由于本地受限环境不允许测试服务器绑定监听端口，HTTP 端到端测试没有在该环境中作为正式数字结果报告。用户在 Mac 和 ECS 上实际启动过 Controller，并通过 `curl` 验证了 `/health`、任务提交、任务查询和任务完成接口。

## 8. 主要结论

早期实验得到四个工程结论：

1. 自动路由可行：用户只需提交任务，Router 可以从语义和注册表状态选择执行单元。
2. 移动端适合轮询：对于手机等难以被外部直接访问的设备，Agent 主动拉取任务比 Controller 回调更稳妥。
3. Controller 应拥有任务状态和健康监视：Router 只做选择，Registry 保存状态，Controller 负责租约、重试和恢复。
4. MicroVM 应当是任务级一次性执行环境：任务完成后销毁，并预热补充新的 MicroVM，避免跨任务状态污染。

## 9. 局限性

本实验仍有明显边界：

- Mac Agent、iPhone Agent 是专用原型，不是统一 Harness；
- MicroVM Pool 使用 Mock Backend，没有真实 KVM、Firecracker 或 CubeSandbox；
- SQLite 适合单 Controller 本地实验，不代表多 Controller 高可用；
- Controller 使用明文 HTTP，正式部署需要 HTTPS、鉴权和网络隔离；
- iPhone 任务的执行逻辑较简单，部分流程由 App 手动标记完成；
- 没有进行不同负载、并发度和故障注入强度下的统计学性能比较；
- 早期代码与主线 DSH 代码耦合较多，维护复杂度较高。

## 10. 对当前主线的影响

早期实验保留的核心思想是：

```text
Controller 监听任务和状态
Router 自动选择能力匹配的执行单元
Registry 管理动态加入的执行单元
MicroVM Pool 管理隔离环境生命周期
```

被剥离的部分是 Mac/iPhone 专用实现。当前主线改为统一的 DeepSeek Harness：不同 Harness 实例通过插件和能力注册产生差异，Harness 内部自行发现 Agent、组建 Agent Team 和决定协作方式。这样既继承早期自动路由和故障恢复机制，又为后续世界模型预测、真实 MicroVM 后端和多节点调度留出接口。

## 11. 复现实验的建议

如果需要复现早期结果，应将本目录作为历史版本参考；如果需要继续做实验，应使用项目根目录的：

- `controller.py`；
- `execution_units_dsh.json`；
- `dsh_agent.py`；
- `microvm_pool.py`；
- `microvm_nodes.json`。

早期代码仅用于对照，不应与当前 DSH 主线混合启动。
