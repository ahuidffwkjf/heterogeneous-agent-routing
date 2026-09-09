import SwiftUI

struct MobileTask: Codable {
    let jobId: String
    let taskId: String
    let description: String
    let requiredCapabilities: [String]

    enum CodingKeys: String, CodingKey {
        case jobId = "job_id"
        case taskId = "task_id"
        case description
        case requiredCapabilities = "required_capabilities"
    }
}

struct MobileOutcome: Encodable {
    let success: Bool
    let quality: Double
    let latencyMs: Double
    let cost: Double
    let failureType: String?
    let executor: String

    enum CodingKeys: String, CodingKey {
        case success, quality
        case latencyMs = "latency_ms"
        case cost
        case failureType = "failure_type"
        case executor
    }
}

struct ContentView: View {
    @State private var serverURL = "http://192.168.1.20:8080"
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
        status = "轮询中"
        pollingTask = Task {
            while !Task.isCancelled {
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
            let (data, response) = try await URLSession.shared.data(from: url)
            guard let http = response as? HTTPURLResponse else { return }
            if http.statusCode == 204 {
                status = "等待任务"
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

    private func complete(_ task: MobileTask) async {
        guard let url = URL(string: "\(serverURL)/mobile/tasks/\(task.jobId)/result") else {
            return
        }
        var request = URLRequest(url: url)
        request.httpMethod = "POST"
        request.setValue("application/json", forHTTPHeaderField: "Content-Type")
        request.httpBody = try? JSONEncoder().encode(
            MobileOutcome(
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
