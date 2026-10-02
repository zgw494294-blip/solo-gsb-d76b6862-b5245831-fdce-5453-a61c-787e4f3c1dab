# 闭环供水管网平衡台

本地运行的**闭环供水管网稳态平衡**工具：在浏览器中录入节点（1 个定水头水源 + 若干非负需水量的用水节点）与管段（正阻力），后端用 **Python / FastAPI + SciPy** 求解整个管网的稳态水头分布、各管段**带符号流量**与水源总供水量，并按 **1e-6 容差**独立复核节点水量守恒与每根管段的压降定律；前端用**原生 HTML/CSS/JavaScript** 提供可编辑表格、可拖拽的网络图、流向箭头与低水头节点告警。

## 一条命令启动

```bash
docker compose up --build
```

启动后在本机浏览器打开：

```
http://localhost:8000
```

停止：`Ctrl+C`（后台运行用 `docker compose down`）。

> 需要 Docker 20.10+（支持 Compose v2 插件）。端口被占用时修改 `docker-compose.yml` 里的 `"8000:8000"` 左侧端口即可。

## 不用 Docker 的本地运行方式

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --host 127.0.0.1 --port 8000
```

同样访问 http://localhost:8000 。

## 使用说明

1. **节点表**：2～10 个、编号唯一；类型二选一：
   - `水源`：恰好 1 个，填写**固定水头** H_s（有限数值）；
   - `用水`：填写**非负需水量** d。
2. **管段表**：1～16 根、编号唯一；选择**起点/终点**（必须不同，且任意两节点间最多一根管），填写**正阻力 r**。
3. 顶部填写**最低水头限值**，点击 **求解**。
4. 结果区展示：
   - 水源总供水量、总需水量、供耗差；
   - 各节点水头（低于限值的行与图上节点**标红**）；
   - 各管段带符号流量 q、实际流向、`H起−H终` 与压降残差；
   - 网络图上箭头表示**实际流向**（蓝色与录入方向同向，橙色为反向），数字为带符号流量。
5. 点击 **载入示例** 可快速查看一个 5 节点、7 管段的环状管网。
6. **逐根停管分析**：先完成一次稳态求解得到基线，再点击顶栏的 **停管分析**，
   后端对**每根管段各构造一个独立情景**（仅停用该管，其余不变）：
   - **断联情景**：移除后若有用水节点与水源断开，列出**失供节点**与**失供需水量之和**，
     不为该情景生成水头；
   - **连通情景**：仍全部连通则重新求解，列出**最低用水节点水头**、**低于当前最低水头限值的节点**
     以及**比基线新增的低水头节点**；
   - 某个情景单独**不收敛**时单独标明并**排在最后**，不影响其余情景。
   - 排序：断联情景按失供需水量**降序**排在最前，连通情景按最低用水节点水头**升序**随后，
     同分保持管段录入顺序。
   - 只要**编辑节点、管段或最低水头限值**，旧的停管分析立即清除（需要重新点击“停管分析”）。

输入有问题（非有限数值、负需水量、阻力非正、重复编号、端点悬空、图不连通、水源数 ≠ 1 等）或求解器未收敛时，页面会用中文说明具体原因。

## 水力模型

管段 p 按录入方向记 i→j，带符号流量 q_p（正：i→j；负：j→i），采用二次压降定律：

```
H_i − H_j = r_p · q_p · |q_p|
```

每个用水节点 k 满足水量守恒（**净流入 = 需水量**）：

```
Σ_p a_{k,p} · q_p = −d_k
```

其中 a_{k,p} 为节点-管段关联矩阵（起点 +1、终点 −1），故 A·q 为节点净流出。水源节点给定固定水头，因此未知量（n−1 个用水节点水头 + m 个管段流量）与方程数恰好相等，构成平方非线性方程组，用 Newton 型方法求解：

- 以水源为根做 BFS 生成树，按子树需水量摊派初始流量、沿程估算初始水头；
- 依次尝试 `scipy.optimize.root` 的 `hybr`（MINPACK）与 `lm`（Levenberg–Marquardt），并配有带 Armijo 回退的自实现全量 Newton；
- 内部目标残差 1e-9，解出后再以 **1e-6** 容差独立复核每段压降、每个节点守恒以及水源供水量与总需水量之差。

## API

| 方法 | 路径 | 说明 |
| --- | --- | --- |
| `POST` | `/api/solve` | 入参 `{nodes, pipes, min_head}`，返回水头、流量、流向、供水量与各项残差；非法输入返回 `422`/`400` 与中文原因 |
| `POST` | `/api/analyze-outages` | 入参与 `/api/solve` 相同；先求基线，再对每根管段各评估一个“单独停用”情景并按风险排序返回 |
| `POST` | `/api/locate-leak` | 入参为管网参数 + `measurements`（2～8 个互异用水节点的实测水头）+ 正的 `leak_upper_bound`；逐用水节点拟合单点漏量并按测点误差排序 |
| `POST` | `/api/plan-resupply` | 入参为管网参数 + `resupply_nodes`（2～6 个互异、表内需水量为正的用水节点）；先求停供基线，再枚举包含空集在内的全部复供组合，返回恢复需水量总量最大的可行组合 |
| `POST` | `/api/isolation-jobs` | 创建停管隔离作业：入参为管网参数 + 可选 `allow_unserved`（允许暂时失供的用水节点数组，可为空）；管段初始全开，初始有用水节点欠压或不收敛则不创建；返回作业 ID 与版本 0 快照；计算在工作线程进行，期间不阻塞健康检查与其他作业 |
| `GET` | `/api/isolation-jobs/{job_id}` | 读取作业最近一次成功提交的完整快照（关闭管段、失供节点、连通区稳态结果）；即使该作业正在计算，也立即返回已提交快照，不返回中间结果；作业不存在返回 `404` |
| `POST` | `/api/isolation-jobs/{job_id}/toggle` | 入参 `{"version", "pipe_id"}`：切换一根现有管段的开闭；规则满足且收敛才提交（关闭集合+版本+1+稳态结果），否则全部保持原状；求解不阻塞其他请求；同一版本的修订与切换合计最多一次成功提交，版本已变返回 `409`（含 `current_version`） |
| `POST` | `/api/isolation-jobs/{job_id}/rehearsal` | **切换顺序预演（不实际操作作业）**：入参 `{"version", "pipes": 2～6 根互异现有管段编号}`；基于该版本的关闭集合与当前需水量，搜索每根恰好切换一次、其他管段不动的可执行顺序，所有中间状态与终态都须满足允许失供/最低水头/稳态校验，按管段录入序号序列字典序返回最小可行顺序及逐步开闭、关闭集合、失供节点/总量与连通区稳态结果；无可行顺序明确返回不可行，搜索途中求解不收敛或出现非有限数只报“无法确认”（绝不当作不可行）；**不推进版本、不改需水量或快照，计算不阻塞读取与其他作业**；版本已变返回 `409`（含 `current_version`），未知或已结束作业返回 `404`（结束优先于冲突与计算错误） |
| `POST` | `/api/isolation-jobs/{job_id}/demands` | 作业执行中修订需水量（无需重建作业）：入参 `{"version", "demands": 非空修订列表}`，每项 `{"node_id", "demand"}`（互异的现有用水节点、非负有限数、拒绝布尔值）；只改列出的节点，水源/管段/最低水头/允许失供集合不变；按当前关闭集合重算（连通节点用新值且不得欠压，失供节点保存新值并更新失供总量，重连后按新值求解），成功才同时提交需水量+完整快照+版本加一；非法输入/欠压/不收敛/聚合溢出均不改变状态；单个 1e308 这样的有限大值允许，但失供总量等聚合值溢出为非有限数（或候选快照含 NaN/Infinity）时返回 `422`，需水量、关闭集合、快照与版本均保持原状、同版本可重试；与切换共用版本乐观锁，冲突返回 `409`（含 `current_version`） |
| `DELETE` | `/api/isolation-jobs/{job_id}` | 结束不再使用的作业：**无需请求体、无需 version**，成功返回该作业 ID；未知 ID 与重复结束均返回 `404`。正在求解也能立即结束（不等待、不中断底层数值计算）：结束后新发起的读取/切换/修订全部 `404`；结束时尚未提交的在途切换或修订，之后无论算出结果还是触发欠压/不收敛/非有限数错误，都按 `404` 放弃，不能再提交、返回成功或恢复作业；结束前已提交的操作保持原返回语义。其他作业与健康检查不受影响 |
| `GET` | `/api/health` | 健康检查 |

请求示例：

```json
{
  "min_head": 0,
  "nodes": [
    {"id": "S", "type": "source", "head": 30.0},
    {"id": "A", "type": "demand", "demand": 2.0}
  ],
  "pipes": [
    {"id": "P1", "start": "S", "end": "A", "r": 1.0}
  ]
}
```

### 逐根停管分析 `POST /api/analyze-outages`

> 停管分析与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。

请求体与 `/api/solve` 完全一致（沿用同一套输入校验）。响应中 `analysis.baseline` 为基线稳态解
（结构与 `/api/solve` 的 `result` 相同），`analysis.scenarios` 为每根管段一个、已按风险排序的情景：

- `status: "disconnected"`：含 `unserved_nodes`（失供节点，按节点录入顺序）与
  `unserved_demand`（失供需水量之和），**不返回水头**；
- `status: "connected"`：含 `min_demand_head` / `min_demand_head_node`（最低用水节点水头及节点）、
  `below_min_head`（低于当前限值的节点）、`new_below_min_head`（比基线新增的低水头节点）
  以及 `solution`（该子网上的完整稳态解）；
- `status: "not_converged"`：含 `error`（中文原因），该情景单独排最后，其余情景不受影响。

排序键为：断联（组 0）按 `-unserved_demand`、连通（组 1）按 `min_demand_head` 升序、
不收敛（组 2）最后；组内同分按管段录入顺序。`counts` 给出三类情景的数量。

curl 调用示例：

```bash
curl -s http://localhost:8000/api/analyze-outages \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 0,
    "nodes": [
      {"id": "S", "type": "source", "head": 50.0},
      {"id": "A", "type": "demand", "demand": 2.0},
      {"id": "B", "type": "demand", "demand": 3.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0}
    ]
  }'
