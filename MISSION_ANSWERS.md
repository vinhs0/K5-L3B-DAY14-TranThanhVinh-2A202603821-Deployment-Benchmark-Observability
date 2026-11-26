# Day 14 Lab — Mission Answers

> Môi trường chạy: Windows 11 + Docker Desktop 28.5.1 (WSL2 backend), Python 3.12 trong WSL2 Ubuntu 24.04
> (`python -c "import os; print(os.name)"` → `posix`). Mọi output bên dưới là output thật của các lệnh đã chạy.

---

## Part 1: Localhost vs Production

### Exercise 1.1: Anti-patterns trong `01-localhost-vs-production/develop/app.py`

| # | Anti-pattern | Dòng | Hậu quả |
|---|---|---|---|
| 1 | Hardcode secret: `OPENAI_API_KEY = "sk-hardcoded-fake-key-never-do-this"` | 17 | Push lên GitHub là lộ key, phải revoke; đổi key phải sửa code và deploy lại |
| 2 | Hardcode connection string có mật khẩu: `DATABASE_URL = "postgresql://admin:password123@..."` | 18 | Lộ mật khẩu DB, không đổi được DB theo môi trường |
| 3 | Config cứng trong code: `DEBUG = True`, `MAX_TOKENS = 500` | 21–22 | Không bật/tắt debug theo môi trường được, vi phạm 12-Factor (config in env) |
| 4 | Log bằng `print()` và **in luôn secret**: `print(f"[DEBUG] Using key: {OPENAI_API_KEY}")` | 33–34, 38 | Secret nằm trong log (log thường được gửi ra hệ thống khác); log text không parse/lọc được |
| 5 | Không có `/health`, `/ready` | 42 | Platform không biết app chết để restart, LB không biết khi nào ngừng route traffic |
| 6 | `host="localhost"` | 51 | Trong container chỉ nghe loopback → bên ngoài container không gọi được |
| 7 | `port=8000` cứng, không đọc `PORT` | 52 | Railway/Render cấp `PORT` động → app nghe sai cổng, healthcheck fail |
| 8 | `reload=True` | 53 | Chế độ dev: tốn tài nguyên theo dõi file, không dùng cho production |
| 9 | Không xử lý shutdown (không lifespan, không SIGTERM) | — | Deploy/scale down làm đứt request đang chạy |

### Exercise 1.2: Chạy bản develop

```text
$ curl -i -X POST "http://localhost:8000/ask?question=Hello"
HTTP/1.1 200 OK
server: uvicorn
content-type: application/json

{"answer":"Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé."}

$ curl -o /dev/null -w '%{http_code}' http://localhost:8000/health        → 404
$ curl ... -H 'Content-Type: application/json' -d '{"question":"Hello"}'   → 422
```

Log server cho thấy đúng anti-pattern số 4 — secret bị in ra log:

```text
[DEBUG] Got question: Hello
[DEBUG] Using key: sk-hardcoded-fake-key-never-do-this
[DEBUG] Response: Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé.
```

Nó chạy, nhưng chưa production-ready: không có health check (404), lộ secret trong log, chỉ nghe `localhost`.

### Exercise 1.3: So sánh develop vs production

Bản production chạy với `ENVIRONMENT=development PORT=8000 python app.py`:

```text
GET /health → 200 {"status":"ok","uptime_seconds":0.6,"version":"1.0.0","environment":"development","timestamp":"2026-11-26T16:58:08.346699+00:00"}
GET /ready  → 200 {"ready":true}
POST /ask (JSON) → 200 {"question":"Hello","answer":"Agent đang hoạt động tốt! ...","model":"gpt-4o-mini"}
kill -TERM <pid> → "Shutting down … Application shutdown complete", exit=0
```

| Feature | Develop | Production | Tại sao quan trọng? |
|---|---|---|---|
| Config | Hardcode trong `app.py` (key, DB URL, DEBUG, port) | `config.py` đọc env (`os.getenv`), `validate()` bắt buộc `AGENT_API_KEY` khi `ENVIRONMENT=production` | Cùng một image chạy được dev/staging/prod; secret không nằm trong git; thiếu config quan trọng thì fail fast ngay lúc start |
| Secret trong log | In thẳng API key | Chỉ log `question_length`, `client_ip`, không log secret | Log được ship đi nhiều nơi (Loki, Datadog); secret trong log = secret bị lộ |
| Health check | Không có (`/health` → 404) | `/health` (liveness, uptime) + `/ready` (readiness, 503 khi chưa sẵn sàng) | Platform restart container khi liveness fail; LB chỉ gửi traffic khi ready, tránh lỗi lúc khởi động/tắt |
| Logging | `print()` text | `logging` với format JSON | Log JSON parse/lọc/đếm được theo field (event, latency…) |
| Shutdown | Đột ngột (không lifespan) | `lifespan` + handler SIGTERM, uvicorn chờ request in-flight | Deploy/scale down không cắt ngang request của user |
| Host/Port | `localhost:8000` cứng | `0.0.0.0` + `PORT` từ env | Chạy được trong container và trên Railway/Render (cấp `PORT` động) |
| Reload | `reload=True` luôn bật | Chỉ khi `DEBUG=true` | Production không cần/không nên theo dõi file |
| CORS | Không cấu hình | `ALLOWED_ORIGINS` từ env | Giới hạn origin được gọi API từ trình duyệt |

Hai điều mình quan sát thêm khi chạy thật:

