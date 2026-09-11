# 异构多 Agent 系统：ICML 2027 投稿计划（重写版）

> 更新时间：2026 年 9 月 11 日
> 目标会议：ICML 2027  由于官方尚未公布 2027 年具体投稿日期，本文暂按“2027 年 1 月下旬投稿”倒排；正式日期以 ICML 官方通知为准。参考：[ICML Future Meetings](https://icml.cc/Conferences/FutureMeetings)、[ICML 2026 Dates](https://icml.cc/Conferences/2026/Dates)。

## 1. 投稿定位

### 1.1 论文拟解决的问题

现实中的 Agent 并不是静态、同质、随时可用的执行器，而是运行在不同 Harness、硬件和系统环境中的异构执行单元。它们可能具有不同的：

- 平台与硬件：Linux、macOS、iOS、CPU、GPU、移动传感器等；
- 工具和插件：Python、Shell、编译器、文件处理、摄像头等；
- 当前状态：空闲、繁忙、延迟、掉线、资源不足；
- 可观测信息：历史成功率、延迟、质量、成本和近期故障；
- 生命周期：已有 Agent、刚发现的新 Agent、正在后台测试的候选 Agent。

因此，核心问题不是“把任务分给哪个固定 Agent”，而是：

> 给定用户任务、异构 Harness 的能力与动态状态，以及不确定的执行结果，系统如何选择或组织一个更可能成功的执行单元，并在新 Agent 加入、Agent 掉线和执行失败时保持低延迟与高鲁棒性？

### 1.2 论文主张

本文拟研究一种面向动态异构 Harness 网络的、结果感知的任务调度框架。框架包含四个关键思想：

1. **Harness-level routing**：全局 Router 只选择 Harness，不直接干预 Harness 内部 Agent 或 Agent Team 的协作细节。
2. **后台准入**：Harness 发现新 Agent 后，先进入后台测试通道；只有测试达标后，才加入全局调度的能力摘要。
3. **Outcome-aware prediction**：将历史轨迹、实时状态和任务特征用于预测候选 Harness 的成功概率、延迟和风险，并辅助选择。
4. **Selective redundancy**：对高风险任务建立主 Agent Team 与副 Agent Team，通过一致性哈希、版本号、租约和幂等机制实现快速接管。

## 2. 研究边界

### 2.1 当前论文的基本执行单元

论文的第一层实验单位是：

```text
一个用户任务  →  一个全局选中的 Harness
```

Harness 内部可以自行发现 Agent、组建 Agent Team、决定分工和通信方式。全局 Controller 不要求知道其内部具体流程，只接收：

- 是否接受任务；
- 执行状态和心跳；
- 结果、质量、延迟和成本；
- 失败类型和结构化失败原因。

因此，本文不把“动态 DAG、多子任务工作流、全局显式 Agent Team 编排”作为第一阶段的研究重点。跨 Harness 的 Agent Team 组建是后续能力扩展，不应成为当前论文的必要前提。

### 2.2 论文中的层次划分

| 层次 | 负责内容 | 是否由全局 Router 直接控制 |
|---|---|---|
| Controller | 任务接收、租约、重试、监控、故障转移 | 是 |
| Task Parser / World Model | 解析需求、预测结果、估计风险 | 是 |
| Router | 过滤约束、评分候选 Harness、选择主备 | 是 |
| Registry | 保存能力、硬件、心跳、负载、历史统计 | 间接使用 |
| Harness | 将任务翻译为本地执行动作，发现内部 Agent | 否 |
| Agent / Agent Team | 执行具体工作，内部如何协作由 Harness 决定 | 否 |
| MicroVM | 为任务提供隔离、可销毁的运行环境 | 由 Controller / Pool 管理 |

## 3. 当前已经完成的系统基础

### 3.1 现有系统链路

```text
用户任务
   ↓
Controller
   ├── Task Parser：从自然语言推断 capability / tool / platform / GPU / mobile
   ├── Registry：记录 Harness 能力、硬件、心跳、负载与历史表现
   ├── Router：硬约束过滤 + 候选评分
   ├── Adapter：与 Harness 通信、租约、轮询和状态翻译
   ├── MicroVM Pool：预热、租用、销毁、补充
   └── Monitor：超时、掉线、失败重试和重新调度
             ↓
        Harness
             ↓
      Agent / Agent Team
```

### 3.2 已验证的工程能力

- 已有 Controller、Router、Registry、Adapter 和 MicroVM Pool 的基本实现；
- Registry 支持 SQLite 本地实验，记录 Harness 的能力、平台、硬件、负载、心跳和质量统计；
- Router 可以根据任务语义推断需求，并基于硬约束和评分自动选择 Harness；
- DSH Harness 已接入，当前统一使用 DSH 作为 Harness 运行接口；
- ECS 上已经接通 CubeSandbox，并通过 PVM Host Kernel、`/dev/kvm` 和 `kvm_pvm` 运行真实 MicroVM；
- MicroVM Pool 支持预热、租用、任务完成后销毁，并自动补充预热池余额；
- DSH 凭据可以由 Controller 在创建 MicroVM 时注入，不写入公共 Registry；
- 已实现 Harness 发现内部 Agent 后的候选登记、后台 Canary 测试和合格后准入；
- 前台任务通道与后台测试通道隔离，后台测试不阻塞已有 Harness 的前台执行；
- 已完成一次 ECS + CubeSandbox + DSH 的端到端 Python 测试：4 个 `unittest` 用例全部通过，任务成功后 MicroVM 被销毁并补充新的预热实例；
- 核心 Router、可靠性、HTTP 接口和 Harness Runtime 测试已经具备。

### 3.3 当前结果的正确表述

目前已经完成的是**可运行的系统骨架和端到端执行验证**，还不能把当前结果表述为“世界模型已经带来显著收益”或“主备 Agent Team 已完成大规模实验”。后两项应作为下一阶段的研究实现与实验目标。

## 4. 拟提出的方法

### 4.1 约束优先的 Harness 路由

先进行不可违反的硬约束过滤，再进行软目标评分：

```text
候选 Harness
   ↓
平台 / 工具 / 能力 / 硬件 / 权限硬约束过滤
   ↓
成功率、质量、延迟、成本、负载、风险评分
   ↓
主 Harness + 可选副 Harness
```

基础评分可以写成：

\[
S(h\mid T)=w_q Q(h,T)-w_l L(h,T)-w_c C(h,T)-w_r R(h,T)+w_a A(h,T)
\]

其中：

- `Q`：预测质量或成功概率；
- `L`：预测延迟；
- `C`：资源或调用成本；
- `R`：掉线、超时、资源不足等风险；
- `A`：当前可用性和负载余量。

### 4.2 新 Agent 的后台准入

Harness 发现新 Agent 后，不直接改变前台路由结果，而是进入候选池：

```text
Harness 发现新 Agent
        ↓
Registry 标记 testing
        ↓
后台 Canary / Trace Replay / Shadow Task
        ↓
达到准入阈值？
   ├── 否：rejected 或继续观察
   └── 是：eligible，更新 Harness 能力摘要
```

全局 Router 只看到 Harness 的聚合能力，避免把不稳定的单个新 Agent 直接暴露给用户任务。

### 4.3 Trace Replay 辅助测试

对于新 Agent，可以根据一致性哈希从 Trace Store 选择与其能力、任务类型和版本匹配的历史轨迹，经过脱敏后在独立 MicroVM 中重放：

```text
历史任务轨迹
   ↓  脱敏 / 去除原始 CoT、API Key、隐私数据
一致性哈希(task_family, capability, schema_version)
   ↓
新 Agent 后台 Shadow Replay
   ↓
与基线结果比较，结合 Canary 和真实小流量测试
   ↓
更新准入分数与失败原因
```

Trace Replay 只用于评估和预测，不应直接把原始用户数据或未脱敏内部推理过程复制给新 Agent。

### 4.4 世界模型的定位

世界模型不作为一个独立的、面向用户的 Agent，而是蒸馏并整合进 Task Parser / Planner，用于预测：

- 任务更适合哪些能力组合；
- 某个 Harness 在当前负载和网络状态下的成功概率；
- 预期延迟、失败类型和资源消耗；
- 新 Agent 经过少量测试后是否值得加入前台候选池；
- 是否值得为任务付出主备冗余成本。

世界模型先离线训练和评估，再接入在线调度，避免未经验证的模型直接控制生产执行。

### 4.5 主备 Agent Team 与一致性哈希

对于预测风险较高、失败代价较大的任务：

```text
World Model 选择主 Team
        ├── 主 Team：正常执行
        └── 副 Team：接收必要的输入摘要 / 版本信息，等待接管
                         ↓
               主 Team 超时或失败
                         ↓
               副 Team 使用相同任务版本接管
```

实现上需要使用任务版本号、租约、幂等键和结果去重，避免主备同时提交互相冲突的结果。副 Team 不应默认复制全部工作，而应根据预测风险选择性启用。

## 5. 论文贡献点

论文最终应围绕以下三个贡献组织，而不是堆叠大量工程模块：

### Contribution 1：动态异构 Harness 调度模型

建立包含能力、资源、负载、可用性、历史表现和新 Agent 准入状态的任务调度模型，明确“任务—Harness—内部 Agent/Team”三层边界。

### Contribution 2：面向不确定执行结果的预测式调度

将历史 Trace、在线状态和实验反馈蒸馏进 Task Parser / World Model，使 Router 不仅依据静态 capability 匹配，还能根据成功概率、延迟和风险做结果感知决策。

### Contribution 3：动态准入与风险感知容错

提出“后台测试后准入”的新 Agent 生命周期，并结合 Trace Replay 和主备 Team 一致性哈希，在新 Agent 加入、旧 Agent 掉线和任务执行失败时提高系统鲁棒性。

## 6. 实验设计

### 6.1 实验平台

第一阶段使用单台 ECS 模拟多节点逻辑资源：

- Ubuntu 22.04，x86_64；
- CubeSandbox MicroVM；
- PVM Host Kernel + `/dev/kvm`；
- DSH 作为统一 Harness 运行接口；
- Controller、Registry、Router、Adapter 和 MicroVM Pool；
- 通过多个逻辑 Harness、不同插件集合、不同资源画像和随机扰动模拟异构网络。

后续若资源允许，再扩展到多台 ECS，以验证跨节点迁移、不同网络条件和真实资源竞争。

### 6.2 任务集合

构造覆盖不同能力需求的任务族：


| 任务族 | 主要需求 | 典型扰动 |
|---|---|---|
| Python / 编译 | Python、编译器、本地计算 | CPU 竞争、依赖缺失 |
| 文件与文档 | 本地文件、文件插件、文档生成 | 文件不可见、磁盘延迟 |
| 数据处理 | Python、结构化输出、数据插件 | 输入规模变化、内存不足 |
| 移动端任务 | 摄像头、传感器、iOS | 设备离线、网络延迟 |
| 混合能力任务 | 多个能力或高可靠要求 | 主 Harness 故障、主备切换 |

### 6.3 动态状态与故障注入

每个实验场景都应记录任务到达时间、候选 Harness 状态和实际执行结果，并随机注入：

- 心跳延迟或丢失；
- Harness 临时掉线；
- MicroVM 冷启动和创建失败；
- 网络延迟和请求超时；
- CPU / 内存负载升高；
- 新 Agent 中途上线；
- 新 Agent 初始能力声明不准确；
- 主 Team 执行中断；
- Trace Replay 与真实任务分布不一致。

### 6.4 对比方法

至少包含以下基线：

1. Random：从可用 Harness 中随机选择；
2. Capability Rule：只做静态能力匹配；
3. Load-aware：能力匹配后选择当前负载最低者；
4. Historical Score：根据历史成功率和平均延迟选择；
5. Parser without World Model：使用任务解析和规则，但不做结果预测；
6. World Model without Trace Replay：有预测，但新 Agent 不使用历史轨迹测试；
7. World Model without Backup：有预测，但不启用主备 Team；
8. Full：完整系统。

### 6.5 评价指标

| 类别 | 指标 |
|---|---|
| 任务结果 | success rate、quality score、failure reason accuracy |
| 性能 | 平均延迟、P50/P95 延迟、排队时间、MicroVM 冷启动时间 |
| 资源 | CPU / 内存使用、MicroVM 数量、单位任务成本 |
| 调度 | routing regret、错误路由率、候选过滤准确率 |
| 新 Agent 准入 | time-to-eligibility、测试开销、误准入率、误拒绝率 |
| 鲁棒性 | 掉线恢复时间、主备切换时间、任务最终成功率 |
| 一致性 | 重复执行率、冲突结果率、幂等失败率 |
| 预测 | 成功概率校准误差、延迟预测误差、风险排序质量 |

### 6.6 关键实验矩阵

#### 实验 A：静态异构路由

验证 Router 能否在平台、工具、能力和硬件约束下选出可执行 Harness。

#### 实验 B：动态负载路由

固定任务分布，改变 Harness 负载、延迟和心跳状态，验证状态感知路由是否优于静态规则。

#### 实验 C：新 Agent 后台准入

让 Harness 在任务流运行过程中发现新 Agent，比较“立即纳入”和“后台测试后纳入”两种策略的误准入率、任务成功率和准入时间。

#### 实验 D：Trace Replay 的测试效率

比较无历史测试、随机测试、一致性哈希 Trace Replay、Trace Replay + Canary 四种策略，评估达到准入阈值所需的测试成本与泛化能力。

#### 实验 E：世界模型调度

比较规则、历史分数和世界模型预测在动态负载、故障和任务分布变化下的成功率、延迟和调度 regret。

#### 实验 F：主备 Team 容错

注入主 Team 掉线、超时和结果冲突，比较无备份、静态双副本和风险感知主备策略的恢复时间、重复计算和最终成功率。

#### 实验 G：联合消融

逐步加入后台准入、Trace Replay、世界模型和主备 Team，展示各模块的边际收益与成本。

## 7. 消融实验与科学问题

重点回答以下问题：

1. 动态状态是否比静态 capability 更能解释任务结果？
2. 世界模型是否能降低错误路由和尾部延迟？
3. 新 Agent 的后台测试能否降低立即纳入带来的失败？
4. Trace Replay 是否比随机 Canary 更快发现能力边界？
5. 一致性哈希是否能在不显著增加重复计算的情况下缩短故障恢复时间？
6. 哪些任务值得启用副 Team，冗余成本与可靠性收益如何权衡？
7. 当任务分布发生变化时，世界模型是否会失效，如何检测和更新？

消融项包括：

- 去掉 World Model；
- 去掉实时负载和心跳状态；
- 去掉后台准入；
- 去掉 Trace Replay；
- 去掉一致性哈希；
- 固定副 Team 与风险感知副 Team 对比；
- 不输出结构化失败原因；
- `K=1`、`K=2` 和选择性冗余对比。

## 8. 倒排计划

以下日期是基于当前时间和“2027 年 1 月下旬投稿”的暂定安排。若 ICML 2027 官方日期提前，应整体前移；ICML 2027 的正式日期尚未发布。

### 阶段 0：问题冻结与实验协议（9 月 11 日—9 月 21 日）

- 冻结论文题目、研究问题和边界；
- 确定 Harness-level routing 的术语和数据结构；
- 固定任务 Schema、Trace Schema、失败类型和指标；
- 固定实验日志格式、随机种子和配置版本；
- 明确当前工程结果与未来方法结果的区别。

**交付物**：问题定义、系统架构图、实验协议、指标说明。

### 阶段 1：系统与基准完善（9 月 22 日—10 月 5 日）

- 将至少 3 个 DSH Harness 接入 ECS；
- 完善 Registry、Controller、Adapter 和 MicroVM Pool 的日志；
- 完成前台任务与后台测试任务隔离；
- 完成新 Agent 发现、候选状态机和准入阈值；
- 建立 Trace Store、脱敏和 Replay 接口；
- 加入可重复的延迟、掉线、负载和 MicroVM 故障注入。

**交付物**：可重复运行的实验脚本、基准任务集、故障注入模块。

### 阶段 2：基线实验（10 月 6 日—10 月 26 日）

- 完成 Random、Capability Rule、Load-aware、Historical Score 基线；
- 运行静态异构路由和动态负载路由；
- 获取第一版成功率、延迟、成本和资源曲线；
- 先不宣称世界模型收益，确认数据采集链路可靠。

**阶段门槛**：如果基线数据不可复现，暂停模型开发，先修复日志和实验控制。

### 阶段 3：Trace Replay 与新 Agent 准入（10 月 27 日—11 月 9 日）

- 收集并脱敏历史 Trace；
- 实现一致性哈希分桶和 Shadow Replay；
- 比较立即纳入、随机 Canary、Trace Replay 和组合策略；
- 估计测试数量、准入时间和误准入率之间的权衡。

**交付物**：新 Agent 准入实验结果和 Trace Replay 消融结果。

### 阶段 4：世界模型与预测式调度（11 月 10 日—11 月 30 日）

- 建立任务特征、Harness 状态、历史结果和故障标签数据集；
- 先训练离线成功率、延迟和风险预测器；
- 将蒸馏后的模型接入 Task Parser / Planner；
- 对比无模型、历史分数和世界模型路由；
- 评估校准误差、分布外任务和模型更新策略。

**阶段门槛**：世界模型至少需要在一个主要指标上稳定优于规则或历史分数基线，否则降级为分析组件，不强行作为论文核心贡献。

### 阶段 5：主备 Team 与容错（12 月 1 日—12 月 14 日）

- 实现主 Team / 副 Team 的任务版本、租约和幂等机制；
- 实现一致性哈希映射和快速接管；
- 注入主 Team 超时、掉线、结果冲突和网络延迟；
- 比较无备份、静态备份和风险感知备份。

**交付物**：故障恢复曲线、重复计算开销和一致性验证结果。

### 阶段 6：主实验与规模扩展（12 月 15 日—12 月 28 日）

- 固定完整系统和所有基线版本；
- 每个场景运行足够重复次数并报告置信区间；
- 扩展 Harness 数量、任务并发度和故障强度；
- 生成主表、主图、系统开销图和失败案例。

### 阶段 7：消融、泛化与论文初稿（12 月 29 日—2027 年 1 月 10 日）

- 完成模块消融、任务分布变化和新 Agent 分布外测试；
- 分析世界模型失效案例和失败原因；
- 写完方法、实验、局限性和可复现性说明；
- 形成论文 v1 和补充材料 v1。

### 阶段 8：内部评审与投稿冻结（2027 年 1 月 11 日—1 月 20 日）

- 内部评审重点检查：贡献是否集中、实验是否支持主张、基线是否公平；
- 复核所有统计显著性、随机种子和日志；
- 删除无法被实验支持的过强表述；
- 冻结代码、配置、数据处理脚本和匿名材料；
- 按官方最终日期提交。

## 9. 论文结构建议

### 1. Introduction

说明异构 Harness 网络中的动态状态、新 Agent 加入和故障导致静态路由失效，提出结果感知调度问题。

### 2. Problem Formulation and System Model

定义任务、Harness、内部 Agent/Team、状态、Trace、执行结果和调度目标，明确全局 Router 不暴露 Harness 内部协作。

### 3. Method

介绍约束优先路由、后台准入、Trace Replay、世界模型预测和选择性主备 Team。

### 4. Experimental Platform and Benchmark

介绍 ECS、CubeSandbox、MicroVM、DSH Harness、任务族、动态扰动和可复现实验协议。

### 5. Results

依次报告静态路由、动态负载、新 Agent 准入、世界模型和主备容错结果。

### 6. Analysis and Limitations

分析预测失效、Trace 分布偏移、MicroVM 成本、主备冗余开销、真实多节点扩展限制和隐私保护问题。

### 7. Conclusion

总结动态异构 Harness 调度的核心发现，说明该框架如何为未来跨 Harness Agent Team 协作提供基础。

## 10. 风险控制与备选投稿策略

### 风险 1：世界模型收益不稳定

如果世界模型没有稳定超过 Load-aware 或 Historical Score，则把论文主线收敛为“动态新 Agent 准入 + 结果感知调度 + 故障容错”，世界模型作为增强模块和分析实验。

### 风险 2：主备 Team 成本过高

不默认复制全部任务，只对高风险、高价值或不可重试任务启用副 Team，并报告冗余计算成本。

### 风险 3：Trace Replay 泛化不足

将 Replay 定位为后台筛选工具，而不是最终性能保证；继续保留真实 Canary 和小流量 Shadow Task。

### 风险 4：单 ECS 规模有限

论文中明确区分“逻辑多 Harness 并发实验”和“真实多物理节点实验”，不能把逻辑隔离直接宣称为完整集群结论。

### 风险 5：ICML 2027 日期提前

一旦官方公布日期，优先保证问题定义、基线、主实验和论文初稿；世界模型或主备 Team 的扩展实验可以放入补充材料或删减为未来工作。

## 11. 当前最优先的下一步

1. 把现有成功的 ECS + CubeSandbox + DSH 运行过程整理成固定实验脚本；
2. 为 3 个逻辑 DSH Harness 增加可控的延迟、掉线、负载和成功率参数；
3. 固定 Trace Schema、脱敏规则和 Replay 数据划分；
4. 完成“立即纳入新 Agent”与“后台测试后纳入”的第一组对照实验；
5. 建立第一版基线结果，暂时不加入复杂世界模型；
6. 基于真实日志决定世界模型预测的标签、输入特征和更新频率；
7. 在完成基线后，再实现主备 Team 的一致性哈希和故障接管。

## 12. 一句话版本

本文要证明的不是“一个 Router 能否把任务分给不同机器”，而是：

> 在 Harness 内部结构不可见、Agent 状态动态变化、新 Agent 不断加入且执行可能失败的异构环境中，后台准入、结果预测和选择性主备调度能否让任务分配更准确、更快、更鲁棒。