```

返回（节选）：两根管都是桥，停用 P1 会让 A、B 同时失供（Σd=5），排在停用 P2（仅 B 失供，Σd=3）之前：

```json
{
  "ok": true,
  "analysis": {
    "counts": {"disconnected": 2, "connected": 0, "not_converged": 0},
    "scenarios": [
      {"rank": 1, "pipe_id": "P1", "status": "disconnected",
       "unserved_nodes": ["A", "B"], "unserved_demand": 5.0},
      {"rank": 2, "pipe_id": "P2", "status": "disconnected",
       "unserved_nodes": ["B"], "unserved_demand": 3.0}
    ]
  }
}
```

### 单点漏损定位 `POST /api/locate-leak`

> 漏损定位与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。

在管网参数（与 `/api/solve` 完全一致，沿用同一套输入校验）之外，请求体还需包含：

- `measurements`：**2～8 个**实测水头测点，每项为 `{"node": 节点编号, "head": 实测水头}`，
  测点必须**互异且全部为用水节点**，实测水头必须是有限数值；
- `leak_upper_bound`：正的有限数值，即每个候选节点允许增加漏量的搜索上限。

后端先求**无漏损基线**（基线不收敛时整个请求返回 `422` 并说明原因），随后在
**“至多一个用水节点增加漏量”** 的假定下，对每个用水节点各构造一个候选：把该节点需水量
增加 x（x ∈ [0, 上限]），重新做稳态求解，取测点预测水头与实测水头的**均方根误差 RMSE**
作为目标；用黄金分割在 `[0, 上限]` 内一维寻优，**端点 0 与上限都显式参与比较**
（区间内部偶尔求解失败的点按 +∞ 跳过，搜索会自然收缩到可解区域）。

响应 `leak_analysis` 含：

- `baseline_rmse` / `baseline_predicted_heads`：无漏损基线的测点 RMSE 与基线预测水头，
  `baseline` 为完整基线稳态解（结构与 `/api/solve` 的 `result` 相同）；
- `candidates`：每个用水节点一个，按 RMSE **升序**排列，同分按**节点录入顺序**：
  - 成功候选：`status: "fitted"`、`leak`（拟合漏量，可能为端点 0）、`predicted_heads`
    （各测点预测水头）、`head_errors`（预测−实测，带符号）、`rmse`、`sse`、`rank`；
  - 失败候选：`status: "not_converged"`、`error`（中文原因），**单独标明并排最后**，
    不影响其他候选（`counts` 给出两类数量）；
- `indistinguishable` / `indistinguishable_nodes`：若至少有两个候选与最优 RMSE 相差
  **不超过 1e-4**，这些候选一律标为**无法区分**；
- `leak_detected` / `leak_node` / `conclusion`：**最优漏量为 0 时不判定存在漏损**
  （`leak_detected=false`、`leak_node=null`）；否则给出最可能的漏损节点与拟合漏量，
  并在结论文字中说明是否存在无法区分的并列候选。

curl 调用示例（该管网真值为 B 点额外漏损 0.8，实测水头取 A、C 两点）：

```bash
curl -s http://localhost:8000/api/locate-leak \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 10,
    "nodes": [
      {"id": "S", "type": "source", "head": 30.0},
      {"id": "A", "type": "demand", "demand": 1.0},
      {"id": "B", "type": "demand", "demand": 1.0},
      {"id": "C", "type": "demand", "demand": 1.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0},
      {"id": "P3", "start": "B", "end": "C", "r": 1.0},
      {"id": "P4", "start": "C", "end": "S", "r": 1.0},
      {"id": "P5", "start": "S", "end": "B", "r": 2.5}
    ],
    "measurements": [
      {"node": "A", "head": 27.959627},
      {"node": "C", "head": 27.959627}
    ],
    "leak_upper_bound": 2.0
  }'