- Bản production *muốn* log JSON nhưng trong log không thấy dòng JSON nào của app, chỉ có `WARNING:root:OPENAI_API_KEY not set — using mock LLM`. Lý do: `settings.validate()` gọi `logging.warning()` lúc import `config`, việc này tự gọi `basicConfig()` với format mặc định, nên `logging.basicConfig(format='{"time":...}')` ở `app.py` sau đó không còn tác dụng và logger `__main__` ở mức WARNING. Bài học: cấu hình logging phải chạy trước mọi lệnh log. Ngoài ra format `'..."msg":"%(message)s"'` không escape dấu `"` trong message, nên message là JSON (`json.dumps(...)`) sẽ tạo ra dòng JSON không hợp lệ. Vì vậy ở Part 6 mình tự viết `JsonFormatter` dùng `json.dumps` cho cả dòng.
- `signal.signal(SIGTERM, handle_sigterm)` ở cuối file bị uvicorn ghi đè khi server start, nên process vẫn tắt êm (exit 0). Nếu handler tự viết chạy *sau* uvicorn và không gọi lại handler cũ thì uvicorn sẽ không bao giờ tắt.

---

## Part 2: Docker

### Exercise 2.1: Câu hỏi về `02-docker/develop/Dockerfile`

1. **Base image:** `python:3.11` — bản Debian đầy đủ (có compiler, header, nhiều tool) nên image rất to.
2. **Working directory:** `/app` (`WORKDIR /app`), mọi lệnh `COPY`/`RUN`/`CMD` sau đó chạy tương đối từ đây.
3. **Vì sao `COPY requirements.txt` trước:** Docker cache theo layer. `requirements.txt` ít đổi, còn code đổi liên tục. Copy requirements → `pip install` → rồi mới `COPY` code thì khi chỉ sửa code, layer `pip install` vẫn dùng cache, build lại chỉ vài giây. Nếu `COPY . .` trước thì mỗi lần sửa một dòng code là cài lại toàn bộ dependencies.
4. **CMD vs ENTRYPOINT:** `CMD` là lệnh/tham số mặc định, bị thay thế khi chạy `docker run image <lệnh khác>`. `ENTRYPOINT` là chương trình cố định của container; tham số của `docker run` được nối vào sau nó (chỉ đổi được bằng `--entrypoint`). Hay dùng: `ENTRYPOINT` = executable, `CMD` = tham số mặc định. Dùng exec form (`["python","app.py"]`) để process là PID 1 và nhận SIGTERM trực tiếp.

### Exercise 2.2: Build và chạy bản develop

```text
$ docker build -f 02-docker/develop/Dockerfile -t my-agent:develop .
$ docker run -p 8000:8000 my-agent:develop
$ curl -i -X POST "http://localhost:8000/ask?question=What%20is%20Docker%3F"
HTTP/1.1 200 OK
{"answer":"Container là cách đóng gói app để chạy ở mọi nơi. Build once, run anywhere!"}
$ curl http://localhost:8000/health
{"status":"ok","uptime_seconds":1.0,"container":true}
```

### Exercise 2.3: Multi-stage build và kích thước image

- **Stage 1 (`builder`, `python:3.11-slim`)**: cài `gcc`, `libpq-dev` rồi `pip install --user -r requirements.txt` vào `/root/.local`. Stage này có compiler, apt cache… và **không** được deploy.
- **Stage 2 (`runtime`, `python:3.11-slim`)**: tạo user non-root `appuser`, chỉ `COPY --from=builder /root/.local` (site-packages đã cài) + `main.py` + `utils/`, có `HEALTHCHECK`, chạy bằng `USER appuser`.
- **Vì sao nhỏ hơn:** image cuối chỉ chứa các layer của stage cuối. Base `slim` bỏ phần lớn package Debian; gcc, header, apt lists và cache pip nằm ở stage builder nên bị bỏ đi. `docker history my-agent:advanced` cho thấy phần thêm vào chỉ có layer `COPY /root/.local` = 40.8 MB.

Kích thước đo bằng `docker images my-agent` (Docker Desktop 28.5.1, containerd image store):

```text
REPOSITORY   TAG        IMAGE ID       CREATED          SIZE
my-agent     advanced   8acd086d99c3   2 seconds ago    240MB
my-agent     develop    94a0dca81f78   40 seconds ago   1.67GB
```

- Develop: **1670 MB** (1.67 GB)
- Production: **240 MB**
- Chênh lệch: (1670 − 240) / 1670 = **85.6 %** nhỏ hơn

(`docker image inspect -f '{{.Size}}'` báo kích thước nén: develop 426 319 895 B ≈ 407 MB, production 57 409 037 B ≈ 55 MB, tức nhỏ hơn 86 % — cùng kết luận.)

### Exercise 2.4: Docker Compose stack

```text
                      ┌───────────────────────────── network "internal" (bridge) ──────────────┐
 Client ──:80/:443──▶ │  nginx (nginx:alpine)                                                   │
                      │    │  proxy_pass http://agent_backend  (rate limit 10 r/s/IP, security  │
                      │    ▼                                    headers, /health không limit)   │
                      │  agent (build 02-docker/production/Dockerfile, uvicorn :8000, 2 workers)│
                      │    │ REDIS_URL=redis://redis:6379/0    │ QDRANT_URL=http://qdrant:6333   │
                      │    ▼                                    ▼                               │
                      │  redis (redis:7-alpine :6379)        qdrant (qdrant:v1.9.0 :6333)       │
                      │   volume redis_data                   volume qdrant_data                │
                      └─────────────────────────────────────────────────────────────────────────┘
```

