# FedHarness 本地架构

主执行链路完全运行在用户电脑上。全局 Router 只选择黑箱 Harness，不读取 Harness 内部的 Agent、轨迹、模型或本地数据。

```mermaid
flowchart LR
    User["用户<br/>自然语言任务"] --> Controller["Controller<br/>监听 · 租约 · 重试"]
    Controller --> Router["TaskParser + Router<br/>硬约束 · 偏好 · 评分"]
    Registry["SQLite Registry<br/>能力 · 状态 · Outcome"] -.-> Router
    Router --> Adapter["Local Adapter<br/>注册 · 心跳 · 轮询"]
    Adapter --> Harness["Selected Harness<br/>本地模型 · 数据 · 插件"]
    Harness --> Result["最终产物<br/>或失败原因"]
    Result --> Controller

    Optional["Optional Sandbox Backend<br/>Mock / CubeSandbox"] -. "需要隔离时接入" .-> Adapter
```

## 本地任务生命周期

```mermaid
sequenceDiagram
    participant U as User
    participant C as Controller
    participant R as TaskParser/Router
    participant A as Local Adapter
    participant H as Black-box Harness

    U->>C: POST /tasks(description)
    C->>R: 解析硬需求、偏好和风险
    R-->>C: selected Harness + 可解释评分
    A->>C: heartbeat + poll
    C-->>A: task + lease_id
    A->>H: 增强后的执行 Prompt
    H-->>A: final outcome / failure reason
    A->>C: POST result(lease_id)
    C-->>U: completed / retry_wait / failed
```

## 设计边界

| 层 | 负责 | 不负责 |
|---|---|---|
| Controller | 任务状态、租约、心跳、重试和准入 | 不观察 Harness 内部协作 |
| TaskParser | 将自然语言转换为约束、偏好和目标 | 不指定 Harness |
| Router | 过滤并选择 Harness | 不选择内部 Agent |
| Registry | 保存能力、状态和任务级 Outcome | 不保存思维链或内部 Trace |
| Adapter | 协议翻译、轮询和结果回传 | 不决定全局路由 |
| Harness | 使用本地硬件、模型、数据和 Agent Team 执行 | 不必公开内部结构 |
| Sandbox Backend | 可选的执行隔离 | 不参与任务语义判断 |

默认 `local_runtime.py` 使用本地进程和独立工作目录。CubeSandbox 仅在需要硬件隔离或云端扩展时作为插件接入，不是算法实验的必要条件。
