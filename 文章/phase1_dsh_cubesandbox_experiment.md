# 第一阶段实验报告：DSH Harness 与 CubeSandbox MicroVM 协同验证

## 1. 实验概述

本报告记录项目当前第一阶段的正式工程实验。实验部署在 ECS 服务器上，使用 CubeSandbox 提供真实 MicroVM，使用 DeepSeek Harness（DSH）作为统一 Harness 运行时，验证异构 Agent 系统的控制面和执行面闭环。

本阶段关注的不是大模型效果比较，也不是 Harness 内部 Agent 协作策略，而是验证：

```text
用户提交自然语言任务
    → Controller 监听和管理任务
    → Router 自动选择 Harness
    → MicroVM Pool 分配一次性沙箱
    → DSH Harness 执行任务
    → 回传结果、销毁沙箱并补充预热池
```

## 2. 实验目的

### 2.1 验证任务自动路由

用户不指定 `harness_id`、`agent_id` 或执行平台。系统根据任务描述推断所需能力，再结合 Registry 中的能力、工具、硬件和在线状态，自动选择可用 Harness。

### 2.2 验证 Controller 的协调能力

验证 Controller 能否统一管理：

- 任务接收和状态流转；
- Harness 注册与心跳；
- 任务租约和过期结果拒绝；
- Harness 掉线后的失败处理和重试；
- MicroVM 的分配、销毁和补池。

### 2.3 验证真实 MicroVM 执行环境

此前的 MicroVM Pool 使用 Mock Backend，只能验证控制面语义。本阶段切换到 ECS 上的 CubeSandbox Backend，验证真实 MicroVM 的创建、连接、执行和销毁。

### 2.4 为后续研究建立基础

本阶段为后续的 Harness 随机扰动、动态加入、跨 Harness 选择、World Model 预测和多节点调度提供可复用的实验基础设施。

## 3. 系统功能

### 3.1 Controller

Controller 是系统的任务监听器和控制面核心，负责：

- 接收 `POST /tasks` 任务请求；
- 调用 TaskParser 和 Router 生成路由决策；
- 创建任务租约并维护 `job_id`、`attempt`、`lease_id`；
- 将任务放入指定 Harness 的轮询队列；
- 监测 Harness 心跳和在线状态；
- 处理失败、超时、掉线和重试；
- 任务完成后销毁 MicroVM 并触发补池。

### 3.2 TaskParser + Router

TaskParser 从自然语言和任务字段中推断：

- `required_capabilities`；
- `required_tools`；
- `allowed_platforms`；
- 是否需要 GPU；
- 是否需要移动端；
- 路由判断依据。

Router 先过滤不满足硬约束的执行单元，再根据质量、延迟、成本、负载和历史成功率进行选择。用户不能绕过 Router 手动指定执行单元。

### 3.3 Registry

Registry 使用 SQLite 保存执行单元和任务相关状态，包括：

- Harness 身份、类型和能力；
- 可用工具和插件；
- 平台、架构、CPU、内存和 GPU 信息；
- 在线状态、负载和心跳时间；
- 成功率、质量分数和平均延迟；
- 后台探针结果和置信度。

当前 ECS 实验中，`dsh_harness_01` 已正常注册并处于 `idle` 状态；`dsh_harness_02` 和 `dsh_harness_03` 尚未启动 Adapter，因此处于 `offline`。Mac/iPhone 执行单元保留在历史数据库中，但不参与本阶段 ECS 任务。

### 3.4 DSH Adapter

Adapter 是长期运行的 Harness 接入进程，负责：

```text
注册 → heartbeat → poll task → 连接任务 MicroVM
     → 在 MicroVM 中运行 dsh → 回传统一结果
```

Adapter 不负责全局路由，也不负责 MicroVM 生命周期。它只连接 Controller 分配的 `microvm_id`，从而把 Harness 的执行逻辑与控制面解耦。

### 3.5 MicroVM Pool

MicroVM Pool 采用任务级一次性沙箱策略：

```text
预热 ready MicroVM
    → 任务租用一个 MicroVM
    → 任务执行期间状态为 busy
    → 任务结束后销毁该 MicroVM
    → 创建新的 MicroVM 补足 ready 数量
```

