"""merge_2_8r_heads

Phase 2.8R 的 3 个 migration 创建时没指定 ``down_revision`` 与既有 head 关系,
导致 alembic 出现 multiple heads:

  * a8b9c0d1e2f3 → b2_8r_b_create_execution_requests → b2_8r_e_idempotency
    → c2_8r_e_event_idemp_uniq  (head #1)
  * a8b9c0d1e2f3 → b9c0d1e2f3a4  (head #2,Phase 2.6 已有)

本 merge migration 把两条 head 合一 → 唯一 head = 2_8r_merge。
后续 2.8R 阶段必须以本 revision 为 ``down_revision``。

无 DDL,纯空操作。
"""

from typing import Sequence, Union

from alembic import op


# revision identifiers, used by Alembic.
revision: str = "2_8r_merge_heads"
down_revision: Union[str, None] = (
    "c2_8r_e_event_idemp_uniq",
    "b9c0d1e2f3a4",
)
branch_labels: Union[str, Sequence[str], None] = None
depends_on: Union[str, Sequence[str], None] = None


def upgrade() -> None:
    """Merge two alembic heads:无 schema 改动。"""
    pass


def downgrade() -> None:
    """回退:拆回两条 head,无 schema 改动。"""
    pass
