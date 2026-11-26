# Benchmark & Observability Report — Day 14

> Mọi con số lấy từ `reports/*.json`, `benchmark-results/*_stats.csv`, Prometheus API và Jaeger API trong lúc chạy.
> Không có số ước lượng; chỗ nào là phép tính từ số đo thì ghi rõ công thức.

**Họ tên:** Trần Thanh Vinh
**Ngày chạy:** 26–27/11/2026 (khoảng 23:58 → 00:47, GMT+7)
**Môi trường:** Windows 11, 16 logical CPU / 7.8 GB RAM, Docker Desktop 28.5.1 (WSL2 backend). Bước 1 (`develop/`) chạy bằng Python 3.12.3 trong WSL2 Ubuntu 24.04; load test và agent chạy trên cùng máy. Bước 2 dùng stack `production/` với `docker compose -p k5-day14-observability`, mỗi agent giới hạn 0.5 CPU / 256 MB.

---

## 1. SLO của agent

| SLI | SLO | Lý do chọn ngưỡng này |
|---|---|---|
| p95 latency `/ask` | < 500 ms | Ở tải bình thường (c ≤ 50) p95 đo được 221–237 ms, nên 500 ms còn dư ~2× cho jitter mà vẫn đủ nhanh cho chat. Đặt theo p95 chứ không theo avg vì avg che tail latency (xem 2.2). Alert `AgentHighLatencyP95` dùng ngưỡng lỏng hơn (1 s trong 2 phút) để không báo động vì spike ngắn. |
| Error rate `/ask` | < 2 % | Mock LLM có ~1 % lỗi upstream (`MOCK_ERROR_RATE=0.01`); đo được 0.67–1.0 % ở c ≤ 50. 2 % cho error budget gấp đôi mức nền. Alert ở 5 % trong 1 phút. |
| Accuracy trên golden dataset | ≥ 85 % (CI), mục tiêu 100 % | 85 % là ngưỡng chặn merge trong `.github/workflows/eval.yml`; cộng điều kiện **không regression** so với baseline. Sau khi sửa KB, v2 đạt 23/23 nên lần release này được chạy gate với `--min-accuracy 1.0`. |

---

## 2. Benchmark hiệu năng (develop, `load_test.py`)

### 2.1 Concurrency vs latency (agent v2, `--requests 300`, `MOCK_SLOW_RATE=0.03` mặc định)

| Concurrency | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Error rate | SLO |
|---|---|---|---|---|---|---|
| 1 | 4.26 | 196.1 | 228.2 | 1717.3 | 0.67 % | ✅ PASS |
| 10 | 40.11 | 194.3 | 221.2 | 1706.4 | 0.67 % | ✅ PASS |
| 50 | 105.29 | 193.3 | 237.4 | 1713.9 | 1.00 % | ✅ PASS |
| 100 | 80.06 | 380.3 | 2410.5 | 2615.8 | 2.00 % | ❌ FAIL (p95) |

Nguồn: `reports/load_v2_c1.json`, `load_v2_c10.json`, `load_v2_c50.json`, `load_v2_c100.json`.

**Nhận xét:** RPS tăng gần tuyến tính từ 1 → 10 (4.26 → 40.11, ×9.4) vì latency gần như không đổi (~195 ms), đúng định luật Little: `RPS ≈ concurrency / latency` (10 / 0.247 s ≈ 40). Từ 10 → 50 RPS chỉ tăng ×2.6 thay vì ×5: mỗi worker của load test chỉ gửi 6 request, nên tổng thời gian 2.85 s bị chi phối bởi vài request chậm 1.7 s ở cuối (tail kéo dài wall-clock), chưa phải server bão hoà (p50/p95 vẫn ~ như c=10). Ở 100, agent bão hoà: một process uvicorn duy nhất phải parse JSON, chạy middleware, ghi log cho 100 request cùng lúc, cộng thêm load test chạy cùng máy, nên request phải xếp hàng trên event loop. p50 tăng gấp đôi (380 ms), p95 lên 2.4 s, RPS **giảm** còn 80. Điểm bão hoà nằm khoảng 50–100 concurrent; muốn hơn thì scale ngang (thêm instance), không phải tăng concurrency.

