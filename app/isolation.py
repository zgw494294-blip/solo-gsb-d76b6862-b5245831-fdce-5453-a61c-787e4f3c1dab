"""
停管隔离作业 —— 可逐步执行的停管/复管操作与作业内需水量修订（仅存于单进程
内存，重启即清空）。

每个作业记录：创建时校验通过的管网（沿用 solver 的校验与稳态求解；需水量可在
作业执行中修订，水源、管段、最低水头与允许失供集合永不改变）、允许暂时失供的
用水节点集合（可为空）、当前关闭管段集合、版本号与最近一次成功计算的状态快照。
管段初始全部开启。

每次“切换一根现有管段的开闭”或“修订一批用水节点的需水量”，都按全部关闭
管段重新计算水源连通区：

  * 失供节点（与水源断开的用水节点）必须全部在允许暂时失供的集合内；
  * 连通区内用水节点按当前需水量（含已提交的修订）求解，且任何用水节点水头
    不得低于最低限值；
  * 失供节点也保存新需水量并计入失供总量，重新连通后自动按新值参与求解；
  * 失供需水量之和等任何聚合值都不得溢出为非有限数，候选快照内的每个数值也
    都必须可严格 JSON 编码（允许把单个节点改为 1e308 这样的有限大值，但多个
    大值相加溢出时按规则失败）；
  * 连通区仅剩水源时供水量为零（不再调用求解器）。

只有全部规则满足且稳态求解收敛时才一次性提交：切换提交关闭集合、修订提交
需水量，二者都提交完整快照并把版本号 +1；非法操作、聚合溢出、快照含非有限
数、不收敛或规则不满足时全部保持原状并说明原因。版本号即乐观锁：同一版本的
修订与管段切换合计最多成功一次；读取始终返回某一版本的完整快照，绝不混合
不同版本的数据。

``terminate_job`` 结束不再使用的作业：无需重启服务、无需请求体与版本号，
成功即把作业从注册表移除并返回该 ID，未知 ID 或重复结束按 404 处理。结束不
中断底层数值计算，也不等待进行中的计算：作业被打上终止标记并摘除后，新发起
的读取、切换与修订一律 404；结束时尚未提交的切换或修订（无论之后算出结果、
触发欠压/不收敛/非有限数错误，还是其基态版本已被并发推进）在到达提交点时
发现标记，全部按 404 结束，不能再提交、不能返回成功、也不会恢复作业。结束
前已经完成提交的操作保持原有返回语义。其他作业与健康检查不受影响。

耗时的连通区重算与稳态求解在作业锁之外进行，因此计算期间健康检查、已提交
快照的读取以及其他作业的操作都不会被阻塞；同一作业也允许后来的切换先算完
并先提交。临界区只在提交瞬间持有：提交时重新比对版本，版本已被后来者推进
则本次结果作废、返回 409 与当前版本，绝不覆盖新状态；规则校验或求解失败
同样不推进版本。

模块级 ``_COMPUTE_GATE`` 仅供测试使用：生产环境为 None；测试可把它替换成
可调用对象，在每次重算开始（规则判定与求解器调用之前）同步阻塞该次计算，
从而用“暂停/放行”而不是大管网或长时间等待来确定 HTTP 并发顺序。
``stage`` 取 ``"create"`` / ``"toggle"`` / ``"revise"`` / ``"rehearse"``，
分别对应创建、管段切换、需水量修订与切换顺序预演触发的重算（预演只在搜索
开始时触发一次 gate）。

``rehearse_switch_plan`` 为隔离作业做**纯预演**：携带版本与 2～6 根互异的
现有管段，在该版本的关闭集合与当前需水量之上，搜索“每根恰好切换一次、
其他管段不动”的可执行切换顺序（不提交任何状态）。所有中间状态与终态都
必须满足同一套允许失供/最低水头/稳态收敛规则；按管段录入序号序列的
字典序取最小的可行顺序。搜索途中任何状态求解不收敛或出现非有限数（含
聚合溢出）都只令结论变为“无法确认”，绝不当作不可行。预演在锁外计算，
不推进版本、不改需水量或快照，也不阻塞读取和其他作业；作业在请求时或
计算结束时已被结束返回 404（结束优先于冲突与计算错误），版本已变返回
409 与 current_version。
"""

from __future__ import annotations

import math
import threading
import uuid
from collections import deque
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from .solver import (
    CHECK_TOL,
    Network,
    SolverNotConverged,
    ValidationError,
    _as_finite,
    solve_network,
    validate_payload,
)


class JobNotFoundError(KeyError):
    """作业编号不存在（作业仅存于单进程内存，服务重启后即清空）。"""


class VersionConflictError(RuntimeError):
    """请求携带的版本与作业当前版本不一致：同一版本最多允许一次成功提交。"""

    def __init__(self, expected: int, current: int) -> None:
        super().__init__(
            f"版本冲突：请求携带的版本为 {expected}，作业当前版本已为 {current}；"
            "同一版本的修订与管段切换合计最多成功一次，"
            "请重新读取最新状态后再试。")
        self.expected = expected
        self.current = current


class IsolationRuleError(RuntimeError):
    """切换后不满足停管隔离规则（或连通区求解不收敛）：作业状态全部保持原状。"""


class StateUnconfirmedError(RuntimeError):
    """某关闭集合的状态无法确认（求解不收敛/聚合溢出/候选含非有限值）。

    与“明确不合法”（失供超允许集合、欠压）严格区分：隔离作业的真实切换中
    这些情况按失败处理（``_build_snapshot`` 转成 ``IsolationRuleError``）；
    但预演搜索（``rehearse_switch_plan``）途中遇到时只能报告“无法确认”，
    绝不能当作不可行分支剪枝，更不能据此宣称无可行顺序。
    """

    def __init__(self, kind: str, detail: Any = None) -> None:
        super().__init__(f"状态无法确认（{kind}）")
        self.kind = kind  # "not_converged" / "overflow" / "non_finite"
        self.detail = detail


