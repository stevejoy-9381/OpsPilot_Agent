"""Backwards-compatible re-export for await_review_node."""

from agent.graph.nodes.await_review import (
    ExecutionPort,
    await_execution_node,
    await_review_node,
)

await_approval_node = await_review_node

__all__ = [
    "ExecutionPort",
    "await_approval_node",
    "await_execution_node",
    "await_review_node",
]