### 2.2 Tail latency (`--requests 300 --concurrency 20`)

| `MOCK_SLOW_RATE` | avg (ms) | p95 (ms) | p99 (ms) |
|---|---|---|---|
| 0 | 192.2 | 222.0 | 232.3 |
| 0.03 (mặc định) | 242.5 | 224.0 | 1709.7 |
| 0.1 | 382.6 | 1700.0 | 1715.5 |

Nguồn: `reports/load_v2_slow_0.json`, `load_v2_slow_0.03.json`, `load_v2_slow_0.1.json`.

**Nhận xét:** Từ 0 → 0.03, avg chỉ tăng 50 ms (+26 %) và p95 gần như đứng yên (222 → 224 ms), nhưng **p99 tăng 7.4 lần** (232 → 1710 ms): 3 % request chậm nằm hoàn toàn trên ngưỡng p97, nên chỉ p99 "nhìn thấy". Ở 0.1 (10 % > 5 %) thì p95 cũng nhảy lên 1700 ms, còn avg mới 383 ms. **p99 nhạy nhất** với request chậm, sau đó là p95; avg nhạy kém nhất vì 97 request nhanh "pha loãng" 3 request chậm. SLO không nên đặt theo average vì avg 242 ms trông rất ổn trong khi cứ 100 user thì 3 người phải chờ ~1.7 s. SLO theo percentile nói trực tiếp "x % người dùng nhận được trải nghiệm tốt hơn ngưỡng". Cũng lưu ý RPS tụt theo slow rate (100.9 → 65.3 → 45.3) vì worker của load test bị giữ lâu hơn ở các request chậm.

---

## 3. Benchmark chất lượng (develop, `eval/run_eval.py`)

| Chỉ số | Agent v1 | Agent v2 | Agent v2 (sau khi sửa KB) |
|---|---|---|---|
| Accuracy | 4.3 % (1/23) | 91.3 % (21/23) | 100 % (23/23) |
| Avg tokens / câu | 57.3 | 109.1 | 116.4 |
| Tổng cost (USD) | 0.000407 | 0.000762 | 0.000814 |
| Cost / câu đúng (USD) | 0.00040695 | 0.00003627 | 0.00003541 |
| Quality gate | FAIL (accuracy < 80 %) | PASS (≥ 80 %, 0 regression so với v1) | PASS (`--min-accuracy 1.0`, 0 regression so với v2) |

Nguồn: `reports/eval_v1.json`, `reports/eval_v2.json`, `reports/eval_v2_fixed.json`.

**Những câu v2 trả lời sai ban đầu và cách sửa:**

| ID | Keyword bị thiếu | Bạn đã sửa gì |
|---|---|---|
| security-02 ("JWT khác gì API key trong xác thực?") | `token`, `hết hạn`, `chữ ký` | KB không có tài liệu nào khớp câu hỏi, nên v2 rơi về câu trả lời chung chung. Thêm entry `(["jwt", "json web token", "xác thực"], ...)` vào `KNOWLEDGE_BASE` trong `develop/utils/mock_llm.py`: giải thích API key là chuỗi cố định; JWT là token có chữ ký số, mang user/role và thời điểm hết hạn (exp), server xác minh chữ ký mà không tra DB. |
| scaling-03 ("Kubernetes HPA scale dựa trên những chỉ số nào?") | `cpu`, `memory`, `custom metrics` | Thêm entry `(["hpa", "kubernetes", "autoscal"], ...)`: HPA tăng giảm số pod theo CPU, memory hoặc custom metrics (RPS, độ dài hàng đợi). |

