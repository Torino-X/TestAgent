"""Task attachment schema registry for PHASE-3 binding."""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class AttachmentRoleSchema:
    name: str
    min_count: int = 0
    max_count: int | None = None
    compatible_extensions: tuple[str, ...] = ()

    @property
    def required(self) -> bool:
        return self.min_count > 0


@dataclass(frozen=True)
class TaskAttachmentSchema:
    task_type: str
    roles: tuple[AttachmentRoleSchema, ...]

    @property
    def required_role_names(self) -> list[str]:
        return [role.name for role in self.roles if role.required]

    def role(self, name: str) -> AttachmentRoleSchema:
        for role in self.roles:
            if role.name == name:
                return role
        raise KeyError(name)


class TaskAttachmentSchemaRegistry:
    """Code-level source of truth for task attachment requirements."""

    _DOCUMENT_EXTENSIONS = ("docx", "txt", "md", "json")

    def __init__(self) -> None:
        self._schemas = {
            "test_plan_generation": TaskAttachmentSchema(
                task_type="test_plan_generation",
                roles=(
                    AttachmentRoleSchema(
                        "requirement_source",
                        min_count=1,
                        max_count=None,
                        compatible_extensions=self._DOCUMENT_EXTENSIONS,
                    ),
                    AttachmentRoleSchema(
                        "output_template",
                        min_count=1,
                        max_count=1,
                        compatible_extensions=("docx",),
                    ),
                    AttachmentRoleSchema(
                        "reference_material",
                        min_count=0,
                        max_count=None,
                        compatible_extensions=self._DOCUMENT_EXTENSIONS,
                    ),
                ),
            ),
            "normal_chat": TaskAttachmentSchema(task_type="normal_chat", roles=()),
        }

    def for_task_type(self, task_type: str) -> TaskAttachmentSchema:
        return self._schemas.get(task_type, self._schemas["normal_chat"])

    def is_compatible(self, role_name: str, extension: str) -> bool:
        normalized = (extension or "").lower().lstrip(".")
        for schema in self._schemas.values():
            for role in schema.roles:
                if role.name == role_name:
                    return normalized in role.compatible_extensions
        return False



# ════════════════════════════════════════════════════════════════════════════════
# 模块链路位置 (任务附件类型 Schema 注册表):
#
#   链路:
#     TestPlan 任务触发时,task_attachment_resolver 按 task_type + capability
#       → 在本 registry 查"该 task_type 接受哪些 file_type"
#       → 例如:test_plan_generation 接受 (.docx, .md, .pdf),不包括 (.xlsx)
#     resolver 用此注册表在解析阶段拦截非法类型。
#
# 关键约束(供开发者速查):
#   - task_type 与 file_type 的允许关系在这里集中维护;
#   - 新增 task_type 必须在 registry 显式登记,否则默认拒绝;
#   - 升级时不能"放宽"既有约束,只能"新加"(向后兼容)。