class _IllegalStateError(IsolationRuleError):
    """关闭集合明确不满足隔离规则（内部信号）：kind 为 "unserved"/"below_head"。

    携带不达标节点列表，由 ``_build_snapshot`` 按调用阶段（create/toggle/
    revise）组装与历史完全一致的中文错误文案。
    """

    def __init__(self, kind: str, nodes: list[str]) -> None:
        super().__init__(kind)
        self.kind = kind
        self.nodes = nodes


# 预演请求中指定管段的根数范围
MIN_REHEARSE_PIPES, MAX_REHEARSE_PIPES = 2, 6


@dataclass
class _IsolationJob:
    """单个停管隔离作业的全部可变状态（仅在持有自身锁时修改）。"""
    job_id: str
    network: Network
    allowed_unserved: frozenset[str]
    closed_pipes: frozenset[str]
    version: int
    snapshot: dict[str, Any]
    lock: threading.Lock = field(default_factory=threading.Lock)
    # 结束标记：置位后作业已从注册表摘除（新发起的读取/切换/修订直接 404）；
    # 结束时尚未提交、仍在锁外重算的操作到达提交点时据此放弃结果，无论成功
    # 还是规则失败都按 404 处理，绝不提交、成功或恢复作业。
    terminated: bool = False


# 作业注册表：仅存于单进程内存，服务重启即清空
_JOBS: dict[str, _IsolationJob] = {}
_REGISTRY_LOCK = threading.Lock()

# 测试钩子：None（生产默认）表示直通；测试可替换为形如
# gate(job_id, stage, *, version, closed) -> None 的可调用对象，
# 在每次快照重算开始时同步调用并允许阻塞（作业锁在此时并不持有）。
# stage 为 "create" / "toggle" / "revise" 之一。
_COMPUTE_GATE: Callable[..., None] | None = None


# --------------------------------------------------------------------------- #
# 输入校验
# --------------------------------------------------------------------------- #
def _validate_allow_unserved(raw: Any, net: Network) -> frozenset[str]:
    """校验允许暂时失供的用水节点集合（可省略或为空数组），返回 frozenset。"""
    if raw is None:
        return frozenset()
    if not isinstance(raw, list):
        raise ValidationError(
            "allow_unserved 必须是允许暂时失供的用水节点编号数组（可为空数组）。")
    demand_ids = {n["id"] for n in net.nodes if n["type"] == "demand"}
    seen: set[str] = set()
    for k, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                f"允许失供集合第 {k} 项缺少有效的节点编号（非空字符串）。")
        nid = item.strip()
        if nid not in demand_ids:
            raise ValidationError(
                f"允许失供节点“{nid}”不是已定义的用水节点"
                "（集合内必须全部为用水节点，不能是水源或未定义节点）。")
        if nid in seen:
            raise ValidationError(f"允许失供节点“{nid}”重复，集合内节点必须互异。")
        seen.add(nid)
    return frozenset(seen)


def _validate_demand_updates(raw: Any, net: Network) -> list[dict[str, float]]:
    """校验需水量修订列表，返回按请求顺序的 [{"node_id","demand"}]。

    列表必须非空；每项含 node_id（作业内互异的现有用水节点编号）与 demand
    （非负有限数，拒绝布尔值）。水源、未知节点、重复节点一律拒绝。
    """
    if not isinstance(raw, list):
        raise ValidationError(
            "demands 必须是非空的修订项数组，每项包含 node_id 与 demand。")
    if not raw:
        raise ValidationError(
            "demands 修订列表不能为空：至少需要一项 node_id 与 demand。")
    demand_ids = {n["id"] for n in net.nodes if n["type"] == "demand"}
    updates: list[dict[str, float]] = []
    seen: set[str] = set()
    for k, item in enumerate(raw, start=1):
        if not isinstance(item, dict):
            raise ValidationError(f"修订列表第 {k} 项必须是包含 node_id 与 demand 的对象。")
        nid = item.get("node_id")
        if not isinstance(nid, str) or not nid.strip():
            raise ValidationError(
                f"修订列表第 {k} 项缺少有效的 node_id（非空字符串）。")
        nid = nid.strip()
        if nid not in demand_ids:
            raise ValidationError(
                f"修订节点“{nid}”不是该作业中已定义的用水节点"
                "（只能修订现有用水节点的需水量，不能是水源或未定义节点）。")
        if nid in seen:
            raise ValidationError(
                f"修订节点“{nid}”在列表中重复，一次修订内的节点必须互异。")
        demand = _as_finite(
            item.get("demand"), f"用水节点“{nid}”的修订需水量", nonneg=True)
        seen.add(nid)
        updates.append({"node_id": nid, "demand": demand})
    return updates


def _network_with_demands(net: Network, demand_map: dict[str, float]) -> Network:
    """按 demand_map（用水节点编号 -> 当前需水量）构造新 Network。

    水源、管段与最低水头保持不变；节点与管段的录入顺序也保持不变，
    因此快照中一切“按录入顺序”的字段在修订后仍稳定。
    """
    nodes: list[dict[str, Any]] = []
    for node in net.nodes:
        if node["type"] == "demand":
            nodes.append({"id": node["id"], "type": "demand",
                          "demand": float(demand_map[node["id"]])})
        else:
            nodes.append(node)
    return Network(nodes=nodes, pipes=net.pipes, min_head=net.min_head)


