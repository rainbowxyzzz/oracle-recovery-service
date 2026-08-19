"""Lightweight task state labels for API display."""

from recovery_service.core.enums import TaskState

VALID_TRANSITIONS: dict[TaskState, set[TaskState]] = {
    TaskState.CREATED: {TaskState.DISCOVERING, TaskState.CANCELLED},
    TaskState.DISCOVERING: {TaskState.POLICY_RUNNING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.POLICY_RUNNING: {
        TaskState.METADATA_READY,
        TaskState.IMPORTING,
        TaskState.CORRECTING,
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED_WITH_WARNINGS,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.METADATA_READY: {TaskState.IMPORTING, TaskState.FAILED, TaskState.CANCELLED},
    TaskState.IMPORTING: {
        TaskState.CORRECTING,
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED_WITH_WARNINGS,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.CORRECTING: {
        TaskState.IMPORTING,
        TaskState.SUCCEEDED,
        TaskState.SUCCEEDED_WITH_WARNINGS,
        TaskState.FAILED,
        TaskState.CANCELLED,
    },
    TaskState.SUCCEEDED: set(),
    TaskState.SUCCEEDED_WITH_WARNINGS: set(),
    TaskState.FAILED: set(),
    TaskState.CANCELLED: set(),
}


def can_transition(current: TaskState, new: TaskState) -> bool:
    return new in VALID_TRANSITIONS.get(current, set())
