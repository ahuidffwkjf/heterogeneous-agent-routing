# 项目架构图

这张图只保留一条主执行链路。全局 Router 只选择 Harness，不直接接触 Harness 内部的 Agent 或 Agent Team。

```mermaid
flowchart LR
    User["用户<br/>自然语言任务"] --> Controller["Controller<br/>接收任务 · 租约 · 重试"]
    Controller --> Router["TaskParser + Router<br/>自动选择 Harness"]
    Router --> Pool["MicroVM Pool<br/>预热 · 租用 · 销毁 · 补池"]
    Pool --> VM["一次性 CubeSandbox<br/>MicroVM"]
    VM --> Harness["DSH Harness<br/>插件 + Agent / Team"]
    Harness --> Result["统一结果"]
    Result --> Controller

    Registry["Registry<br/>能力 · 硬件 · 心跳"] -. "状态" .-> Router
    Controller -. "任务 + vm_id" .-> Adapter["DSH Adapter<br/>注册 · 心跳 · 轮询"]
    Adapter -. "连接执行" .-> VM
    Secret["Secret file<br/>运行时注入 API Key"] -. "只进入新 VM" .-> Pool
```

## 一次任务的生命周期

```mermaid
sequenceDiagram
    participant U as User
    participant C as Controller
    participant R as Router
    participant P as MicroVM Pool
    participant V as Selected MicroVM
    participant A as DSH Adapter
    participant D as DSH Harness

    U->>C: POST /tasks(description)
    C->>R: parse + route
    R-->>C: selected Harness
    C->>P: reserve(profile, job_id)
    P-->>C: vm_id / node_id
    A->>C: heartbeat + poll
    C-->>A: task + lease_id + vm_id
    A->>V: 在 MicroVM 中启动执行
    V->>D: dsh --profile headless task
    D-->>A: aggregate outcome
    A->>C: POST result(lease_id)
    C->>P: destroy task VM
    C->>P: replenish warm pool
    C-->>U: completed / retry_wait / failed
```

## 设计要点

| 层 | 只负责什么 | 不负责什么 |
|---|---|---|
| Controller | 任务状态、租约、心跳、重试和恢复 | 不决定 Harness 内部如何协作 |
| Router | 根据需求和注册表选择 Harness | 不接受用户强制指定 Agent |
| Registry | 保存能力、硬件、负载和在线状态 | 不执行任务 |
| DSH Adapter | 注册、心跳、轮询任务、连接 MicroVM、回传结果 | 不决定全局路由、不管理 VM 生命周期 |
| DSH Harness | 插件调用、内部 Agent 发现、自组织 Team | 不直接修改全局路由结果 |
| MicroVM Pool | 隔离环境生命周期和节点资源 | 不理解任务语义、不参与 Harness 内部协作 |

当前图中的 Node A / Node B 可以是同一台 ECS 上的逻辑节点；只有接入真实 KVM、MicroVM Backend 和共享存储后，才具备跨物理节点运行的实验含义。
