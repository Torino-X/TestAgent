"""Compression（Context Preflight / Pruning / Compaction / Rehydrate）子模块。

CE-04：Compression 在 Preflight 内（select 后、compose 前），只处理
SelectedContextSet；Payload First；Anchors 从 TaskStateRef/ContextRequest 生成；
Full Replace 默认关；不创建第二套 Snapshot/Summary。
"""
# auto-appended module-level note: compression 子包: 长上下文压缩入口(轮次/token/长期记忆)。
# auto-appended module-level note: compression 子包: 长上下文压缩入口(轮次/token/长期记忆合成)。