# --------------------------------------------------------------------------- #
# 状态计算
# --------------------------------------------------------------------------- #
def _connected_component(net: Network, closed: frozenset[str]):
    """按全部关闭管段做无向 BFS，返回（连通区节点编号、连通区内开启管段），均按录入顺序。"""
    source_id = next(n["id"] for n in net.nodes if n["type"] == "source")
    adj: dict[str, list[str]] = {n["id"]: [] for n in net.nodes}
    for pipe in net.pipes:
        if pipe["id"] in closed:
            continue
        adj[pipe["start"]].append(pipe["end"])
        adj[pipe["end"]].append(pipe["start"])
    seen = {source_id}
    queue = deque([source_id])
    while queue:
        u = queue.popleft()
        for v in adj[u]:
            if v not in seen:
                seen.add(v)
                queue.append(v)
    nodes = [n["id"] for n in net.nodes if n["id"] in seen]
    # 开启管段的一个端点可达，另一个端点必然也可达
    pipes = [p for p in net.pipes if p["id"] not in closed and p["start"] in seen]
    return nodes, pipes


def _source_only_solution(net: Network, source: dict[str, Any]) -> dict[str, Any]:
    """连通区仅剩水源时的平凡解：不调用求解器，供水量为零，键与 solve_network 一致。"""
    head = float(source["head"])
    below = head < net.min_head
    return {
        "converged": True,
        "tolerance": CHECK_TOL,
        "method": "source-only",
        "iterations": 0,
        "solver_residual_inf": 0.0,
        "node_results": [{
            "id": source["id"],
            "type": "source",
            "head": head,
            "net_inflow": 0.0,
            "below_min_head": below,
            "fixed_head": head,
            "outflow": 0.0,
        }],
        "pipe_results": [],
        "heads": {source["id"]: head},
        "flows": {},
        "source_node": source["id"],
        "source_supply": 0.0,
        "total_demand": 0.0,
        "balance_residual": 0.0,
        "max_node_residual": 0.0,
        "max_pipe_residual": 0.0,
        "min_head": net.min_head,
        "below_min_head": [source["id"]] if below else [],
    }


def _find_non_finite_path(value: Any, prefix: str = "") -> str | None:
    """深度优先返回快照中首个非有限 float 的点分路径（供错误说明引用）。

    只拒绝 Python float（含 numpy 浮点，它是 float 的子类）；布尔值虽为 int
    子类但不是 float，不受影响。找不到非有限值时返回 None。
    """
    if isinstance(value, float):
        if not math.isfinite(value):
            return prefix or "<root>"
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            path = _find_non_finite_path(item, f"{prefix}.{key}" if prefix else str(key))
            if path is not None:
                return path
    elif isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            path = _find_non_finite_path(item, f"{prefix}[{index}]" if prefix else f"[{index}]")
            if path is not None:
                return path
    return None


def _evaluate_closed_set(net: Network, allowed: frozenset[str],
                         closed: frozenset[str]) -> dict[str, Any]:
    """计算某一关闭集合、某套当前需水量对应的状态体（不含 job_id/version）。

    这是创建、切换、修订与**顺序预演**共用的纯评估，结论分三类：

      * 合法：返回状态体（结构与历史快照相同，仅缺 job_id/version 两个
        随调用而变的字段）；
      * 明确不合法：抛 ``_IllegalStateError``（kind="unserved" 失供超出
        允许集合，或 kind="below_head" 连通区欠压），可作为不可行分支
        剪枝；
      * 无法确认：抛 ``StateUnconfirmedError``（求解不收敛、失供总量聚合
        溢出、候选含非有限值），调用方只能报告“无法确认”，**绝不能当作
        不可行**。
    """
    source_id = next(n["id"] for n in net.nodes if n["type"] == "source")
    demand_map = {n["id"]: n["demand"] for n in net.nodes if n["type"] == "demand"}

    component_nodes, component_pipes = _connected_component(net, closed)
    component_set = set(component_nodes)
    # 失供节点同样保存当前需水量，重连后自动按新值参与求解
    unserved = [nid for nid in demand_map if nid not in component_set]

    # 规则 1：失供节点必须全部在允许暂时失供的集合内
    outside = [nid for nid in unserved if nid not in allowed]
    if outside:
        raise _IllegalStateError("unserved", outside)

    # 规则 1b：失供需水量之和等聚合值不得溢出为非有限数。单个节点允许取
    # 1e308 这样的有限大值（不设任何人为上限），但多个大值相加溢出为 inf 时，
    # 快照无法严格 JSON 编码，必须按“无法确认”处理，绝不截断或用字符串掩盖。
    unserved_total = float(sum(demand_map[nid] for nid in unserved))
    if not math.isfinite(unserved_total):
        raise StateUnconfirmedError(
            "overflow",
            {"unserved_nodes": list(unserved), "unserved_demand": unserved_total})

    # 规则 2：连通区用水节点按当前需水量求解且不得欠压；仅剩水源时供水量为零
    if len(component_nodes) == 1:
        solution = _source_only_solution(
            net, next(n for n in net.nodes if n["type"] == "source"))
    else:
        sub_net = Network(
            nodes=[n for n in net.nodes if n["id"] in component_set],
            pipes=component_pipes,
            min_head=net.min_head,
        )
        try:
            solution = solve_network(sub_net)
        except SolverNotConverged as exc:
            raise StateUnconfirmedError("not_converged", {"error": str(exc)}) from exc
        # 非有限值属于数值故障，必须先于水头限值判定：含 NaN/Infinity 的解
        # 不能当作“明确欠压”的非法状态（否则预演会错误地把无法确认当不可行）。
        bad_in_solution = _find_non_finite_path(solution)
        if bad_in_solution is not None:
            raise StateUnconfirmedError(
                "non_finite", {"path": f"solution.{bad_in_solution}"})
        below = [nid for nid in component_nodes
                 if nid in demand_map and solution["heads"][nid] < net.min_head]
        if below:
            raise _IllegalStateError("below_head", below)

    state = {
        "min_head": net.min_head,
        "source_node": source_id,
        "allowed_unserved": [nid for nid in demand_map if nid in allowed],
        # 全部当前需水量（含失供节点保存的新值），按节点录入顺序
        "demands": [{"node_id": nid, "demand": float(demand_map[nid])}
                    for nid in demand_map],
        "closed_pipes": [p["id"] for p in net.pipes if p["id"] in closed],
        "open_pipes": [p["id"] for p in net.pipes if p["id"] not in closed],
        "pipe_states": [
            {"id": p["id"], "start": p["start"], "end": p["end"],
             "closed": p["id"] in closed}
            for p in net.pipes
        ],
        "unserved_nodes": unserved,
        "unserved_demand": unserved_total,
        "component_nodes": component_nodes,
        "component_pipes": [p["id"] for p in component_pipes],
        "source_only": len(component_nodes) == 1,
        "source_supply": solution["source_supply"],
        "total_demand": solution["total_demand"],
        "solution": solution,
    }

    # 兜底：候选状态的任何数值字段非有限都不能作为合法结论（快照必须可严格
    # JSON 编码）。失供总量的聚合溢出已在上面提前拦截，这里再对完整候选
    # （含求解结果）做一遍终检。
    bad_path = _find_non_finite_path(state)
    if bad_path is not None:
        raise StateUnconfirmedError("non_finite", {"path": bad_path})

    return state