```

返回（节选）：无漏损基线 RMSE≈0.7462；B 点以漏量 0.8 把 RMSE 降到 ≈0，明显优于 A、C
（RMSE≈0.1151），故判定漏损在 B；`head_errors` 为预测水头减实测水头：

```json
{
  "ok": true,
  "leak_analysis": {
    "leak_upper_bound": 2.0,
    "tie_tolerance": 0.0001,
    "baseline_rmse": 0.746177,
    "baseline_predicted_heads": {"A": 28.705804, "C": 28.705804},
    "candidates": [
      {"rank": 1, "node_id": "B", "status": "fitted", "leak": 0.8,
       "predicted_heads": {"A": 27.959627, "C": 27.959627},
       "head_errors": {"A": 0.0, "C": 0.0}, "rmse": 0.0},
      {"rank": 2, "node_id": "A", "status": "fitted", "leak": 0.74109,
       "predicted_heads": {"A": 27.874847, "C": 28.098663},
       "head_errors": {"A": -0.08478, "C": 0.139036}, "rmse": 0.115149},
      {"rank": 3, "node_id": "C", "status": "fitted", "leak": 0.74109,
       "predicted_heads": {"A": 28.098663, "C": 27.874847},
       "head_errors": {"A": 0.139036, "C": -0.08478}, "rmse": 0.115149}
    ],
    "counts": {"fitted": 3, "not_converged": 0},
    "best": {"node_id": "B", "leak": 0.8, "rmse": 0.0},
    "indistinguishable": false,
    "indistinguishable_nodes": ["B"],
    "leak_detected": true,
    "leak_node": "B",
    "conclusion": "最可能的单点漏损位于用水节点“B”，拟合漏量为 0.8（测点 RMSE=1.07e-14）。"
  }
}
```

非法输入（测点数不在 2～8、测点重复或不是用水节点、实测水头/上限非有限值、上限非正、
原有管网校验未通过等）返回 `422`/`400` 并附中文原因。

### 停供节点同时复供组合优选 `POST /api/plan-resupply`

> 复供优选与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。

在管网参数（与 `/api/solve` 完全一致，沿用同一套输入校验）之外，请求体还需包含：

- `resupply_nodes`：**2～6 个互异**的候选停供节点编号，必须**全部为表内需水量为正**
  的用水节点（不能是水源，也不能是零需水节点）；候选的处理顺序一律按**节点录入顺序**，
  与数组书写顺序无关。

后端把这些候选节点视为**当前停供**：

1. **先求复供前基线（空集）**：所有候选节点按**零需水**计算，其他节点（含非候选用水
   节点）保持表内需水量不变；基线若有用水节点水头低于当前最低水头限值，则现有管网在
   复供前即**欠压**，`status: "baseline_below_limit"`，**无法复供**（不再评估其他组合）。
2. 基线合格时，再核对包含空集在内的 **2^k 个组合**：每个组合中被选中的候选**恢复表内
   需水量**，未选中的候选继续按零需水；每个组合都独立做稳态求解。只有**稳态收敛且所有
   用水节点水头均不低于最低水头限值**的组合才标记为 `feasible`（可选）。
3. 在所有可行组合中，选**恢复需水量总和最大**者；**总量相同先选节点更多的，再按节点
   录入顺序的字典序选**（例：总量同为 4 时 `{A,B}` 胜 `{C}`，`{A,B}` 胜 `{A,C}`）。
4. **求解失败绝不按欠压处理**：基线不收敛时 `status: "baseline_not_converged"`、
   `baseline` 为 `null`，无法确认最优；任一非空组合不收敛时
   `status: "not_confirmed"`，该组合记为 `not_converged`（不计入欠压数）并排最后，
   其余组合照常评估，响应同时给出当前已确认可行组合中的**临时最优**
   （`selected_provisional: true`），并在 `conclusion` 中说明该结果可能被未求解的
   组合超越。

响应 `resupply_analysis` 含：

- `status`：`optimal`（全部组合收敛且最优已确认）/ `baseline_below_limit`（基线欠压，
  无法复供）/ `baseline_not_converged`（基线不收敛）/ `not_confirmed`（部分组合不收敛，
  无法确认最优）之一；`optimal_confirmed` 仅在 `optimal` 时为 `true`；
- `baseline`：复供前基线的完整稳态解（结构与 `/api/solve` 的 `result` 相同；
  基线不收敛时为 `null`，原因在 `baseline_error`），`baseline_below_min_head` 为基线
  低于限值的用水节点；
- `combinations`：全部 2^k 个组合（空集排第一参与枚举），按“可行→欠压→不收敛”分组、
  组内按上述优选规则排序并带 `rank`：
  - `feasible`：`nodes`（复供节点，按节点录入顺序）、`node_count`、`restored_demand`
    （恢复需水量总和）、`min_demand_head` / `min_demand_head_node`、
    `below_min_head: []` 与该组合的完整 `solution`；
  - `below_limit`：稳态收敛但有用水节点低于限值，含 `below_min_head`，**不可选**；
  - `not_converged`：含 `error`（中文原因），单独排最后，**不计为欠压**；
- `selected`：最优（或临时最优）组合记录；`restored_demand_total` 为恢复总量；
  `unrestored_nodes` 为仍按零需水、**未复供**的候选节点；
- `table_demands` / `table_demand_total`：候选节点的表内需水量与总和；
  `counts` 给出三类组合数量；`conclusion` 为中文结论。

curl 调用示例（树状网 S–A–B–C，候选 A/B/C 表内需水均为 2，限值 9）：

```bash
curl -s http://localhost:8000/api/plan-resupply \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 9,
    "nodes": [
      {"id": "S", "type": "source", "head": 30.0},
      {"id": "A", "type": "demand", "demand": 2.0},
      {"id": "B", "type": "demand", "demand": 2.0},
      {"id": "C", "type": "demand", "demand": 2.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0},
      {"id": "P3", "start": "B", "end": "C", "r": 1.0}
    ],
    "resupply_nodes": ["A", "B", "C"]
  }'
