from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from src.core.enums import AckPolicy, InterruptStatus, MailboxDeliveryStatus, NodeStatus, TaskStatus
from src.core.models import Checkpoint, Interrupt, InterruptAnswer, MailboxDelivery, MailboxMessage, Task, TaskNode

from .errors import LifecycleTransitionError
from .rust_contract import cancel_node_target, contract_value, status_list, transition_allowed, transition_target


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleTransitionError(message)


def _target(enum_cls: type, operation: str):
    return enum_cls(transition_target(operation))


def mark_delivery_delivered(delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    operation = "mailbox_delivery.mark_delivered"
    _ensure(transition_allowed(operation, delivery.status), "Only pending deliveries can be marked delivered.")
    return replace(delivery, status=_target(MailboxDeliveryStatus, operation), delivered_at=now, updated_at=now)


def acknowledge_delivery(message: MailboxMessage, delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    operation = "mailbox_delivery.acknowledge"
    _ensure(message.ack_policy == AckPolicy.STRONG, "Only strong-ACK messages can be explicitly acknowledged.")
    _ensure(transition_allowed(operation, delivery.status), "Only delivered messages can be acknowledged.")
    return replace(delivery, status=_target(MailboxDeliveryStatus, operation), acknowledged_at=now, updated_at=now)


def resolve_delivery(message: MailboxMessage, delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    operation = "mailbox_delivery.resolve_strong" if message.ack_policy == AckPolicy.STRONG else "mailbox_delivery.resolve_light"
    if message.ack_policy == AckPolicy.STRONG:
        message_for_error = "Strong-ACK deliveries must be acknowledged before resolve."
    else:
        message_for_error = "Light-ACK deliveries must be delivered before resolve."
    _ensure(transition_allowed(operation, delivery.status), message_for_error)
    return replace(delivery, status=_target(MailboxDeliveryStatus, operation), resolved_at=now, updated_at=now)


def handle_delivery_timeout(
    delivery: MailboxDelivery,
    *,
    now: datetime,
    retry_delay: timedelta,
) -> MailboxDelivery:
    if delivery.expires_at is None or now < delivery.expires_at:
        return delivery
    if str(delivery.status) in status_list("delivery_timeout_terminal_statuses"):
        return delivery

    next_attempt_count = delivery.attempt_count + 1
    ttl_error_code = contract_value("delivery_timeout_error_code")
    ttl_error_message = contract_value("delivery_timeout_error_message")
    if next_attempt_count < delivery.max_attempts:
        operation = "mailbox_delivery.retry_timeout"
        _ensure(transition_allowed(operation, delivery.status), "Delivery cannot be retried from its current status.")
        next_expiry = now + timedelta(seconds=delivery.ttl_seconds or 0) if delivery.ttl_seconds is not None else None
        return replace(
            delivery,
            status=_target(MailboxDeliveryStatus, operation),
            attempt_count=next_attempt_count,
            expires_at=next_expiry,
            next_retry_at=now + retry_delay,
            last_error_code=ttl_error_code,
            last_error_message=ttl_error_message,
            updated_at=now,
        )

    operation = "mailbox_delivery.expire_timeout"
    _ensure(transition_allowed(operation, delivery.status), "Delivery cannot expire from its current status.")
    return replace(
        delivery,
        status=_target(MailboxDeliveryStatus, operation),
        attempt_count=next_attempt_count,
        next_retry_at=None,
        last_error_code=ttl_error_code,
        last_error_message=ttl_error_message,
        updated_at=now,
    )


def open_interrupt(interrupt: Interrupt, node: TaskNode, *, now: datetime) -> tuple[Interrupt, TaskNode]:
    operation = "node.open_interrupt"
    _ensure(transition_allowed(operation, node.status), "Node cannot enter waiting_for_input from its current status.")
    return (
        replace(interrupt, status=InterruptStatus.OPEN, created_at=interrupt.created_at or now),
        replace(node, status=_target(NodeStatus, operation)),
    )


def answer_interrupt(
    interrupt: Interrupt,
    answer: InterruptAnswer,
    node: TaskNode,
    *,
    now: datetime,
) -> tuple[Interrupt, InterruptAnswer, TaskNode]:
    _ensure(transition_allowed("interrupt.answer", interrupt.status), "Only open interrupts can be answered.")
    _ensure(
        transition_allowed("node.answer_interrupt", node.status),
        "Node must be waiting_for_input to accept an interrupt answer.",
    )
    normalized_answer = replace(
        answer,
        accepted=True,
        accepted_at=answer.accepted_at or now,
    )
    return (
        replace(interrupt, status=_target(InterruptStatus, "interrupt.answer"), answered_at=normalized_answer.accepted_at),
        normalized_answer,
        replace(node, status=_target(NodeStatus, "node.answer_interrupt")),
    )


def begin_resume(node: TaskNode) -> TaskNode:
    operation = "node.begin_resume"
    _ensure(transition_allowed(operation, node.status), "Only ready_to_resume nodes can enter resuming.")
    return replace(node, status=_target(NodeStatus, operation))


def begin_task_cancellation(task: Task, *, now: datetime) -> Task:
    if is_task_cancellation_noop(task):
        return task
    operation = "task.begin_cancellation"
    _ensure(transition_allowed(operation, task.status), "Task cannot enter cancelling from its current status.")
    return replace(task, status=_target(TaskStatus, operation), cancel_requested_at=task.cancel_requested_at or now, updated_at=now)


def is_task_cancellation_noop(task: Task) -> bool:
    return str(task.status) in status_list("task_cancellation_noop_statuses")


def finalize_task_cancellation(task: Task, *, now: datetime) -> Task:
    operation = "task.finalize_cancellation"
    _ensure(transition_allowed(operation, task.status), "Task cannot be finalized as cancelled from its current status.")
    return replace(task, status=_target(TaskStatus, operation), updated_at=now)


def cancel_node(node: TaskNode) -> TaskNode:
    target = cancel_node_target(node.status)
    if target is None:
        return node
    return replace(node, status=NodeStatus(target))


def cancel_interrupt(interrupt: Interrupt, *, now: datetime) -> Interrupt:
    operation = "interrupt.cancel"
    if not transition_allowed(operation, interrupt.status):
        return interrupt
    return replace(interrupt, status=_target(InterruptStatus, operation), cancelled_at=now)


def cancel_mailbox_delivery(delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    operation = "mailbox_delivery.cancel"
    if not transition_allowed(operation, delivery.status):
        return delivery
    return replace(
        delivery,
        status=_target(MailboxDeliveryStatus, operation),
        resolved_at=delivery.resolved_at or now,
        updated_at=now,
    )


def invalidate_checkpoint(checkpoint: Checkpoint, *, now: datetime) -> Checkpoint:
    if checkpoint.invalidated_at is not None:
        return checkpoint
    return replace(checkpoint, invalidated_at=now)


def can_accept_late_result(task: Task | None) -> bool:
    if task is None:
        return False
    return str(task.status) not in status_list("late_result_rejected_task_statuses")
