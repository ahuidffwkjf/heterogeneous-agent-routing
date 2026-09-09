# 项目架构图 v2

这张图把系统按“控制面、执行面、隔离层、恢复路径”重新整理。全局 Router 只选择 Harness，不直接接触 Harness 内部的 Agent 或 Agent Team。

```mermaid
flowchart TB
    User["用户 / 实验脚本\n只提交任务描述"]

    subgraph Control["控制面 Control Plane"]
        Controller["Controller\n任务监听、租约、状态机、重试"]
        Parser["TaskParser\n自然语言需求推断"]
        Router["Router\n硬约束过滤 + 软目标评分"]
        Registry["Registry\nSQLite：能力、硬件、心跳、状态"]
        Jobs["Job Store\n任务、attempt、lease、失败历史"]
        Ops["Ops API\n节点隔离、排空、恢复"]
    end

    subgraph Execution["执行面 Execution Plane"]
        A1["DSH Adapter 1\n文件 / 文档 / Python"]
        A2["DSH Adapter 2\n代码 / Shell / 编译"]
        A3["DSH Adapter 3\n数据 / 结构化输出"]
        subgraph Harnesses["统一 DeepSeek Harness 实例"]
            H1["Harness 1\n插件集合 P1"]
            H2["Harness 2\n插件集合 P2"]
            H3["Harness 3\n插件集合 P3"]
        end
    end

    subgraph Isolation["隔离层 Isolation Layer"]
        Pool["MicroVM Pool\n预热、租用、销毁、补池"]
        NodeA["Node A\nCPU / 内存 / Runtime"]
        NodeB["Node B\nCPU / 内存 / Runtime"]
        Snap["Shared Snapshot Store\npause / resume / 跨节点恢复"]
    end

    subgraph Private["Harness 私有域（全局不可见）"]
        Discovery["内部 Agent Discovery"]
        Team["自组织 Agent Team\n协作策略由 Harness 决定"]
        Result["统一结果聚合"]
    end

    User --> Controller
    Controller --> Parser --> Router
    Controller <--> Registry
    Controller <--> Jobs
    Controller --> Ops
    Router --> H1
    Router --> H2
    Router --> H3
    Pool --> NodeA
    Pool --> NodeB
    Pool <--> Snap
    H1 --> A1
    H2 --> A2
    H3 --> A3
    A1 -. "outbound polling + heartbeat" .-> Controller
    A2 -. "outbound polling + heartbeat" .-> Controller
    A3 -. "outbound polling + heartbeat" .-> Controller
    Pool -. "为任务分配 MicroVM" .-> H1
    Pool -. "为任务分配 MicroVM" .-> H2
    Pool -. "为任务分配 MicroVM" .-> H3
    H1 --> Discovery
    H2 --> Discovery
    H3 --> Discovery
    Discovery --> Team --> Result
    Result -. "统一 outcome" .-> A1
    Result -. "统一 outcome" .-> A2
    Result -. "统一 outcome" .-> A3
    Controller -. "成功/失败后 destroy + replenish" .-> Pool
    Controller -. "掉线后换 Harness + 新 lease" .-> Router

    classDef control fill:#e8f1ff,stroke:#3772c6,color:#102a43
    classDef execution fill:#eaf8ef,stroke:#328452,color:#123524
    classDef isolation fill:#fff4df,stroke:#c98a18,color:#4a3200
    classDef private fill:#f4eafd,stroke:#8b5bb5,color:#32194a
    class Controller,Parser,Router,Registry,Jobs,Ops control
    class H1,H2,H3,A1,A2,A3 execution
    class Pool,NodeA,NodeB,Snap isolation
    class Discovery,Team,Result private
```

## 一次任务的生命周期

```mermaid
sequenceDiagram
    participant U as User
    participant C as Controller
    participant R as Router
    participant P as MicroVM Pool
    participant A as DSH Adapter
    participant D as DSH Harness

    U->>C: POST /tasks(description)
    C->>R: parse + route
    R-->>C: selected Harness
    C->>P: reserve(profile, job_id)
    P-->>C: vm_id / node_id
    A->>C: heartbeat + poll
    C-->>A: task + lease_id + vm_id
    A->>D: dsh --profile headless task
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
| DSH Harness | 插件调用、内部 Agent 发现、自组织 Team | 不直接修改全局路由结果 |
| MicroVM Pool | 隔离环境生命周期和节点资源 | 不理解任务语义 |

当前图中的 Node A / Node B 可以是同一台 ECS 上的逻辑节点；只有接入真实 KVM、MicroVM Backend 和共享存储后，才具备跨物理节点运行的实验含义。
