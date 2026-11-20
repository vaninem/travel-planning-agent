# 旅序 · 开发与服务配置指南

项目概览、架构与评测摘要见 [README](README.md)。本页保留完整的供应商配置、详细业务规则和开发命令。

输入城市、出行日期、兴趣和交通方式，后台生成每天的景点、地图线路、酒店地点候选和天气建议。页面支持查看任务历史、改景点顺序或换成已核验的候选，以及回放规划进度。

## 启动

依赖已安装且前端已构建时，在项目目录执行 `.venv/bin/python -m travel_agent`。

首次安装需要 Python 3.11+ 和 Node.js 20+。如果 `python3` 指向旧版本，请用对应的解释器创建环境，例如 `python3.13 -m venv .venv`。

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e '.[dev]'
npm --prefix frontend ci
npm --prefix frontend run build
python -m travel_agent
```

访问 [旅行助手](http://127.0.0.1:8000)。合并启动命令会运行 API 和独立的 Worker 子进程，Worker 退出后会自动拉起。页面的新行程使用实时数据，历史行程仍可打开。API 文档在 [FastAPI Swagger](http://127.0.0.1:8000/docs)。

开发时可运行 `npm --prefix frontend run dev`，由 Vite 将 API 请求转发到本机 8000 端口。也可以分开运行服务和 Worker。

```bash
python -m uvicorn travel_agent.api:app --host 127.0.0.1 --port 8000
```

```bash
python -m travel_agent.worker
```

## 实况数据和攻略库

复制 `.env.example` 为 `.env`，填写 `AMAP_MCP_KEY`、`TRAVEL_MODEL_BASE_URL`、`TRAVEL_MODEL_API_KEY` 和 `TRAVEL_MODEL_NAME` 后重启。Agent 优先用高德 MCP 查询景点、天气和路线，缺少可核验地点时尝试公开地图数据。已有 Web 服务密钥可以填入 `AMAP_WEB_KEY` 并将 `AMAP_TRANSPORT` 设为 `mcp`。也可只填 Web 服务密钥使用 REST。模型服务需要支持兼容 Chat Completions 的工具调用及 JSON 输出。模型服务地址示例为 `https://your-model-service/v1`。模型调用失败时，系统按核验后的地点和用户要求继续排程，并在行程中说明。天气超出预报范围时，行程会标注缺少对应日期的预报。 [高德 MCP 接入文档](https://lbs.amap.com/api/mcp-server/gettingstarted)

## 预算和价格来源

主表单的总预算是整组旅客的人民币上限。费用明细包含门票、市内交通、住宿和餐饮预留，往返目的地的交通另计。新行程默认每人每天预留 ¥120 餐饮费，可按目的地和用餐习惯调整。有预算时，规划会在候选酒店和核实过票价的景点中寻找尽量接近上限的组合，保留必去景点和天气要求。待核实门票暂按零计，并提供官方来源或票务查询入口。住宿、交通或外币费用未能计入时，页面显示已估小计。