def _build_snapshot(job_id: str, version: int, net: Network,
                    allowed: frozenset[str], closed: frozenset[str],
                    *, stage: str) -> dict[str, Any]:
    """计算某一关闭集合、某套当前需水量对应的状态快照。

    规则判定与数值求解全部委托给 ``_evaluate_closed_set``；本函数只负责
    测试求解门（gate）与把纯评估的三类结论翻译成作业操作的中文错误文案：
    明确不合法（失供/欠压）与无法确认（不收敛/溢出/非有限值）在真实作业
    操作里都按失败处理（抛 ``IsolationRuleError``，状态保持原状）。``stage``
    取 "create"（创建初始状态，失败文案为“不创建作业”）、"toggle"（管段
    切换）或 "revise"（需水量修订）；后两者失败文案均为“本次操作未生效，
    作业保持原状"。
    """
    if _COMPUTE_GATE is not None:
        _COMPUTE_GATE(job_id, stage, version=version, closed=frozenset(closed))

    try:
        snapshot = _evaluate_closed_set(net, allowed, closed)
    except _IllegalStateError as exc:
        if exc.kind == "unserved":
            names = "、".join(f"“{nid}”" for nid in exc.nodes)
            if stage == "create":
                raise IsolationRuleError(
                    f"初始状态下用水节点 {names} 即失供，且不在允许暂时失供的集合内，"
                    "不创建停管隔离作业。") from exc
            action = "修订需水量" if stage == "revise" else "切换"
            raise IsolationRuleError(
                f"{action}后用水节点 {names} 将失供，但其不在允许暂时失供的节点集合内；"
                "本次操作未生效，关闭集合、版本与稳态结果全部保持原状。") from exc
        # kind == "below_head"
        names = "、".join(f"“{nid}”" for nid in exc.nodes)
        if stage == "create":
            raise IsolationRuleError(
                f"初始状态（全部管段开启）下，用水节点 {names} 的水头低于最低"
                f"水头限值 {net.min_head:g}，不创建停管隔离作业。") from exc
        if stage == "revise":
            raise IsolationRuleError(
                f"修订需水量后连通区内用水节点 {names} 的水头将低于最低水头限值 "
                f"{net.min_head:g}；本次操作未生效，需水量、关闭集合、版本与稳态"
                "结果全部保持原状。") from exc
        raise IsolationRuleError(
            f"切换后连通区内用水节点 {names} 的水头将低于最低水头限值 "
            f"{net.min_head:g}；本次操作未生效，关闭集合、版本与稳态结果全部保持原状。"
        ) from exc
    except StateUnconfirmedError as exc:
        if exc.kind == "overflow":
            names = "、".join(f"“{nid}”" for nid in exc.detail["unserved_nodes"])
            value = exc.detail["unserved_demand"]
            if stage == "create":
                raise IsolationRuleError(
                    f"初始状态下失供节点（{names}）的失供需水量之和溢出为非有限值，"
                    "超出浮点可表示范围，不创建停管隔离作业；请避免使用会导致聚合"
                    "溢出的过大需水量（单个有限大值允许，聚合不得溢出）。") from exc
            if stage == "revise":
                raise IsolationRuleError(
                    "修订需水量后，失供节点（"
                    f"{names}）保存的新需水量之和（失供总量）溢出为非有限值"
                    f"（{value!r}），超出浮点可表示范围，候选快照无法以严格 JSON "
                    "编码；本次修订未生效，需水量、关闭集合、快照与版本号全部保持"
                    "原状。请把相关失供节点的需水量改小后用同一版本重试（单个 1e308 "
                    "这样的有限大值仍然允许，只要聚合不溢出；不得用上限、字符串、"
                    "null 或截断掩盖溢出）。") from exc
            raise IsolationRuleError(
                f"切换后失供节点（{names}）的当前需水量之和（失供总量）溢出为非有限值"
                f"（{value!r}），超出浮点可表示范围，候选快照无法以严格 JSON "
                "编码；本次操作未生效，关闭集合、需水量、快照与版本号全部保持原状。"
            ) from exc
        if exc.kind == "non_finite":
            bad_path = exc.detail["path"]
            if stage == "revise":
                raise IsolationRuleError(
                    "修订需水量后候选快照的数值字段 "
                    f"“{bad_path}”为非有限值（NaN/Infinity/-Infinity），快照无法以"
                    "严格 JSON 编码；本次修订未生效，需水量、关闭集合、快照与版本号"
                    "全部保持原状，请用同一版本重试。") from exc
            if stage == "create":
                raise IsolationRuleError(
                    f"初始状态候选快照的数值字段“{bad_path}”为非有限值"
                    "（NaN/Infinity/-Infinity），快照无法以严格 JSON 编码，"
                    "不创建停管隔离作业。") from exc
            raise IsolationRuleError(
                f"切换后候选快照的数值字段“{bad_path}”为非有限值"
                "（NaN/Infinity/-Infinity），快照无法以严格 JSON 编码；"
                "本次操作未生效，关闭集合、需水量、快照与版本号全部保持原状。"
            ) from exc
        # kind == "not_converged"
        reason = exc.detail["error"]
        if stage == "create":
            raise IsolationRuleError(
                "初始状态（全部管段开启）的稳态求解未能收敛，不创建停管隔离作业。"
                f"（{reason}）") from exc
        if stage == "revise":
            raise IsolationRuleError(
                "修订需水量后连通区的稳态求解未能收敛；本次操作未生效，需水量、"
                f"关闭集合、版本与稳态结果全部保持原状。（{reason}）") from exc
        raise IsolationRuleError(
            "切换后连通区的稳态求解未能收敛；本次操作未生效，关闭集合、版本与"
            f"稳态结果全部保持原状。（{reason}）") from exc

    snapshot = {"job_id": job_id, "version": version, **snapshot}
    return snapshot