```

返回（节选）：复供前基线 A/B/C 全停（按零需水），各点水头均为 30；三节点同时复供会让
末端水头跌到 −26，`{A,C}`、`{B,C}` 也分别因 C、B/C 欠压不可选；可行组合中
`{A,B}` 恢复总量 4 最大（此时 H_A=14、H_B=H_C=10 均不低于限值 9），C 未复供：

```json
{
  "ok": true,
  "resupply_analysis": {
    "status": "optimal",
    "optimal_confirmed": true,
    "min_head": 9.0,
    "resupply_nodes": ["A", "B", "C"],
    "table_demands": {"A": 2.0, "B": 2.0, "C": 2.0},
    "table_demand_total": 6.0,
    "evaluated_combinations": 8,
    "total_combinations": 8,
    "baseline_below_min_head": [],
    "counts": {"feasible": 5, "below_limit": 3, "not_converged": 0},
    "selected": {
      "rank": 1,
      "status": "feasible",
      "nodes": ["A", "B"],
      "node_count": 2,
      "restored_demand": 4.0,
      "min_demand_head": 10.0,
      "min_demand_head_node": "B",
      "below_min_head": []
    },
    "restored_demand_total": 4.0,
    "unrestored_nodes": ["C"],
    "failed_combinations": [],
    "conclusion": "全部 8 个组合均已稳态收敛；在所有用水节点水头均不低于最低水头限值 9 的可行组合中，恢复需水量总和最大的组合为 {A、B}，恢复总量 4。"
  }
}
```

全部 8 个组合（节选，`feasible` 在前、`below_limit` 在后）：

| rank | 组合 | 状态 | 恢复总量 | 最低用水节点水头 | 低于限值节点 |
| --- | --- | --- | --- | --- | --- |
| 1 | `{A,B}` | feasible | 4 | 10（B） | — |
| 2 | `{A}` | feasible | 2 | 26（A） | — |
| 3 | `{B}` | feasible | 2 | 22（B） | — |
| 4 | `{C}` | feasible | 2 | 18（C） | — |
| 5 | `{}`（基线/空集） | feasible | 0 | 30 | — |
| 6 | `{A,B,C}` | below_limit | 6 | −26（C） | A、B、C |
| 7 | `{A,C}` | below_limit | 4 | 6（C） | C |
| 8 | `{B,C}` | below_limit | 4 | −6（C） | B、C |

非法输入（候选数不在 2～6、候选重复、不是用水节点或为水源、表内需水量非正、
`resupply_nodes` 不是数组、原有管网校验未通过等）返回 `422`/`400` 并附中文原因；
基线欠压与“无法确认最优”属于业务结论，HTTP 状态码仍为 `200`，请依据 `status` 字段区分。

### 停管隔离作业 `POST /api/isolation-jobs`（分步停管/复管）

> 停管隔离作业与求解共用同一服务、同一端口，**不需要任何额外服务或新的启动步骤**——
> `docker compose up --build` 后即可直接调用。作业**只保存在单进程内存中，服务重启
> 即全部清空**；该功能仅提供 HTTP API，**没有对应页面**。

一次“停管隔离作业”表示对同一张现有管网进行**一系列逐步的停管/复管操作**：

1. **创建作业** `POST /api/isolation-jobs`：请求体在 `/api/solve` 参数之外可带
   `allow_unserved`——**允许暂时失供的现有用水节点编号数组（可为空数组或省略）**。
   管段初始全部开启；后端沿用同一套校验与稳态求解，**初始状态下只要有用水节点欠压
   （水头低于 `min_head`）或求解不收敛，就不创建作业**（返回 `422` 与中文原因）。
   创建成功返回作业 `job_id` 与初始 `version: 0` 的完整状态快照。
2. **读取状态** `GET /api/isolation-jobs/{job_id}`：返回该作业当前版本的完整快照，
   快照整体取自同一版本，**绝不混合不同版本的数据**；作业不存在返回 `404`。
3. **切换一根管段** `POST /api/isolation-jobs/{job_id}/toggle`：请求体
   `{"version": 当前版本号, "pipe_id": "现有管段编号"}`，把该管段**由开转关或由关转开**。
   后端按**全部关闭管段**重新计算水源连通区并校验：
   - **失供节点**（与水源断开的用水节点）必须全部在 `allow_unserved` 集合内；
   - 连通区内用水节点**按当前需水量**重新稳态求解，且**任何用水节点不得欠压**；
   - 失供节点**保留当前需水量**，重新连通后自动恢复参与求解；
   - 连通区**仅剩水源时供水量为零**（不再调用求解器）。

   全部满足才**一次性提交**：关闭集合更新、`version` 加一、稳态结果替换；非法操作
   （未知管段、版本号类型错误等）、求解不收敛或上述规则不满足时，**关闭集合、版本号、
   稳态结果全部保持原状**，返回 `422` 并说明原因。
4. **修订用水节点需水量（无需重建作业）**
   `POST /api/isolation-jobs/{job_id}/demands`：请求体
   `{"version": 当前版本号, "demands": [{"node_id": 节点编号, "demand": 新需水量}, …]}`。   `demands` 必须是**非空修订列表**，每项的 `node_id` 必须是作业内**互异的现有用水
   节点**（不能是水源或未定义节点），`demand` 必须是**非负有限数**（允许 `0`，
   **拒绝布尔值**）。**只改列出的节点**：水源、管段、最低水头限值与允许失供集合
   一律不变。后端按**当前关闭集合**重算：
   - **连通节点**使用新需水量重新稳态求解，且**不得欠压**；
   - **失供节点也保存新值**、快照中的**失供需水量之和随之更新**；该节点以后
     **重连时自动按新值**参与求解（不会恢复成创建时的旧值）；
   - 连通区仅剩水源时不调用求解器，失供节点的修订同样立即生效。

   只有重算合格（无欠压且收敛）才**一次性同时提交**：新需水量、完整快照与
   `version` 加一；非法输入、欠压或不收敛时**需水量、关闭集合、版本号与稳态结果
   全部保持原状**，返回 `422` 并说明原因（失败不消耗版本）。**失供需水量之和等
   聚合值同样不得溢出**：单个节点允许取 `1e308` 这样的有限大值（后端**不设任何
   人为上限**，也不会用字符串、`null` 或截断掩盖），但多个大值相加溢出为
   `Infinity`、或候选快照中出现 `NaN`/`Infinity` 时（快照将无法以严格 JSON 编码），
   同样返回 `422` 并说明是哪个聚合/字段溢出，**需水量、关闭集合、快照与版本号
   全部保持原状**，用同一版本改发不溢出的修订即可重试。

`version` 是整个作业的**乐观锁**：`version` 与当前版本不一致时，切换与修订都返回
**`409`**——**同一版本的修订与管段切换合计最多成功一次**，先到者成功，其余一律
`409`（响应含 `current_version`），请重新读取最新状态后再试。

**计算期间的并发与读取语义**（单进程 Uvicorn 内完成，不引入任何外部服务）：

- 稳态求解可能耗时，但**不会占住服务**：创建、切换或修订在工作线程中计算，计算期间
  `GET /api/health`、**已存在作业的读取**以及**其他作业的合法操作**都能在该次计算
  结束前立即返回；
- 计算期间的读取**只返回最近一次已成功提交的完整快照**，绝不返回“算了一半”的中间
  结果（例如创建尚未成功时该作业尚不存在，读取按 `404` 处理）；
- **同一作业也允许多个操作（切换/修订）同时计算，后来的操作可以先算完先提交**。每个
  操作在提交的瞬间重新比对版本：若计算期间版本已被别人推进，则本次结果作废、返回
  `409` 与 `current_version`，**绝不覆盖新状态**——基于旧需水量算成的迟到修订不会
  覆盖先提交的新需水量；
- 切换与修订仍遵守允许失供、欠压与不收敛规则；这些失败**不推进版本**，用同一版本
  改发合法操作仍可成功（含在一次慢计算返回失败后立即重试）。
5. **预演切换顺序（不实际操作作业）**
   `POST /api/isolation-jobs/{job_id}/rehearsal`：请求体
   `{"version": 当前版本号, "pipes": [管段编号, …]}`（**2～6 根互异现有管段**，
   书写顺序不影响结果）。后端在该版本的关闭集合与当前需水量之上，搜索
   **每根恰好切换一次、其他管段不动**的可执行顺序：从基态起逐步应用同一套
   允许失供/最低水头/稳态收敛规则，**所有中间状态与终态都必须合法**，多条
   可行顺序按**管段录入序号序列的字典序**取最小。找到时逐步返回每根管段的
   开闭、关闭集合、失供节点及失供总量与连通区稳态结果；**无可行顺序明确返回
   不可行**；搜索途中任一状态**求解不收敛或出现非有限数**（含聚合溢出）都
   报告**无法确认**（`not_confirmed`，绝不当作不可行：可能附带一条临时可行
   顺序，但不保证其字典序最小性）。预演**不推进版本、不改需水量或快照**，
   计算不阻塞读取与其他作业；版本已变返回 `409`（含 `current_version`），
   未知/已结束作业返回 `404`（结束优先于冲突与计算错误）。详见下文
   “切换顺序预演示例”。
6. **结束作业** `DELETE /api/isolation-jobs/{job_id}`：作业不再使用时可随时结束，
   **无需重启服务即可清空该作业，也不需要请求体或 `version`**。成功返回
   `{"ok": true, "terminated_job": 作业ID}`；**未知 ID 与重复结束都返回 `404`**。
   - **正在求解也能立即结束**：结束不等待、也**不要求中断底层数值计算**——进行中
     的计算线程自然跑完，但结果一律作废；
   - 结束成功后，**新发起的读取、切换与需水量修订全部返回 `404`**，作业不能被
     任何操作恢复（同 ID 也不会被复用），重复结束同样 `404`；
   - **结束时尚未提交**的在途切换或修订：之后无论正常算出结果，还是触发失供/
     **欠压**、**不收敛**或**非有限数（NaN/Infinity/聚合溢出）错误**，到达提交点
     时都按 **`404`** 结束——不能再提交、不能返回成功（`200`）或失败（`422`），
     也不会恢复作业；
   - **结束前已经完成提交的操作保持原返回语义**（结束前先提交者仍为 `200`、
     结束前已确认的版本冲突仍为 `409`）；判定以“提交是否发生在结束之前”为准；
   - **其他作业与 `/api/health` 健康检查照常响应**；未结束作业继续遵守原版本
     冲突、失败回滚与快照规则，接口字段与状态码保持不变。

每次成功响应（创建、读取、切换、修订）都返回最新快照，主要字段：`demands`
（**按节点录入顺序的全部当前需水量**，每项 `{"node_id", "demand"}`，含失供节点
保存的新值）、`closed_pipes` / `open_pipes` / `pipe_states`（关闭管段与逐管开闭）、
`unserved_nodes` / `unserved_demand`（失供节点与失供量之和）、
`component_nodes` / `component_pipes`（水源连通区）、`source_only`、`source_supply` /
`total_demand`，以及 `solution`（连通区上的完整稳态解，结构与 `/api/solve` 的
`result` 相同；仅剩水源时为供水量为零的平凡解）。修订接口的成功响应另带
`revised_demands`（本次实际生效的修订项），切换接口另带 `toggled_pipe` /
`toggled_to`，其余字段与状态码保持不变。

#### 操作示例

以树状网 `S(50) ─P1→ A(需2) ─P2→ B(需3)` 为例，允许 B 暂时失供：

```bash
# 1) 创建作业（管段初始全开），记下返回的 job_id
curl -s -X POST http://localhost:8000/api/isolation-jobs \
  -H 'Content-Type: application/json' \
  -d '{
    "min_head": 0,
    "nodes": [
      {"id": "S", "type": "source", "head": 50.0},
      {"id": "A", "type": "demand", "demand": 2.0},
      {"id": "B", "type": "demand", "demand": 3.0}
    ],
    "pipes": [
      {"id": "P1", "start": "S", "end": "A", "r": 1.0},
      {"id": "P2", "start": "A", "end": "B", "r": 1.0}
    ],
    "allow_unserved": ["B"]
  }'
