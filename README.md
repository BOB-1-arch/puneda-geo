# GEO 智能体后端（MVP：仅接入 DeepSeek）

当前阶段只做一件事：跑通「输入问题 → 服务器用真实 DeepSeek API Key 调用官方接口 → 返回真实原始回答」的最短链路。

API Key 只保存在**服务器端环境变量**里，前端不发送、不接收、不保存 API Key。

## 目录结构

```
geo-backend/
├── main.py          # FastAPI 服务，核心接口 POST /api/ask/deepseek
├── requirements.txt
├── .env.example      # 环境变量模板，不含真实Key
└── ask_cli.py        # 命令行测试脚本
```

## 一、安装依赖

```bash
cd geo-backend
python -m venv venv
source venv/bin/activate      # Windows 用 venv\Scripts\activate
pip install -r requirements.txt
```

## 二、配置真实 API Key

1. 去 DeepSeek 开放平台（platform.deepseek.com）申请一个 API Key。
2. 复制模板并填入真实 Key：

```bash
cp .env.example .env
```

编辑 `.env`：

```
DEEPSEEK_API_KEY=sk-你的真实key
```

`.env` 只在你本机/服务器上存在，**不要提交到 Git，不要放进前端代码**。

## 三、启动服务

```bash
uvicorn main:app --reload --port 8000
```

看到 `Uvicorn running on http://0.0.0.0:8000` 就说明启动成功。

## 四、测试是否真实连通

方式一：命令行脚本

```bash
python ask_cli.py "车载冰箱哪个品牌好？"

# 指定模型
python ask_cli.py "车载冰箱哪个品牌好？" --model deepseek-v4-pro

# 开启思维链（thinking）模式
python ask_cli.py "车载冰箱哪个品牌好？" --thinking
```

会打印 DeepSeek 的真实原始回答，以及完整原始 JSON（后续做品牌提及/排名/引用来源解析都基于这个原始JSON）。

方式二：curl

```bash
curl -X POST http://localhost:8000/api/ask/deepseek \
  -H "Content-Type: application/json" \
  -d '{"question": "车载冰箱哪个品牌好？"}'
```

方式三：检查是否已配置Key（不消耗调用额度，也不会返回Key本身）

```bash
curl http://localhost:8000/api/platforms/deepseek/status
```

## 五、接口说明

### `GET /api/platforms/deepseek/status`
返回：
```json
{
  "platform": "DeepSeek",
  "configured": true,
  "default_model": "deepseek-v4-flash",
  "available_models": ["deepseek-v4-flash", "deepseek-v4-pro"]
}
```
只表示服务器是否配置了Key，不返回Key本身。

### `POST /api/ask/deepseek`
请求体：
```json
{
  "question": "车载冰箱哪个品牌好？",
  "model": "deepseek-v4-flash",
  "thinking": false
}
```
- `model` 可选，默认 `deepseek-v4-flash`。也可以传 `deepseek-v4-pro`（能力更强，单价约为 flash 的 3.1 倍）。
- `thinking` 可选，默认 `false`（关闭思维链，对齐旧 `deepseek-chat` 的非思考行为，延迟更低、成本更低）。设为 `true` 则开启思维链推理。

返回体：
```json
{
  "platform": "DeepSeek",
  "model": "deepseek-v4-flash",
  "raw_answer": "……模型的真实文本回答……",
  "reasoning_content": null,
  "raw_response": { "...": "DeepSeek API 返回的完整原始JSON" }
}
```
`reasoning_content` 仅在 `thinking=true` 时有值，是模型给出最终答案前的思维链内容。

## 六、重要提示

- **模型名称已更新**：`deepseek-chat` / `deepseek-reasoner` 已于 **2026-07-24 15:59 UTC 正式弃用**，调用会直接报错，不再自动路由。已按官方迁移指引改为显式使用 `deepseek-v4-flash`（默认）/ `deepseek-v4-pro`。Base URL（`https://api.deepseek.com`）和接口路径（`/chat/completions`）均未变化。
- **V4 系列默认开启思维链（thinking），会显著增加延迟和token开销**。为了保持和旧版 `deepseek-chat`（非思考）一致的行为，代码里默认把 `thinking` 设为关闭（`{"type": "disabled"}`）。如果某个问题需要更强的推理能力，可以在请求里把 `thinking` 设为 `true`，或者换用 `deepseek-v4-pro`。
- 默认模型也可以通过服务器环境变量 `DEEPSEEK_DEFAULT_MODEL` 整体切换（比如以后想默认全部用 `deepseek-v4-pro`），不需要改代码，具体看 `.env.example`。
- DeepSeek 官方 `/chat/completions` 接口**默认不带联网检索**，返回的是裸模型回答，不含引用来源角标。这一点和技术方案文档第一节里提到的认知一致：后续如果要拿到"带引用来源"的结果，需要另外接入联网检索（自建 RAG 或官方联网插件，如果开放）。这一版先只做"跑通真实裸模型回答"这一步，符合当前阶段的验收目标。
- 当前 CORS 是完全放开的（`allow_origins=["*"]`），只适合本机开发调试。等确定了真实前端部署域名后，需要收紧到具体域名，避免任何网站都能调用你的后端消耗额度。
- 本环境（我这边的沙箱）没有联网权限，所以我没法帮你实际跑一次真实调用来验证——需要你在自己有网络、且填了真实 Key 的机器上执行上面第四步。如果报错，把报错信息发给我，我可以帮你排查。
