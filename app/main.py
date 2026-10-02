"""FastAPI 入口：/api/solve 等求解接口、/api/isolation-jobs 停管隔离作业接口（含 DELETE 结束作业与 /rehearsal 切换顺序预演），其余路径提供原生前端静态页面。"""

from __future__ import annotations

import json
import os
from typing import Any

from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from starlette.concurrency import run_in_threadpool

from .isolation import (
    IsolationRuleError,
    JobNotFoundError,
    VersionConflictError,
    create_job,
    get_snapshot,
    rehearse_switch_plan,
    revise_demands,
    terminate_job,
    toggle_pipe,
)
from .solver import (
    SolverNotConverged,
    ValidationError,
    analyze_pipe_outages,
    locate_single_leak,
    plan_resupply,
    solve_network,
    validate_leak_payload,
    validate_payload,
    validate_resupply_payload,
)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(BASE_DIR, "static")

app = FastAPI(title="闭环供水管网平衡台", version="1.0.0")


def _reject_constant(value: str) -> Any:
    """拒绝 JSON 标准之外的 NaN / Infinity / -Infinity 记号。"""
    raise ValueError(f"非法的 JSON 数值记号：{value}")


@app.get("/api/health")
def health() -> dict[str, str]:
    return {"status": "ok"}


async def _load_payload(request: Request, *, empty_hint: str | None = None
                        ) -> tuple[Any, JSONResponse | None]:
    """读取原始请求体并解析 JSON；任何问题转成统一的 400 响应。"""
    raw = await request.body()
    if not raw:
        return None, JSONResponse(status_code=400, content={
            "ok": False,
            "error": empty_hint or "请求体为空，请先填写节点与管段数据。"})
    try:
        payload = json.loads(raw.decode("utf-8"), parse_constant=_reject_constant)
    except (ValueError, UnicodeDecodeError) as exc:
        return None, JSONResponse(status_code=400, content={
            "ok": False,
            "error": f"请求体不是合法 JSON：{exc}。请确认所有输入均为有限数值。"})
    return payload, None


@app.post("/api/solve")
async def solve(request: Request) -> JSONResponse:
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        network = validate_payload(payload)
        result = solve_network(network)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "result": result})


@app.post("/api/analyze-outages")
async def analyze_outages(request: Request) -> JSONResponse:
    """逐根停管情景分析：先求基线，再对每根管段各评估一个移除情景。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        network = validate_payload(payload)
        analysis = analyze_pipe_outages(network)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "analysis": analysis})


@app.post("/api/locate-leak")
async def locate_leak(request: Request) -> JSONResponse:
    """基于实测水头的单点漏损定位：沿用管网校验与稳态求解，逐用水节点拟合漏量。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        case = validate_leak_payload(payload)
        analysis = locate_single_leak(case)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "leak_analysis": analysis})


@app.post("/api/plan-resupply")
async def plan_resupply_endpoint(request: Request) -> JSONResponse:
    """停供节点同时复供组合优选：先求复供前基线，再核对包含空集在内的全部组合。"""
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad

    try:
        case = validate_resupply_payload(payload)
        analysis = plan_resupply(case)
    except ValidationError as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})
    except SolverNotConverged as exc:
        return JSONResponse(status_code=422,
                            content={"ok": False, "error": str(exc)})

    return JSONResponse(content={"ok": True, "resupply_analysis": analysis})


# --------------------------------------------------------------------------- #
# 停管隔离作业（仅存于单进程内存，重启即清空；无对应页面）
# --------------------------------------------------------------------------- #
_ISOLATION_ERRORS = (
    ValidationError,
    IsolationRuleError,
    SolverNotConverged,
    VersionConflictError,
    JobNotFoundError,
)


def _isolation_error(exc: Exception) -> JSONResponse:
    """把停管隔离作业的各类失败映射为统一的中文错误响应（作业状态保持原状）。"""
    if isinstance(exc, JobNotFoundError):
        return JSONResponse(status_code=404, content={
            "ok": False,
            "error": f"停管隔离作业“{exc.args[0]}”不存在"
                     "（作业仅存于单进程内存，服务重启后即清空）。"})
    if isinstance(exc, VersionConflictError):
        return JSONResponse(status_code=409, content={
            "ok": False, "error": str(exc), "current_version": exc.current})
    return JSONResponse(status_code=422,
                        content={"ok": False, "error": str(exc)})


@app.post("/api/isolation-jobs")
async def create_isolation_job(request: Request) -> JSONResponse:
    """创建停管隔离作业：沿用校验与稳态求解，初始欠压或不收敛则不创建。

    校验与初始求解放到工作线程执行：单进程单事件循环下，计算期间健康检查、
    已有作业读取和其他作业的切换仍可立即返回。
    """
    payload, bad = await _load_payload(request)
    if bad is not None:
        return bad
    try:
        snapshot = await run_in_threadpool(create_job, payload)
    except _ISOLATION_ERRORS as exc:
        return _isolation_error(exc)
    return JSONResponse(content={"ok": True, "job": snapshot})