```

返回（节选）：初始版本 0，无关闭管段、无失供节点，供水量 5：

```json
{"ok": true, "job": {"job_id": "05ced70d…（每次创建随机生成）", "version": 0,
 "closed_pipes": [], "unserved_nodes": [], "component_nodes": ["S", "A", "B"],
 "source_supply": 5.0, "total_demand": 5.0}}
```

```bash
# 2) 携带当前版本关闭 P2：B 失供但在允许集合内，操作成功，版本 0 → 1
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 0, "pipe_id": "P2"}'
```

返回（节选）：连通区只剩 S、A，按原需水量求解，供水量降为 2：

```json
{"ok": true, "toggled_pipe": "P2", "toggled_to": "closed",
 "job": {"version": 1, "closed_pipes": ["P2"], "unserved_nodes": ["B"],
 "unserved_demand": 3.0, "component_nodes": ["S", "A"], "source_supply": 2.0}}
```

#### 冲突示例（同一版本最多成功一次）

```bash
# 3) 仍携带旧版本 0 再操作：版本已推进到 1，返回 409，状态不变
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 0, "pipe_id": "P1"}'
```

```json
{"ok": false, "current_version": 1,
 "error": "版本冲突：请求携带的版本为 0，作业当前版本已为 1；同一版本的修订与管段切换合计最多成功一次，请重新读取最新状态后再试。"}
```

并发场景同理：多个客户端携带**同一版本**同时进行切换或修订时，**合计最多一个成功**
（版本 +1），其余全部收到 `409`，重新读取后再携带新版本重试。并发的操作可以**同时
进行计算**——哪怕后来的请求先算完，也由提交瞬间的版本比对决定胜负：先提交者成功，
基于旧版本（或旧需水量）算成的后来提交者收 `409`（响应带 `current_version`），其
结果被直接丢弃，**不会覆盖**先提交的新状态（含新需水量）。计算进行期间，读取接口
始终只返回**最近已提交**的版本快照。

#### 规则不满足示例（全部保持原状）

```bash
# 4) 携带当前版本 1 关闭 P1：A 将失供但不在允许集合内，返回 422，状态不变
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "pipe_id": "P1"}'
```

```json
{"ok": false,
 "error": "切换后用水节点 “A” 将失供，但其不在允许暂时失供的节点集合内；本次操作未生效，关闭集合、版本与稳态结果全部保持原状。"}
```

欠压（切换后连通区内有用水节点水头低于 `min_head`）与求解不收敛同样返回 `422`
并全部保持原状；**失败不消耗版本号**，用同一版本改发合法操作仍可成功。

```bash
# 5) 重开 P2：B 恢复当前需水量重新参与求解，供水量回到 5，版本 1 → 2
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "pipe_id": "P2"}'

# 6) 随时读取当前版本完整快照；服务重启后作业清空，再读返回 404
curl -s http://localhost:8000/api/isolation-jobs/<job_id>
```

#### 修订需水量示例（作业执行中修改，无需重建作业）

沿用上面版本 1 的状态（P2 关闭、B 失供，连通区为 S、A）。把**失供节点 B** 的需水量
改为 1、同时把**连通节点 A** 的需水量改为 4——失供节点保存新值并更新失供总量，
连通区按 A 的新值重新求解：

```bash
# 7) 携带当前版本提交非空修订列表（每项 node_id + demand），版本 1 → 2
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/demands \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "demands": [{"node_id": "B", "demand": 1},
                                  {"node_id": "A", "demand": 4}]}'
```

返回（节选）：快照 `demands` 是**按节点录入顺序的全部当前需水量**，B 虽失供也保存
新值；失供总量降为 1，连通区供水量变为 4：

```json
{"ok": true,
 "revised_demands": [{"node_id": "B", "demand": 1.0}, {"node_id": "A", "demand": 4.0}],
 "job": {"version": 2, "closed_pipes": ["P2"],
  "demands": [{"node_id": "A", "demand": 4.0}, {"node_id": "B", "demand": 1.0}],
  "unserved_nodes": ["B"], "unserved_demand": 1.0,
  "component_nodes": ["S", "A"], "source_supply": 4.0, "total_demand": 4.0}}
```

```bash
# 8) 重连示例：重开 P2，B 自动按“新值 1”（而非旧值 3）参与求解，版本 2 → 3
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 2, "pipe_id": "P2"}'
```

返回（节选）：新需水量 A=4、B=1，供水量为 **5**，H_A=34、H_B=33：

```json
{"ok": true, "toggled_pipe": "P2", "toggled_to": "open",
 "job": {"version": 3, "closed_pipes": [], "unserved_nodes": [],
  "demands": [{"node_id": "A", "demand": 4.0}, {"node_id": "B", "demand": 1.0}],
  "source_supply": 5.0, "total_demand": 5.0}}
