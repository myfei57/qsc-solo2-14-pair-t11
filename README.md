# BreweryCtl 啤酒酿造糖化与发酵控制平台（Python 版）

BreweryCtl 是一个自包含的单体控制服务，覆盖「投料 → 糖化 → 煮沸 → 冷却 → 发酵 → 成熟」
整条工艺链。所有状态写入本地文件快照与追加日志，不依赖数据库、消息队列或外部服务；
仅使用 Python 标准库，控制台前端由内置 HTTP 服务直接托管。

## 运行

```bash
python -m breweryctl check                # 初始化并输出平台自检摘要
python -m breweryctl snapshot             # 写出一次快照
python -m breweryctl serve                # 启动控制台，默认 127.0.0.1:8080
python -m breweryctl serve --port 8090 --data-dir var/breweryctl
```

启动后访问 `http://127.0.0.1:8080/`，四个页面分别是糖化、发酵、清洗 CIP、告警与审计。

配置既可用命令行参数，也可用环境变量：

| 变量 | 说明 | 默认值 |
| --- | --- | --- |
| `BREWERYCTL_HOST` | 监听地址 | `127.0.0.1` |
| `BREWERYCTL_PORT` | 监听端口 | `8080` |
| `BREWERYCTL_DATA_DIR` | 数据目录 | `var/breweryctl` |
| `BREWERYCTL_FSYNC` | 是否强制落盘 | `true` |
| `BREWERYCTL_MAX_ACTIVE_BATCHES` | 单厂在制批次上限 | `4` |
| `BREWERYCTL_PITCH_TEMP_MAX_C` | 接种温度上限 | `12.0` |
| `BREWERYCTL_PRESSURE_LIMIT_BAR` | 罐压联锁上限 | `1.8` |

## 目录结构

| 路径 | 职责 |
| --- | --- |
| `breweryctl/core` | 配置、时钟、标识、校验与统一错误层级 |
| `breweryctl/persistence` | 原子快照、JSONL 追加日志、按键加锁、集合存储 |
| `breweryctl/domain` | 命名空间、配方版本、糖化、麦汁、煮沸、酒花、温控、CO2、CIP、发酵、告警、审计 |
| `breweryctl/service` | 批次编排、控制服务、遥测服务、维护服务与组合根 |
| `breweryctl/console` | JSON API 路由、序列化与静态页面服务器 |
| `breweryctl/runtime` | 应用对象与命令行入口 |
| `breweryctl/web` | 糖化、发酵、清洗、告警四个控制台页面 |

## 关键工艺约束

- 投料前必须先把水温确认结果写入快照，重启后仍可判断是否允许投料。
- 酒花按配方序次投加，前序未投加或超出时间窗都会被拒绝。
- 转罐要求发酵罐已清洗且清洗凭证在有效期内。
- 接种要求降温目标已经达成，且目标温度不高于接种上限。
- 罐压超过联锁上限立即闩锁并打开排气阀；闩锁复位前禁止操作其他阀门。
- CIP 必须按预冲洗、碱洗、中间冲洗、酸洗、终冲洗的顺序推进，跳步会被拒绝。
- 每一次状态变更都会写入审计记录，批次可导出完整审计包。

## HTTP API

`GET /api/pages` 会返回全部路由与页面清单。主要分组：

| 分组 | 代表接口 |
| --- | --- |
| 状态 | `GET /api/state`、`GET /api/health` |
| 配方 | `GET/POST /api/recipes`、`POST /api/recipes/{id}/publish`、`POST /api/recipes/{id}/rollback` |
| 批次 | `POST /api/batches`、`GET /api/batches/{id}`、`POST /api/batches/{id}/water|charge|heat|rest|filter|ignite|boil|hop|whirlpool|cool|cooled|transfer|pitch|mature|complete|abort` |
| 控制 | `POST /api/control/temperature`、`POST /api/control/tanks/{id}/pressure|relieve|latch/reset|valves` |
| 遥测 | `POST /api/telemetry/readings`、`GET /api/telemetry/probes/{id}/health|report` |
| 维护 | `POST /api/maintenance/tanks/{id}/clean`、`POST /api/maintenance/cycles/{id}/advance|finish` |
| 告警 | `GET /api/alarms`、`POST /api/alarms/{id}/ack|resolve` |
| 审计 | `GET /api/audit?batch_id=...`、`GET /api/batches/{id}/audit` |

## 数据一致性

每次写操作先追加日志再写原子快照：`state.json` 由临时文件 `os.replace` 原子替换，
`journal.jsonl` 逐行记录事件序号。服务启动时先读快照，再重放序号更大的日志事件，
因此进程在写入中途退出也不会丢已经确认的业务动作。存储层同时提供按 key 的可重入锁，
批次、罐体、探头与清洗过程的「读—判断—写」序列都在同一把锁内完成。

## 规模与质量自检

| 指标 | 数值 |
| --- | --- |
| 生产 Python 文件（不含 `__init__.py`） | 34 |
| 生产代码总行数 | 6103 |
| 非空非注释行 | 5073 |
| 包目录 | 7 |
| 前端页面 | 4 |
| 第三方运行时依赖 | 0（仅标准库） |

自检方式：

```bash
python -m compileall -q breweryctl          # 语法检查
python -m breweryctl check --no-fsync       # 启动、初始化、崩溃恢复自检
python -m breweryctl snapshot --no-fsync    # 快照落盘自检
```

代码按入口可达性审计：路由表覆盖全部服务方法，领域与服务层没有未被引用的函数、
类或模块常量，也没有 `pass` 占位、`TODO`、`NotImplemented` 之类的脚手架代码。
