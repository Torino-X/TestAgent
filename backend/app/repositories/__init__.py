

# Repositories 子包说明:所有 ORM 仓储集中在这里,每个文件对应 models/ 下的一张或一类表。
# service 层只调 repo,**不直接 import session 或 ORM**。
# 关键约定:
#   - 所有 repo stateless(session 由调用方传);
#   - 不做业务校验(由 service / StateManager 层负责);
#   - 不与 message / task 跨 domain 联表(联表放 service)。
