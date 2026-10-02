"""切换顺序预演（POST /api/isolation-jobs/{job_id}/rehearsal）的端到端测试。

覆盖：可行顺序按管段录入序号序列字典序取最小（输入数组顺序不影响结果）、
每根恰好切换一次且其他管段不动、所有中间状态与终态都须合法（失供超允许
集合/欠压剪枝）、基态已关闭管段的“先关后开/先开后关”混合序列（含中途
失供后经重开恢复供水）、不可行明确返回 infeasible、搜索途中求解不收敛或
非有限值只能 not_confirmed（绝不当不可行；含找到临时顺序与完全无结论
两种）、预演不推进版本/不改需水量/不改快照、版本冲突 409、结束优先于
冲突与计算错误（门暂停期间结束，放行后 404）、计算不阻塞健康检查/读取/
其他作业、2～6 根互异现有管段等全部输入校验，以及响应严格 JSON 可编码。
"""

from __future__ import annotations

import json
import math
import threading

import pytest
from fastapi.testclient import TestClient

import app.isolation as iso
from app.main import app
from app.solver import SolverNotConverged

client = TestClient(app)


def _strict_json_loads(text):
    """按 JSON 标准严格解析：NaN/Infinity/-Infinity 记号一律视为失败。"""
    return json.loads(
        text, parse_constant=lambda c: (_ for _ in ()).throw(ValueError(c)))


def _tree_payload(allow=()):
    """树状网：S(50) ─P1→ A(需2) ─P2→ B(需3)；P1、P2 都是桥。"""
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 50.0},
            {"id": "A", "type": "demand", "demand": 2.0},
            {"id": "B", "type": "demand", "demand": 3.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
        ],
        "min_head": 0.0,
        "allow_unserved": list(allow),
    }


