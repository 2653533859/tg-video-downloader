"""下载任务状态机迁移校验测试。"""

from src.download.transitions import (
    TERMINAL_STATES,
    ACTIVE_STATES,
    is_terminal,
    can_transition,
)


class TestIsTerminal:
    def test_terminal_and_active_sets(self):
        assert TERMINAL_STATES == {"done", "skipped", "error", "cancelled"}
        assert ACTIVE_STATES == {"submitting", "queued", "downloading", "paused"}
        for s in TERMINAL_STATES:
            assert is_terminal(s) is True
        for s in ACTIVE_STATES:
            assert is_terminal(s) is False
        assert is_terminal(None) is False


class TestCanTransition:
    def test_normal_forward_flow(self):
        assert can_transition(None, "submitting") is True
        assert can_transition("submitting", "queued") is True
        assert can_transition("queued", "downloading") is True
        assert can_transition("downloading", "done") is True
        assert can_transition("downloading", "paused") is True
        assert can_transition("paused", "downloading") is True

    def test_downloading_to_error_allowed(self):
        # watchdog 对进行中的任务标记 error 是合法的
        assert can_transition("downloading", "error") is True
        assert can_transition("downloading", "cancelled") is True

    def test_terminal_not_overwritten(self):
        # 核心保护：终态不能被自动流程改写为其它状态
        assert can_transition("done", "error") is False
        assert can_transition("done", "downloading") is False
        assert can_transition("cancelled", "done") is False
        assert can_transition("error", "cancelled") is False
        assert can_transition("skipped", "error") is False

    def test_terminal_revive_only_with_flag(self):
        # resume/retry 显式复活：allow_revive=True 才放行，且仅限复活目标
        assert can_transition("error", "queued", allow_revive=True) is True
        assert can_transition("error", "submitting", allow_revive=True) is True
        assert can_transition("cancelled", "downloading", allow_revive=True) is True
        # 不带 flag 一律拒绝
        assert can_transition("error", "queued", allow_revive=False) is False
        # 复活也不能直接跳到另一个终态
        assert can_transition("error", "done", allow_revive=True) is False

    def test_idempotent_and_edge_cases(self):
        assert can_transition("done", "done") is True          # 幂等
        assert can_transition("downloading", "downloading") is True
        assert can_transition("downloading", "") is True       # 不改 status
        assert can_transition("downloading", None) is True
        assert can_transition("weird_unknown", "downloading") is True  # 未知来源不限制