Chọn keyword kích hoạt không trùng câu khác (không dùng `token` hay `scale`, vì `cost-01` và `scaling-01` cũng chứa các từ đó), nên retrieval top-1 của 21 câu cũ không đổi. Kết quả: `Câu được cải thiện: 2`, `Câu bị regression: 0`. Cùng thay đổi được chép sang `production/utils/mock_llm.py` để stack Docker cũng trả lời đúng.

**Nhận xét về trade-off chất lượng / chi phí / latency giữa v1 và v2:** v2 thêm bước retrieval nên prompt dài hơn: token mỗi câu tăng ×1.9 (57.3 → 109.1), tổng cost tăng ×1.87, p95 latency của eval tăng ~30 ms (193.6 → 223.3 ms, gồm ~20 ms retrieval và thời gian xử lý prompt dài hơn). Đổi lại accuracy tăng từ 4.3 % lên 91.3 %, nên **cost trên mỗi câu đúng giảm 11.2 lần** (0.000407 → 0.0000363 USD). Nhìn "cost / câu" thì v2 có vẻ đắt gấp đôi; nhìn "cost / câu đúng" thì v2 rẻ hơn hẳn, vì tiền trả cho câu trả lời sai là tiền mất trắng. Sau khi sửa KB, cost / câu đúng còn giảm thêm (0.00003541).

Lưu ý: p95 của `eval_v2_fixed.json` là 1554.9 ms do 1 trong 23 câu rơi vào request chậm của mock (3 %). Với 23 mẫu, chỉ 1 request chậm là đủ đẩy p95 lên. Đây là lý do latency phải đo bằng load test (300 request), không đo bằng eval.

**Quality gate trong CI:** `run_eval.py` thoát với **exit code 1** khi accuracy < `--min-accuracy` hoặc có câu regression so với `--baseline`. Đã kiểm chứng: chạy eval v1 với `--baseline reports/eval_v2.json` ra `Câu bị regression: 20`, `Quality gate ... ❌ FAIL`, `exit=1`. File `.github/workflows/eval.yml` chạy agent v2 (`MOCK_ERROR_RATE=0` để gate không fail ngẫu nhiên vì lỗi upstream giả lập), rồi chạy `run_eval.py --min-accuracy 0.85 --baseline reports/eval_v2.json`. Exit code ≠ 0 làm step fail, job fail; bật branch protection "Require status checks to pass" cho job `quality-gate/eval` thì PR không merge được.

---

## 4. Observability stack (production)

> Dockerfile của `production/` có sẵn lỗi: user `agent` có home là `/app`, nên package copy vào `/home/agent/.local` không nằm trên `sys.path`, agent crash ngay khi start (`ModuleNotFoundError: No module named 'uvicorn'`) và restart liên tục. Đã sửa bằng `ENV PYTHONUSERBASE=/home/agent/.local` (commit `fix(part7): set PYTHONUSERBASE…`). Unit test alert rules: `promtool test rules /p/alert_rules_test.yml` → `SUCCESS`.

### 4.1 Screenshot

- [x] Dashboard Grafana khi đang có tải: [`screenshots/grafana-dashboard.png`](screenshots/grafana-dashboard.png). Khung "Last 15 minutes" chụp lúc ~00:29, thấy 2 đợt benchmark 100 user (scale_1 rồi scale_3) và lúc bắt đầu tải nền 30 user trên 2 instance (Traffic 15.4 req/s, Error rate 0.36 %, p95 294 ms, 2 instance up).
- [x] Một trace chậm trong Jaeger: [`screenshots/jaeger-slow-trace.png`](screenshots/jaeger-slow-trace.png) (2.5 s, span `chat mock-llm-small` chiếm gần trọn chiều dài).
- [x] Một trace lỗi trong Jaeger: [`screenshots/jaeger-error-trace.png`](screenshots/jaeger-error-trace.png) (`POST /ask` 503, `agent.run` và `chat mock-llm-small` có dấu lỗi). Event `exception` của span đọc qua Jaeger API (xem 4.3).
- [x] Trang Alerts của Prometheus với 2 alert cùng **firing**: [`screenshots/prometheus-alert-firing.png`](screenshots/prometheus-alert-firing.png) (`AgentHighErrorRate` + `AgentHighLatencyP95`, chụp lúc 00:34:31).

