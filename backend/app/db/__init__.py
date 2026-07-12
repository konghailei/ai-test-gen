"""数据库层包。

包含：
- database.py: SQLAlchemy 引擎 + Session 管理
- models.py: ORM 模型（对应 templates/knowledge-base-schema.json）
- milvus_store.py: Milvus 向量库封装（ChromaDB 已弃用）
- seed.py: 种子数据（测试模式库）
"""