# --------------------------------------------------------------------------- #
# 作业生命周期：创建 / 读取 / 切换 / 修订需水量 / 结束
# --------------------------------------------------------------------------- #
def create_job(payload: Any) -> dict[str, Any]:
    """创建停管隔离作业，返回版本 0 的初始快照。

    沿用同一套管网校验与稳态求解；校验失败、初始有用水节点欠压或初始求解
    不收敛时抛异常，不创建作业。初始重算不持有任何作业锁，阻塞期间不影响
    其他作业的读取与操作。
    """
    net = validate_payload(payload)
    allowed = _validate_allow_unserved(payload.get("allow_unserved"), net)
    job_id = uuid.uuid4().hex
    # 重算在注册表锁之外完成；通过前作业尚不存在，失败自然不会留下任何状态
    snapshot = _build_snapshot(job_id, 0, net, allowed, frozenset(), stage="create")
    job = _IsolationJob(
        job_id=job_id,
        network=net,
        allowed_unserved=allowed,
        closed_pipes=frozenset(),
        version=0,
        snapshot=snapshot,
    )
    with _REGISTRY_LOCK:
        _JOBS[job_id] = job
    return snapshot


def get_snapshot(job_id: str) -> dict[str, Any]:
    """读取作业最近一次成功提交的完整快照（快照为不可变整体，绝不混合不同版本的数据）。

    即使该作业此刻正在重算，本函数也只短暂加锁后立即返回最近已提交的快照，
    不会等待进行中的计算。作业已被结束（注册表中不再存在，或摘除与终止标记
    之间的瞬间仍被已摘除对象上的迟到读取命中）时抛 JobNotFoundError。
    """
    with _REGISTRY_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    with job.lock:
        if job.terminated:
            raise JobNotFoundError(job_id)
        return job.snapshot


def terminate_job(job_id: str) -> str:
    """结束不再使用的作业：从注册表摘除并打上终止标记，返回作业 ID。

    不要求也不携带版本号；未知 ID 与重复结束（第二次起作业已不在注册表）
    一律抛 JobNotFoundError（HTTP 层映射为 404）。不中断底层数值计算、也不
    等待任何进行中的重算：先在注册表锁内把作业摘出（此后新发起的读取、切换
    与修订都因注册表查无此作业而 404，且该步骤不与计算锁嵌套、不会等待
    计算线程），再短暂获取作业锁置位终止标记，保证结束请求返回后任何到达
    提交点的在途操作都能观察到标记并放弃结果。
    """
    with _REGISTRY_LOCK:
        job = _JOBS.pop(job_id, None)
    if job is None:
        raise JobNotFoundError(job_id)
    with job.lock:
        job.terminated = True
    return job_id


