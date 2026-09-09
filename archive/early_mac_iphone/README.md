# 早期 Mac/iPhone 实验归档

这里保存项目第一阶段的 Mac + iPhone 原型代码。该阶段的目标是验证“Controller 根据任务语义自动选择执行单元”，而不是手动指定 Mac 或 iPhone。

## 文件说明

- `phase1_mac_iphone.py`：早期 Controller，包含任务 API、注册表、轮询和故障恢复。
- `mac_agent.py`：Mac 端 HTTP Agent。
- `harness_runtime.py`：早期 Harness 内部 Agent 发现和 Agent Team 原型。
- `execution_units.json`：Mac/iPhone 执行单元配置。
- `MobileAgent/`、`ios_agent/`：iPhone Xcode 客户端及配套代码。
- `ContentView.swift`、`MobileAgentApp.swift`：早期 SwiftUI 入口文件备份。
- `data/`：早期 Mac 本地文件测试数据。
- `test_*.py`：对应的早期测试。

## 运行方式

这些文件依赖早期的目录结构，默认不再作为当前主线启动。归档目录用于阅读、对照和复现实验设计；若要实际运行旧版本，需要根据旧脚本补充项目根目录的 Python import 路径，并在旧的 Mac/iPhone 环境中配置网络地址。

当前主线已经统一为：`controller.py` + `execution_units_dsh.json` + `dsh_agent.py` + `microvm_pool.py`。早期实验的过程、结果和局限见 [`reports/early_mac_iphone_experiment.md`](../../reports/early_mac_iphone_experiment.md)。
