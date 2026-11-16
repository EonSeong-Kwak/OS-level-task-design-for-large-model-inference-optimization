# llmOS

前后端分离的大模型推理操作系统仿真。仿真内核走离散事件时钟，不加载真实模型、不依赖真实 GPU；真实模型方向背书由独立的 `adapters/` 层完成（本地 RTX 5060 + Qwen2.5-0.5B 的 G1 实验已跑通，结论 `CONSISTENT_WITH_SIM_E1`），结果只进 `gpu_results/` 与报告附录，不混入仿真链路。详见 `docs/系统设计文档.md` §7。

```
llmOS/
  backend/     FastAPI + 仿真内核 + MySQL（可回退 SQLite）
  frontend/    Vue 3 评测台
  docs/        技术方案、系统设计、评测报告、测试报告
```

## 环境

- Python 3.10+
- Node.js 18+
- 可选 MySQL 8/9

数据库账号只用环境变量，不要把密码提交进仓库：

```bash
export MYSQL_USER=root
export MYSQL_PASSWORD=你的密码
```

未配置或连不上时使用 `backend/data/llmos.sqlite3`。

## 启动

```bash
cd backend
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
pytest
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

另开终端：

```bash
cd frontend
npm install
npm run dev
```

评测台：http://127.0.0.1:5173

```bash
cd backend
python scripts/run_ab.py
```

JSON/PNG 写入 `backend/results/`，评测报告只引用这些文件。