### Exercise 7.4 — đọc dashboard (30 user, 2 instance; Prometheus query lúc 00:29:59)

| Chỉ số | Giá trị |
|---|---|
| Traffic `/ask` | 19.96 req/s |
| Error rate | 0.81 % |
| p50 / p95 / p99 | 178.8 / 296.8 / 1814.2 ms |
| avg | 235.9 ms |
| Req/s theo instance | 172.19.0.7: 9.95 · 172.19.0.8: 10.01 |
| Tokens/s | input 1548.5 · output 817.1 |
| In-flight | 3 |

1. **p95 ≈ 297 ms, avg ≈ 236 ms.** avg lớn hơn p50 57 ms dù 95 % request ≤ 297 ms. Lý do là ~3 % request chậm 1.7–1.8 s (p99 = 1814 ms) kéo avg lên. Khoảng cách avg ↔ p95 nhỏ nhưng p95 ↔ p99 rất lớn: phân bố lệch phải, có một đuôi dài. Avg không cho biết đuôi đó dài bao nhiêu, còn p99 cho biết.
2. **Nginx chia đều:** 9.95 vs 10.01 req/s (chênh 0.6 %), đúng round-robin (`server agent:8000 resolve`, `keepalive`). Panel "Requests/s theo instance" là hai đường gần trùng nhau.
3. **Input tokens/s (1548) > output tokens/s (817), gấp ~1.9 lần:** input = system prompt + tài liệu retrieval (context) + câu hỏi, còn output chỉ là câu trả lời. Với RAG, context luôn làm prompt dài hơn câu trả lời. Vì vậy giá input token ảnh hưởng nhiều tới chi phí của agent v2.
4. **`curl http://localhost:8080/metrics` → `HTTP/1.1 404 Not Found` (Server: nginx/1.28.3).** `nginx.conf` có `location = /metrics { return 404; }`: metrics lộ thông tin nội bộ (route, lưu lượng, version, chi phí) nên không public ra internet. Prometheus vẫn scrape được vì nó gọi thẳng `agent:8000/metrics` trong network nội bộ `obs_net` (DNS service discovery), không đi qua Nginx.

### 4.2 Thí nghiệm "phá hệ thống" (tải nền: Locust 30 user, 2 instance)

Thời gian đo bằng script poll `GET /api/v1/alerts` mỗi 2 s, tính từ lúc chạy lệnh đổi cấu hình (`… docker compose -p k5-day14-observability up -d --scale agent=2 agent`).

