# 完成总结 - 需求上传功能修复

## 问题描述
用户报告需求上传失败，错误信息：`ReqAgent.parse() got an unexpected keyword argument 'raw_text'`

## 根本原因
1. **参数名不匹配**: `/upload/multi` 端点调用 `req_agent.parse(raw_text=...)`，但 `ReqAgent.parse()` 的参数名是 `raw_requirement`
2. **缺少 DB 持久化**: `/upload/multi` 只调用 LLM 解析，没有写入数据库
3. **数据库表结构不完整**: `requirements` 表缺少 `supersedes` 等字段

## 修复内容

### 1. 后端代码修复
- `backend/app/api/requirements.py`:
  - 修正 `req_agent.parse()` 调用参数（`raw_text` → `raw_requirement`）
  - 添加完整的 DB 持久化逻辑（创建 Requirement 对象 + session.commit）
  - 扩展文件格式支持（新增 `.txt`）

### 2. 数据库迁移
- `backend/app/db/database.py`:
  - 在 `SCHEMA_MIGRATIONS` 中添加 `requirements` 表的缺失字段迁移脚本
  - 包含: `superseded_by`, `change_type`, `change_summary`, `diff_json`, `project_id`, `supersedes`

### 3. 前端功能增强
- `frontend/src/pages/Requirements.tsx`:
  - 单文件上传 → 多文件上传（支持选择多个文件）
  - 新增 `.txt` 格式支持
  - 移除手动文本输入框
  - 新增已选文件列表（可逐个移除）
  
- `frontend/src/api/client.ts`:
  - 新增 `requirements.uploadMultiFiles()` 方法

### 4. Docker 部署修复
- 修复 `testcases.py` 参数顺序错误（导致 backend 启动失败）
- 修复 `frontend/Dockerfile` healthcheck（`localhost` → `127.0.0.1`）

## 测试验证

### 单文件上传测试
```bash
curl -X POST http://localhost:8000/api/requirements/upload/file \
  -H "Authorization: Bearer <token>" \
  -F "file=@test_req_full.md" \
  -F "product_id=PROD-DEFAULT"
```
**结果**: ✅ 成功，返回 `req_id="REQ-2026-002"`

### 多文件上传测试
```bash
curl -X POST http://localhost:8000/api/requirements/upload/multi \
  -H "Authorization: Bearer <token>" \
  -F "files=@test_req_full.md" \
  -F "files=@test_req_2.md" \
  -F "product_id=PROD-DEFAULT"
```
**结果**: ✅ 成功，返回 `req_id="REQ-2026-003"`，两个文件内容被合并分析

## 当前状态

| 服务 | 状态 | 端口 |
|------|------|------|
| ai-test-gen-api | healthy ✅ | 8000 |
| ai-test-gen-web | healthy ✅ | 80 |

## 使用说明

1. **单文件上传**: 在"需求管理"页面点击"上传需求"，选择一个 .md/.docx/.txt 文件
2. **多文件上传**: 在"上传需求"弹窗中，选择多个文件（支持 .md/.docx/.txt）
3. **移除文件**: 在已选文件列表中，点击文件右侧的 ✕ 按钮
4. **上传进度**: 上传后会显示实时进度（正在调用 AI 解析...）
5. **完成后跳转**: 解析完成后自动跳转到需求详情页

## 注意事项

1. **文档内容要求**: 上传的文档需要包含足够详细的需求描述，否则 LLM 可能无法提取完整的结构化数据
2. **Token 过期**: JWT token 有效期为 8 小时，过期后需要重新登录
3. **文件大小限制**: 单个文件最大 10MB，总大小不超过 50MB