```

非法修订（`demands` 为空或不是数组、`node_id` 缺失/重复/是水源或未知节点、
`demand` 为负数/布尔/字符串/`NaN` 等）返回 `400`/`422`；修订后连通节点欠压或求解
不收敛同样返回 `422`，**需水量与版本均保持原状**，可用同一版本重试：

```bash
# 9) 把 A 改成 100：修订后水头低于限值 0，返回 422，版本仍是 3、需水量不变
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/demands \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "demands": [{"node_id": "A", "demand": 100}]}'
```

```json
{"ok": false,
 "error": "修订需水量后连通区内用水节点 “A”、“B” 的水头将低于最低水头限值 0；本次操作未生效，需水量、关闭集合、版本与稳态结果全部保持原状。"}
```

#### 聚合溢出失败后重试示例（不留下不可返回的快照）

失供需水量之和等聚合值溢出时同样 `422`，且**需水量、关闭集合、快照与版本号
全部保持原状**——后端不会把含 `Infinity` 的快照提交出去（那种快照读回来无法
以严格 JSON 编码），也**不用任何人为上限、字符串、`null` 或截断掩盖**。以
`S(50) ─P1→ A(需2) ─P2→ B(需3)`、`min_head: 0`、允许 A/B 失供为例，关闭
`P1`（S—A）后 A、B 同时失供，此时版本为 1：

```bash
# 10) 版本 1 把 A、B 同时改为 1e308：两个有限大值相加溢出为 Infinity，返回 422
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/demands \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "demands": [{"node_id": "A", "demand": 1e308},
                                  {"node_id": "B", "demand": 1e308}]}'
```

```json
{"ok": false,
 "error": "修订需水量后，失供节点（“A”、“B”）保存的新需水量之和（失供总量）溢出为非有限值（inf），超出浮点可表示范围，候选快照无法以严格 JSON 编码；本次修订未生效，需水量、关闭集合、快照与版本号全部保持原状。请把相关失供节点的需水量改小后用同一版本重试（单个 1e308 这样的有限大值仍然允许，只要聚合不溢出；不得用上限、字符串、null 或截断掩盖溢出）。"}
```

失败后读取仍是**版本 1 的旧状态**（A=2、B=3，失供总量 5），随后用同一版本
重试即可：

```bash
# 11) GET 仍是版本 1 的旧快照（严格合法 JSON）；单个 1e308 未溢出，重试成功
curl -s http://localhost:8000/api/isolation-jobs/<job_id>
# => "version": 1, "demands": [A=2.0, B=3.0], "unserved_demand": 5.0

curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/demands \
  -H 'Content-Type: application/json' \
  -d '{"version": 1, "demands": [{"node_id": "A", "demand": 1e308}]}'
```

返回（节选）：单个 `1e308` 是有限值、失供总量仍是有限的 `1e308`，修订成功、
版本 1 → 2（响应仍为严格合法 JSON）；之后再把 B 改成 `1e308` 使聚合溢出时，
依旧是 `422`、版本不推进，继续用版本 2 改发不溢出的值即可。

```json
{"ok": true,
 "revised_demands": [{"node_id": "A", "demand": 1e308}],
 "job": {"version": 2, "closed_pipes": ["P1"],
  "demands": [{"node_id": "A", "demand": 1e308}, {"node_id": "B", "demand": 3.0}],
  "unserved_nodes": ["A", "B"], "unserved_demand": 1e308,
  "component_nodes": ["S"], "source_only": true, "source_supply": 0.0}}
```

#### 修订冲突示例（修订与切换共用同一把版本锁）

```bash
# 10) 仍携带旧版本 3 修订：版本已被推进（如别处刚成功切换），返回 409，状态不变
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/demands \
  -H 'Content-Type: application/json' \
  -d '{"version": 3, "demands": [{"node_id": "A", "demand": 2}]}'
```

```json
{"ok": false, "current_version": 4,
 "error": "版本冲突：请求携带的版本为 3，作业当前版本已为 4；同一版本的修订与管段切换合计最多成功一次，请重新读取最新状态后再试。"}
```

并发场景下，携带**同一版本**的修订与管段切换**合计最多一个成功**（版本 +1），
其余全部收到 `409`；多个操作可以**同时进行计算**——哪怕后来的请求先算完，也由
提交瞬间的版本比对决定胜负，**基于旧需水量算成的迟到修订会被直接丢弃，绝不会覆盖
先提交的新需水量**。计算进行期间，读取接口始终只返回**最近已提交**的版本快照。

#### 切换顺序预演示例（只预演、不实际操作作业）

当一次隔离作业需要连续切换好几根管段、又想在动手前确认“按什么顺序切才不会
在中途踩失供/欠压红线”时，调用

```
POST /api/isolation-jobs/{job_id}/rehearsal
```

请求体为 `{"version": 当前版本号, "pipes": 管段编号数组}`：

- `pipes` 必须是 **2～6 根互异的现有管段**（非空字符串，不能是未定义编号），
  **书写顺序不影响结果**——后端一律按**管段录入序号**归并；
- 预演在**该版本的关闭集合与当前需水量**之上进行：指定的每根管段**恰好切换
  一次**（基态开着的关掉、基态关着的重开），**其他管段全程不动**；
- 从基态起逐步核对**同一套规则**：失供节点必须全部在允许集合内、连通区用水
  节点不得低于最低水头、稳态求解收敛且快照不含非有限数——**所有中间状态与
  终态都必须合法**；
- 多条可行顺序按**管段录入序号序列的字典序**取最小；**预演不推进版本、不改
  需水量或快照**，计算在工作线程进行，不阻塞健康检查、该作业读取与其他作业。

响应 `rehearsal` 的 `status` 有三种：

- `feasible`：已找到字典序最小的可行顺序（`minimum_confirmed: true`）；
- `infeasible`：全部顺序都至少有一个中间状态或终态明确不合法，**无可行顺序**；
- `not_confirmed`：搜索分支中出现**求解不收敛或非有限数**（含失供总量聚合
  溢出），无法确认结论——**绝不把这类情况当作不可行**；若同时找到了可行顺序，
  会作为**临时结果**返回（`plan` 非空但 `minimum_confirmed: false`），并在
  `unconfirmed_states` 列出无法确认的中间状态（到达它的步骤前缀、`reason`
  为 `not_converged`/`overflow`/`non_finite` 及对应说明；溢出值以 `repr`
  字符串描述，响应始终是严格合法 JSON）。

`plan` 含 `pipe_order` / `pipe_entry_indices`（字典序最小的顺序及各自录入
序号），`steps` 逐步给出 `pipe_id`、`action`（`close`/`open`）、切换前后
开闭、该步的关闭集合、`unserved_nodes`/`unserved_demand`、水源连通区与
完整 `solution`；终态另以 `final_*` 字段给出。`base_version` 与
`base_snapshot` 标明预演所基于的版本与基态快照；`state_counts` 与
`evaluated_states` 给出搜索中合法/明确非法/无法确认状态的统计。

沿用树状网 `S(50) ─P1→ A(需2) ─P2→ B(需3)`、允许 A、B 暂时失供（两根都是
桥：先关 P1 时连通区仅剩水源，随后再关 P2 仍合法；反过来先关 P2 再关 P1
同样合法，字典序取录入序号 1→2，即 P1→P2）：

```bash
# 13) 预演：把 P1、P2 各切一次（数组书写顺序不影响结果）
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/rehearsal \
  -H 'Content-Type: application/json' \
  -d '{"version": 0, "pipes": ["P2", "P1"]}'
