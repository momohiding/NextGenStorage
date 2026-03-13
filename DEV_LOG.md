# NextGenStorage 开发日志

## 项目概述

NextGenStorage 是统一对象存储抽象库，提供 Python（pip 包名 `nextgen-oss`）和 JavaScript（npm 包名 `nextgen-storage`）两套客户端，支持 S3 兼容协议（腾讯云 COS / 阿里云 OSS / AWS S3）。

**仓库地址**: https://github.com/momohiding/NextGenStorage  
**主分支**: `main`

---

## 2026-03-11

### Python 端 OSS 兼容性全面修复

- **BotoConfig**：对 OSS 必须设置 `payload_signing_enabled=False`、`request_checksum_calculation='when_required'`、`response_checksum_validation='when_required'`，因为 botocore >= 1.36 默认开启 flexible checksums 导致 `aws-chunked` 编码
- **上传方法**：`_do_upload_file` 对 OSS 使用 `put_object` + `f.read()` (bytes) + `ContentLength` + `ContentMD5` 头
- **唯一性校验**：上传前 Metadata MD5 去重 + 传输中 Content-MD5 服务端校验
- **ETag 处理**：OSS 的 ETag 不等于标准 MD5（大小写混合的非标准值），对 OSS 跳过 ETag 检查

### 预签名 URL 兼容修复

- 禁用 flexible checksums 以兼容阿里云 OSS 预签名 URL
- 区分上传方法以兼容阿里云 OSS（`put_object` vs `upload_file`）
- 禁用阿里云 OSS 的 payload 签名

---

## 2026-03-08

### OSS 内网端点配置

- 添加 OSS 内网端点配置支持（`internal_endpoint`）

---

## 2026-03-06

### JS 统一存储客户端

- 新增 JavaScript 客户端（`js/` 目录，npm 包名 `nextgen-storage`）
- 支持浏览器端 S3 兼容存储操作
- 用于前端页面

---

## 2026-03-04

### 初始化

- 初始化 `nextgen-oss` 统一对象存储库
- Python 客户端支持批量操作和缓存凭证
- 从 `genesis-storage` 重命名为 `nextgen-oss`

---

## 踩坑记录

### 阿里云 OSS 与 S3 协议差异

1. **ETag 非标准**：OSS 返回的 ETag 不是标准 MD5，大小写混合，不能用于上传后校验
2. **Flexible Checksums 不兼容**：botocore >= 1.36 默认开启的 `aws-chunked` 编码导致 OSS 报 `SignatureDoesNotMatch`
3. **预签名 URL**：OSS 的预签名 URL 对 payload 签名有特殊要求，必须禁用
4. **上传方法**：OSS 对 `upload_file`（multipart）和 `put_object`（单次）行为不同，小文件应使用 `put_object`
