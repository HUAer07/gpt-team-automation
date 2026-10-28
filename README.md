# ChatGPT Team 自动化全流程工具 (带可视化 Web 控制台)

基于 Playwright、`pyotp`、FastAPI 与 WebSocket 实时日志流的 ChatGPT Team 自动化控制台，支持母号与子号全凭据驱动的 Team 开通、拉人入组、Primary Owner 主所有权转移与母号安全退组。

---

## 目录结构

```
gpt-team-automation/
├── static/
│   └── index.html        # 可视化仪表盘前端 (Tailwind + WebSocket 实时流)
├── server.py             # FastAPI 核心服务 (接口调度 + 状态机 + 日志广播)
├── start_ui.sh           # 一键启动 Web 控制台脚本
├── .env.example          # 环境变量示例
├── requirements.txt      # Python 依赖项
├── config.py             # 配置解析与数据模型
├── auth.py               # Playwright 自动化登录与 TOTP 验证码毫秒级接码
├── team_client.py        # OpenAI Workspace API 客户端 (邀请/接受/转让/退出)
├── test_totp.py          # 2FA 密钥本地校验小工具
├── main.py               # CLI 命令行调度入口
└── README.md             # 本说明文档
```

---

## 快速使用方式

### 方式 A：启动可视化 Web 控制台（推荐 ⭐）

在外置硬盘项目根目录中直接执行：

```bash
cd "/Volumes/SSK Drive/Projects/gpt-team-automation"
./start_ui.sh
```

打开浏览器访问：
**`http://127.0.0.1:8899`**

在前端页面中可以直接：
1. **可视化填写凭据**：输入母号、子号账密及 2FA Secret（支持实时验码与倒计时预览）。
2. **直观调整防风控参数**：一键切换 Headless 无头模式、步间延迟滑块、代理配置。
3. **一键启动/中止**：点击“启动全流程”，5 步可视化进度条（Stepper）实时高亮流动。
4. **实时日志窗口**：WebSocket 毫秒级推送彩色控制台日志与异常告警。

---

### 方式 B：CLI 命令行纯后台执行

如果你习惯在终端直接运行：

```bash
cd "/Volumes/SSK Drive/Projects/gpt-team-automation"
cp .env.example .env
# 填写 .env 中的凭据

# 启动运行
./venv/bin/python main.py
```