```

返回（节选）：状态 `feasible`，字典序最小顺序为 **P1 → P2**，每一步的
关闭集合、失供节点与连通区稳态结果都随响应给出；作业本身仍停留在版本 0：

```json
{"ok": true, "rehearsal": {
  "status": "feasible", "minimum_confirmed": true,
  "base_version": 0, "requested_pipes": ["P1", "P2"],
  "requested_pipe_entry_indices": [1, 2],
  "pipe_count": 2, "total_orders": 2, "evaluated_states": 2,
  "state_counts": {"legal": 2, "illegal": 0, "unconfirmed": 0},
  "unconfirmed_states": [],
  "base_closed_pipes": [],
  "plan": {
    "pipe_order": ["P1", "P2"], "pipe_entry_indices": [1, 2],
    "steps": [
      {"step": 1, "pipe_id": "P1", "action": "close",
       "toggled_from": "open", "toggled_to": "closed",
       "closed_pipes": ["P1"], "unserved_nodes": ["A", "B"],
       "unserved_demand": 5.0, "component_nodes": ["S"],
       "source_only": true, "source_supply": 0.0, "total_demand": 0.0},
      {"step": 2, "pipe_id": "P2", "action": "close",
       "toggled_from": "open", "toggled_to": "closed",
       "closed_pipes": ["P1", "P2"], "unserved_nodes": ["A", "B"],
       "unserved_demand": 5.0, "component_nodes": ["S"],
       "source_only": true, "source_supply": 0.0, "total_demand": 0.0}
    ],
    "final_closed_pipes": ["P1", "P2"], "final_open_pipes": [],
    "final_unserved_nodes": ["A", "B"], "final_unserved_demand": 5.0,
    "final_component_nodes": ["S"], "final_source_only": true,
    "final_source_supply": 0.0, "final_total_demand": 0.0}}}
```

预演结束后读取作业，仍是预演前的版本与关闭集合（**预演绝不切换任何管段**）：

```bash
curl -s http://localhost:8000/api/isolation-jobs/<job_id>
# => "version": 0, "closed_pipes": []
```

若允许失供集合为空，同一请求会明确返回 `infeasible`（两根管都关的终态必然
让 A、B 失供，且任何顺序都无法避免中间或终态违规）；若搜索途中某个中间
状态的稳态求解不收敛或出现 NaN/Infinity（含失供总量聚合溢出），状态为
`not_confirmed`——此时**不能宣称不可行**：响应要么带上一条已确认可行的
临时顺序（其字典序最小性无法确认），要么 `plan` 为 `null` 并在
`unconfirmed_states` 说明卡在哪个步骤、原因是什么。

预演同样受版本乐观锁约束，但**不消耗版本**：携带落后版本返回 `409` 与
`current_version`（例如预演计算期间该作业被别的请求成功切换），重新读取
后携带新版本再预演即可；未知作业或作业已结束返回 `404`——即使预演请求
在计算途中作业才被结束，计算结束时也按 **`404`** 返回（**结束优先于冲突
与计算错误**，不会出现 200/409）。`pipes` 数量不在 2～6、编号重复或不是
现有管段、`version` 类型错误等返回 `422`，空请求体/非法 JSON 返回 `400`。

#### 结束作业示例（不再使用即结束，无需重启与 version）

```bash
# 12) 结束作业：无需请求体、无需版本号，即使正在求解也立即返回
curl -s -X DELETE http://localhost:8000/api/isolation-jobs/<job_id>
```

成功返回该作业 ID：

```json
{"ok": true, "terminated_job": "<job_id>"}
```

结束成功后，新发起的读取、切换与修订一律 `404`，作业不会被恢复；重复结束同样
`404`，未知 ID 也是 `404`：

```bash
curl -s http://localhost:8000/api/isolation-jobs/<job_id>
# => 404
curl -s -X DELETE http://localhost:8000/api/isolation-jobs/<job_id>
# => 404（重复结束）
curl -s -X POST http://localhost:8000/api/isolation-jobs/<job_id>/toggle \
  -H 'Content-Type: application/json' \
  -d '{"version": 0, "pipe_id": "P2"}'
# => 404（结束后不能再提交或返回成功）
```

```json
{"ok": false,
 "error": "停管隔离作业“<job_id>”不存在（作业仅存于单进程内存，服务重启后即清空）。"}