- Service được start: `redis`, `qdrant` (chờ healthy) → `agent` → `nginx`. Chỉ nginx publish cổng ra host (80, 443); agent/redis/qdrant chỉ nằm trong network `internal`.
- Giao tiếp bằng tên service qua DNS của Docker (`agent:8000`, `redis:6379`, `qdrant:6333`). `depends_on: condition: service_healthy` đảm bảo agent chỉ start khi redis + qdrant đã healthy.

Kết quả chạy (`touch .env.local` rồi `docker compose -p k5-day14-docker up -d --build`):

```text
SERVICE   IMAGE                   STATUS                           PORTS
agent     k5-day14-docker-agent   Up 1 second (health: starting)   8000/tcp
nginx     nginx:alpine            Up Less than a second            0.0.0.0:80->80/tcp, 0.0.0.0:443->443/tcp
qdrant    qdrant/qdrant:v1.9.0    Up 8 seconds (healthy)           6333-6334/tcp
redis     redis:7-alpine          Up 8 seconds (healthy)           6379/tcp

$ curl -i http://localhost/health
HTTP/1.1 200 OK
Server: nginx
X-Frame-Options: DENY
X-Content-Type-Options: nosniff
{"status":"ok","uptime_seconds":1.1,"version":"2.0.0","timestamp":"2026-11-26T17:01:33.560424"}

$ curl -i http://localhost/ask -X POST -H "Content-Type: application/json" -d '{"question": "Explain microservices"}'
HTTP/1.1 200 OK
Server: nginx
{"answer":"Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận."}
```

Lần gọi đầu tiên ngay sau `up` trả `502 Bad Gateway` vì agent chưa start xong mà nginx không chờ agent healthy (`depends_on: - agent` dạng ngắn). Header `Server: nginx` + security headers chứng tỏ request đi qua Nginx.

---

## Part 3: Cloud Deployment

### Exercise 3.1 / 3.2: Deploy agent Part 6

<!-- DEPLOY: điền sau khi deploy xong -->
- Nền tảng chính: _(chưa deploy — xem `DEPLOYMENT.md`)_
- URL: _(chưa có)_
- Screenshot: [dashboard](screenshots/dashboard.png) · [running](screenshots/running.png) · [test](screenshots/test.png)

### So sánh `railway.toml` và `render.yaml` (của `06-lab-complete/`)

| | `railway.toml` | `render.yaml` |
|---|---|---|
| Định dạng / phạm vi | TOML, cấu hình **một service** (build + deploy) | YAML Blueprint, khai báo **cả hạ tầng**: web service + Key Value (Redis) trong một file |
| Build | `[build] builder = "DOCKERFILE"`, `dockerfilePath` | `runtime: docker`, `rootDir: 06-lab-complete`, `dockerfilePath` |
| Thư mục gốc | Chạy `railway up` ngay trong `06-lab-complete/` nên không cần khai báo | Render đọc repo từ gốc → phải khai báo `rootDir` và chọn Blueprint Path |
| Health check | `healthcheckPath = "/health"`, `healthcheckTimeout = 60` | `healthCheckPath: /health` |
| Restart | `restartPolicyType = "ON_FAILURE"`, tối đa 3 lần | Render tự restart, không cấu hình trong file |
| Port | Railway cấp `PORT`, Dockerfile nghe `${PORT:-8000}` | Giống vậy (Render cũng cấp `PORT`) |
| Biến môi trường / secret | **Không** khai báo trong file; đặt bằng `railway variables` / dashboard | Khai báo ngay trong file: giá trị thường (`ENVIRONMENT`, `RATE_LIMIT_PER_MINUTE`…), secret dùng `sync: false` (nhập tay trên dashboard) |
| Redis | Tạo Redis riêng trong project, đặt `REDIS_URL=${{Redis.REDIS_URL}}` trên dashboard | `fromService: {type: keyvalue, name: day14-redis, property: connectionString}` tự nối `REDIS_URL` |
| Region / plan | Chọn trên dashboard | `region: singapore`, `plan: free` trong file |

Vì sao khác: Railway coi file config là phần bổ sung cho từng service (hạ tầng như Redis, biến môi trường quản lý trên dashboard/CLI), còn Render Blueprint là Infrastructure-as-Code cho cả stack nên phải mô tả luôn Redis và cách nối biến môi trường giữa các service.

### Exercise 3.3: Pipeline Cloud Run (`03-cloud-deployment/production-cloud-run/`)

`cloudbuild.yaml` chạy 4 bước nối tiếp nhau bằng `waitFor`: (1) **test** — trong `python:3.11-slim` cài requirements rồi `pytest tests/`, fail là dừng pipeline; (2) **build** — `docker build` gắn 2 tag `:$COMMIT_SHA` và `:latest`, dùng `--cache-from :latest` để tận dụng layer cache; (3) **push** — đẩy mọi tag lên Container Registry `gcr.io/$PROJECT_ID/ai-agent`; (4) **deploy** — `gcloud run deploy` image theo đúng `$COMMIT_SHA` (deploy được truy vết tới commit, rollback dễ) ở `asia-southeast1`, min 1 / max 10 instance, 512Mi/1 CPU, timeout 60s, `OPENAI_API_KEY` lấy từ Secret Manager. `service.yaml` là bản khai báo Knative tương đương: autoscale 1–10 instance theo `containerConcurrency: 80`, `livenessProbe` gọi `/health`, `startupProbe` gọi `/ready`, secret `AGENT_API_KEY`/`OPENAI_API_KEY` đọc từ `secretKeyRef` thay vì ghi thẳng giá trị.

