# Stage 0 本地 Web 壳契约

状态：S0-04 已实现，并通过开发 agent 定向检查和主 agent 独立验收；S0-07
全量复跑仍未完成。这不是 Stage 0 完成结论，Stage 1 未开始。

## 边界

- 正式应用由 `quantking.web.app.create_app` 构造，只接收任务只读应用端口、可选静态根和可选测试时钟；路由不创建数据库，也不导入 SQLAlchemy、SQLite 或日志文件。
- 健康、任务快照、REST 事件页和 SSE 使用独立 Pydantic schema。任务响应不暴露输入/配置/版本 manifest、日志标识或本地路径。
- API 错误使用 `{ "error": { "code", "message" } }`；400、404、422 和公共 QuantKing 错误不返回异常上下文、验证明细或本地路径。
- 生产静态文件与 API 同源。未知 `/api/*` 始终返回 JSON 404，不能落入 React 回退；静态根必须是已有绝对目录并包含安全的 `index.html`，目录不提供索引列表。
- 启动入口只使用常量 `127.0.0.1`，只允许配置端口；应用不安装 CORS 中间件。

## 持久事件恢复

REST `GET /api/job-events` 接受非负 `after_event_id` 和 `1..1000` 的 `limit`，按全局单调 `event_id` 返回严格位于游标之后的有界批次和 `next_cursor`。

SSE `GET /api/job-events/stream` 支持 `Last-Event-ID` 或 `after_event_id`。两者同时存在时必须相等，否则返回 400。每帧固定为：

```text
id: <event_id>
event: job-event
data: <与 REST 相同的 JobEvent JSON>

```

SSE 每批仍通过持久应用端口查询，不使用进程内 Worker 总线。同步 SQLite 查询通过工作线程执行，避免 `busy_timeout` 和有界重试阻塞事件循环；整批必须严格递增后才会发送。

浏览器 reducer 忽略 `event_id <= cursor` 的重复或过期事件。EventSource 断线后，前端先请求 `event_id > cursor` 的 REST 批次补洞，再以恢复后的游标按 1、2、4、8 秒上限退避重连；卸载时关闭 EventSource 并清除计时器。

## 最小界面与测试

界面只包含中文标题、健康状态、任务 ID 查询、任务状态、持久事件/游标和连接状态，不包含行情、因子、模型、回测、调仓、账户或其他业务导航。

定向检查使用临时 SQLite/Alembic 和合成通用任务。Playwright 测试服务器也只创建临时数据库，注入正式应用工厂并托管 `frontend/dist`；测试使用本机 Microsoft Edge，主动中止第一次 SSE，验证 REST 补洞、恢复游标重连和事件无重复。产品路由没有测试专用参数或故障开关。

开发 agent 已运行：

- `pytest tests/web -q -p no:cacheprovider`
- `npm test`
- `npm run typecheck`
- `npm run build`
- `npm run test:e2e`

主 agent 已独立复跑后端、前端、生产构建、质量检查和本机 Edge Playwright；S0-07
仍须复跑全阶段矩阵，且只有 S0-07 能作 Stage 0 完成判断。