```

若切换或修订在**结束时已经发起但尚未提交**（例如其重算仍在进行），结束会立即
返回而不等待该计算；之后该计算无论正常算出结果，还是触发失供/欠压、不收敛或
非有限数错误，HTTP 响应都统一为 **`404`**（不会出现 `200`/`422`），结果直接
丢弃、作业不复活。反过来，**结束前已经完成提交**的操作不受影响：先提交成功的
仍为 `200`，结束前已确认的落后版本仍为 `409`。其他作业与健康检查全程照常。

## 输入约束汇总

- 节点 2～10 个，编号为非空且唯一的字符串；
- 恰好 1 个 `source`（固定水头，有限数值），其余为 `demand`（需水量为非负有限数）；
- 管段 1～16 根，编号唯一，起点/终点均为已定义且互不相同的节点，同一对节点之间只允许一根管段，阻力 r 为正有限数；
- 管网必须是**无向连通图**（孤立节点/孤岛一律拒绝）；
- 不接受 `NaN` / `Infinity`（含 JSON 文本中的 `NaN`/`Infinity` 记号）；
- 仅 `/api/locate-leak` 额外要求：`measurements` 为 2～8 个测点，节点编号互异且均为
  用水节点，实测水头为有限数值；`leak_upper_bound` 为正有限数。
- 仅 `/api/plan-resupply` 额外要求：`resupply_nodes` 为 2～6 个互异节点编号，均为
  表内需水量为正的用水节点（不能是水源或零需水节点）；候选顺序按节点录入顺序归并。
- 仅停管隔离作业：创建时 `allow_unserved` 为可选数组（可为空），元素为**互异的现有
  用水节点**编号；切换时请求体为 `{"version": 非负整数, "pipe_id": 现有管段编号}`；
  修订时请求体为 `{"version": 非负整数, "demands": 非空修订列表}`，列表每项为
  `{"node_id": 互异的现有用水节点编号, "demand": 非负有限数}`（允许 0、拒绝布尔值），
  只改列出的节点；单个需水量允许 `1e308` 这样的有限大值（**不设人为上限**，也不用
  字符串/`null`/截断掩盖），但失供需水量之和等聚合值溢出为非有限数或候选快照含
  `NaN`/`Infinity` 时按规则 `422` 且不改变任何状态（同版本可重试）；两类操作的
  `version` 都必须等于作业当前版本（同一版本的修订与切换合计最多成功一次，冲突
  返回 `409` 并带 `current_version`），非法输入、欠压、不收敛或聚合溢出返回
  `400`/`422` 且不改变状态；**预演** `POST .../rehearsal` 的 `pipes` 为 2～6 根
  互异的现有管段编号（按管段录入序号归并，与书写顺序无关），每根恰好切换一次、
  其他管段不动，所有中间状态与终态都须满足上述规则，按录入序号序列字典序返回
  最小可行顺序（含逐步开闭/关闭集合/失供节点与总量/连通区稳态结果），无可行顺序
  明确返回不可行，搜索途中不收敛或出现非有限数只报无法确认（绝不当作不可行）；
  预演不推进版本、不改需水量或快照，落后版本返回 `409`（带 `current_version`），
  未知/已结束作业返回 `404`（结束优先于冲突与计算错误）；作业只存于单进程内存，
  重启即清空，也可随时以
  `DELETE /api/isolation-jobs/{job_id}` 结束（无需 version，成功返回该 ID，
  未知 ID/重复结束 `404`；结束后新发起的读取/切换/修订/预演与结束时未提交的在途
  操作一律 `404`，不影响其他作业与健康检查）。

## 项目结构

```
.
├── app/
│   ├── main.py            # FastAPI 入口：/api/solve、/api/analyze-outages、/api/locate-leak、/api/plan-resupply、/api/isolation-jobs（创建/读取/切换/修订需水量/切换顺序预演/结束）、/api/health、静态页面
│   ├── solver.py          # 输入校验 + Newton 稳态求解 + 1e-6 复核 + 逐根停管情景分析 + 单点漏损定位（黄金分割）+ 停供节点复供组合枚举
│   ├── isolation.py       # 停管隔离作业：内存作业注册表、水源连通区计算、失供/欠压规则（纯状态评估：合法/明确非法/无法确认三分）、需水量修订、版本乐观锁（锁外重算+提交时版本比对：计算期间可交互、同版本修订/切换合计最多成功一次、409 不覆盖新需水量）、切换顺序预演（录入序号字典序 DFS+子集记忆化：全部中间状态与终态合法、不收敛/非有限数只报无法确认、纯预演不提交）、结束作业（摘除注册表+终止标记：新操作与结束时未提交的在途操作一律 404，不中断计算、不影响其他作业）
│   └── static/
│       ├── index.html     # 原生页面（含停管分析入口与结果表）
│       ├── style.css
│       └── app.js         # 可编辑表格、力图布局、拖拽、流向绘制、停管分析
├── tests/
│   ├── test_solver.py     # pytest：解析解、环状网、校验分支、API、随机压力测试、停管分析排序、漏损定位、复供组合优选
│   ├── test_isolation.py  # pytest：停管隔离作业的创建/切换/需水量修订/规则/版本冲突/并发/快照一致性/结束
│   └── test_rehearsal.py  # pytest：切换顺序预演的字典序最小、逐步开闭与稳态结果、不可行、不收敛/非有限数“无法确认”、纯预演不提交、409/404（结束优先）与计算期间并发
├── Dockerfile
├── docker-compose.yml
└── requirements.txt
```

## 测试

```bash
pip install -r requirements.txt pytest httpx
PYTHONPATH=. pytest -q
```

测试包含单管解析解、反向录入符号、环状网独立复核、低水头标记、各类非法输入、`NaN` JSON 拒绝，以及 60 个随机连通管网（2～10 节点、最多 16 管、阻力跨 6 个数量级）的收敛性与残差检查；停管分析部分覆盖树状网全断联按失供量排序、环+枝混合分组排序、比基线新增低水头节点、同分保持录入顺序、单情景不收敛单独排最后，以及 `/api/analyze-outages` 的 200/422/400 分支；漏损定位部分覆盖真值漏点与漏量回收、无漏损基线 RMSE=0 不判漏、对称网络多候选无法区分、最优漏量落在上限端点、测点/上限各类非法输入、基线不收敛整体报错、单候选失败隔离排最后，以及 `/api/locate-leak` 的 200/422/400 分支；复供优选部分覆盖可行组合按恢复总量排序、总量同分先比节点数再按录入顺序字典序、空集最优、基线欠压无法复供、非候选节点需水不变、基线不收敛与单组合不收敛的“无法确认最优”区分（求解失败不计欠压）、候选 2～6 个/互异/正需水等校验分支，以及 `/api/plan-resupply` 的 200/422/400 分支；停管隔离作业部分覆盖创建（初始欠压/不收敛不创建、`allow_unserved` 校验与录入顺序归并）、切换管段的失供/欠压/不收敛规则（失败全部保持原状且不消耗版本）、**作业执行中修订需水量**（只改列出节点、连通节点按新值求解、失供节点保存新值并更新失供总量、重连后按新值而非旧值、零需水量与整数接受/布尔拒绝等全部校验分支、欠压/不收敛不改变状态且不消耗版本、快照补充按录入顺序的全部当前需水量、**失供总量聚合溢出（两个 1e308 相加为 Infinity）时返回 422 且需水量/关闭集合/快照/版本全部保持原状、422 与随后 GET 的快照都可按严格 JSON（拒绝 NaN/Infinity 记号）解析、失败后同版本改发单个 1e308 等未溢出修订成功且不设人为上限、溢出失败后改回小值重连恢复正常**）、失供节点保留当前需水量并在重连后恢复、仅剩水源供水量为零、版本冲突 409（修订与切换共用同一把版本锁）、多线程并发同一版本修订与切换合计最多成功一次、读取快照不混合版本、两作业相互独立，以及未知作业 404 与各类 400/422 分支；**计算期间的 HTTP 并发**部分在守护线程内启动真实单进程 Uvicorn（随机端口，与 Compose 同一进程模型）并用**可暂停/可放行的求解门**（测试钩子 stage 区分 create/toggle/revise，在求解前阻塞目标重算，不靠大管网或长时间等待）验证：计算期间健康检查、已提交快照读取与其他作业操作均能立即返回，读取只给最近已提交快照；同一作业后来的合法操作（含修订）可先算完先提交，迟到提交收 409 与 `current_version` 且**基于旧需水量算成的修订绝不覆盖先提交的新需水量**；同版本修订与切换并发时先提交者成功、另一类操作收 409 且互不污染；被暂停的失供/欠压/欠压修订失败放行后不推进版本，同版本改发合法操作重试成功；创建计算期间其他请求同样可交互；**结束作业**部分覆盖：结束成功返回该 ID 且无需 version、未知 ID 与重复结束 `404`、求解暂停期间结束立即返回（不等待也不中断底层计算、期间健康检查与其他作业照常）、结束后新发起的读取/切换/修订/重复结束全部 `404`（不能再提交、成功或恢复作业、同 ID 不复用）、结束时尚未提交的在途操作放行后无论算成还是触发失供/欠压/不收敛/非有限数溢出都按 `404` 放弃（而非 `200`/`422`）、结束前已提交的操作保持原 `200`/`409` 语义（含基态版本已被推进的迟到修订在结束后仍按 `404` 而非 `409`），以及结束不影响其他作业继续遵守版本冲突/失败回滚/快照规则与健康检查；**切换顺序预演**
部分覆盖：可行顺序按管段录入序号序列字典序取最小（与请求数组书写顺序无关）、
每根恰好切换一次且其他管段不动、逐步开闭/关闭集合/失供节点与总量/连通区稳态
结果正确、基态已关闭管段的“先关后开”混合序列（含中途失供后由重开恢复供水）、
所有中间状态与终态必须合法（失供超允许集合/欠压前缀剪枝）、无可行顺序明确
infeasible、搜索途中求解不收敛或候选含 Infinity/聚合溢出时为 not_confirmed
（绝不当作不可行；含“找到临时顺序但最小性无法确认”与“完全无顺序”两种）、
预演不推进版本/不改需水量/不改快照、2～6 根互异现有管段与 version 的全部输入
校验、未知/已结束作业 404（结束优先于冲突与计算错误）、落后版本 409 与
current_version、响应严格 JSON 可编码；并用可暂停求解门（stage="rehearsal"）
在真实 Uvicorn 进程上验证：预演计算期间健康检查/读取/其他作业立即响应、预演
不提交任何状态，计算期间版本被推进则结束时返回 409，计算期间作业被结束则
放行后返回 404（即使本会算成 200，或版本同时已被推进——结束优先）。