任务沙箱不归还，避免文件、进程、环境变量和网络状态在不同任务之间泄漏。Pool 同时维护节点资源、运行时版本、MicroVM 状态和逻辑快照语义。

### 3.6 CubeSandbox Backend

ECS 上使用真实 CubeSandbox Backend 替代 Mock Backend。当前配置为：

| 项目 | 配置 |
|---|---|
| Cube API | `http://127.0.0.1:3000` |
| MicroVM 节点 | `node-ecs-01` |
| 节点资源 | 8 vCPU、16 GB 内存 |
| MicroVM 默认配额 | 1 vCPU、512 MB |
| DSH 模板 | `tpl-30c729759b3d4afe8e9e5818` |
| Pool 最小预热数 | 1 |
| Pool 最大 MicroVM 数 | 2 |
| 运行时版本 | `dsh-0.1` |

模板中已经安装：

```text
Node.js 24.21.0
npm 11.19.0
dsh 0.1.5-rc.1
```

### 3.7 凭证和安全边界

DeepSeek API Key 保存在 ECS Controller 主机的：

```text
/root/deepseek_api_key
```

Controller 创建 MicroVM 时，通过 `env_vars` 运行时注入：

```text
DEEPSEEK_API_KEY
DSH_PERMISSION_MODE=danger-full-access
```

API Key 不进入任务请求、Registry、Harness 注册信息或任务结果。`danger-full-access` 只关闭 DSH 在 MicroVM 内部的第二层本地沙箱，实际隔离边界仍然是外层 CubeSandbox MicroVM。

## 4. 实验环境

### 4.1 ECS 环境

- 操作系统：Ubuntu 22.04 LTS；
- CPU 架构：x86_64；
- PVM Host Kernel：6.6.69；
- KVM 模块：`kvm_pvm`；
- 设备：`/dev/kvm` 已存在；
- CubeSandbox API 健康检查通过；
- CubeOps 健康检查通过；
- Cubelet 数据目录：`/data/cubelet`。

### 4.2 活跃 Harness

本阶段启动了一个真实 DSH Harness：

```text
unit_id: dsh_harness_01
profile: headless
execution_mode: cube
capabilities: local_file_access, document_generation, python
tools: dsh, python, shell
plugins: file, document, python
```

## 5. 实验流程

### 第一步：制作 DSH MicroVM 模板

基于 CubeSandbox 的代码模板创建临时 Sandbox，在其中安装 Node.js、npm 和 DSH，然后提交为新的 READY 模板。

模板验证结果：

```text
/usr/bin/node
/usr/bin/npm
/usr/bin/dsh
v24.21.0
11.19.0
0.1.5-rc.1
```

### 第二步：验证 MicroVM 内直接执行

首次运行时发现 DSH 默认还会尝试启动内部本地沙箱，但 MicroVM 内没有 Bubblewrap/Landlock 后端。通过注入：

```text
DSH_PERMISSION_MODE=danger-full-access
```

让 DSH 直接执行，而由 CubeSandbox 提供外层隔离。

之后在真实 MicroVM 内成功完成 Python 测试：

```text
hello from python
test passed
exit code: 0
```

### 第三步：启动 Controller 和 Adapter

Controller 使用 `--microvm-backend cubesandbox`，Adapter 使用 `--execution-mode cube`。Adapter 成功注册后，Controller 为 `dsh-headless-file` Profile 创建预热 MicroVM。

### 第四步：提交自然语言任务

提交请求只包含任务描述和能力需求：

```json
{
  "task_id": "e2e-dsh-003",
  "description": "运行一个简单的 Python 测试并返回结果",
  "required_capabilities": ["python"]
}
```

请求没有指定 Harness 或 MicroVM ID。

## 6. 实验结果

### 6.1 路由结果

Controller 自动推断：

```json
{
  "required_capabilities": ["python"],
  "required_tools": ["python"],
  "allowed_platforms": [],
  "requires_gpu": false,
  "requires_mobile": false
}
```

Router 自动选择：