| # | Thay đổi | Alert nào firing | Thời gian từ lúc đổi → firing | Panel nào phản ánh rõ nhất |
|---|---|---|---|---|
| A | `MOCK_ERROR_RATE=0.2` (00:30:28) | `AgentHighErrorRate` (error rate đo được 20.6 %) | pending sau 30 s, **firing sau 86 s** (00:31:54) | Error rate (stat đỏ), Requests/s theo status code (503 tăng), LLM calls theo kết quả |
| B | `MOCK_SLOW_RATE=0.3` (00:32:02, vẫn giữ A) | `AgentHighLatencyP95` (p50 197 ms, **p95 1884 ms**, p99 1981 ms, avg 539 ms) | pending sau 29 s, **firing sau 149 s** (00:34:31) | Latency /ask p50/p95/p99: p95 và p99 vọt lên ~1.9 s, avg tăng ít hơn |
| C | `LLM_MODEL=mock-llm-large` (00:35:10, bỏ A/B) | `LLMCostBurnRateHigh` (cost $1.73/h → $17.74/h lúc firing → $24.49/h sau 1 phút) | pending sau 26 s, **firing sau 145 s** (00:37:35) | Cost burn rate (USD/giờ), LLM latency p95 theo model, bảng Phiên bản đang chạy (model = large) |
| D | `docker pause k5-day14-observability-agent-1` (00:39:14) | `AgentInstanceDown` (instance 172.19.0.8:8000, `up == 0`) | pending sau 13 s, **firing sau 43 s** (00:39:57) | Instances up 2 → 1; Req/s theo instance. Nginx log có **28 dòng 504** trong 2 phút, nhưng error rate của agent chỉ 1.7 % (bình thường) |
| E | `AGENT_VERSION=v1` (00:40:13) | **Không có** alert SLO nào trong 3 phút theo dõi (p95 201 ms, error 1.07 %). `LLMCostBurnRateHigh` vẫn firing tới 00:43:43, nhưng đó là phần còn lại của thí nghiệm C trong cửa sổ `rate(...[5m])`, không do v1 gây ra | — | Retrieval hit ratio từ 100 % → không còn dữ liệu (v1 không retrieve); Tokens/s giảm (input 1134 → 730, output 598 → 399); bảng version đổi sang v1 |

Thêm 2 quan sát từ log trạng thái alert:

- Mỗi lần recreate container (A, B, C, D, E), `AgentInstanceDown` vào **pending** 4–10 s rồi về inactive vì container mới chưa kịp được scrape. Nhờ `for: 30s` nên không lần nào firing. Nếu `for: 0s` thì mỗi lần deploy sẽ có một cảnh báo giả.
- `LLMCostBurnRateHigh` cũng firing ở tải nền (00:26:43, sau đợt benchmark 100 user). Tính từ token rate 1 phút lúc 00:29:59 với 30 user: 1.5485 k input tok/s × $0.00015 + 0.8171 k output tok/s × $0.0006 = $0.000722/s ≈ **$2.60/giờ**, đã vượt ngưỡng $2/giờ. Ngưỡng này quá chặt cho mức tải 30 user. Production nên đặt theo budget thật (vd budget ngày / 24 × hệ số) để alert có ý nghĩa.

**Thí nghiệm E không làm alert nào firing. Bạn sẽ phát hiện loại sự cố này trong production bằng cách nào?**

Đổi sang v1 làm accuracy giảm từ 91 % xuống 4 %, nhưng request vẫn 200 và vẫn nhanh (thậm chí nhanh và rẻ hơn), nên mọi metric hạ tầng (latency, error, saturation) đều xanh. Cần giám sát **chất lượng**, không chỉ hạ tầng:
1. **Trước deploy:** quality gate eval trong CI (`run_eval.py --baseline`), chặn ngay PR làm accuracy giảm hoặc có regression (đã chứng minh: v1 so với baseline v2 → 20 regression, exit 1).
2. **Sau deploy:** chạy eval định kỳ trên production (synthetic probe gửi golden dataset tới URL thật), hoặc online eval: lấy mẫu ~1–5 % traffic thật, chấm bằng LLM-as-judge, xuất metric `agent_eval_accuracy` rồi alert khi giảm.
3. **Proxy metric có sẵn:** alert khi `agent_retrieval_total{result="hit"}` tỉ lệ giảm mạnh hoặc mất hẳn (panel Retrieval hit ratio), khi `agent_info{version}` đổi ngoài lịch release, hoặc khi output token/câu thay đổi bất thường.
4. **Tín hiệu từ user:** tỉ lệ thumbs-down / hỏi lại / bỏ cuộc, đo như một SLI chất lượng.

### 4.3 Debug bằng trace

Chọn trace chậm nhất trong 435 trace ≥ 1 s (Jaeger API, sắp xếp theo duration):

