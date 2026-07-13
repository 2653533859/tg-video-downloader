"""下载任务状态机：显式迁移表与校验。

终态一旦写入，禁止被自动流程（watchdog、进度回调、卡死修复）覆盖；
只有 resume/retry 这类"复活"入口（allow_revive=True）能把终态重置回过渡态。
"""

TERMINAL_STATES = frozenset({"done", "skipped", "error", "cancelled"})
ACTIVE_STATES = frozenset({"submitting", "queued", "downloading", "paused"})
ALL_STATES = TERMINAL_STATES | ACTIVE_STATES

# 允许从终态"复活"到的目标（仅供 resume/retry 显式使用）
_REVIVE_TARGETS = frozenset({"submitting", "queued", "downloading"})

# 显式迁移邻接表：current -> 允许的 new 集合（不含复活路径，复活由 allow_revive 单独放行）
_ALLOWED = {
    None: ALL_STATES,
    "submitting": frozenset({"queued", "downloading", "paused", "done", "skipped", "error", "cancelled"}),
    "queued": frozenset({"submitting", "downloading", "paused", "done", "skipped", "error", "cancelled"}),
    "downloading": frozenset({"paused", "done", "skipped", "error", "cancelled"}),
    "paused": frozenset({"queued", "downloading", "done", "skipped", "error", "cancelled"}),
    # 终态默认不可迁出（复活走 allow_revive 分支）
    "done": frozenset(),
    "skipped": frozenset(),
    "error": frozenset(),
    "cancelled": frozenset(),
}


def is_terminal(state):
    return state in TERMINAL_STATES


def can_transition(current, new, *, allow_revive=False):
    """判断 current -> new 是否为合法状态迁移。

    - new 为空/None：视为"不改动 status"，放行（调用方只更新其它字段）。
    - current 为空/None：视为新建，放行。
    - current == new：幂等，放行。
    - current 为未知状态（不在 ALL_STATES）：不加限制，放行（避免误伤未预期状态）。
    - allow_revive=True 且 current 为终态、new 为复活目标：放行。
    """
    current = current or None
    if not new:
        return True
    if current == new:
        return True
    if current is not None and current not in ALL_STATES:
        return True
    if new in _ALLOWED.get(current, frozenset()):
        return True
    if allow_revive and current in TERMINAL_STATES and new in _REVIVE_TARGETS:
        return True
    return False