---

## Part 4: API Security

### Exercise 4.1: API key (`04-api-gateway/develop`, chạy `AGENT_API_KEY=secret-key-123 python app.py`)

- **Key được check ở đâu:** dependency `verify_api_key()` dùng `APIKeyHeader(name="X-API-Key", auto_error=False)`, gắn vào `/ask` bằng `Depends(verify_api_key)`.
- **Sai key thì sao:** thiếu header → **401** "Missing API key"; có header nhưng sai → **403** "Invalid API key." (Part 6 yêu cầu cả hai đều 401, nên ở `06-lab-complete/app/auth.py` mình trả 401 cho cả hai trường hợp và so sánh bằng `hmac.compare_digest`).
- **Rotate key:** key đọc từ env `AGENT_API_KEY`, nên đổi biến môi trường trên platform rồi restart/redeploy, không sửa code. Muốn rotate không downtime thì cho phép tạm 2 key (cũ + mới) trong một khoảng thời gian, client đổi xong mới bỏ key cũ.

```text
$ curl -i http://localhost:8000/ask -X POST -H "Content-Type: application/json" -d '{"question": "Hello"}'
HTTP/1.1 401 Unauthorized
{"detail":"Missing API key. Include header: X-API-Key: <your-key>"}

$ curl -i ... -H "X-API-Key: wrong" ...
HTTP/1.1 403 Forbidden
{"detail":"Invalid API key."}

$ curl -i ... -H "X-API-Key: secret-key-123" ...
HTTP/1.1 200 OK
{"question":"Hello","answer":"Tôi là AI agent được deploy lên cloud. Câu hỏi của bạn đã được nhận."}
```

### Exercise 4.2: JWT (`04-api-gateway/production`)

Flow trong `auth.py`: `POST /auth/token` kiểm tra username/password (`authenticate_user`) → `create_token()` ký payload `{sub, role, iat, exp=+60 phút}` bằng HS256 với `JWT_SECRET` → client gửi `Authorization: Bearer <token>` → `verify_token()` gọi `jwt.decode()` kiểm tra chữ ký + hạn; hết hạn → 401, sai chữ ký → 403. Server không cần tra DB mỗi request vì thông tin user/role nằm trong token đã ký.

```text
$ curl -X POST http://localhost:8000/auth/token -H "Content-Type: application/json" -d '{"username": "student", "password": "demo123"}'
{"access_token":"eyJ***REDACTED***","token_type":"bearer","expires_in_minutes":60,"hint":"Include in header: Authorization: Bearer eyJ***REDACTED***"}

$ curl -i http://localhost:8000/ask -X POST -H "Content-Type: application/json" -d '{"question": "Explain JWT"}'
HTTP/1.1 401 Unauthorized
www-authenticate: Bearer
{"detail":"Authentication required. Include: Authorization: Bearer <token>"}

$ curl -i http://localhost:8000/ask -X POST -H "Authorization: Bearer $TOKEN" -H "Content-Type: application/json" -d '{"question": "Explain JWT"}'
HTTP/1.1 200 OK
x-content-type-options: nosniff
x-frame-options: DENY
{"question":"Explain JWT","answer":"Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé.","usage":{"requests_remaining":9,"budget_remaining_usd":1.6e-05}}
```

### Exercise 4.3: Rate limiting

- **Thuật toán:** sliding window log — mỗi user có một `deque` timestamp; mỗi request xoá timestamp cũ hơn 60s rồi đếm.
- **Giới hạn:** user thường 10 request/60s (`rate_limiter_user`), admin 100 request/60s (`rate_limiter_admin`).
- **Bypass cho admin:** không có bypass hoàn toàn; `/ask` chọn limiter theo `role` trong JWT, nên user role `admin` (tài khoản `teacher`) được limiter 100/phút.
- Hạn chế: state nằm trong RAM của process → nhiều worker/instance thì mỗi nơi đếm riêng, restart là mất. Part 6 chuyển sang Redis sorted set.

Vòng lặp 20 request bằng token `student` (lần chạy thứ hai; xem ghi chú bên dưới):

```text
req  1 -> 200 (0.102096s)
req  2 -> 200 (0.102172s)
...
req 10 -> 200 (0.103885s)
req 11 -> 429 (0.002215s)
req 12 -> 429 (0.001275s)
...
req 20 -> 429 (0.001554s)

$ curl -i (request 21)
HTTP/1.1 429 Too Many Requests
x-ratelimit-limit: 10
x-ratelimit-remaining: 0
x-ratelimit-reset: 1795712740
retry-after: 59
{"detail":{"error":"Rate limit exceeded","limit":10,"window_seconds":60,"retry_after_seconds":59}}
```

Ghi chú: lần chạy đầu cả 20 request đều 200. Mỗi request lần đó mất khoảng 6 giây (chưa xác định được nguyên nhân; lần chạy lại với `curl` thường tới `localhost` chỉ ~0.1 s/request), 20 request trải dài hơn 2 phút nên trong mọi cửa sổ 60 giây chưa bao giờ có đủ 10 request. Đây đúng là hành vi của sliding window: giới hạn tính trên *tốc độ*, không phải tổng số.