def toggle_pipe(job_id: str, payload: Any) -> dict[str, Any]:
    """携带当前版本切换一根现有管段的开闭，返回新版本快照。

    版本不一致抛 VersionConflictError（同一版本的修订与切换合计最多成功
    一次）；管段不存在、规则不满足或求解不收敛时抛异常，关闭集合、版本号
    与稳态结果全部保持原状。

    耗时的快照重算在作业锁之外进行：同一作业的多个操作可以同时计算，
    后来的操作甚至可以先算完先提交。临界区只在提交瞬间出现：提交时重新
    比对版本，若在本次计算期间版本已被其他成功操作推进，则本次结果作废
    并抛 VersionConflictError（携带作业当前版本），绝不覆盖新状态。
    """
    with _REGISTRY_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是包含 version 与 pipe_id 的对象。")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValidationError("version 必须是非负整数（即作业当前版本号）。")
    pipe_id = payload.get("pipe_id")
    if not isinstance(pipe_id, str) or not pipe_id.strip():
        raise ValidationError("pipe_id 必须是要切换开闭的现有管段编号（非空字符串）。")
    pipe_id = pipe_id.strip()

    # 第 1 段临界区：仅做输入归属核对与基态快照，立即释放，不带着锁做重算
    with job.lock:
        if job.terminated:
            raise JobNotFoundError(job_id)
        if version != job.version:
            raise VersionConflictError(version, job.version)
        if pipe_id not in {p["id"] for p in job.network.pipes}:
            raise ValidationError(
                f"管段“{pipe_id}”不是该作业管网中已定义的管段。")
        base_version = job.version
        base_closed = job.closed_pipes
        net = job.network
        allowed = job.allowed_unserved

    new_closed = base_closed ^ {pipe_id}  # 开→关 或 关→开
    # 锁外重算：其他请求（健康检查、读取、其他作业、后来的操作）在此期间照常返回
    try:
        candidate = _build_snapshot(job.job_id, base_version + 1, net,
                                    allowed, new_closed, stage="toggle")
    except IsolationRuleError:
        # 重算期间作业可能已被结束：结束时本次操作尚未提交，结束后无论
        # 算出什么失败（失供/欠压/不收敛/非有限数）都统一按 404 处理，
        # 绝不返回 422，更不会恢复作业。
        with job.lock:
            if job.terminated:
                raise JobNotFoundError(job_id) from None
        raise

    # 第 2 段临界区：提交时重新比对版本——计算期间版本若已推进则丢弃本次结果
    with job.lock:
        if job.terminated:
            raise JobNotFoundError(job_id)
        if job.version != base_version:
            raise VersionConflictError(version, job.version)
        # 全部规则满足且求解收敛：一次性提交关闭集合、版本号与稳态结果
        job.closed_pipes = new_closed
        job.version = base_version + 1
        job.snapshot = candidate
        return candidate


def revise_demands(job_id: str, payload: Any) -> tuple[dict[str, Any], list[dict[str, float]]]:
    """携带当前版本修订一批现有用水节点的需水量，返回 (新版本快照, 修订项)。

    修订项为非空列表，每项含 node_id（作业内互异的现有用水节点）与 demand
    （非负有限数，拒绝布尔值）；只改列出的节点，水源、管段、最低水头与允许
    失供集合均不变。按当前关闭集合重算：连通节点使用新需水量且不得欠压，
    失供节点保存新值并更新失供总量（重连后同样按新值求解）。

    版本不一致抛 VersionConflictError（同一版本的修订与管段切换合计最多
    成功一次）；非法输入、聚合溢出、候选快照含非有限数、欠压或不收敛时抛
    异常，需水量、关闭集合、版本号与稳态结果全部保持原状。锁外重算与提交时
    版本比对的并发语义与 ``toggle_pipe`` 完全一致：基于旧版本算成的迟到提交
    收 409，绝不覆盖新需水量。
    """
    with _REGISTRY_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if not isinstance(payload, dict):
        raise ValidationError("请求体必须是包含 version 与非空 demands 修订列表的对象。")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValidationError("version 必须是非负整数（即作业当前版本号）。")

    # 第 1 段临界区：版本比对、归属核对与基态快照；修订项校验在此一并完成，
    # 立即释放后不带着锁做重算
    with job.lock:
        if job.terminated:
            raise JobNotFoundError(job_id)
        if version != job.version:
            raise VersionConflictError(version, job.version)
        updates = _validate_demand_updates(payload.get("demands"), job.network)
        base_version = job.version
        base_closed = job.closed_pipes
        net = job.network
        allowed = job.allowed_unserved

    # 只覆盖列出的节点：在基态网络上构造新网络，其余需水量原样保留
    revised = {n["id"]: n["demand"] for n in net.nodes if n["type"] == "demand"}
    for item in updates:
        revised[item["node_id"]] = item["demand"]
    new_net = _network_with_demands(net, revised)
    # 锁外重算：失供节点保存新值、连通节点按新值求解，均由 _build_snapshot 完成
    try:
        candidate = _build_snapshot(job.job_id, base_version + 1, new_net,
                                    allowed, base_closed, stage="revise")
    except IsolationRuleError:
        # 与切换相同：结束时尚未提交的修订，结束后即使结果是欠压/不收敛/
        # 聚合溢出/非有限数等失败，也统一按 404 处理，绝不返回 422 或恢复作业。
        with job.lock:
            if job.terminated:
                raise JobNotFoundError(job_id) from None
        raise

    # 第 2 段临界区：提交时重新比对版本——计算期间版本若已推进则丢弃本次结果
    with job.lock:
        if job.terminated:
            raise JobNotFoundError(job_id)
        if job.version != base_version:
            raise VersionConflictError(version, job.version)
        # 全部规则满足且求解收敛：一次性提交需水量、完整快照与版本号
        job.network = new_net
        job.version = base_version + 1
        job.snapshot = candidate
        return candidate, updates


# --------------------------------------------------------------------------- #
# 切换顺序预演（纯计算：不推进版本、不改需水量或快照）
# --------------------------------------------------------------------------- #
def _validate_rehearse_pipes(raw: Any, net: Network) -> list[str]:
    """校验预演请求中的管段编号数组，返回按管段录入顺序归并后的编号列表。

    必须是 2～6 根互异的现有管段（非空字符串，不能是未定义编号）。归并顺序
    为管段录入顺序，与请求数组的书写顺序无关——按“管段录入序号序列的字典序”
    取最小顺序时，输入数组顺序不得影响结果。
    """
    if not isinstance(raw, list):
        raise ValidationError(
            "pipes 必须是要预演的管段编号数组（2～6 根互异的现有管段）。")
    if not (MIN_REHEARSE_PIPES <= len(raw) <= MAX_REHEARSE_PIPES):
        raise ValidationError(
            f"预演管段必须为 {MIN_REHEARSE_PIPES}～{MAX_REHEARSE_PIPES} 根，"
            f"当前为 {len(raw)} 根。")
    pipe_ids = {p["id"] for p in net.pipes}
    seen: set[str] = set()
    for k, item in enumerate(raw, start=1):
        if not isinstance(item, str) or not item.strip():
            raise ValidationError(
                f"预演管段第 {k} 项缺少有效的管段编号（非空字符串）。")
        pid = item.strip()
        if pid not in pipe_ids:
            raise ValidationError(
                f"管段“{pid}”不是该作业管网中已定义的管段。")
        if pid in seen:
            raise ValidationError(
                f"预演管段“{pid}”重复，参加预演的管段必须互异。")
        seen.add(pid)
    return [p["id"] for p in net.pipes if p["id"] in seen]


