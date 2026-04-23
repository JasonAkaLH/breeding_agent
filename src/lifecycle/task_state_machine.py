from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timedelta

from src.core.enums import AckPolicy, InterruptStatus, MailboxDeliveryStatus, NodeStatus, TaskStatus
from src.core.models import Checkpoint, Interrupt, InterruptAnswer, MailboxDelivery, MailboxMessage, Task, TaskNode

from .errors import LifecycleTransitionError


def _ensure(condition: bool, message: str) -> None:
    if not condition:
        raise LifecycleTransitionError(message)


def mark_delivery_delivered(delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    _ensure(delivery.status == MailboxDeliveryStatus.PENDING, "Only pending deliveries can be marked delivered.")
    return replace(delivery, status=MailboxDeliveryStatus.DELIVERED, delivered_at=now, updated_at=now)


def acknowledge_delivery(message: MailboxMessage, delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    _ensure(message.ack_policy == AckPolicy.STRONG, "Only strong-ACK messages can be explicitly acknowledged.")
    _ensure(delivery.status == MailboxDeliveryStatus.DELIVERED, "Only delivered messages can be acknowledged.")
    return replace(delivery, status=MailboxDeliveryStatus.ACKNOWLEDGED, acknowledged_at=now, updated_at=now)


def resolve_delivery(message: MailboxMessage, delivery: MailboxDelivery, *, now: datetime) -> MailboxDelivery:
    if message.ack_policy == AckPolicy.STRONG:
        _ensure(
            delivery.status == MailboxDeliveryStatus.ACKNOWLEDGED,
            "Strong-ACK deliveries must be acknowledged before resolve.",
        )
    else:
        _ensure(
            delivery.status in {MailboxDeliveryStatus.DELIVERED, MailboxDeliveryStatus.ACKNOWLEDGED},
            "Light-ACK deliveries must be delivered before resolve.",
        )
    return replace(delivery, status=MailboxDeliveryStatus.RESOLVED, resolved_at=now, updated_at=now)


def handle_delivery_timeout(
    delivery: MailboxDelivery,
    *,
    now: datetime,
    retry_delay: timedelta,
) -> MailboxDelivery:
    if delivery.expires_at is None or now < delivery.expires_at:
        return delivery
    if delivery.status in {MailboxDeliveryStatus.RESOLVED, MailboxDeliveryStatus.CANCELLED, MailboxDeliveryStatus.EXPIRED}:
        return delivery

    next_attempt_count = delivery.attempt_count + 1
    ttl_error_code = "ttl_expired"
    ttl_error_message = "delivery exceeded ttl window"
    if next_attempt_count < delivery.max_attempts:
        next_expiry = now + timedelta(seconds=delivery.ttl_seconds or 0) if delivery.ttl_seconds is not None else None
        return replace(
            delivery,
            status=MailboxDeliveryStatus.PENDING,
            attempt_count=next_attempt_count,
            expires_at=next_expiry,
            next_retry_at=now + retry_delay,
            last_error_code=ttl_error_code,
            last_error_message=ttl_error_message,
            updated_at=now,
        )

    return replace(
        delivery,
        status=MailboxDeliveryStatus.EXPIRED,
        attempt_count=next_attempt_count,
        next_retry_at=None,
        last_error_code=ttl_error_code,
        last_error_message=ttl_error_message,
        updated_at=now,
    )


def open_interrupt(interrupt: Interrupt, node: TaskNode, *, now: datetime) -> tuple[Interrupt, TaskNode]:
    _ensure(
        node.status in {NodeStatus.PENDING, NodeStatus.READY, NodeStatus.RUNNING, NodeStatus.WAITING_FOR_DEPENDENCY},
        "Node cannot enter waiting_for_input from its current status.",
    )
    return (
        replace(interrupt, status=InterruptStatus.OPEN, created_at=interrupt.created_at or now),
        replace(node, status=NodeStatus.WAITING_FOR_INPUT),
    )


def answer_interrupt(
    interrupt: Interrupt,
    answer: InterruptAnswer,
    node: TaskNode,
    *,
    now: datetime,
) -> tuple[Interrupt, InterruptAnswer, TaskNode]:
    _ensure(interrupt.status == InterruptStatus.OPEN, "Only open interrupts can be answered.")
    _ensure(node.status == NodeStatus.WAITING_FOR_INPUT, "Node must be waiting_for_input to accept an interrupt answer.")
    normalized_answer = replace(
        answer,
        accepted=True,
        accepted_at=answer.accepted_at or now,
    )
    return (
        replace(interrupt, status=InterruptStatus.ANSWERED, answered_at=normalized_answer.accepted_at),
        normalized_answer,
        replace(node, status=NodeStatus.READY_TO_RESUME),
    )


def begin_resume(node: TaskNode) -> TaskNode:
    _ensure(node.status == NodeStatus.READY_TO_RESUME, "Only ready_to_resume nodes can enter resuming.")
    return replace(node, status=NodeStatus.RESUMING)


def begin_task_cancellation(task: Task, *, now: datetime) -> Task:
    if task.status in {TaskStatus.CANCELLED, TaskStatus.COMPLETED, TaskStatus.FAILED}:
        return task
    return replace(task, status=TaskStatus.CANCELLING, cancel_requested_at=task.cancel_requested_at or now, updated_at=now)


def finalize_task_cancellation(task: Task, *, now: datetime) -> Task:
    if task.status == TaskStatus.CANCELLED:
        return task
    return replace(task, status=TaskStatus.CANCELLED, updated_at=now)


def cancel_node(node: TaskNode) -> TaskNode:
    if node.status in {NodeStatus.PENDING, NodeStatus.READY}:
        return replace(node, status=NodeStatus.BLOCKED_BY_CANCELLATION)
    if node.status in {
        NodeStatus.RUNNING,
        NodeStatus.WAITING_FOR_DEPENDENCY,
        NodeStatus.WAITING_FOR_INPUT,
        NodeStatus.READY_TO_RESUME,
        NodeStatus.RESUMING,
        NodeStatus.CANCELLING,
    }:
        return replace(node, status=NodeStatus.CANCELLED)
    return node


def cancel_interrupt(interrupt: Interrupt, *, now: datetime) -> Interrupt:
    if interrupt.status in {InterruptStatus.CANCELLED, InterruptStatus.EXPIRED}:
        return interrupt
    return replace(interrupt, status=InterruptStatus.CANCELLED, cancelled_at=now)


def invalidate_checkpoint(checkpoint: Checkpoint, *, now: datetime) -> Checkpoint:
    if checkpoint.invalidated_at is not None:
        return checkpoint
    return replace(checkpoint, invalidated_at=now)


def can_accept_late_result(task: Task | None) -> bool:
    if task is None:
        return False
    return task.status not in {TaskStatus.CANCELLING, TaskStatus.CANCELLED}