@app.get("/api/isolation-jobs/{job_id}")
async def read_isolation_job(job_id: str) -> JSONResponse:
    """读取最近一次成功提交的完整快照；即使作业正在计算也立即返回，绝不混合版本。"""
    try:
        snapshot = get_snapshot(job_id)
    except JobNotFoundError as exc:
        return _isolation_error(exc)
    return JSONResponse(content={"ok": True, "job": snapshot})


@app.post("/api/isolation-jobs/{job_id}/toggle")
async def toggle_isolation_pipe(job_id: str, request: Request) -> JSONResponse:
    """携带当前版本切换一根现有管段的开闭；成功才提交新版本，失败全部保持原状。

    重算在工作线程进行，不阻塞事件循环：计算期间该作业的读取返回最近已提交
    快照，其他作业与健康检查照常服务。
    """
    payload, bad = await _load_payload(
        request, empty_hint="请求体为空，请提供 version（作业当前版本号）与 pipe_id。")
    if bad is not None:
        return bad
    try:
        snapshot = await run_in_threadpool(toggle_pipe, job_id, payload)
    except _ISOLATION_ERRORS as exc:
        return _isolation_error(exc)
    pipe_id = payload["pipe_id"].strip()
    return JSONResponse(content={
        "ok": True,
        "toggled_pipe": pipe_id,
        "toggled_to": "closed" if pipe_id in snapshot["closed_pipes"] else "open",
        "job": snapshot,
    })


def _rehearsal_conflict_response(base_version: int,
                                 current_version: int) -> JSONResponse:
    """预演计算出口发现版本已变：409 并带当前 current_version。"""
    return JSONResponse(status_code=409, content={
        "ok": False,
        "error": (
            f"版本冲突：请求携带的版本为 {base_version}，"
            f"作业当前版本已为 {current_version}；预演基于旧版本完成，"
            "其关闭集合或需水量可能已不是当前状态，请重新读取最新快照后再试。"),
        "current_version": current_version})


def _rehearsal_exit_review(job_id: str, request_version: Any, *,
                           check_version: bool = True) -> JSONResponse | None:
    """预演的**所有计算出口**统一复核，严格按以下优先级给出结论：

      1. 作业已结束（或未知）——返回 404（结束优先于一切，包括冲突与错误）；
      2. ``check_version`` 为真且作业仍存在、版本已推进——返回 409 与
         current_version；
      3. 作业存续且版本未变（或不做版本复核）——放行（返回 None），由调用方
         处理原结果或原错误。

    ``check_version`` 仅对“真正基于 request_version 完成了一段计算/或本身即
    版本冲突”的出口开启：同步入参校验失败（含 version 非法，如负数、布尔、
    字符串）时并未开始任何基于该版本的计算，结束复核仍优先（404），但不得把
    负版本号这类非法输入改判成 409——版本比对只在 ``request_version`` 为
    非布尔整数时参与。
    """
    try:
        current = get_snapshot(job_id)
    except JobNotFoundError:
        return _isolation_error(JobNotFoundError(job_id))
    valid_version = (isinstance(request_version, int)
                     and not isinstance(request_version, bool))
    if (check_version and valid_version
            and current["version"] != request_version):
        return _rehearsal_conflict_response(request_version, current["version"])
    return None