- `trace_id`: `10142aba5c160c760a0e39d37e689acf` (instance `400543041e75`, trong đợt benchmark scale_1, 100 user / 1 instance)
- Tổng thời gian: **2491.2 ms**
- Span chiếm nhiều thời gian nhất: **`chat mock-llm-small` (2458.3 ms, 98.7 %)**; `agent.retrieve_context` chỉ 25.8 ms (1.0 %); `agent.run` 2484.3 ms bao cả hai. Attributes của span LLM: `gen_ai.request.model=mock-llm-small`, `gen_ai.usage.input_tokens=74`, `gen_ai.usage.output_tokens=37`.
- Dòng log tương ứng (`docker compose -p k5-day14-observability logs agent | grep 10142aba5c160c760a0e39d37e689acf`):

```json
{"ts": "2026-11-26T17:23:54.184925+00:00", "level": "INFO", "service": "observable-agent", "instance": "400543041e75", "event": "llm_call", "trace_id": "10142aba5c160c760a0e39d37e689acf", "span_id": "3d3e0b74f11a9c14", "request_id": "66de1a2f30ed", "model": "mock-llm-small", "agent_version": "v2", "input_tokens": 74, "output_tokens": 37, "cost_usd": 3.3e-05, "llm_ms": 1686.0}
{"ts": "2026-11-26T17:23:54.188030+00:00", "level": "INFO", "service": "observable-agent", "instance": "400543041e75", "event": "http_request", "trace_id": "10142aba5c160c760a0e39d37e689acf", "span_id": "3d3e0b74f11a9c14", "request_id": "66de1a2f30ed", "method": "POST", "route": "/ask", "status": 200, "ms": 2488.5}
```

Kết luận: request chậm do **bước gọi LLM**, không do code của agent hay retrieval. Log ghi `llm_ms=1686.0` (latency mock tự báo, gồm 1500 ms "slow" + base), nhưng span đo được 2458 ms. Phần chênh ~770 ms là thời gian coroutine chờ được chạy lại trên event loop của một instance bị giới hạn 0.5 CPU khi đang chịu 100 user. Trace đo thời gian thật, còn số tự báo của dependency thì không thấy phần chờ này.

Trace lỗi: `023f3f3b9fdb6e575d919cafc83a9c6f`, 93.6 ms, `POST /ask` → **503**. Span `chat mock-llm-small` (72.0 ms) và `agent.run` có status ERROR `LLMError: Upstream LLM error: 503 Service Unavailable (mock)` và event `exception` (`exception.type=utils.mock_llm.LLMError`, `exception.message=Upstream LLM error: 503 Service Unavailable (mock)`, kèm stacktrace tới `app/main.py` dòng 153 `run_agent`). Log tương ứng:

```json
{"ts": "2026-11-26T17:26:39.439304+00:00", "level": "INFO", "service": "observable-agent", "instance": "be9ae2b35acc", "event": "llm_error", "trace_id": "023f3f3b9fdb6e575d919cafc83a9c6f", "span_id": "a0f67d3e81989e22", "request_id": "edf1097130dc", "error": "Upstream LLM error: 503 Service Unavailable (mock)"}
{"ts": "2026-11-26T17:26:39.439577+00:00", "level": "INFO", "service": "observable-agent", "instance": "be9ae2b35acc", "event": "http_request", "trace_id": "023f3f3b9fdb6e575d919cafc83a9c6f", "span_id": "a0f67d3e81989e22", "request_id": "edf1097130dc", "method": "POST", "route": "/ask", "status": 503, "ms": 93.2}
```

---

## 5. Benchmark scaling (production, Locust headless, 100 users, ramp 10/s, 2 phút)

Số liệu dòng `POST /ask` trong `benchmark-results/scale_1_stats.csv` và `scale_3_stats.csv`:

| Số instance | RPS | p50 (ms) | p95 (ms) | p99 (ms) | Failures |
|---|---|---|---|---|---|
| 1 | 63.75 | 200 | 320 | 1700 | 86 / 7045 (1.22 %) |
| 3 | 64.46 | 190 | 240 | 1700 | 63 / 7106 (0.89 %) |