```text
selected_unit: dsh_harness_01
selected_unit_type: harness
```

这证明任务请求不需要手动指定 Harness，Router 可以根据任务和 Registry 自动完成选择。

### 6.2 任务状态

任务经历了：

```text
queued → running → completed
```

最终结果：

```json
{
  "success": true,
  "quality": 0.8,
  "latency_ms": 10863.093,
  "executor": "dsh_harness_01",
  "dsh_profile": "headless",
  "execution_mode": "cube",
  "returncode": 0
}
```

### 6.3 Python 测试结果

DSH 在 MicroVM 内自主选择并执行 Python 测试，最终使用标准库 `unittest` 完成 4 个测试：

```text
test_add_floats ... ok
test_add_integers ... ok
test_divide ... ok
test_divide_by_zero_raises ... ok

Ran 4 tests in 0.001s

OK
```

4 个测试全部通过，进程退出码为 `0`。

### 6.4 MicroVM 生命周期结果

任务获得了独立 MicroVM：

```text
microvm_profile: dsh-headless-file
microvm_node_id: node-ecs-01
microvm_vcpus: 1
microvm_memory: 512 MB
```

任务完成后返回：

```text
lifecycle: destroyed_after_success
```

任务结果中的 `microvm_id` 被 Controller 清理，随后 Pool 创建新的 `ready` MicroVM 补回余额。实验观察到 Pool 保持 1 个 `ready` MicroVM。

## 7. 已达到的功能

截至本报告，项目已经实现并验证：

| 功能 | 状态 | 说明 |
|---|---|---|
| 自然语言任务解析 | 已完成 | 从任务描述推断能力、工具和资源需求 |
| 自动 Harness 路由 | 已完成 | Router 自动选择 `dsh_harness_01` |
| 动态 Harness 注册 | 已完成 | Adapter 可运行时注册并发送心跳 |
| Harness 在线状态管理 | 已完成 | Registry 保存 idle/offline 等状态 |
| DSH Adapter 轮询 | 已完成 | Adapter 通过轮询获取任务 |
| 真实 CubeSandbox MicroVM | 已完成 | ECS 上真实创建和连接 Sandbox |
| DSH 在 MicroVM 内执行 | 已完成 | `execution_mode=cube` 验证通过 |
| API Key 运行时注入 | 已完成 | Key 不进入任务和 Registry |
| MicroVM 预热池 | 已完成 | 保持最小 ready 数量 |
| 任务级 MicroVM 销毁 | 已完成 | 成功后 `destroyed_after_success` |
| MicroVM 补池 | 已完成 | 任务结束后创建干净替代 VM |
| 失败租约和重试机制 | 已完成 | 已通过可靠性单元测试验证 |
| Controller 重启恢复 | 已完成 | 已通过可靠性单元测试验证 |
| Harness 发现 Agent 后后台准入 | 已完成 | Canary 成功后才汇总能力进入前台路由 |
| 多 Harness 实际并行运行 | 部分完成 | 结构已支持，当前只启用一个 Harness |
| 多物理节点调度 | 未完成 | 当前 ECS 只有一个物理节点 |
| World Model 预测路由 | 未完成 | 作为后续研究模块 |

### 7.1 Harness 动态发现 Agent 的后台准入

已加入 Harness 发现候选 Agent 的准入机制。Harness 通过 `/harness/discover` 上报候选摘要，Controller 不会立即把候选能力加入 Router，而是将其标记为 `testing`，并把 Canary Task 放入独立后台队列。

候选验证成功后，Controller 才将其状态改为 `eligible`，并把候选的能力和工具汇总到父 Harness 的前台能力摘要中。验证失败的候选会继续重试，超过最大次数后标记为 `rejected`。全局 Router 仍然只看到父 Harness，不会看到 Harness 内部 Agent 的身份和协作细节。

已通过单元测试验证：

```text
新发现 Agent → testing
前台任务暂时无法使用新能力
后台 Canary 成功 → eligible
父 Harness 获得新能力 → Router 可以选择父 Harness
```

## 8. 测试证据

本地代码测试包括：

