import Foundation
import SwiftUI

struct MobileTask: Codable {
    let jobId: String
    let leaseId: String
    let taskId: String
    let description: String
    let requiredCapabilities: [String]

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case leaseId = "lease_id"
        case taskId = "task_id"
        case description
        case requiredCapabilities = "required_capabilities"
    }
}

struct MobileOutcome: Encodable {
    let leaseId: String
    let success: Bool
    let quality: Double
    let latencyMs: Double
    let cost: Double
    let failureType: String?
    let executor: String

    enum CodingKeys: String, CodingKey {
        case success, quality
        case leaseId = "lease_id"
        case latencyMs = "latency_ms"
        case cost
        case failureType = "failure_type"
        case executor
    }
}

struct ContentView: View {
    @State private var serverURL = "http://192.168.3.81:8081"
    @State private var registryToken = ""
    @State private var status = "未连接"
    @State private var currentTask: MobileTask?
    @State private var polling = false
    @State private var pollingTask: Task<Void, Never>?

    var body: some View {
        NavigationStack {
            Form {
                Section("Mac Controller") {
                    TextField("局域网地址", text: $serverURL)
                        .textInputAutocapitalization(.never)
                        .keyboardType(.URL)
                    SecureField("Registry Token", text: $registryToken)
                    Text(status)
                    Button(polling ? "停止轮询" : "开始轮询") {
                        polling ? stopPolling() : startPolling()
                    }
                }

                Section("当前任务") {
                    if let currentTask {
                        Text(currentTask.taskId).font(.headline)
                        Text(currentTask.description)
                        Text(currentTask.requiredCapabilities.joined(separator: ", "))
                            .foregroundStyle(.secondary)
                        Button("标记任务完成") {
                            Task { await complete(currentTask) }
                        }
                    } else {
                        Text("暂无任务")
                            .foregroundStyle(.secondary)
                    }
                }
            }
            .navigationTitle("Mobile Agent")
        }
        .onDisappear { stopPolling() }
    }

    private func startPolling() {
        polling = true
        status = "正在注册"
        pollingTask = Task {
            await register()
            while !Task.isCancelled {
                await heartbeat()
                await pollOnce()
                try? await Task.sleep(for: .seconds(5))
            }
        }
    }

    private func stopPolling() {
        polling = false
        pollingTask?.cancel()
        pollingTask = nil
        status = "已停止"
    }

    private func pollOnce() async {
        guard let url = URL(string: "\(serverURL)/mobile/tasks/next?unit_id=iphone_agent_01") else {
            status = "地址无效"
            return
        }
        do {
            var request = URLRequest(url: url)
            request.setValue(registryToken, forHTTPHeaderField: "X-Registry-Token")
            let (data, response) = try await URLSession.shared.data(for: request)
            guard let http = response as? HTTPURLResponse else { return }
            if http.statusCode == 204 {
                if currentTask == nil {
                    status = "等待任务"
                }
                return
            }
            guard http.statusCode == 200 else {
                status = "拉取失败：HTTP \(http.statusCode)"
                return
            }
            currentTask = try JSONDecoder().decode(MobileTask.self, from: data)
            status = "收到任务"
        } catch {
            status = "连接失败：\(error.localizedDescription)"
        }
    }

    private func register() async {
        guard let url = URL(string: "\(serverURL)/registry/register") else {
            status = "地址无效"
            return
        }
        let payload: [String: Any] = [
            "unit_id": "iphone_agent_01",
            "unit_type": "single_agent",
            "platforms": ["ios"],
            "capabilities": ["mobile", "camera", "sensor", "image_inference"],
            "tools": ["ios_app"],
            "state": "idle",
            "load": 0.0,
            "success_rate": 0.88,
            "quality_score": 0.82,
            "avg_latency_ms": 1400,
            "cost_score": 0.10,
            "metadata": [
                "transport": "poll",
                "heartbeat_required": true,
                "harness_type": "ios_mobile",
                "scope": "harness",
                "internal_agents": [
                    [
                        "agent_id": "iphone_camera_agent",
                        "capabilities": ["mobile", "camera"],
                        "tools": ["ios_app"],
                        "quality_score": 0.84
                    ],
                    [
                        "agent_id": "iphone_sensor_agent",
                        "capabilities": ["mobile", "sensor"],
                        "tools": ["ios_app"],
                        "quality_score": 0.80
                    ]
                ],
                "hardware": ["platform": "ios", "gpu": false]
            ]
        ]
        await postJSON(url: url, payload: payload, successStatus: 201, successText: "轮询中")
    }

    private func heartbeat() async {
        guard let url = URL(string: "\(serverURL)/registry/heartbeat") else { return }
        let payload: [String: Any] = [
            "unit_id": "iphone_agent_01",
            "state": currentTask == nil ? "idle" : "busy",
            "load": currentTask == nil ? 0.0 : 1.0
        ]
        _ = await postJSON(url: url, payload: payload, successStatus: 200, successText: nil)
    }

    private func postJSON(
        url: URL,
        payload: [String: Any],
        successStatus: Int,
        successText: String?
    ) async -> Bool {
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.setValue(registryToken, forHTTPHeaderField: "X-Registry-Token")
        request.httpBody = try? JSONSerialization.data(withJSONObject: payload)
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            let ok = (response as? HTTPURLResponse)?.statusCode == successStatus
            if ok, let successText {
                status = successText
            } else if !ok {
                status = "注册/心跳失败"
            }
            return ok
        } catch {
            status = "连接失败：\(error.localizedDescription)"
            return false
        }
    }

    private func complete(_ task: MobileTask) async {
        guard let url = URL(string: "\(serverURL)/mobile/tasks/\(task.jobId)/result") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(
            MobileOutcome(
                leaseId: task.leaseId,
                success: true,
                quality: 0.80,
                latencyMs: 100,
                cost: 0.05,
                failureType: nil,
                executor: "iphone_agent_01"
            )
        )
        do {
            let (_, response) = try await URLSession.shared.data(for: request)
            if let http = response as? HTTPURLResponse, http.statusCode == 200 {
                currentTask = nil
                status = "任务已完成"
            }
        } catch {
            status = "结果回传失败：\(error.localizedDescription)"
        }
    }
}