### Exercise 4.4: Cost guard

Cách làm trong `04-api-gateway/production/cost_guard.py`: ước lượng token (`len(words) * 2`), tính `cost = input/1000 × $0.00015 + output/1000 × $0.0006`; trước khi gọi LLM `check_budget(user)` trả **402** nếu chi phí trong ngày của user ≥ $1, trả **503** nếu tổng toàn hệ thống ≥ $10, cảnh báo log khi dùng ≥ 80%. Sau khi gọi LLM, `record_usage()` cộng dồn. Điểm yếu: lưu trong dict RAM.

Bản mình làm cho Part 6 (`06-lab-complete/app/cost_guard.py`) lưu trong Redis để mọi worker/instance dùng chung:

```python
def _day_key() -> str:
    return "cost:" + datetime.now(timezone.utc).strftime("%Y-%m-%d")   # reset mỗi ngày UTC

def check_budget() -> None:            # gọi TRƯỚC LLM
    spent = float(r.get(_day_key()) or 0.0)
    if spent >= settings.daily_budget_usd:
        raise HTTPException(402, ...)

def record_cost(input_tokens, output_tokens) -> float:   # gọi SAU LLM
    cost = input_tokens/1000*PRICE_INPUT_PER_1K + output_tokens/1000*PRICE_OUTPUT_PER_1K
    pipe = r.pipeline(); pipe.incrbyfloat(key, cost); pipe.expire(key, 2*24*3600); pipe.execute()
```

- Key theo ngày (`cost:YYYY-MM-DD`) nên tự "reset" khi sang ngày mới, `EXPIRE` 2 ngày để Redis tự dọn. Muốn budget theo tháng thì đổi key thành `%Y-%m` và TTL ~32 ngày (như solution trong CODE_LAB).
- `INCRBYFLOAT` là atomic nên 2 worker cộng cùng lúc không mất số.
- Redis lỗi lúc `check_budget` → trả 503 (fail closed: không biết đã tiêu bao nhiêu thì không cho gọi LLM). Lỗi lúc `record_cost` thì bỏ qua vì câu trả lời đã có.
- **Rate limit bảo vệ tốc độ, cost guard bảo vệ tiền:** rate limit 10 req/phút vẫn cho phép 14 400 request/ngày; nếu prompt dài hoặc model đắt thì tiền vẫn cháy. Ngược lại, chỉ có cost guard thì một client có thể đốt hết budget trong vài giây và chặn mọi user khác. Cần cả hai.

---

## Part 5: Scaling & Reliability

### Exercise 5.1: Health checks (`05-scaling-reliability/develop`)

```text
$ curl http://localhost:8000/health
{"status":"ok","uptime_seconds":0.5,"version":"1.0.0","environment":"development","timestamp":"2026-11-26T17:05:07.228589+00:00","checks":{"memory":{"status":"ok","used_percent":32.9}}}
$ curl http://localhost:8000/ready
{"ready":true,"in_flight_requests":1}
```

- **Liveness (`/health`)**: "process còn sống không?". Fail → platform **restart** container. Không được phụ thuộc dependency bên ngoài (Redis chết mà restart agent thì cũng không chữa được, chỉ gây restart loop).
- **Readiness (`/ready`)**: "có nên gửi traffic vào instance này không?". Fail (503) → LB **ngừng route** traffic tới instance nhưng **không restart**. Dùng khi đang khởi động, đang shutdown, hoặc mất Redis/DB. Đây là lý do ở Part 6 `/health` luôn 200 còn `/ready` trả 503 khi Redis không kết nối được.

### Exercise 5.2: Graceful shutdown

Gửi request rồi `kill -TERM` server ngay sau 50ms:

```text
in-flight result: {"answer":"Agent đang hoạt động tốt! (mock response) Hỏi thêm câu hỏi đi nhé."} [HTTP 200, 2.803271s]
server exit code: 0
--- server log
INFO:     Shutting down
INFO:     127.0.0.1:59918 - "POST /ask?question=Long%20task HTTP/1.1" 200 OK
INFO:     Waiting for application shutdown.
2026-11-27 00:05:10,387 INFO 🔄 Graceful shutdown initiated...
2026-11-27 00:05:10,387 INFO ✅ Shutdown complete
INFO:     Application shutdown complete.
INFO:     Finished server process [3838]
2026-11-27 00:05:10,387 INFO Received signal 15 — uvicorn will handle graceful shutdown
```

Request đang chạy vẫn nhận 200, process thoát với exit code 0. Thứ tự log cho thấy uvicorn bắt SIGTERM trước ("Shutting down" = ngừng nhận kết nối mới), chờ request in-flight xong, chạy phần shutdown của `lifespan` (đóng kết nối, dọn dẹp), rồi mới thoát. Handler `handle_sigterm` tự đăng ký chỉ được gọi ở cuối (uvicorn khôi phục handler cũ rồi phát lại signal khi thoát), nên logic cleanup quan trọng phải đặt trong `lifespan`, không đặt trong signal handler.

### Exercise 5.3: Stateless design

`05-scaling-reliability/production/app.py` lưu session/history vào Redis (`setex session:<id>` với TTL 1 giờ), không giữ dict trong RAM. Khi chạy nhiều instance sau LB, request kế tiếp của cùng user có thể rơi vào instance khác; nếu state nằm trong RAM của instance trước thì instance mới "không biết" user. Đưa state ra Redis thì instance nào cũng đọc được, và instance có thể bị kill/restart/scale bất kỳ lúc nào mà không mất dữ liệu. Ở Part 6, rate limit và cost guard cũng nằm trong Redis vì cùng lý do (Dockerfile chạy 2 worker, script chấm restart agent rồi kiểm tra vẫn bị 429).