实时行程分批核查所有已选景点。预算自动换景点和手动调整时，新景点会在重算预算前查询票价。数据来源包括地图标注的景区官网、政务公告和票务平台。搜索默认使用 360 与 Bing RSS，配置 `BRAVE_SEARCH_API_KEY` 时优先用 Brave Search。模型摘录网页原文，程序验证票种、价格和日期，也可按官方季节价格表选择票价。官网明确的普通票缺少生效日期时可标成参考价。动态票务页须明确覆盖参观日，起价、套票或证据不足的项目显示待核实。可用 `TRAVEL_TICKET_WEB=0` 关闭公网核价。 [Bing RSS 搜索](https://blogs.bing.com/search/2005/1/RSS-Feeds-for-Search-Results/) · [Brave Web Search API](https://api-dashboard.search.brave.com/app/documentation/web-search/get-started)

个人开发者可申请 RollingGo Hotel MCP 的 API Key。拿到密钥后，在项目根目录的 `.env` 填写 `ROLLINGGO_API_KEY`，重启服务并提交新行程。密钥申请仍在处理时，可以继续规划行程，住宿卡片会显示可查的酒店地点，房价待核实。 [申请 RollingGo API Key](https://travelportal-partner-center.dida.com/register?lang=zh) · [RollingGo Hotel MCP 官方文档](https://github.com/RollingGo-AI/rollinggo-hotel-mcp)

已有 Booking.com Managed Affiliate Partner 权限，也可以使用 Booking.com Demand API 查询实时住宿和第三方门票报价。在 `.env` 填写 `BOOKING_API_TOKEN` 和 `BOOKING_AFFILIATE_ID`，重启服务后提交新行程。 [Booking.com 接入条件](https://developers.booking.com/demand/docs/getting-started/prerequisites)

酒店按入住和离店日期、人数与房间数查询，房间按最多两位旅客预留。配置 RollingGo 后优先用它查房型报价，根据平均每晚报价展示预计住宿合计和预订链接。多间房按相应入住人数的单间报价估算。Booking.com 返回总价时，页面也会计算平均每晚金额。价格和库存以服务商页面为准。 [RollingGo 房价字段说明](https://docs.rollinggo.store/guides/tools)

配置 `WENDAO_API_KEY` 后，Agent 会按目的地、日期、人数和预算调用携程问道的只读查询接口。返回的酒店和景点详情页显示在「携程查询结果」中。预算只使用可验证的结构化报价和已标注的估算。 [携程问道授权入口](https://www.ctrip.com/wendao/openclaw) · [携程接入说明](https://developer.cloud.tencent.com/article/2660097)

行程地图使用 OpenFreeMap 的浅色街道和建筑底图，必要时切换到 OpenStreetMap 栅格底图。中国大陆也可以配置独立的高德 JS API，在 `frontend/.env.local` 设置 `VITE_AMAP_JS_KEY`，按部署方式配置 `VITE_AMAP_SECURITY_CODE` 或 `VITE_AMAP_SERVICE_HOST` 后重新构建。地图用虚线标出没有查到道路路线的相邻点位。天气在行程下方按日展示，覆盖到的日期来自高德或公开的当地预报服务。 [OpenFreeMap](https://openfreemap.org/quick_start/) · [OpenStreetMap 底图政策](https://operations.osmfoundation.org/policies/tiles/) · [美国国家气象局预报接口](https://www.weather.gov/documentation/services-web-API)

攻略 RAG 使用项目内置的 `travel_agent.rag_engine.tools.rag.RAGTool`，支持 BM25 词法召回、PostgreSQL + pgvector 向量召回，以及使用 RRF 融合两路名次的混合检索。`TRAVEL_GUIDE_RETRIEVAL=auto` 在嵌入服务与数据库配置齐全时选择混合模式，否则使用本地 BM25；也可显式选择 `vector`、`hybrid` 或 `lexical`。Markdown 语料放在 `travel_agent/guide_data/`，也可通过 `TRAVEL_GUIDE_DIR` 指定目录。文本按标题、段落、句子边界切块，每条攻略命中保留文件名、标题和原文行号。攻略用于推荐和建议，行程的 POI ID、坐标和路线由工具数据核验。攻略检索遇到文件读取异常时会记录不可用提示，行程可继续生成。

### 攻略向量库

安装 `python -m pip install -e '.[embedding]'`，配置 `.env` 中的 `EMBEDDING_*`、`DATABASE_URL` 和 `EMBEDDING_DB_SCHEMA`（默认 `travel_guide_embeddings`），使用 `docker compose up -d postgres` 启动独立数据库。已安装 PostgreSQL 16 与 pgvector 的本机也可执行以下命令，随后将第二条输出的 DSN 填入本地 `.env` 的 `DATABASE_URL`：

```bash
python scripts/local_guide_postgres.py start
python scripts/local_guide_postgres.py dsn
python -m travel_agent.guides --require-mode hybrid
```

Worker 启动前同步来源快照，向量任务按内容哈希缓存，已准备的索引在查询阶段只编码查询。文件清单变化隔离到新命名空间，已有的相同文本向量仍可复用；单文件正文变化采用增量更新。默认遵循系统 HTTP 代理配置；如果嵌入服务需要直连，可以显式设置 `TRAVEL_GUIDE_EMBEDDING_DIRECT=1`。初始化时遇到配置、数据库或嵌入故障会记录降级，并在本 Worker 生命周期内使用本地 BM25；查询中的临时向量异常只降级本次查询。恢复配置后重启 Worker 重新准备索引。固定样本评测始终使用词法路径，不会占用外部嵌入额度。

## 后台任务

- API 创建任务并返回任务 ID，同一幂等键及相同请求指向同一任务。Worker 依次提交资料、草案和校验检查点。状态和进度事件随检查点一起提交。
- Worker 运行中续租，回写时重新校验租约。电脑休眠唤醒后，只要任务尚未被接管，同一 Worker 可用原令牌续租。进程退出后由下一次领取接手已提交的阶段。尚未提交的阶段可能重新调用模型或高德。
- 临时故障按指数退避重试，耗尽预算进入 `dead`。`GET /api/jobs?status=dead` 可查询失败任务，`POST /api/jobs/{id}/retry` 可从最后的检查点再试。
- 事件流支持 `Last-Event-ID` 和 `?after=`。调整景点时提交行程版本，后端拒绝过期版本并重新估算受影响的通勤。
- 默认直接从 SQLite 领取任务。启用 Redis 时设置 `TRAVEL_QUEUE=redis` 和 `TRAVEL_REDIS_URL`。Redis 提供领取通知，SQLite 持久化任务、检查点、租约和事件。Worker 定期查询 SQLite，因而已经提交的任务在 Redis 断线或通知丢失后也能继续。