def _loop_payload(allow=()):
    """环网：S(30)，A/B 各需 1；P3(S-B) 阻力 100。

    关 P1 或关 P2 都令 B 经 P3 取水而欠压（明确非法）；关 P3 合法
    （H_A=26、H_B=25）。
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 30.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
            {"id": "P3", "start": "S", "end": "B", "r": 100.0},
        ],
        "min_head": 0.0,
        "allow_unserved": list(allow),
    }


def _ring_lollipop_payload():
    """环 S-A-B-C-S（P1..P4，r 均很小）加叶子 D（P5 桥）；允许 D 暂时失供。

    用于“基态已关 P5（D 失供），序列先关 P1 再重开 P5”：中途 D 失供，
    重开后恢复供水，其他管段全程不动。
    """
    return {
        "nodes": [
            {"id": "S", "type": "source", "head": 50.0},
            {"id": "A", "type": "demand", "demand": 1.0},
            {"id": "B", "type": "demand", "demand": 1.0},
            {"id": "C", "type": "demand", "demand": 1.0},
            {"id": "D", "type": "demand", "demand": 1.0},
        ],
        "pipes": [
            {"id": "P1", "start": "S", "end": "A", "r": 0.01},
            {"id": "P2", "start": "A", "end": "B", "r": 0.01},
            {"id": "P3", "start": "B", "end": "C", "r": 0.01},
            {"id": "P4", "start": "C", "end": "S", "r": 0.01},
            {"id": "P5", "start": "B", "end": "D", "r": 0.01},
        ],
        "min_head": 0.0,
        "allow_unserved": ["D"],
    }


def _create(payload) -> dict:
    resp = client.post("/api/isolation-jobs", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]


def _toggle(job_id: str, version: int, pipe_id: str):
    return client.post(f"/api/isolation-jobs/{job_id}/toggle",
                       json={"version": version, "pipe_id": pipe_id})


def _revise(job_id: str, version: int, updates):
    return client.post(f"/api/isolation-jobs/{job_id}/demands",
                       json={"version": version, "demands": updates})


def _rehearse(job_id: str, version: int, pipes):
    return client.post(f"/api/isolation-jobs/{job_id}/rehearsal",
                       json={"version": version, "pipes": pipes})


def _rehearsal(job_id: str, version: int, pipes) -> dict:
    resp = _rehearse(job_id, version, pipes)
    assert resp.status_code == 200, resp.text
    _strict_json_loads(resp.text)
    return resp.json()["rehearsal"]


# ---------------------------- 可行顺序与字典序 ---------------------------- #
def test_rehearse_tree_feasible_lex_min_input_order_independent():
    job = _create(_tree_payload(allow=["A", "B"]))
    # 请求数组故意按录入逆序书写：结果必须与顺序无关，一律按录入序号归并
    for pipes in (["P1", "P2"], ["P2", "P1"]):
        d = _rehearsal(job["job_id"], 0, pipes)
        assert d["status"] == "feasible"
        assert d["minimum_confirmed"] is True
        assert d["base_version"] == 0
        assert d["requested_pipes"] == ["P1", "P2"]
        assert d["requested_pipe_entry_indices"] == [1, 2]
        assert d["pipe_count"] == 2 and d["total_orders"] == 2
        assert d["unconfirmed_states"] == []
        plan = d["plan"]
        assert plan["pipe_order"] == ["P1", "P2"]
        assert plan["pipe_entry_indices"] == [1, 2]
        # 每根恰好切换一次、其他管段不动：终态关闭集合 = 基态 △ 指定管段
        assert plan["final_closed_pipes"] == ["P1", "P2"]
        assert plan["final_open_pipes"] == []
        s1, s2 = plan["steps"]
        assert s1["step"] == 1 and s1["pipe_id"] == "P1"
        assert s1["pipe_entry_index"] == 1
        assert s1["toggled_from"] == "open" and s1["action"] == "close"
        assert s1["toggled_to"] == "closed"
        assert s1["closed_pipes"] == ["P1"]
        assert s1["unserved_nodes"] == ["A", "B"]
        assert s1["unserved_demand"] == pytest.approx(5.0)
        assert s1["component_nodes"] == ["S"] and s1["source_only"] is True
        assert s1["source_supply"] == 0.0 and s1["total_demand"] == 0.0
        assert s1["solution"]["heads"] == {"S": 50.0}
        assert s2["step"] == 2 and s2["pipe_id"] == "P2"
        assert s2["toggled_from"] == "open" and s2["action"] == "close"
        assert s2["closed_pipes"] == ["P1", "P2"]
        assert s2["component_nodes"] == ["S"]
        # 连通区稳态结果随每一步返回（键与 /solve 结果一致）
        assert s1["solution"]["converged"] is True
        assert plan["final_unserved_nodes"] == ["A", "B"]
        assert plan["final_unserved_demand"] == pytest.approx(5.0)
        assert plan["final_component_nodes"] == ["S"]
        assert plan["final_source_only"] is True


def test_rehearse_embeds_base_snapshot_and_conclusion():
    job = _create(_tree_payload(allow=["A", "B"]))
    d = _rehearsal(job["job_id"], 0, ["P1", "P2"])
    assert d["base_snapshot"] == job  # 基态快照整体嵌入
    assert d["base_closed_pipes"] == []
    assert "P1" in d["conclusion"] and "字典序最小" in d["conclusion"]
    assert d["state_counts"] == {"legal": 2, "illegal": 0, "unconfirmed": 0}
    assert d["evaluated_states"] == 2
    assert d["distinct_states"] == 3  # 基态 + 两个非基态关闭集合


def test_rehearse_loop_lex_min_skips_below_head_prefixes():
    """环网三根管各切一次：以 P1/P2 开头都欠压非法，最小可行顺序为 P3→P1→P2。"""
    job = _create(_loop_payload(allow=["A", "B"]))
    d = _rehearsal(job["job_id"], 0, ["P1", "P2", "P3"])
    assert d["status"] == "feasible" and d["minimum_confirmed"] is True
    assert d["total_orders"] == 6
    plan = d["plan"]
    assert plan["pipe_order"] == ["P3", "P1", "P2"]
    assert plan["pipe_entry_indices"] == [3, 1, 2]
    s1, s2, s3 = plan["steps"]
    # 第 1 步关 P3：仍全连通，H_A=26、H_B=25
    assert s1["closed_pipes"] == ["P3"]
    assert s1["unserved_nodes"] == [] and s1["source_only"] is False
    assert s1["solution"]["heads"]["A"] == pytest.approx(26.0, abs=1e-9)
    assert s1["solution"]["heads"]["B"] == pytest.approx(25.0, abs=1e-9)
    # 第 2 步关 P1：A-B 经 P2 成孤岛，全部失供，连通区仅剩水源
    assert s2["closed_pipes"] == ["P1", "P3"]
    assert s2["unserved_nodes"] == ["A", "B"] and s2["source_only"] is True
    # 第 3 步关 P2：终态三管全关
    assert s3["closed_pipes"] == ["P1", "P2", "P3"]
    assert plan["final_closed_pipes"] == ["P1", "P2", "P3"]
    # P1/P2 开头的中间态都被明确判为欠压非法（非“无法确认”）
    assert d["state_counts"]["illegal"] >= 2
    assert d["state_counts"]["unconfirmed"] == 0


def test_rehearse_mixed_reopen_sequence_with_intermediate_state():
    """基态 P2 已关（B 失供）：选 P1、P2 各切一次，含一次重开；序列逐步合法。"""
    job = _create(_tree_payload(allow=["A", "B"]))
    assert _toggle(job["job_id"], 0, "P2").status_code == 200  # v1：B 失供
    d = _rehearsal(job["job_id"], 1, ["P2", "P1"])
    assert d["status"] == "feasible"
    assert d["base_version"] == 1 and d["base_closed_pipes"] == ["P2"]
    plan = d["plan"]
    # 字典序：P1(序号1) 在 P2(序号2) 前；P1 基态开→关，P2 基态关→开
    assert plan["pipe_order"] == ["P1", "P2"]
    s1, s2 = plan["steps"]
    assert s1["toggled_from"] == "open" and s1["action"] == "close"
    assert s1["closed_pipes"] == ["P1", "P2"] and s1["source_only"] is True
    assert s2["toggled_from"] == "closed" and s2["action"] == "open"
    assert s2["toggled_to"] == "open"
    assert s2["closed_pipes"] == ["P1"]  # 终态只剩 P1 关
    assert plan["final_closed_pipes"] == ["P1"]
    assert plan["final_unserved_nodes"] == ["A", "B"]
    # 预演不改变作业：仍为 v1、P2 关闭
    got = client.get(f"/api/isolation-jobs/{job['job_id']}").json()["job"]
    assert got["version"] == 1 and got["closed_pipes"] == ["P2"]


def test_rehearse_reopen_restores_supply_mid_sequence():
    """中途失供的叶子 D 在后续“重开”步骤恢复供水，终态仍合法。"""
    job = _create(_ring_lollipop_payload())
    jid = job["job_id"]
    assert _toggle(jid, 0, "P5").status_code == 200  # v1：D 失供（允许）
    d = _rehearsal(jid, 1, ["P5", "P1"])
    assert d["status"] == "feasible"
    plan = d["plan"]
    # P1 序号 1 < P5 序号 5：先关 P1（环被打开但仍连通，D 仍因 P5 关闭而失供），
    # 再重开 P5：D 恢复供水；终态仅 P1 关闭，全部节点连通且不欠压
    assert plan["pipe_order"] == ["P1", "P5"]
    s1, s2 = plan["steps"]
    assert s1["action"] == "close" and s1["closed_pipes"] == ["P1", "P5"]
    assert s1["unserved_nodes"] == ["D"]
    assert s1["component_nodes"] == ["S", "A", "B", "C"]
    assert s2["action"] == "open" and s2["closed_pipes"] == ["P1"]
    assert s2["unserved_nodes"] == []
    assert s2["component_nodes"] == ["S", "A", "B", "C", "D"]
    assert plan["final_unserved_nodes"] == [] and plan["final_source_only"] is False


# ---------------------------- 不可行 ---------------------------- #
def test_rehearse_infeasible_unserved_outside_allowed():
    """终态必令 A 失供且不允许：所有排列的终态同一关闭集合，明确不可行。"""
    job = _create(_tree_payload(allow=["B"]))  # 仅允许 B 失供
    d = _rehearsal(job["job_id"], 0, ["P1", "P2"])
    assert d["status"] == "infeasible"
    assert d["minimum_confirmed"] is True
    assert d["plan"] is None
    assert d["unconfirmed_states"] == []
    assert d["state_counts"]["unconfirmed"] == 0
    assert d["state_counts"]["illegal"] >= 1
    assert "不存在可执行的切换顺序" in d["conclusion"]


def test_rehearse_infeasible_loop_all_three_empty_allow():
    """环网三管全切且允许失供集合为空：任何全关终态都让 A、B 失供。

    搜索实际评估到的状态：{P1}/{P2} 欠压（非法，剪枝）、{P3} 合法但其后
    {P1,P3}/{P2,P3} 失供（非法，剪枝，全关终态随之不会被到达）——没有任何
    顺序能走完全部 3 根，故整体不可行（全部为确定性结论，无“无法确认”）。
    """
    job = _create(_loop_payload())
    d = _rehearsal(job["job_id"], 0, ["P1", "P2", "P3"])
    assert d["status"] == "infeasible"
    assert d["minimum_confirmed"] is True
    assert d["plan"] is None
    assert d["state_counts"] == {"legal": 1, "illegal": 4, "unconfirmed": 0}
    assert d["evaluated_states"] == 5


# ---------------------------- 无法确认（不当作不可行） ---------------------------- #
def _fail_on_component_pipe_set(pipe_set: set[str], *, bad: str = "not_converged"):
    """返回一个 iso.solve_network 替身：连通区管段集合恰为 pipe_set 时失败。"""
    real_solve = iso.solve_network

    def flaky(net):
        ids = {p["id"] for p in net.pipes}
        if ids == pipe_set:
            if bad == "not_converged":
                raise SolverNotConverged("模拟该关闭集合不收敛")
            result = real_solve(net)
            result = dict(result)
            result["heads"] = dict(result["heads"])
            result["heads"]["B"] = float("inf")
            return result
        return real_solve(net)

    return flaky


def test_rehearse_not_confirmed_smaller_branch_nonconverged(monkeypatch):
    """字典序更靠前的 P1 分支求解不收敛：找到临时顺序但不能确认其最小性。"""
    job = _create(_loop_payload(allow=["A", "B"]))
    # closed={P1} 时连通区管段为 P2、P3（B 本会欠压；这里令求解器先不收敛）
    monkeypatch.setattr(iso, "solve_network",
                        _fail_on_component_pipe_set({"P2", "P3"}))
    resp = _rehearse(job["job_id"], 0, ["P1", "P2", "P3"])
    assert resp.status_code == 200
    d = resp.json()["rehearsal"]
    _strict_json_loads(resp.text)
    assert d["status"] == "not_confirmed"
    assert d["minimum_confirmed"] is False
    # 仍返回已找到的可行顺序作为临时结果
    assert d["plan"]["pipe_order"] == ["P3", "P1", "P2"]
    recs = d["unconfirmed_states"]
    assert len(recs) == 1
    assert recs[0]["reason"] == "not_converged"
    assert recs[0]["after_steps"] == ["P1"]
    assert recs[0]["after_step_indices"] == [1]
    assert "模拟该关闭集合不收敛" in recs[0]["error"]
    assert d["state_counts"]["unconfirmed"] == 1
    assert "无法确认" in d["conclusion"] and "字典序最小" in d["conclusion"]
    # 作业状态不变
    got = client.get(f"/api/isolation-jobs/{job['job_id']}").json()["job"]
    assert got["version"] == 0 and got["closed_pipes"] == []


def test_rehearse_not_confirmed_without_any_plan(monkeypatch):
    """唯一可能可行的 P3 分支不收敛：没有可返回顺序，也不能宣称不可行。"""
    job = _create(_loop_payload(allow=["A", "B"]))
    # closed={P3} 时连通区管段为 P1、P2（本是合法状态），令其不收敛
    monkeypatch.setattr(iso, "solve_network",
                        _fail_on_component_pipe_set({"P1", "P2"}))
    resp = _rehearse(job["job_id"], 0, ["P1", "P2", "P3"])
    d = resp.json()["rehearsal"]
    assert d["status"] == "not_confirmed"
    assert d["minimum_confirmed"] is False
    assert d["plan"] is None
    assert {r["reason"] for r in d["unconfirmed_states"]} == {"not_converged"}
    assert d["state_counts"] == {"legal": 0, "illegal": 2, "unconfirmed": 1}
    assert "无法确认是否真的无可行顺序" in d["conclusion"]
    _strict_json_loads(resp.text)


def test_rehearse_not_confirmed_non_finite_value(monkeypatch):
    """候选解含 Infinity：记为 non_finite 无法确认，响应仍是严格合法 JSON。

    字典序最靠前的 {P1} 分支被注入 Infinity 而无法确认；其后仍存在一条
    已确认可行的顺序（P3→P1→P2，第 2 步入仅剩水源的平凡态、不再求解），
    故返回该临时顺序并声明其字典序最小性无法确认。
    """
    job = _create(_loop_payload(allow=["A", "B"]))
    monkeypatch.setattr(iso, "solve_network",
                        _fail_on_component_pipe_set({"P2", "P3"}, bad="non_finite"))
    resp = _rehearse(job["job_id"], 0, ["P1", "P2", "P3"])
    d = resp.json()["rehearsal"]
    assert d["status"] == "not_confirmed"
    assert d["minimum_confirmed"] is False
    assert d["plan"]["pipe_order"] == ["P3", "P1", "P2"]  # 临时可行顺序
    recs = d["unconfirmed_states"]
    assert len(recs) == 1 and recs[0]["reason"] == "non_finite"
    assert recs[0]["after_steps"] == ["P1"]
    assert recs[0]["field_path"].startswith("solution.heads.")
    _strict_json_loads(resp.text)  # Infinity 不得混入 JSON


def test_rehearse_overflow_classified_as_unconfirmed():
    """失供总量聚合溢出（两个 1e308 失供节点）：报无法确认而非不可行。

    基态通过“先关 P1 再分别把 A、B 修订为 1e308”无法在真实接口上构造
    （第二次修订会因聚合溢出 422 且不提交），因此直接对预演所复用的
    底层评估函数断言：溢出归类为 StateUnconfirmedError("overflow")，
    而不是“明确非法”——这正是预演不能把该状态当作不可行分支的依据。
    端到端路径上，overflow 与 not_converged 共用同一套 not_confirmed
    语义（not_converged 已由上面的 HTTP 级用例覆盖）。
    """
    from app.isolation import StateUnconfirmedError, _evaluate_closed_set

    net = iso.Network(
        nodes=[
            {"id": "S", "type": "source", "head": 50.0},
            {"id": "A", "type": "demand", "demand": 1e308},
            {"id": "B", "type": "demand", "demand": 1e308},
        ],
        pipes=[
            {"id": "P1", "start": "S", "end": "A", "r": 1.0},
            {"id": "P2", "start": "A", "end": "B", "r": 1.0},
        ],
        min_head=0.0,
    )
    assert math.isfinite(1e308) and not math.isfinite(1e308 + 1e308)
    with pytest.raises(StateUnconfirmedError) as exc_info:
        _evaluate_closed_set(net, frozenset({"A", "B"}), frozenset({"P1"}))
    assert exc_info.value.kind == "overflow"
    assert set(exc_info.value.detail["unserved_nodes"]) == {"A", "B"}


def test_evaluate_non_finite_head_precedes_below_head_classification(monkeypatch):
    """解含 Infinity 水头时必须归类为“无法确认”，而不是“明确欠压非法”。

    回归点：非有限值是数值故障（此时大小比较无意义），若先做欠压判定会把
    无法确认的状态错分成确定非法，预演就可能错误地宣称不可行。
    """
    from app.isolation import (_IllegalStateError, StateUnconfirmedError,
                               _evaluate_closed_set)
    from app.solver import Network, validate_payload

    net = validate_payload(_loop_payload(allow=["A", "B"]))
    real_solve = iso.solve_network

    def poisoned(sub_net):
        result = real_solve(sub_net)
        # B 的水头置为 inf（< min_head 的比较无意义，必须报无法确认）
        result = dict(result)
        result["heads"] = dict(result["heads"])
        result["heads"]["B"] = float("inf")
        return result

    monkeypatch.setattr(iso, "solve_network", poisoned)
    with pytest.raises(StateUnconfirmedError) as exc_info:
        _evaluate_closed_set(net, frozenset({"A", "B"}), frozenset({"P1"}))
    assert exc_info.value.kind == "non_finite"
    # 对照：真实（有限）解在同一关闭集合上是明确欠压非法
    monkeypatch.setattr(iso, "solve_network", real_solve)
    with pytest.raises(_IllegalStateError) as exc_info2:
        _evaluate_closed_set(net, frozenset({"A", "B"}), frozenset({"P1"}))
    assert exc_info2.value.kind == "below_head"
    # 关 P1 时流量全压上 P3（r=100）：H_A=−371、H_B=−370，两点都欠压
    assert exc_info2.value.nodes == ["A", "B"]


# ---------------------------- 当前需水量 ---------------------------- #
def test_rehearse_uses_current_demands_and_does_not_change_them():
    """预演基于作业当前需水量；预演本身不修订需水量、不推进版本。"""
    job = _create(_tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    assert _revise(jid, 0, [{"node_id": "A", "demand": 3.0}]).status_code == 200  # v1
    d = _rehearsal(jid, 1, ["P1", "P2"])
    assert d["status"] == "feasible"
    # 第 1 步关 P1 后 A、B 全失供：失供总量按当前需水量 A=3、B=3 为 6
    assert d["plan"]["steps"][0]["unserved_demand"] == pytest.approx(6.0)
    assert d["base_snapshot"]["demands"] == [
        {"node_id": "A", "demand": 3.0},
        {"node_id": "B", "demand": 3.0},
    ]
    # 预演后需水量与版本原样（仍是 v1、A=3）
    got = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 1 and got["closed_pipes"] == []
    assert got["demands"][0]["demand"] == 3.0


def test_rehearse_revised_demands_can_make_order_infeasible():
    """修订后预演按新需水量判定：A=100 时所有仍含 A 的连通态都欠压，不可行。

    树网、允许集合仅含 B：先关 P1（A 失供不允许）非法，先关 P2（A 仍
    连通、A=100 必欠压）非法；预演本身不回滚修订。
    """
    job = _create(_tree_payload(allow=["B"]))
    jid = job["job_id"]
    # 在基态先关 P2（B 失供、允许）：v1，连通区为 S-A；再把 A 改为 100，
    # 该修订会因欠压 422 且不提交——因此换用合法的较小修订 A=50（H_A=50-
    # 2500<0 仍欠压，同样 422）。要得到“已提交的高需水量”，必须让 A 失供：
    # 允许 A、B 失供，关 P1 使连通区仅剩水源后可把 A 改成任意大值。
    job2 = _create(_tree_payload(allow=["A", "B"]))
    jid2 = job2["job_id"]
    assert _toggle(jid2, 0, "P1").status_code == 200  # v1：仅剩水源
    assert _revise(jid2, 1, [{"node_id": "A", "demand": 100.0}]).status_code == 200
    # v2：A=100、P1 关。选 P1、P2 预演：
    #   P1 先开：A 以 100 参与求解，H_A=50-10000<0 欠压（非法）；
    #   P2 先开：B 仍失供（P1 关），再开 P1 时 A=100 欠压（非法）。
    d = _rehearsal(jid2, 2, ["P1", "P2"])
    assert d["status"] == "infeasible"
    assert d["plan"] is None and d["unconfirmed_states"] == []
    got = client.get(f"/api/isolation-jobs/{jid2}").json()["job"]
    assert got["version"] == 2 and got["demands"][0]["demand"] == 100.0
    # 另一作业保持 v0 不受影响
    assert client.get(f"/api/isolation-jobs/{jid}").json()["job"]["version"] == 0


# ---------------------------- 输入校验 ---------------------------- #
def test_rehearse_validation_errors():
    job = _create(_loop_payload(allow=["A", "B"]))
    jid = job["job_id"]

    def expect_422(body, fragment):
        resp = client.post(f"/api/isolation-jobs/{jid}/rehearsal", json=body)
        assert resp.status_code == 422, resp.text
        assert fragment in resp.json()["error"]

    expect_422({"version": 0}, "pipes")
    expect_422({"version": 0, "pipes": "P1"}, "数组")
    expect_422({"version": 0, "pipes": []}, "2～6")
    expect_422({"version": 0, "pipes": ["P1"]}, "2～6")
    expect_422({"version": 0, "pipes": ["P1", "P2", "P3", "P1", "P2", "P3",
                                        "P1"]}, "2～6")
    expect_422({"version": 0, "pipes": ["P1", "P1"]}, "互异")
    expect_422({"version": 0, "pipes": ["P1", "PX"]}, "已定义的管段")
    expect_422({"version": 0, "pipes": ["P1", "  "]}, "非空字符串")
    expect_422({"version": 0, "pipes": [1, 2]}, "非空字符串")
    # version 校验与切换/修订一致
    for bad_version in ("0", 0.0, True, -1):
        resp = client.post(f"/api/isolation-jobs/{jid}/rehearsal",
                           json={"version": bad_version, "pipes": ["P1", "P2"]})
        assert resp.status_code == 422 and "非负整数" in resp.json()["error"]
    # 请求体不是对象 / 空请求体
    assert client.post(f"/api/isolation-jobs/{jid}/rehearsal",
                       json=[1, 2]).status_code == 422
    resp = client.post(f"/api/isolation-jobs/{jid}/rehearsal", content=b"",
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400
    # JSON 文本中的 NaN 记号在解析层即被拒绝
    resp = client.post(f"/api/isolation-jobs/{jid}/rehearsal",
                       content=('{"version":0,"pipes":["P1",NaN]}'),
                       headers={"Content-Type": "application/json"})
    assert resp.status_code == 400 and "JSON" in resp.json()["error"]
    # 全部失败后状态不变
    got = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 0 and got["closed_pipes"] == []


def test_rehearse_six_pipe_bound():
    """6 根管段（上界）可正常预演；每根恰好切换一次。"""
    # 以水源为星型中心的 6 条支管，叶子全部允许失供：任意顺序都合法
    payload = {
        "nodes": [{"id": "S", "type": "source", "head": 50.0}] + [
            {"id": f"D{i}", "type": "demand", "demand": 1.0}
            for i in range(1, 7)],
        "pipes": [
            {"id": f"P{i}", "start": "S", "end": f"D{i}", "r": 0.01}
            for i in range(1, 7)],
        "min_head": 0.0,
        "allow_unserved": [f"D{i}" for i in range(1, 7)],
    }
    job = _create(payload)
    d = _rehearsal(job["job_id"], 0, [f"P{i}" for i in range(1, 7)])
    assert d["status"] == "feasible"
    assert d["total_orders"] == 720  # 6!
    assert d["plan"]["pipe_order"] == [f"P{i}" for i in range(1, 7)]
    assert len(d["plan"]["steps"]) == 6
    assert d["plan"]["final_closed_pipes"] == [f"P{i}" for i in range(1, 7)]


# ---------------------------- 404 / 409 / 结束优先 ---------------------------- #
def test_rehearse_unknown_job_returns_404():
    resp = client.post("/api/isolation-jobs/does-not-exist/rehearsal",
                       json={"version": 0, "pipes": ["P1", "P2"]})
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]


def test_rehearse_terminated_job_returns_404_even_with_bad_input():
    job = _create(_tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    assert client.delete(f"/api/isolation-jobs/{jid}").status_code == 200
    # 结束后：正常请求、落后版本、非法管段列表一律 404（结束优先于一切）
    assert _rehearse(jid, 0, ["P1", "P2"]).status_code == 404
    resp = client.post(f"/api/isolation-jobs/{jid}/rehearsal",
                       json={"version": 99, "pipes": ["P1", "P2"]})
    assert resp.status_code == 404
    resp = client.post(f"/api/isolation-jobs/{jid}/rehearsal",
                       json={"version": 0, "pipes": ["P1"]})
    assert resp.status_code == 404


def test_rehearse_stale_version_conflict_409():
    job = _create(_tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    assert _toggle(jid, 0, "P2").status_code == 200  # v0 -> v1
    resp = _rehearse(jid, 0, ["P1", "P2"])
    assert resp.status_code == 409
    data = resp.json()
    assert data["ok"] is False and data["current_version"] == 1
    assert "版本冲突" in data["error"]
    # 用当前版本预演正常
    assert _rehearsal(jid, 1, ["P1", "P2"])["status"] == "feasible"
    # 冲突不改变状态
    got = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 1 and got["closed_pipes"] == ["P2"]


def test_rehearse_does_not_affect_other_jobs():
    job_a = _create(_tree_payload(allow=["A", "B"]))
    job_b = _create(_loop_payload(allow=["A", "B"]))
    _rehearsal(job_a["job_id"], 0, ["P1", "P2"])
    got_b = client.get(f"/api/isolation-jobs/{job_b['job_id']}").json()["job"]
    assert got_b["version"] == 0 and got_b["closed_pipes"] == []
    assert got_b["source_supply"] == pytest.approx(2.0)


# ---- 出口复核优先级：搜索抛 RuntimeError 等意外异常时的 404/409/500 分流 ---- #
def test_rehearse_terminated_inside_search_then_runtime_error_is_404(monkeypatch):
    """搜索的首次求解里先结束作业、再抛 RuntimeError：出口按 404（而非 500）。"""
    job = _create(_loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    state = {"fired": False}

    def boom_after_terminate(net):
        if not state["fired"]:
            state["fired"] = True
            # 模拟计算途中作业被另一请求结束（预演搜索不持作业锁，可立即生效）
            iso.terminate_job(jid)
            raise RuntimeError("模拟预演搜索期间的意外异常")
        return iso.solve_network(net)

    monkeypatch.setattr(iso, "solve_network", boom_after_terminate)
    resp = _rehearse(jid, 0, ["P1", "P2"])
    assert resp.status_code == 404
    assert "不存在" in resp.json()["error"]
    assert client.get(f"/api/isolation-jobs/{jid}").status_code == 404


def test_rehearse_version_advanced_inside_search_then_runtime_error_is_409(
        monkeypatch):
    """搜索的首次求解里先推进版本、再抛 RuntimeError：出口按 409 带当前版本。"""
    job = _create(_loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    real_solve = iso.solve_network
    state = {"fired": False}

    def advance_then_boom(net):
        if not state["fired"]:
            state["fired"] = True
            # 用真实求解器完成一次合法切换推进版本（v0 -> v1），随后再抛意外异常
            monkeypatch.setattr(iso, "solve_network", real_solve)
            iso.toggle_pipe(jid, {"version": 0, "pipe_id": "P3"})
            raise RuntimeError("模拟预演搜索期间的意外异常")
        return real_solve(net)

    monkeypatch.setattr(iso, "solve_network", advance_then_boom)
    resp = _rehearse(jid, 0, ["P1", "P2"])
    assert resp.status_code == 409
    data = resp.json()
    assert data["ok"] is False and data["current_version"] == 1
    assert "版本冲突" in data["error"]
    # RuntimeError 不得覆盖新提交的状态
    got = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 1 and got["closed_pipes"] == ["P3"]


def test_rehearse_terminate_and_advance_inside_search_then_error_is_404(
        monkeypatch):
    """搜索中先推进版本再结束作业、随后抛 RuntimeError：404（结束优先于冲突）。"""
    job = _create(_loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    real_solve = iso.solve_network
    state = {"fired": False}

    def advance_terminate_then_boom(net):
        if not state["fired"]:
            state["fired"] = True
            monkeypatch.setattr(iso, "solve_network", real_solve)
            iso.toggle_pipe(jid, {"version": 0, "pipe_id": "P3"})  # v0 -> v1
            iso.terminate_job(jid)
            raise RuntimeError("模拟预演搜索期间的意外异常")
        return real_solve(net)

    monkeypatch.setattr(iso, "solve_network", advance_terminate_then_boom)
    resp = _rehearse(jid, 0, ["P1", "P2"])
    assert resp.status_code == 404  # 而非 409/500
    assert "不存在" in resp.json()["error"]


def test_rehearse_unchanged_job_runtime_error_is_server_500(monkeypatch):
    """作业存续且版本未变时搜索抛 RuntimeError：保持原服务器错误 500。

    绝不能把它改报成 infeasible/not_confirmed（业务结论），也不是 409/404。
    """
    job = _create(_loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    real_solve = iso.solve_network
    state = {"fired": False}

    def boom_once(net):
        if not state["fired"]:
            state["fired"] = True
            raise RuntimeError("模拟预演搜索期间的意外异常")
        return real_solve(net)

    monkeypatch.setattr(iso, "solve_network", boom_once)
    # raise_server_exceptions=False：把未捕获异常按真实 HTTP 500 返回捕获
    with TestClient(app, raise_server_exceptions=False) as err_client:
        resp = err_client.post(f"/api/isolation-jobs/{jid}/rehearsal",
                               json={"version": 0, "pipes": ["P1", "P2"]})
    assert resp.status_code == 500
    assert "infeasible" not in resp.text and "not_confirmed" not in resp.text
    # 作业未被改变（只读）：仍为版本 0、无关闭管段
    got = client.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 0 and got["closed_pipes"] == []
    # 修复后同版本预演仍可正常得出原业务结论（环网允许 A、B 失供时可行）
    d = _rehearsal(jid, 0, ["P1", "P2", "P3"])
    assert d["status"] == "feasible"
    assert d["plan"]["pipe_order"] == ["P3", "P1", "P2"]


def test_rehearse_solver_not_converged_is_not_confirmed_not_500(monkeypatch):
    """对照：业务性 SolverNotConverged 仍归 not_confirmed（200），不是 500。

    只有 RuntimeError 这类“意外异常”才走服务器错误；求解不收敛是搜索已定义
    的三态之一。环网首状态 {P1} 全连通必调求解器，令其一次性不收敛。
    """
    job = _create(_loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    real_solve = iso.solve_network
    state = {"fired": False}

    def fail_once(net):
        if not state["fired"]:
            state["fired"] = True
            raise SolverNotConverged("模拟某关闭集合不收敛")
        return real_solve(net)

    monkeypatch.setattr(iso, "solve_network", fail_once)
    resp = _rehearse(jid, 0, ["P1", "P2"])
    assert resp.status_code == 200
    d = resp.json()["rehearsal"]
    assert d["status"] == "not_confirmed"
    assert d["minimum_confirmed"] is False



# ------------------ 计算期间的 HTTP 并发（可暂停求解门 + 真实 Uvicorn） ------------------ #
class _PausableCompute:
    """与 test_isolation 同款的求解门：命中 (job_id, stage, closed) 时阻塞。"""

    def __init__(self, *, job_id, stage, closed=frozenset(), hold_seconds=5.0):
        self._want_job = job_id
        self._want_stage = stage
        self._want_closed = frozenset(closed)
        self._hold = hold_seconds
        self._claimed = threading.Lock()
        self.arrived = threading.Event()
        self.released = threading.Event()

    def __call__(self, gate_job_id, stage, *, version, closed):
        hit = (gate_job_id == self._want_job and stage == self._want_stage
               and frozenset(closed) == self._want_closed)
        if not hit or not self._claimed.acquire(blocking=False):
            return
        self.arrived.set()
        if not self.released.wait(timeout=self._hold):
            raise AssertionError("求解门在超时内未被放行，测试疑似死锁")


class _PausableFailingCompute:
    """暂停并放行的计算替身：门放行后让本次预演搜索的首次求解抛“意外异常”。

    与 ``_PausableCompute`` 一样在 (job_id, "rehearse") 命中时阻塞，但放行的
    瞬间才把 ``iso.solve_network`` 换成一次性替身——搜索随后的第 1 次状态
    评估直接抛出 ``error``（默认 ``RuntimeError``，区别于业务性的
    ``SolverNotConverged``：后者会被搜索归为 not_confirmed，前者必须原样
    逸出，由出口复核决定 404/409/500）。

    替身只在**放行之后**安装并“上膛”，且只命中一次：暂停期间用来推进版本
    的并发切换/修订仍走真实求解器，绝不被这个失败替身污染。这正是用于确定
    “搜索抛出意外异常的同一时刻，作业已结束 / 版本已推进 / 作业未变化”
    三种交错顺序的手段。
    """

    def __init__(self, *, job_id, monkeypatch, hold_seconds=5.0,
                 error=RuntimeError, message="模拟预演搜索期间的意外异常"):
        self._want_job = job_id
        self._hold = hold_seconds
        self._monkeypatch = monkeypatch
        self._error = error
        self._message = message
        self._claimed = threading.Lock()
        self.arrived = threading.Event()
        self.released = threading.Event()
        self.fail_armed = False  # 放行后上膛；替身首次命中后自动退膛

    def __call__(self, gate_job_id, stage, *, version, closed):
        if stage != "rehearse" or gate_job_id != self._want_job:
            return
        if not self._claimed.acquire(blocking=False):
            return
        self.arrived.set()
        if not self.released.wait(timeout=self._hold):
            raise AssertionError("求解门在超时内未被放行，测试疑似死锁")
        real_solve = iso.solve_network

        def failing(net):
            if self.fail_armed:
                self.fail_armed = False  # 一次性：只让搜索的首次评估炸
                raise self._error(self._message)
            return real_solve(net)

        # 先上膛再换替身：两者都在本（预演）线程放行后、搜索继续前完成，
        # 暂停期间的并发操作不会看到这个替身。
        self.fail_armed = True
        self._monkeypatch.setattr(iso, "solve_network", failing)


import socket  # noqa: E402

import httpx  # noqa: E402
import uvicorn  # noqa: E402


@pytest.fixture(scope="module")
def server():
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    sock.close()
    config = uvicorn.Config(app, host="127.0.0.1", port=port,
                            log_level="warning", lifespan="off")
    server_inst = uvicorn.Server(config)
    thread = threading.Thread(target=server_inst.run, daemon=True)
    thread.start()
    import time
    deadline = time.monotonic() + 10
    while time.monotonic() < deadline:
        try:
            with socket.create_connection(("127.0.0.1", port), timeout=0.5):
                break
        except OSError:
            time.sleep(0.02)
    else:
        raise RuntimeError("测试用 Uvicorn 未能在超时内就绪")
    with httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=10) as http:
        yield http
    server_inst.should_exit = True
    thread.join(timeout=5)


def _create_on(http, payload) -> dict:
    resp = http.post("/api/isolation-jobs", json=payload)
    assert resp.status_code == 200, resp.text
    return resp.json()["job"]


def _post_async_on(http, path, body):
    result: dict[str, object] = {}

    def worker():
        try:
            result["response"] = http.post(path, json=body)
        except Exception as exc:  # noqa: BLE001
            result["error"] = exc

    t = threading.Thread(target=worker, daemon=True)
    t.start()
    return t, result


def test_http_rehearse_paused_does_not_block_and_commits_nothing(
        server, monkeypatch):
    """预演重算暂停期间：健康检查/读取/其他作业立即可用；预演不提交任何状态。"""
    job = _create_on(server, _tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    other = _create_on(server, _loop_payload())
    gate = _PausableCompute(job_id=jid, stage="rehearse", closed=frozenset())
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)

    # 暂停期间服务照常交互
    assert server.get("/api/health").status_code == 200
    assert server.get(f"/api/isolation-jobs/{jid}").json()["job"]["version"] == 0
    resp = server.post(f"/api/isolation-jobs/{other['job_id']}/toggle",
                       json={"version": 0, "pipe_id": "P3"})
    assert resp.status_code == 200 and resp.json()["job"]["version"] == 1

    gate.released.set()
    t_reh.join(timeout=5)
    assert not t_reh.is_alive()
    assert "error" not in res_reh, res_reh.get("error")
    response = res_reh["response"]
    assert response.status_code == 200
    d = response.json()["rehearsal"]
    assert d["status"] == "feasible"
    assert d["plan"]["pipe_order"] == ["P1", "P2"]
    # 预演绝不推进版本、不改快照
    got = server.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 0 and got["closed_pipes"] == []


def test_http_rehearse_version_advanced_during_compute_returns_409(
        server, monkeypatch):
    """预演计算期间另一操作成功推进版本：预演结束时返回 409 与 current_version。"""
    job = _create_on(server, _tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableCompute(job_id=jid, stage="rehearse", closed=frozenset())
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    # 计算期间成功切换：v0 -> v1
    resp = server.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0, "pipe_id": "P2"})
    assert resp.status_code == 200 and resp.json()["job"]["version"] == 1

    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    assert response.status_code == 409
    assert response.json()["current_version"] == 1
    assert "版本冲突" in response.json()["error"]
    # 作业状态是切换提交的 v1，与预演无关
    got = server.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 1 and got["closed_pipes"] == ["P2"]


def test_http_rehearse_terminated_during_compute_returns_404(
        server, monkeypatch):
    """预演被暂停时结束作业：DELETE 立即返回，放行后预演按 404 结束（结束优先）。"""
    job = _create_on(server, _tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableCompute(job_id=jid, stage="rehearse", closed=frozenset())
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    # 健康检查与读取在暂停期间照常
    assert server.get("/api/health").status_code == 200
    assert server.get(f"/api/isolation-jobs/{jid}").status_code == 200
    # 结束立即返回
    assert server.delete(f"/api/isolation-jobs/{jid}").status_code == 200
    # 结束后、放行前的新请求 404
    assert server.post(f"/api/isolation-jobs/{jid}/rehearsal",
                       json={"version": 0, "pipes": ["P1", "P2"]}).status_code == 404

    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    # 本会算成 200 的预演，因结束优先而为 404（不是 200/409）
    assert response.status_code == 404
    assert "不存在" in response.json()["error"]
    assert server.get(f"/api/isolation-jobs/{jid}").status_code == 404


def test_http_rehearse_terminate_and_version_both_ended_is_404(server, monkeypatch):
    """计算结束时版本已推进且作业已结束：404（结束优先于冲突）。"""
    job = _create_on(server, _tree_payload(allow=["A", "B"]))
    jid = job["job_id"]
    other = _create_on(server, _loop_payload())
    gate = _PausableCompute(job_id=jid, stage="rehearse", closed=frozenset())
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    # 计算期间其他作业不受影响
    assert server.post(f"/api/isolation-jobs/{other['job_id']}/toggle",
                       json={"version": 0, "pipe_id": "P3"}).status_code == 200
    # 先推进版本再结束
    assert server.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0, "pipe_id": "P2"}).status_code == 200
    assert server.delete(f"/api/isolation-jobs/{jid}").status_code == 200

    gate.released.set()
    t_reh.join(timeout=5)
    assert res_reh["response"].status_code == 404


# --- 意外异常（RuntimeError 等）与结束/版本推进的交错：出口复核优先级回归 --- #
def test_http_rehearse_terminated_during_compute_unexpected_error_is_404(
        server, monkeypatch):
    """合法预演搜索暂停期间结束作业，放行后搜索抛 RuntimeError：仍按 404。

    回归点：意外异常曾经直接逸出成 500、跳过结束复核。修复后所有计算出口
    先复核——已结束一律 404（结束优先于冲突与计算错误），且绝不恢复作业。
    用环网 P1/P2 预演：DFS 首状态 {P1} 全连通，必调用求解器，替身一定命中。
    """
    job = _create_on(server, _loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableFailingCompute(job_id=jid, monkeypatch=monkeypatch)
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    # 暂停期间健康检查、读取照常
    assert server.get("/api/health").status_code == 200
    assert server.get(f"/api/isolation-jobs/{jid}").json()["job"]["version"] == 0
    # 结束立即返回（不等待在途计算）
    assert server.delete(f"/api/isolation-jobs/{jid}").status_code == 200

    # 放行：搜索继续即抛 RuntimeError（本会是 500），但作业已结束 -> 404
    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    assert response.status_code == 404
    assert "不存在" in response.json()["error"]
    # 预演没有复活作业、没有提交任何状态
    assert server.get(f"/api/isolation-jobs/{jid}").status_code == 404


def test_http_rehearse_version_advanced_then_unexpected_error_is_409(
        server, monkeypatch):
    """搜索暂停期间版本被推进，放行后搜索抛 RuntimeError：返回 409。

    回归点：意外异常曾经跳过版本复核直接 500。修复后作业仍存在但版本已变
    时返回 409 与 current_version，且异常不得改报 infeasible/not_confirmed。
    暂停期间用真实求解器关 P3（合法）推进版本，再让被暂停预演的搜索炸掉。
    """
    job = _create_on(server, _loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableFailingCompute(job_id=jid, monkeypatch=monkeypatch)
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    # 暂停期间成功切换：v0 -> v1（走真实求解器，未被失败替身影响）
    resp = server.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0, "pipe_id": "P3"})
    assert resp.status_code == 200 and resp.json()["job"]["version"] == 1

    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    assert response.status_code == 409
    data = response.json()
    assert data["ok"] is False and data["current_version"] == 1
    assert "版本冲突" in data["error"]
    # 作业是切换提交的 v1，RuntimeError 没有覆盖新状态
    got = server.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 1 and got["closed_pipes"] == ["P3"]


def test_http_rehearse_terminate_and_advance_then_unexpected_error_is_404(
        server, monkeypatch):
    """版本已推进且作业已结束、搜索随后抛 RuntimeError：404（结束最高优先）。"""
    job = _create_on(server, _loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableFailingCompute(job_id=jid, monkeypatch=monkeypatch)
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    assert server.post(f"/api/isolation-jobs/{jid}/toggle",
                       json={"version": 0, "pipe_id": "P3"}).status_code == 200
    assert server.delete(f"/api/isolation-jobs/{jid}").status_code == 200

    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    assert response.status_code == 404  # 而非 409/500
    assert "不存在" in response.json()["error"]


def test_http_rehearse_unchanged_job_unexpected_error_stays_500(
        server, monkeypatch):
    """作业存续且版本未变时搜索抛 RuntimeError：仍按原服务器错误 500 处理。

    不得改报 infeasible（200）或 not_confirmed（200）；作业保持只读不变。
    """
    job = _create_on(server, _loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableFailingCompute(job_id=jid, monkeypatch=monkeypatch)
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    # 不结束、不推进版本，直接放行让搜索抛 RuntimeError
    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    assert response.status_code == 500
    body = response.text
    assert "infeasible" not in body and "not_confirmed" not in body
    # 作业未变化：版本/关闭集合/快照都保持预演前。500 是未捕获异常，Uvicorn
    # 会复位该 keep-alive 连接，故用新连接核对作业（与业务语义无关）。
    with httpx.Client(base_url=str(server.base_url), timeout=10) as fresh:
        got = fresh.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 0 and got["closed_pipes"] == []


def test_http_rehearse_unchanged_job_business_error_stays_not_confirmed(
        server, monkeypatch):
    """对照：业务性的 SolverNotConverged 在搜索中仍归 not_confirmed（200）。

    只有 RuntimeError 这类意外异常才按 500；求解不收敛始终是业务结论。
    环网首状态 {P1} 全连通，替身一次性抛 SolverNotConverged，后续状态仍走
    真实求解器，整体结论为 not_confirmed 而非 500。
    """
    job = _create_on(server, _loop_payload(allow=["A", "B"]))
    jid = job["job_id"]
    gate = _PausableFailingCompute(
        job_id=jid, monkeypatch=monkeypatch, error=SolverNotConverged,
        message="模拟某关闭集合不收敛")
    monkeypatch.setattr(iso, "_COMPUTE_GATE", gate)

    t_reh, res_reh = _post_async_on(
        server, f"/api/isolation-jobs/{jid}/rehearsal",
        {"version": 0, "pipes": ["P1", "P2"]})
    assert gate.arrived.wait(timeout=5)
    gate.released.set()
    t_reh.join(timeout=5)
    response = res_reh["response"]
    assert response.status_code == 200
    d = response.json()["rehearsal"]
    assert d["status"] == "not_confirmed"
    assert d["minimum_confirmed"] is False
    _strict_json_loads(response.text)
    # 作业仍只读不变
    got = server.get(f"/api/isolation-jobs/{jid}").json()["job"]
    assert got["version"] == 0 and got["closed_pipes"] == []