### Exercise 5.4: Load balancing (`docker compose -p k5-day14-scaling up -d --build --scale agent=3`)

```text
NAME                       STATUS                            PORTS
k5-day14-scaling-agent-1   Up 2 seconds (health: starting)   8000/tcp
k5-day14-scaling-agent-2   Up 4 seconds (health: starting)   8000/tcp
k5-day14-scaling-agent-3   Up 3 seconds (health: starting)   8000/tcp
k5-day14-scaling-nginx-1   Up 2 seconds                      0.0.0.0:8080->80/tcp
k5-day14-scaling-redis-1   Up 15 seconds (healthy)           6379/tcp
```

10 request `POST /chat` qua Nginx, header `X-Served-By`:

```text
172.19.0.4 → 172.19.0.3 → 172.19.0.4 → 172.19.0.5 → 172.19.0.3 → 172.19.0.4 → 172.19.0.5 → 172.19.0.3 → 172.19.0.4 → 172.19.0.5
```

Cả 3 instance đều nhận request (4/3/3), xoay vòng round-robin.

### Exercise 5.5: Test stateless

```text
$ python test_stateless.py
Session ID: 5351cc59-ba9a-455a-b0ef-0d65d67e5681
Request 1: [instance-4fd9a9]  Q: What is Docker?
Request 2: [instance-17dddb]  Q: Why do we need containers?
Request 3: [instance-1e53a1]  Q: What is Kubernetes?
Request 4: [instance-4fd9a9]  Q: How does load balancing work?
Request 5: [instance-17dddb]  Q: What is Redis used for?
Total requests: 5
Instances used: {'instance-4fd9a9', 'instance-1e53a1', 'instance-17dddb'}
✅ All requests served despite different instances!
--- Conversation History ---
Total messages: 10
✅ Session history preserved across all instances via Redis!
```

5 câu của cùng một session đi qua cả 3 instance, nhưng history vẫn đủ 10 message (5 user + 5 assistant) vì mọi instance đọc/ghi cùng key `session:<id>` trong Redis.

Thêm bước mà script không làm — tắt hẳn một instance giữa session:

```text
session=ddd241b7-4310-421e-b542-a043c227a494       (turn 1-2 trước khi tắt)
$ docker stop k5-day14-scaling-agent-2
served_by instance-17dddb turn 3 HTTP 200
served_by instance-1e53a1 turn 4 HTTP 200
served_by instance-17dddb turn 5 HTTP 200
served_by instance-1e53a1 turn 6 HTTP 200
history count: 10
```

Sau khi mất 1 instance, Nginx chuyển traffic sang 2 instance còn lại, không request nào lỗi và session vẫn tiếp tục.

---

## Part 6: Final Project — output `check_production_ready.py`

Chạy bản đầy đủ (có Docker) trong `06-lab-complete/`, Python trong WSL2 (`os.name == 'posix'`) để script gửi được SIGTERM:

```text
================================================================
  📋 Checklist Deliverable
     ✅ Dockerfile (multi-stage, < 500 MB)
     ✅ docker-compose.yml (agent + redis)
     ✅ .dockerignore
     ✅ Health check endpoint (GET /health)
     ✅ Readiness endpoint (GET /ready)
     ✅ API Key authentication
     ✅ Rate limiting
     ✅ Cost guard
     ✅ Config từ environment variables
     ✅ Structured logging
     ✅ Graceful shutdown
     ✅ Public URL ready (Railway / Render config)
----------------------------------------------------------------
  Result: 12/12 tiêu chí đạt · 43/43 checks passed
  🎉 PASS 12/12 — PRODUCTION READY! Deploy nào!
================================================================
```

Một vài dòng chi tiết từ cùng lần chạy: image `63 MB`; `429 sau 5 request thành công (RATE_LIMIT_PER_MINUTE=5)`; restart agent vẫn bị 429; `402 ở request thứ 1`; `17 dòng JSON`; SIGTERM lúc đang có request → `exit code 0`; `docker stop` → `exit code 0, 1.9s`.

Cách làm từng phần (code trong `06-lab-complete/app/`):