(Dòng Aggregated, gồm cả `/health`: 70.08 vs 70.65 req/s, p95 290 vs 240 ms.) Mọi failure đều là `503` do lỗi upstream giả lập của mock LLM (`scale_*_failures.csv`), không có 502/504.

**Giải thích kết quả:** RPS **không tăng** khi scale 1 → 3 vì Locust là tải vòng kín (closed-loop): mỗi user gửi request, chờ trả lời, rồi nghỉ `between(0.5, 2)` s (trung bình 1.25 s). Với 100 user và ~0.25 s latency, trần lý thuyết là 100 / (1.25 + 0.25) ≈ 67 req/s, và cả hai lần đều đạt ~64–70 req/s. Throughput bị giới hạn bởi số user, không bởi server, nên thêm instance không tạo thêm request. Scale vẫn **cải thiện p95 (320 → 240 ms, −25 %) và giảm max (2494 → 1961 ms)**: với 1 instance (0.5 CPU) xử lý ~64 req/s, request phải chờ trên event loop; chia cho 3 instance thì hết xếp hàng. **p99 không đổi (1700 ms)** vì đó là request "slow" của LLM upstream (`MOCK_SLOW_RATE=0.03`, +1500 ms). Thêm instance không làm LLM nhanh hơn. Nút thắt: tail latency nằm ở **LLM provider**, throughput nằm ở **phía client/số user**; chỉ phần xếp hàng CPU mới được scale ngang giải quyết. Muốn thấy RPS tăng theo số instance phải tăng số user (hoặc giảm think-time) tới khi 1 instance bão hoà.

---

## 6. Kết luận

- 3 điều mình học được về benchmark & observability:
  1. **Average nói dối, percentile thì không.** Với 3 % request chậm, avg chỉ tăng 26 % còn p99 tăng 7.4 lần. SLO và alert phải đặt theo p95/p99, và cỡ mẫu phải đủ lớn (23 câu eval không đủ để nói về latency).
  2. **Hạ tầng xanh ≠ agent đúng.** Thí nghiệm E và bảng eval cho thấy một bản tệ hơn 20 lần về chất lượng vẫn qua mọi alert hạ tầng. Chất lượng cần cơ chế riêng: eval + quality gate dựa trên exit code trong CI, và online eval sau deploy. So sánh 2 phiên bản nên dùng "cost / câu đúng" chứ không phải "cost / câu".
  3. **Metrics → trace → log là một chuỗi.** Dashboard báo p95 tăng, Jaeger chỉ ra span LLM chiếm 98.7 %, `trace_id` dẫn tới đúng dòng log JSON. Trace còn lộ ra phần thời gian mà số tự báo (`llm_ms`) bỏ sót. Ngoài ra metric của một service chỉ thấy những gì service đó xử lý: 504 ở Nginx không có trong error rate của agent.
- Nếu đưa agent này lên production thật, mình sẽ thêm:
  - Metric/alert ở **tầng load balancer** (tỉ lệ 5xx/504 của Nginx) và **synthetic check** từ bên ngoài gọi `/ask` thật, để bắt lỗi kiểu thí nghiệm D.
  - **Burn-rate alert nhiều cửa sổ** cho error budget (vd 1h/5m và 6h/30m) thay vì ngưỡng tĩnh, và ngưỡng cost theo budget ngày thật thay cho $2/giờ.
  - Metric chất lượng: `agent_eval_accuracy` từ online eval (sample + LLM-as-judge), tỉ lệ retrieval hit, tỉ lệ feedback xấu; alert khi giảm so với baseline.
  - Alert **saturation** (`agent_in_flight_requests` cao kéo dài) để biết lúc cần scale trước khi p95 vỡ, và latency/lỗi tách theo `model` để phát hiện provider có vấn đề.