def _unconfirmed_record(pipes: list[str], prefix: tuple[str, ...],
                        exc: StateUnconfirmedError) -> dict[str, Any]:
    """把一次“无法确认”的状态评估组装成对外记录（只含可严格 JSON 编码的值）。

    ``prefix`` 为到达该状态已切换的管段序列（按录入顺序归并后的编号）；
    非有限值本身以 repr 字符串描述，绝不把 Infinity/NaN 放进 JSON。
    """
    record: dict[str, Any] = {
        "after_steps": list(prefix),
        # 管段录入序号（从 1 起），便于核对该状态在字典序搜索中的位置
        "after_step_indices": [pipes.index(pid) + 1 for pid in prefix],
        "reason": exc.kind,
    }
    if exc.kind == "not_converged":
        record["error"] = exc.detail["error"]
    elif exc.kind == "overflow":
        record["unserved_nodes"] = list(exc.detail["unserved_nodes"])
        record["unserved_demand_repr"] = repr(exc.detail["unserved_demand"])
    else:  # non_finite
        record["field_path"] = exc.detail["path"]
    return record


def rehearse_switch_plan(job_id: str, payload: Any) -> dict[str, Any]:
    """携带版本对 2～6 根互异现有管段预演切换顺序，返回搜索结论（不提交状态）。

    基态为**请求版本**的关闭集合与当前需水量（该版本快照已在创建/提交时
    通过同一套规则校验，预演不重新求解基态）。搜索每根指定管段恰好切换
    一次、其余管段不动的全部排列：沿管段录入序号序列的字典序做深度优先
    枚举，所有中间状态与终态都必须合法（失供不超允许集合、最低水头、稳态
    收敛、聚合有限）；第一个走到的合法全路径即为字典序最小的可行顺序。

      * 明确不合法（失供超允许集合/欠压）的状态剪去该前缀；
      * 求解不收敛或出现非有限数（含聚合溢出）的状态记为“无法确认”并剪枝，
        **绝不当作不可行**：找到可行顺序但更靠前分支含无法确认状态时，只能
        返回该顺序的临时结果（不能确认其字典序最小性）；没找到可行顺序但
        存在无法确认状态时，同样不能下“不可行”结论；
      * 只有枚举过的更靠前分支全部确认不可行，才报告可行（且为字典序最小）
        或整体不可行。

    纯计算语义：不推进版本、不改需水量或快照，计算在 HTTP 层放入工作线程，
    不阻塞读取与其他作业。版本不一致抛 VersionConflictError；作业不存在或
    已结束抛 JobNotFoundError（HTTP 层保证结束优先于冲突与计算错误，并在
    计算结束时再次核对结束标记与版本）。
    """
    with _REGISTRY_LOCK:
        job = _JOBS.get(job_id)
    if job is None:
        raise JobNotFoundError(job_id)
    if not isinstance(payload, dict):
        raise ValidationError(
            "请求体必须是包含 version 与 pipes（2～6 根互异现有管段编号）的对象。")
    version = payload.get("version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise ValidationError("version 必须是非负整数（即作业当前版本号）。")

    # 唯一一段临界区：结束/版本核对与入参校验；之后不持任何锁完成全部搜索
    with job.lock:
        if job.terminated:
            raise JobNotFoundError(job_id)
        if version != job.version:
            raise VersionConflictError(version, job.version)
        pipes = _validate_rehearse_pipes(payload.get("pipes"), job.network)
        base_version = job.version
        base_closed = frozenset(job.closed_pipes)
        base_snapshot = job.snapshot
        net = job.network
        allowed = job.allowed_unserved

    # 搜索只在开始时触发一次测试求解门（stage="rehearse"），位置在任何规则
    # 判定与求解器调用之前；生产环境 _COMPUTE_GATE 为 None（直通）。
    if _COMPUTE_GATE is not None:
        _COMPUTE_GATE(job.job_id, "rehearse",
                      version=base_version, closed=base_closed)

    k = len(pipes)
    total_orders = math.factorial(k)

    # 记忆化（关闭集合 -> 评估结论）：同一集合无论经由哪条排列前缀到达，
    # 结论完全一致，至多评估 2^k-1 个非基态集合。值为：
    #   ("legal", 状态体) / ("illegal", 异常) / ("unknown", 异常)
    memo: dict[frozenset[str], tuple[str, Any]] = {}
    # “无法确认”记录按 DFS 首次遇到顺序；只记录被搜索实际遍历到的前缀
    unconfirmed_records: list[dict[str, Any]] = []
    evaluated_states = 0

    def classify(closed: frozenset[str],
                 prefix: tuple[str, ...]) -> str:
        nonlocal evaluated_states
        cached = memo.get(closed)
        if cached is not None:
            # 记忆命中不重复记录：同一集合的首个到达前缀已在首次评估时记录
            return cached[0]
        evaluated_states += 1
        try:
            state = _evaluate_closed_set(net, allowed, closed)
        except _IllegalStateError as exc:
            memo[closed] = ("illegal", exc)
            return "illegal"
        except StateUnconfirmedError as exc:
            memo[closed] = ("unknown", exc)
            unconfirmed_records.append(_unconfirmed_record(pipes, prefix, exc))
            return "unknown"
        memo[closed] = ("legal", state)
        return "legal"

    found_order: tuple[str, ...] | None = None

    def dfs(chosen: tuple[str, ...], closed: frozenset[str]) -> bool:
        """按录入序号字典序枚举排列；首个合法全路径找到即整体停止。"""
        nonlocal found_order
        if len(chosen) == k:
            found_order = chosen
            return True
        for pid in pipes:  # 录入顺序 = 字典序展开顺序
            if pid in chosen:
                continue
            next_chosen = chosen + (pid,)
            next_closed = closed ^ {pid}
            verdict = classify(next_closed, next_chosen)
            if verdict == "unknown":
                continue  # 无法确认：剪枝搜索，但绝不当作不可行
            if verdict == "illegal":
                continue  # 中间状态明确非法：该前缀的任何延伸都不合格
            if dfs(next_chosen, next_closed):
                return True
        return False

    dfs((), base_closed)

    counts = {"legal": 0, "illegal": 0, "unknown": 0}
    for verdict, _ in memo.values():
        counts[verdict] += 1

    if found_order is not None:
        # 沿找到的顺序重放，逐步取记忆化中已算好的合法状态体
        steps: list[dict[str, Any]] = []
        closed = base_closed
        for index, pid in enumerate(found_order, start=1):
            was_closed = pid in closed
            closed = closed ^ {pid}
            state = memo[closed][1]
            steps.append({
                "step": index,
                "pipe_id": pid,
                "pipe_entry_index": pipes.index(pid) + 1,
                "toggled_from": "closed" if was_closed else "open",
                "action": "open" if was_closed else "close",
                "toggled_to": "open" if was_closed else "closed",
                "closed_pipes": state["closed_pipes"],
                "open_pipes": state["open_pipes"],
                "pipe_states": state["pipe_states"],
                "unserved_nodes": state["unserved_nodes"],
                "unserved_demand": state["unserved_demand"],
                "component_nodes": state["component_nodes"],
                "component_pipes": state["component_pipes"],
                "source_only": state["source_only"],
                "source_supply": state["source_supply"],
                "total_demand": state["total_demand"],
                "solution": state["solution"],
            })
        final_state = memo[closed][1]
        plan: dict[str, Any] | None = {
            "pipe_order": list(found_order),
            "pipe_entry_indices": [pipes.index(pid) + 1 for pid in found_order],
            "steps": steps,
            "final_closed_pipes": final_state["closed_pipes"],
            "final_open_pipes": final_state["open_pipes"],
            "final_pipe_states": final_state["pipe_states"],
            "final_unserved_nodes": final_state["unserved_nodes"],
            "final_unserved_demand": final_state["unserved_demand"],
            "final_component_nodes": final_state["component_nodes"],
            "final_component_pipes": final_state["component_pipes"],
            "final_source_only": final_state["source_only"],
            "final_source_supply": final_state["source_supply"],
            "final_total_demand": final_state["total_demand"],
            "final_solution": final_state["solution"],
        }
        if unconfirmed_records:
            # 可行顺序已找到，但其字典序最小性无法确认：更靠前的分支含
            # “无法确认”状态，那里可能藏着更小的可行顺序。
            status = "not_confirmed"
            confirmed = False
            conclusion = (
                "已找到一条全部中间状态与终态均合法的切换顺序 "
                f"（{' → '.join(found_order)}，终态关闭 "
                f"{('、'.join(final_state['closed_pipes']) or '（无）')}），"
                "但按管段录入序号序列字典序排在它之前的搜索分支中存在求解不收敛"
                "或非有限数的状态，无法确认该顺序就是字典序最小的可行顺序"
                "（搜索途中的无法确认不能当作不可行）。")
        else:
            status = "feasible"
            confirmed = True
            conclusion = (
                f"在全部 {total_orders} 种每根恰好切换一次的顺序中，"
                "字典序最小且所有中间状态与终态均合法的切换顺序为 "
                f"{' → '.join(found_order)}，终态关闭 "
                f"{('、'.join(final_state['closed_pipes']) or '（无）')}，"
                f"失供节点 "
                f"{('、'.join(final_state['unserved_nodes']) or '无')}，"
                f"失供总量 {final_state['unserved_demand']:g}。"
                "预演不推进版本、不改需水量或快照。")
    else:
        plan = None
        if unconfirmed_records:
            status = "not_confirmed"
            confirmed = False
            conclusion = (
                "未能在已确认合法的状态中找到可执行顺序，但搜索分支中存在求解"
                "不收敛或非有限数的状态，无法确认是否真的无可行顺序"
                "（搜索途中的无法确认不能当作不可行）。")
        else:
            status = "infeasible"
            confirmed = True
            conclusion = (
                f"全部 {total_orders} 种每根恰好切换一次的顺序均至少有一个中间"
                "状态或终态不满足隔离规则（失供超出允许集合或最低水头限值），"
                "不存在可执行的切换顺序。")

    return {
        "status": status,
        "minimum_confirmed": confirmed,
        "job_id": job_id,
        "base_version": base_version,
        "requested_pipes": list(pipes),
        "requested_pipe_entry_indices": [
            [p["id"] for p in net.pipes].index(pid) + 1 for pid in pipes],
        "pipe_count": k,
        "total_orders": total_orders,
        "evaluated_states": evaluated_states,
        "distinct_states": len(memo) + 1,  # 含未重新评估的基态
        "state_counts": {"legal": counts["legal"],
                         "illegal": counts["illegal"],
                         "unconfirmed": counts["unknown"]},
        "unconfirmed_states": unconfirmed_records,
        "base_closed_pipes": list(base_snapshot["closed_pipes"]),
        "base_snapshot": base_snapshot,
        "plan": plan,
        "conclusion": conclusion,
    }