| File | Làm gì |
|---|---|
| `config.py` | `Settings` dataclass, mỗi field đọc env bằng `default_factory` (`PORT`, `ENVIRONMENT`, `LOG_LEVEL`, `AGENT_API_KEY`, `REDIS_URL`, `RATE_LIMIT_PER_MINUTE`, `DAILY_BUDGET_USD`); parse int/float có fallback. `AGENT_API_KEY` mặc định rỗng → mọi `/ask` bị 401 (fail closed), không có key mặc định trong code. |
| `auth.py` | `APIKeyHeader(name="X-API-Key", auto_error=False)`; thiếu/sai → `HTTPException(401)`, so sánh bằng `hmac.compare_digest`. |
| `redis_client.py` | `redis.from_url(...)` không kết nối lúc import, nên app vẫn start khi chưa có Redis. `redis_healthy()` ping, lỗi → False. Timeout 1 s. |
| `rate_limiter.py` | Sliding window 60 s bằng sorted set theo **hash** của API key: `ZREMRANGEBYSCORE` → `ZCARD` → vượt thì 429 + `Retry-After` (tính từ request cũ nhất) → `ZADD` + `EXPIRE`. Redis lỗi → 503. |
| `cost_guard.py` | Key `cost:YYYY-MM-DD`, `check_budget()` → 402 khi ≥ `DAILY_BUDGET_USD`, `record_cost()` dùng `INCRBYFLOAT` + `EXPIRE` (xem Part 4.4). |
| `main.py` | `JsonFormatter` (mỗi dòng là `json.dumps` của ts/lvl/logger/msg + field `extra`), middleware log mọi request kèm `request_id`; `/health` luôn 200; `/ready` 503 khi Redis chết; `/ask` = `Depends(verify_api_key)` → `check_rate_limit` → `check_budget` → LLM → `record_cost`; handler lỗi 422/500 trả JSON gọn, không lộ stack trace; `lifespan` log startup/shutdown, không tự đăng ký SIGTERM nên handler của uvicorn giữ nguyên. |
| `Dockerfile` | 2 stage `python:3.11-slim`; builder cài vào venv `/opt/venv`, runtime chỉ copy venv + `app/` + `utils/`, user `app` non-root, `HEALTHCHECK` bằng Python urllib, `CMD ["sh","-c","exec uvicorn ... --port ${PORT:-8000} --workers 2"]` để uvicorn là PID 1. |
| `docker-compose.yml` | `agent` (build `.`, env_file `.env` `required: false`, `REDIS_URL=redis://redis:6379/0`, `AGENT_API_KEY`/limit lấy từ shell hoặc `.env`, `depends_on: redis: service_healthy`) + `redis:7-alpine` có healthcheck và volume. |
| `railway.toml` / `render.yaml` | Builder `DOCKERFILE`, `healthcheckPath = "/health"`; Render: `runtime: docker`, `rootDir`, `AGENT_API_KEY` `sync: false`, `REDIS_URL` lấy từ Key Value bằng `fromService`. |

Test tay với `AGENT_API_KEY=local-test-key docker compose -p k5-day14-final up -d --build`:

```text
POST /ask không key           → 401 {"detail":"Invalid or missing API key"}
POST /ask key sai             → 401 {"detail":"Invalid or missing API key"}
POST /ask key đúng            → 200 {"question":"What is deployment?","answer":"Deployment là quá trình đưa code ...","usage":{"input_tokens":4,"output_tokens":19,"cost_usd":1.2e-05}}
POST /ask {"question":""}     → 422 {"error":"invalid_request","details":[{"field":"question","msg":"String should have at least 1 character"}]}
POST /ask body "not json"     → 422 {"error":"invalid_request","details":[{"field":"0","msg":"JSON decode error"}]}
10 request liên tiếp          → 200 ×9, 429   (cộng request 200 ở trên = 10/phút)
                              → HTTP/1.1 429, retry-after: 58, {"detail":"Rate limit exceeded: 10 requests/minute"}
docker compose restart agent  → vẫn 429 (state nằm trong Redis)
docker compose stop redis     → /health 200, /ready 503 {"detail":"Redis unavailable"}
```

Lỗi mình tìm ra khi test tay (script chấm không bắt được): bản compose đầu tiên chỉ truyền `REDIS_URL`/`PORT` vào container nên `AGENT_API_KEY` export ở shell không tới được agent, mọi `/ask` đều 401. Đã sửa bằng `AGENT_API_KEY: ${AGENT_API_KEY:-}` (compose lấy giá trị từ shell hoặc `.env`, không ghi giá trị vào file), sau đó chạy lại script chấm vẫn `PASS 12/12`.

---

## Part 7: Benchmark & Observability

Số liệu, bảng và phân tích đầy đủ ở [`BENCHMARK_REPORT.md`](BENCHMARK_REPORT.md). Phần dưới chỉ trả lời các câu hỏi lý thuyết của Exercise 7.1–7.3.

### Exercise 7.1

Output `/metrics` sau 1 request (agent v2, cổng 8002), mỗi loại một ví dụ:

```text
# Counter — chỉ tăng
http_requests_total{method="POST",route="/ask",status="200"} 1.0
llm_tokens_total{model="mock-llm-small",type="input"} 91.0
# Gauge — lên xuống
agent_in_flight_requests 0.0
agent_info{model="mock-llm-small",version="v2"} 1.0
# Histogram — _bucket/_sum/_count
http_request_duration_seconds_bucket{le="0.2",method="POST",route="/ask"} 1.0
http_request_duration_seconds_count{method="POST",route="/ask"} 1.0
http_request_duration_seconds_sum{method="POST",route="/ask"} 0.1826649479999105
```

**Dùng `request.url.path` làm label?** Mỗi URL khác nhau là một giá trị label mới → một time series mới cho `http_requests_total` (× mỗi status) và **14 series** cho histogram latency (12 bucket + `_sum` + `_count`) cho mỗi URL. Bot quét hàng nghìn URL ngẫu nhiên sẽ tạo hàng chục nghìn series không bao giờ dùng lại, Prometheus tốn RAM và chậm dần (cardinality explosion). Hơn nữa đây là input của người ngoài, nên ai cũng có thể làm Prometheus của mình hết RAM. Mình đã thử gọi `/wp-admin`, `/.env`, `/a1b2c3`: cả 3 chỉ cộng vào **một** series `http_requests_total{method="GET",route="unmatched",status="404"} 3.0`.