@app.post("/api/isolation-jobs/{job_id}/rehearsal")
async def rehearse_isolation_plan(job_id: str, request: Request) -> JSONResponse:
    """预演 2～6 根互异现有管段的可执行切换顺序，不实际操作作业。

    请求体为 {"version": 当前版本号, "pipes": 管段编号数组}；每根恰好切换
    一次、其他管段不动，基于该版本的关闭集合与当前需水量逐步核对允许失供、
    最低水头与稳态收敛（所有中间状态与终态均须合法），按管段录入序号序列
    的字典序取最小可行顺序。搜索途中求解不收敛或出现非有限数时结论为
    “无法确认”，绝不当作不可行。预演不推进版本、不改需水量或快照；搜索
    在工作线程进行，不持作业锁，不阻塞读取、结束与其他作业。

    计算的**每一个出口**（正常算完、原有业务异常、意外异常）都先经
    ``_rehearsal_exit_review`` 复核，响应优先级固定为：

      * 作业已结束（含计算途中被结束）——一律 404，绝不恢复已结束作业、
        绝不覆盖新状态，也不会改报 200/409/422；
      * 作业仍存在但版本已被推进——409 并带 current_version；
      * 作业存续且版本未变——正常结果返回 200，原有业务错误（422/409）
        保持原语义；搜索抛出的 RuntimeError 等**意外异常**不属于
        infeasible/not_confirmed，仍按原服务器错误（500）抛出，绝不改报
        “不可行”或“无法确认”。
    """
    payload, bad = await _load_payload(
        request,
        empty_hint="请求体为空，请提供 version（作业当前版本号）与 pipes（2～6 根互异现有管段编号）。")
    if bad is not None:
        return bad
    request_version = payload.get("version") if isinstance(payload, dict) else None

    # 计算在工作线程进行：搜索全程不持作业锁，读取、结束与其他作业均可并发
    try:
        result = await run_in_threadpool(rehearse_switch_plan, job_id, payload)
    except _ISOLATION_ERRORS as exc:
        # 这些错误都在搜索开始前的短暂临界区（入参归属、版本与字段校验）
        # 同步抛出，计算尚未开始：出口仍先复核结束（404 优先）。只有“版本
        # 冲突”本身才顺带刷新 current_version；入参校验类错误（version 非法
        # 如负数、pipes 非法等）不做版本比对，保持原 422，绝不把负版本号
        # 改判成 409。
        override = _rehearsal_exit_review(
            job_id, request_version,
            check_version=isinstance(exc, VersionConflictError))
        if override is not None:
            return override
        return _isolation_error(exc)
    except Exception:
        # 搜索期间的意外异常（RuntimeError 等，非 SolverNotConverged 等业务
        # 错误）：绝不改报“不可行/无法确认”。先复核结束标记与版本——
        # 已结束 404、版本已推进 409；仅当作业存续且版本未变时才让该意外
        # 异常按原服务器错误逸出（FastAPI 记日志并返回 500）。
        override = _rehearsal_exit_review(job_id, request_version)
        if override is not None:
            return override
        raise

    # 正常计算出口：结束优先于版本冲突（若二者同时成立，按 404）
    override = _rehearsal_exit_review(job_id, result["base_version"])
    if override is not None:
        return override
    return JSONResponse(content={"ok": True, "rehearsal": result})


@app.post("/api/isolation-jobs/{job_id}/demands")
async def revise_isolation_demands(job_id: str, request: Request) -> JSONResponse:
    """携带当前版本修订一批现有用水节点的需水量，无需重建作业。

    请求体为 {"version": 当前版本号, "demands": 非空修订列表}，每项含
    node_id（作业内互异的现有用水节点）与 demand（非负有限数，拒绝布尔值）。
    只改列出的节点；按当前关闭集合重算，连通节点用新需水量且不得欠压，
    失供节点保存新值并更新失供总量。单个节点允许取 1e308 这样的有限大值
    （不设任何人为上限），但失供需水量之和等聚合值溢出为非有限数、或候选
    快照含 NaN/Infinity 时，按规则返回 422 并说明原因，绝不让不可严格 JSON
    编码的快照提交。成功才同时提交需水量、完整快照与版本加一；非法输入、
    聚合溢出、欠压或不收敛均不改变状态。同版本的修订与管段切换合计最多
    成功一次，落后提交返回 409 与 current_version。

    与切换相同，重算在工作线程进行：计算期间该作业读取返回最近已提交快照，
    其他作业与健康检查照常服务。
    """
    payload, bad = await _load_payload(
        request,
        empty_hint="请求体为空，请提供 version（作业当前版本号）与非空的 demands 修订列表。")
    if bad is not None:
        return bad
    try:
        snapshot, updates = await run_in_threadpool(revise_demands, job_id, payload)
    except _ISOLATION_ERRORS as exc:
        return _isolation_error(exc)
    return JSONResponse(content={
        "ok": True,
        "revised_demands": updates,
        "job": snapshot,
    })


@app.delete("/api/isolation-jobs/{job_id}")
async def terminate_isolation_job(job_id: str) -> JSONResponse:
    """结束不再使用的停管隔离作业：无需请求体与版本号，成功返回该作业 ID。

    结束立即生效、不等待也不中断正在进行的底层数值计算：作业从内存注册表
    摘除后，新发起的读取、切换与需水量修订一律 404；结束时尚未提交的在途
    操作无论之后算成还是触发失供/欠压/不收敛/非有限数错误，到达提交点时
    同样按 404 放弃，不能再提交、返回成功或恢复作业。未知 ID 与重复结束
    均返回 404；其他作业与健康检查不受影响。
    """
    try:
        removed_id = terminate_job(job_id)
    except JobNotFoundError as exc:
        return _isolation_error(exc)
    return JSONResponse(content={"ok": True, "terminated_job": removed_id})


# 静态资源（index.html / style.css / app.js）——该路由必须最后注册
app.mount("/", StaticFiles(directory=STATIC_DIR, html=True), name="static")