```text
核心单元测试：16 passed
HTTP 集成测试：2 passed
语法检查：passed
```

覆盖内容包括：

- Router 需求解析和硬约束选择；
- 动态注册、心跳和掉线；
- 租约、失败历史和重试；
- Controller 重启恢复；
- MicroVM 预热、租用、销毁和补池；
- CubeSandbox SDK 创建、销毁和环境变量注入；
- Harness 发现 Agent 后的后台候选准入和能力晋升；
- HTTP 任务结果路径和统一回传。

ECS 实机验证包括：

- CubeSandbox `/health` 返回正常；
- CubeOps `/health` 返回正常；
- `/dev/kvm` 和 `kvm_pvm` 正常；
- DSH、Node.js 和 npm 在模板中可用；
- DSH 在真实 MicroVM 内成功执行 Python；
- Controller 自动路由并完成真实任务；
- 任务结束后 MicroVM 成功销毁并补池。

## 9. 当前结论

第一阶段已经证明，项目的基础控制面和真实执行面可以协同工作：

1. 用户可以只提交自然语言任务，Router 会自动选择 Harness；
2. Controller 可以协调 Registry、Adapter 和 MicroVM Pool；
3. DSH 可以在 CubeSandbox MicroVM 内完成实际任务；
4. API Key 可以在 MicroVM 创建时安全注入，而不进入任务数据；
5. 任务完成后，MicroVM 会被销毁并由 Pool 创建新的干净实例；
6. 当前架构已经具备进一步加入多个 Harness、故障扰动和 World Model 的基础。

本阶段验证的是系统功能闭环，不应直接解释为大规模性能结论。当前实验只有一个 ECS 物理节点和一个活跃 Harness，`10863.093 ms` 是单次端到端样本，不能代表稳定平均延迟。

## 10. 下一步实验

建议按以下顺序推进：

1. 启动 `dsh_harness_02` 和 `dsh_harness_03`，验证不同插件能力的自动路由；
2. 分别测量冷启动、预热命中、任务执行和销毁补池延迟；
3. 对 Harness 注入延迟、随机失败、心跳中断和进程退出；
4. 验证 Controller 是否会排除故障 Harness 并选择新的 Harness 重试；
5. 增加并发任务，观察 `max_total`、节点资源和 Pool 排队行为；
6. 收集 Harness 状态序列，建立 World Model 的状态预测和路由决策实验。

## 11. 未来研究展望

### 11.1 将蒸馏后的 World Model 融入 TaskParser

当前 TaskParser 主要依靠规则和显式任务字段推断需求。后续希望通过知识蒸馏，将 World Model 对任务、Harness 状态、Agent 能力和历史执行结果的预测能力压缩并整合进 TaskParser，使它不只判断“任务需要什么”，还能够预测：

- 当前任务可能需要哪些类型的 Agent；
- 哪些 Harness 或 Agent Team 更可能成功；
- 不同分工方案的延迟、资源消耗和失败风险；
- 新加入 Agent 在当前任务上的可用性和可信度。

在此基础上，World Model 可以自主决定是否组建 Agent Team，以及 Team 内部如何分工。用户和全局 Router 不需要知道 Team 的内部组织细节，只接收统一的任务结果。

这一设计需要保留一个重要的可解释性出口：如果 World Model 预测的 Team 组合或分工执行失败，Harness 必须返回结构化失败原因，而不是只返回一个笼统的 `failed`。至少应包含：

```json
{
  "success": false,
  "failure_type": "team_execution_failed",
  "failure_stage": "image_preprocessing",
  "failed_capability": "image_compression",
  "reason": "副任务所需的压缩插件不可用",
  "retryable": true
}
```

失败原因既用于用户侧诊断，也用于后续 World Model 更新状态估计和改进下一次 Team 选择。

### 11.2 主 Agent Team 与副 Agent Team 的一致性哈希容灾

在 World Model 选择主 Agent Team 后，系统可以根据任务或数据的稳定 key，通过一致性哈希将任务输入、必要上下文或中间结果同步映射到副 Agent Team。副 Team 不需要立即执行完整任务，但应保持足够的输入和状态副本，以便主 Team 失败时快速接管。