**Metric mới `agent_question_length_chars`** (đã thêm vào `develop/app.py`): Histogram với bucket `(20, 50, 100, 200, 500, 1000, 2000)`, `observe(len(body.question))` ở đầu `/ask`. Sau 1 câu hỏi 40 ký tự: `agent_question_length_chars_bucket{le="50.0"} 1.0`, `_sum 40.0`, `_count 1.0`. Không dùng label `length=...` vì độ dài có vô số giá trị.

### Exercise 7.2

**Vì sao avg chỉ tăng nhẹ trong khi p99 tăng vọt?** Avg là trung bình trên mọi request: 3 % request chậm thêm 1.5 s chỉ cộng ~3 % × 1500 ms ≈ 45 ms vào avg (đo được +50 ms: 192 → 242 ms). Còn p99 là giá trị mà 99 % request nhanh hơn; khi > 1 % request chậm thì p99 *chính là* một request chậm (232 → 1710 ms). **SLO nên đặt theo p95/p99**, vì SLO phải mô tả trải nghiệm của người dùng tệ nhất mà mình chấp nhận, không phải của một người dùng "trung bình" không có thật.

### Exercise 7.3

- Sửa KB: thêm tài liệu JWT và Kubernetes HPA (chi tiết trong `BENCHMARK_REPORT.md` mục 3). Eval cuối: `Accuracy: 100.0% (23/23)`, `Câu bị regression: 0`, `Quality gate (accuracy ≥ 100%, không regression): ✅ PASS`, file `reports/eval_v2_fixed.json`.
- CI: [`.github/workflows/eval.yml`](.github/workflows/eval.yml) chạy agent v2 rồi `run_eval.py --min-accuracy 0.85 --baseline reports/eval_v2.json`. `run_eval.py` trả exit code 1 khi accuracy dưới ngưỡng hoặc có regression → step fail → job fail → branch protection chặn merge. Kiểm chứng: eval v1 với `--baseline reports/eval_v2.json` in `Câu bị regression : 20`, `❌ FAIL`, `exit=1`.

---

## AI log

Bài này được làm cùng **Claude Code (Claude Opus 5.5)** chạy trong terminal của repo. Theo RULES.md mục 1, ghi lại AI đã làm gì và mình kiểm tra thế nào:

| Phần | Hỏi / giao cho AI | AI làm / trả lời gì | Mình tự kiểm tra thế nào |
|---|---|---|---|
| Part 6 (code) | "Làm hết bài lab": sửa các `TODO` trong `06-lab-complete/` | Viết `config.py`, `auth.py`, `redis_client.py`, `rate_limiter.py`, `cost_guard.py`, `main.py`, `Dockerfile`, `docker-compose.yml`, `.dockerignore`, `.env.example`, `railway.toml`, `render.yaml` theo bảng "Script chấm dựa vào…" trong README | Chạy `check_production_ready.py` bản đầy đủ trong WSL2 → `PASS 12/12`; test tay bằng `docker compose` (401/200/422/429/503). Lúc test tay phát hiện compose không truyền `AGENT_API_KEY` → sửa và chạy lại script chấm. Cần đọc lại được từng file trong `app/` để giải thích khi Lab Coach hỏi. |
| Part 1, 2, 4, 5 | Chạy các ví dụ có sẵn và trả lời câu hỏi | Chạy app develop/production, build 2 image, chạy stack compose, các vòng lặp curl, `test_stateless.py`; soạn câu trả lời từ output thật | Mọi output trong file này (mã HTTP, số MB, log) là output của lệnh đã chạy. Số MB lấy từ `docker images`, không chép từ README. Lần chạy 4.3 đầu tiên không ra 429 và được ghi lại đúng như vậy. |
| Part 3 | So sánh `railway.toml`/`render.yaml`, đọc pipeline Cloud Run | Viết bảng so sánh và đoạn mô tả `cloudbuild.yaml`/`service.yaml` | Đối chiếu với 2 file config thật trong repo. |
| Part 7 Bước 1 | Chạy load test, eval, sửa KB, viết job CI | Chạy 4 mức concurrency + 3 mức `MOCK_SLOW_RATE`, eval v1/v2, thêm 2 entry KB, viết `.github/workflows/eval.yml`, thêm metric `agent_question_length_chars` | Số trong `BENCHMARK_REPORT.md` khớp `reports/*.json`. Eval cuối `--min-accuracy 1.0` in `✅ PASS`, 0 regression. |
| Part 7 Bước 2 | Bật stack observability, Locust, trace, alert | Phát hiện và sửa lỗi `PYTHONUSERBASE` trong `production/Dockerfile` (agent crash loop); chạy Locust headless scale 1/3; dùng Prometheus API và Jaeger API v3 để lấy số và tìm trace; script poll `/api/v1/alerts` mỗi 2 s để đo thời gian tới lúc firing; chụp màn hình bằng Chrome headless | Số trong report khớp CSV của Locust và output của Prometheus API lúc chạy. `trace_id` trong report grep ra đúng dòng log JSON. Ảnh Alerts chụp lúc 2 alert cùng firing. |
| Commit | Tách commit theo từng phần việc | Mỗi phần (Part 6, từng bước Part 7, tài liệu) một commit riêng | Xem `git log`. Không có `.env` hay secret trong repo. |

Những chỗ AI **không** tự làm được và cần mình làm: đăng nhập Railway/Render để deploy (Part 3/CP6), đặt `AGENT_API_KEY` trên dashboard, và chụp 3 ảnh `dashboard.png`, `running.png`, `test.png`.