目标流程为：

```text
World Model 选择主 Agent Team
    ↓
一致性哈希计算 primary_team / replica_team
    ↓
数据和必要上下文发送给主 Team 与副 Team
    ↓
主 Team 执行任务
    ├── 成功 → 返回结果，副 Team 释放或保留短期状态
    └── 失败/掉线 → 副 Team 立即接管计算
```

一致性哈希的作用是让任务或数据在 Agent Team 变化、节点扩缩容和局部故障时尽量保持稳定映射，减少大规模重分配。副 Team 的接管需要结合租约和版本号，避免主、副 Team 同时提交结果造成重复写入或结果覆盖。

后续需要重点研究：

1. 主、副 Team 之间同步哪些数据，以及同步到什么粒度；
2. 如何用版本号、租约和幂等提交保证结果一致性；
3. 主 Team 失败后副 Team 的启动延迟和状态恢复开销；
4. 副本数量、同步成本与故障恢复速度之间的权衡；
5. World Model 如何根据任务重要性和失败概率动态决定是否启用副 Team。

### 11.3 基于一致性哈希的 Trace Replay 后台准入

在新 Agent 的后台测试阶段，可以利用其他 Harness 已经产生的历史执行轨迹，减少新 Agent 的冷启动成本。历史轨迹不应以某个 Harness 的私有文件形式直接传递，而应进入统一的 Trace Store，并经过脱敏和结构化处理。

可用于 Replay 的 Trace 内容包括：

```text
任务类型和输入摘要
所需能力与工具
工具调用序列
中间状态摘要
延迟与资源使用
成功/失败结果
结构化失败原因
```

不应直接转移完整 Chain-of-Thought，也不能把 API Key、用户隐私、原始文件内容和其他敏感信息写入 Trace。

新 Agent 加入后，Controller 根据稳定的任务键选择相关 Trace 分片：

```text
trace_key = hash(task_family + capability + schema_version)
```

一致性哈希可以让新 Agent 只接收与自身能力相关的 Trace，避免每次加入新 Agent 时重新复制全部历史数据；当 Harness、节点或 Trace 分片发生变化时，也只需要重新分配少量数据。

后台准入流程为：

```text
Harness 发现新 Agent
    ↓
Controller 标记 testing / background-only
    ↓
一致性哈希选择 Trace 分片
    ↓
在独立 MicroVM 中进行 Shadow Replay
    ↓
比较成功率、延迟、工具调用正确性和失败原因
    ↓
达到阈值 → eligible，加入前台备选池
未达到阈值 → 继续测试或 rejected
```

Replay 不能直接修改真实数据或产生生产副作用。对于线上任务，可以让原 Harness 继续执行真实任务，同时只把脱敏的输入和上下文副本发送给新 Agent：

```text
原 Harness 执行真实任务
    ├── 原结果继续生效
    └── Trace 副本 → 新 Agent Shadow Replay
```

测试集应采用分层抽样：

1. 正常成功任务，用于验证基本能力；
2. 历史失败任务，用于验证新 Agent 是否能修复旧问题；
3. 边界和故障任务，用于验证超时、工具缺失和异常输入下的鲁棒性。

Trace Replay 不能完全替代真实 Canary Task。更合理的后台准入组合是：

```text
历史 Trace Replay + 合成 Canary + 少量真实 Shadow Task
```

最终的准入分数不仅应包含成功率，还应包含延迟、成本、资源占用和失败原因质量。Replay 结果也可以反过来作为 World Model 的训练和状态预测数据。

### 11.4 总体研究目标

最终系统希望形成以下闭环：

```text
TaskParser 接收任务
    ↓
World Model 预测执行状态和失败风险
    ↓
自主组建主 Agent Team 与副 Agent Team
    ↓
主 Team 执行，副 Team 保持可接管状态
    ↓
失败时报告结构化原因并快速切换副 Team
    ↓
执行结果和失败轨迹反哺 World Model
```

这将把当前的“基于静态能力的 Harness 路由”推进为“面向不确定环境的预测式 Team 调度与容错执行”。
