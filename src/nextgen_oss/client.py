"""
统一对象存储客户端

基于 boto3 S3 兼容协议，通过 endpoint_url 切换不同云厂商。
腾讯云 COS / 阿里云 OSS / AWS S3 / MinIO 等均可使用同一套接口。
"""

import os
import base64
import hashlib
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed
from typing import Any, BinaryIO, Callable, Dict, List, Optional, Tuple, Union

import boto3
from botocore.config import Config as BotoConfig

from nextgen_oss.providers import get_provider, ProviderConfig
from nextgen_oss.credentials import TemporaryCredentials

logger = logging.getLogger("nextgen_oss")


class StorageClient:
    """
    统一对象存储客户端

    基于 boto3 S3 兼容接口，所有云厂商使用同一套方法：
      - upload_file / upload_bytes / download_file
      - head_object / list_objects / delete_object
      - put_object_acl / generate_presigned_url

    示例:
        client = StorageClient(
            provider="cos",
            region="ap-guangzhou",
            access_key="...",
            secret_key="...",
        )
        client.upload_file("my-bucket", "path/to/key.png", "/local/file.png")
    """

    def __init__(
        self,
        provider: str = "cos",
        region: str = "",
        access_key: str = "",
        secret_key: str = "",
        session_token: Optional[str] = None,
        endpoint_url: Optional[str] = None,
        bucket: Optional[str] = None,
        provider_config: Optional[ProviderConfig] = None,
        **boto_kwargs,
    ):
        """
        :param provider: 厂商标识 ("cos", "oss", "s3", "minio", "cos-internal")
        :param region: 地域
        :param access_key: AK
        :param secret_key: SK
        :param session_token: 临时凭证 token（可选）
        :param endpoint_url: 自定义 endpoint（优先级高于 provider 预设）
        :param bucket: 默认 bucket（可在每次调用时覆盖）
        :param provider_config: 自定义 ProviderConfig（优先级高于 provider 查表）
        :param boto_kwargs: 传递给 boto3.client 的额外参数
        """
        self._provider_key = provider
        self._region = region
        self._default_bucket = bucket or ""

        # 解析厂商配置
        if provider_config:
            self._provider = provider_config
        else:
            self._provider = get_provider(provider)

        # 确定 endpoint
        if endpoint_url:
            effective_endpoint = endpoint_url
        elif self._provider.endpoint_template:
            effective_endpoint = self._provider.get_endpoint(region)
        else:
            effective_endpoint = None  # AWS S3 原生

        # 构建 boto3 s3 client
        s3_config = {"addressing_style": self._provider.addressing_style}
        extra_boto_config: Dict[str, Any] = {}

        if "oss" in self._provider_key:
            # 阿里云 OSS 不支持 aws-chunked Transfer-Encoding。
            # botocore >= 1.36 默认开启 flexible checksums，会自动使用
            # aws-chunked 编码 + STREAMING-AWS4-HMAC-SHA256-PAYLOAD，
            # 必须显式禁用：
            s3_config["payload_signing_enabled"] = False
            extra_boto_config["request_checksum_calculation"] = "when_required"
            extra_boto_config["response_checksum_validation"] = "when_required"

        boto_config = BotoConfig(
            s3=s3_config,
            signature_version=self._provider.signature_version,
            retries={"max_attempts": 3, "mode": "standard"},
            **extra_boto_config,
        )

        client_kwargs = {
            "service_name": "s3",
            "region_name": region if provider != "minio" else None,
            "aws_access_key_id": access_key,
            "aws_secret_access_key": secret_key,
            "config": boto_config,
        }
        if session_token:
            client_kwargs["aws_session_token"] = session_token
        if effective_endpoint:
            client_kwargs["endpoint_url"] = effective_endpoint
        client_kwargs.update(boto_kwargs)

        self._s3 = boto3.client(**client_kwargs)
        self._endpoint_url = effective_endpoint

        logger.debug(
            f"StorageClient initialized: provider={provider}, "
            f"region={region}, endpoint={effective_endpoint}, "
            f"bucket={self._default_bucket}"
        )

    @property
    def s3(self):
        """暴露底层 boto3 s3 client，方便高级用法"""
        return self._s3

    @property
    def default_bucket(self) -> str:
        return self._default_bucket

    def _resolve_bucket(self, bucket: Optional[str]) -> str:
        b = bucket or self._default_bucket
        if not b:
            raise ValueError("bucket is required (pass it or set default_bucket)")
        return b

    # ======================== 上传 ========================

    def _do_upload_file(
        self,
        bucket: str,
        key: str,
        local_path: str,
        metadata: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        """
        内部统一上传方法，返回服务端响应（put_object）或空字典（upload_file）。

        - 阿里云 OSS：使用 put_object（底层 API），完全避免 S3Transfer
          的 chunked encoding 导致 InvalidArgument 错误。
          同时附带 Content-MD5 头，由服务端校验上传数据完整性。
        - 其他厂商：使用 upload_file（S3Transfer 高级 API），支持自动分片大文件。

        :return: put_object 的响应（包含 ETag 等），或空字典
        """
        extra_args: Dict[str, Any] = {}
        if metadata:
            extra_args["Metadata"] = metadata

        if "oss" in self._provider_key:
            # OSS: 直接用 put_object，读取文件内容一次性上传
            # 附带 Content-MD5 头，让服务端校验数据完整性（Base64 编码的 MD5）
            content_md5 = self._compute_content_md5(local_path)
            kwargs: Dict[str, Any] = {
                "Bucket": bucket,
                "Key": key,
                "ContentMD5": content_md5,
            }
            if metadata:
                kwargs["Metadata"] = metadata
            with open(local_path, "rb") as f:
                # 必须用 f.read() 一次性读入 bytes，而非传文件对象。
                # 若传文件对象（流），boto3 在 S3v4 签名下无法预计算
                # content-sha256，会自动使用 aws-chunked Transfer-Encoding，
                # 而 OSS 不支持该编码，导致 InvalidArgument 错误。
                body = f.read()
                kwargs["Body"] = body
                kwargs["ContentLength"] = len(body)
                response = self._s3.put_object(**kwargs)
            return response
        else:
            # COS / S3 / MinIO: 使用 S3Transfer，支持大文件自动分片
            self._s3.upload_file(
                Filename=local_path,
                Bucket=bucket,
                Key=key,
                ExtraArgs=extra_args or None,
            )
            return {}

    def upload_file(
        self,
        bucket: Optional[str],
        key: str,
        local_path: str,
        metadata: Optional[Dict[str, str]] = None,
        max_size: Optional[int] = None,
    ) -> str:
        """
        上传本地文件

        :param bucket: 桶名（None 则使用 default_bucket）
        :param key: 对象 key
        :param local_path: 本地文件路径
        :param metadata: 自定义元数据
        :param max_size: 文件大小上限（字节），超过则抛 ValueError
        :return: 对象 key
        """
        bucket = self._resolve_bucket(bucket)
        local_path = os.path.abspath(local_path)

        if not os.path.isfile(local_path):
            raise FileNotFoundError(f"File not found: {local_path}")

        if max_size:
            size = os.path.getsize(local_path)
            if size > max_size:
                raise ValueError(
                    f"File size {size / 1024 / 1024:.1f}MB exceeds limit "
                    f"{max_size / 1024 / 1024:.1f}MB: {local_path}"
                )

        logger.info(f"Uploading {local_path} -> s3://{bucket}/{key}")
        self._do_upload_file(bucket, key, local_path, metadata=metadata)
        logger.info(f"Upload done: {key}")
        return key

    def upload_bytes(
        self,
        bucket: Optional[str],
        key: str,
        data: Union[bytes, BinaryIO],
        metadata: Optional[Dict[str, str]] = None,
        content_type: Optional[str] = None,
    ) -> str:
        """
        上传 bytes 或 file-like 对象

        :return: 对象 key
        """
        bucket = self._resolve_bucket(bucket)
        kwargs: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": key,
            "Body": data,
        }
        if metadata:
            kwargs["Metadata"] = metadata
        if content_type:
            kwargs["ContentType"] = content_type

        logger.info(f"Uploading bytes -> s3://{bucket}/{key}")
        self._s3.put_object(**kwargs)
        logger.info(f"Upload done: {key}")
        return key

    def upload_file_with_dedup(
        self,
        bucket: Optional[str],
        key: str,
        local_path: str,
        metadata: Optional[Dict[str, str]] = None,
        retries: int = 3,
    ) -> bool:
        """
        带 MD5 去重的上传（适用于 Worker 等需要断点续传的场景）

        如果远端已存在相同 MD5 的文件则跳过上传。
        同时写入 ``md5`` 和 ``x-cos-meta-md5`` 两种元数据键，确保新旧代码都能识别。

        :return: True=上传成功或已存在, False=失败
        """
        bucket = self._resolve_bucket(bucket)
        local_path = os.path.abspath(local_path)
        md5 = self._file_md5(local_path)

        # 检查远端是否已存在
        try:
            head = self._s3.head_object(Bucket=bucket, Key=key)
            remote_md5 = self._extract_remote_md5(head)
            if remote_md5 == md5:
                logger.info(f"File already exists with same MD5, skip: {key}")
                return True
        except self._s3.exceptions.ClientError as e:
            if e.response["Error"]["Code"] != "404":
                logger.warning(f"head_object error: {e}")

        # 带重试上传，同时写入新旧两种 MD5 元数据键
        meta = {"md5": md5, "x-cos-meta-md5": md5}
        if metadata:
            meta.update(metadata)

        for attempt in range(1, retries + 1):
            try:
                response = self._do_upload_file(
                    bucket=bucket,
                    key=key,
                    local_path=local_path,
                    metadata=meta,
                )

                # 上传后二次校验：用 ETag 验证远端文件完整性
                # AWS S3 / COS 对非分片上传，ETag 就是文件内容的 MD5（带引号包裹）
                # 注意：阿里云 OSS 的 ETag 不等于标准 MD5，不能用于校验
                # （OSS 已通过 Content-MD5 头做了服务端校验，完整性有保障）
                if response and "oss" not in self._provider_key:
                    etag = response.get("ETag", "").strip('"')
                    if etag and etag.lower() != md5.lower():
                        logger.warning(
                            f"ETag mismatch after upload (attempt {attempt}): "
                            f"local_md5={md5}, etag={etag}, key={key}"
                        )
                        if attempt < retries:
                            continue  # 重试
                        raise RuntimeError(
                            f"Upload integrity check failed: ETag {etag} != MD5 {md5}"
                        )

                logger.info(f"Upload success (attempt {attempt}): {key}")
                return True
            except RuntimeError:
                raise  # 完整性校验失败，直接抛出
            except Exception as e:
                logger.error(f"Upload attempt {attempt}/{retries} failed: {e}")
                if attempt == retries:
                    raise
        return False

    # ======================== 下载 ========================

    def download_file(
        self,
        bucket: Optional[str],
        key: str,
        local_path: str,
    ) -> str:
        """
        下载文件到本地

        :return: 本地文件路径
        """
        bucket = self._resolve_bucket(bucket)
        local_path = os.path.abspath(local_path)
        os.makedirs(os.path.dirname(local_path), exist_ok=True)

        logger.info(f"Downloading s3://{bucket}/{key} -> {local_path}")
        self._s3.download_file(
            Bucket=bucket,
            Key=key,
            Filename=local_path,
        )
        logger.info(f"Download done: {local_path}")
        return local_path

    def download_file_with_dedup(
        self,
        bucket: Optional[str],
        key: str,
        local_path: str,
    ) -> str:
        """带 MD5 去重的下载，本地已存在相同文件则跳过（兼容新旧 MD5 元数据格式）"""
        bucket = self._resolve_bucket(bucket)
        local_path = os.path.abspath(local_path)

        if os.path.isfile(local_path):
            local_md5 = self._file_md5(local_path)
            try:
                head = self._s3.head_object(Bucket=bucket, Key=key)
                remote_md5 = self._extract_remote_md5(head)
                if remote_md5 and remote_md5 == local_md5:
                    logger.info(f"Local file matches remote MD5, skip download: {key}")
                    return local_path
            except Exception:
                pass

        return self.download_file(bucket, key, local_path)

    # ======================== 对象操作 ========================

    def head_object(self, bucket: Optional[str], key: str) -> Dict[str, Any]:
        """获取对象元信息"""
        bucket = self._resolve_bucket(bucket)
        return self._s3.head_object(Bucket=bucket, Key=key)

    def delete_object(self, bucket: Optional[str], key: str):
        """删除对象"""
        bucket = self._resolve_bucket(bucket)
        self._s3.delete_object(Bucket=bucket, Key=key)

    def list_objects(
        self,
        bucket: Optional[str],
        prefix: str = "",
        max_keys: int = 1000,
    ) -> List[Dict[str, Any]]:
        """
        列出前缀下的所有对象（自动翻页）

        :return: 对象信息列表 [{"Key": ..., "Size": ..., ...}, ...]
        """
        bucket = self._resolve_bucket(bucket)
        all_objects = []
        continuation_token = None

        while True:
            kwargs = {
                "Bucket": bucket,
                "Prefix": prefix,
                "MaxKeys": max_keys,
            }
            if continuation_token:
                kwargs["ContinuationToken"] = continuation_token

            response = self._s3.list_objects_v2(**kwargs)
            contents = response.get("Contents", [])
            all_objects.extend(contents)

            if response.get("IsTruncated"):
                continuation_token = response.get("NextContinuationToken")
            else:
                break

        return all_objects

    def put_object_acl(
        self,
        bucket: Optional[str],
        key: str,
        acl: str = "public-read",
    ):
        """
        设置对象 ACL

        :param acl: "private", "public-read", "public-read-write" 等
        """
        bucket = self._resolve_bucket(bucket)
        self._s3.put_object_acl(Bucket=bucket, Key=key, ACL=acl)

    def copy_object(
        self,
        bucket: Optional[str],
        src_key: str,
        dst_key: str,
        src_bucket: Optional[str] = None,
        metadata: Optional[Dict[str, str]] = None,
    ) -> str:
        """
        复制对象（同桶或跨桶）

        :param bucket: 目标桶（None 则使用 default_bucket）
        :param src_key: 源对象 key
        :param dst_key: 目标对象 key
        :param src_bucket: 源桶（None 则与 bucket 相同）
        :param metadata: 新的元数据（为 None 时保留源对象元数据）
        :return: 目标对象 key
        """
        bucket = self._resolve_bucket(bucket)
        src_bucket = src_bucket or bucket
        copy_source = {"Bucket": src_bucket, "Key": src_key}

        kwargs: Dict[str, Any] = {
            "Bucket": bucket,
            "Key": dst_key,
            "CopySource": copy_source,
        }
        if metadata is not None:
            kwargs["Metadata"] = metadata
            kwargs["MetadataDirective"] = "REPLACE"

        logger.info(f"Copying s3://{src_bucket}/{src_key} -> s3://{bucket}/{dst_key}")
        self._s3.copy_object(**kwargs)
        logger.info(f"Copy done: {dst_key}")
        return dst_key

    def batch_download(
        self,
        bucket: Optional[str],
        file_map: Dict[str, str],
        max_workers: int = 4,
        skip_existing: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        并发批量下载文件

        :param bucket: 桶名
        :param file_map: {远端 key: 本地路径} 的映射
        :param max_workers: 并发线程数
        :param skip_existing: 是否跳过本地已存在且 MD5 匹配的文件
        :return: 每个文件的下载结果列表 [{"key": ..., "local_path": ..., "status": "ok"/"skipped"/"error", "error": ...}]
        """
        bucket = self._resolve_bucket(bucket)
        results: List[Dict[str, Any]] = []

        def _download_one(key: str, local_path: str) -> Dict[str, Any]:
            local_path = os.path.abspath(local_path)
            try:
                if skip_existing and os.path.isfile(local_path):
                    local_md5 = self._file_md5(local_path)
                    try:
                        head = self._s3.head_object(Bucket=bucket, Key=key)
                        remote_md5 = (head.get("Metadata") or {}).get("md5", "")
                        # 兼容旧格式 x-cos-meta-md5
                        if not remote_md5:
                            remote_md5 = (head.get("Metadata") or {}).get("x-cos-meta-md5", "")
                        if remote_md5 and remote_md5 == local_md5:
                            return {"key": key, "local_path": local_path, "status": "skipped"}
                    except Exception:
                        pass

                os.makedirs(os.path.dirname(local_path), exist_ok=True)
                self._s3.download_file(Bucket=bucket, Key=key, Filename=local_path)
                return {"key": key, "local_path": local_path, "status": "ok"}
            except Exception as e:
                logger.error(f"Download failed {key}: {e}")
                return {"key": key, "local_path": local_path, "status": "error", "error": str(e)}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_download_one, key, local_path): key
                for key, local_path in file_map.items()
            }
            for future in as_completed(futures):
                results.append(future.result())

        ok = sum(1 for r in results if r["status"] == "ok")
        skipped = sum(1 for r in results if r["status"] == "skipped")
        failed = sum(1 for r in results if r["status"] == "error")
        logger.info(f"Batch download done: {ok} ok, {skipped} skipped, {failed} failed")
        return results

    def upload_directory(
        self,
        bucket: Optional[str],
        local_dir: str,
        key_prefix: str,
        file_filter: Optional[Callable[[str], bool]] = None,
        metadata: Optional[Dict[str, str]] = None,
        dedup: bool = True,
        max_workers: int = 4,
    ) -> List[Dict[str, Any]]:
        """
        上传整个目录到远端

        :param bucket: 桶名
        :param local_dir: 本地目录路径
        :param key_prefix: 远端 key 前缀（以 / 结尾）
        :param file_filter: 可选的过滤函数，接收相对路径，返回 True 表示上传
        :param metadata: 统一附加的元数据
        :param dedup: 是否启用 MD5 去重
        :param max_workers: 并发线程数
        :return: 每个文件的上传结果列表
        """
        bucket = self._resolve_bucket(bucket)
        local_dir = os.path.abspath(local_dir)
        if not os.path.isdir(local_dir):
            raise FileNotFoundError(f"Directory not found: {local_dir}")

        # 收集文件
        file_pairs: List[Tuple[str, str]] = []  # (local_path, key)
        for root, _dirs, files in os.walk(local_dir):
            for fname in files:
                local_path = os.path.join(root, fname)
                rel_path = os.path.relpath(local_path, local_dir).replace("\\", "/")
                if file_filter and not file_filter(rel_path):
                    continue
                key = key_prefix.rstrip("/") + "/" + rel_path
                file_pairs.append((local_path, key))

        results: List[Dict[str, Any]] = []

        def _upload_one(local_path: str, key: str) -> Dict[str, Any]:
            try:
                if dedup:
                    self.upload_file_with_dedup(bucket, key, local_path, metadata=metadata)
                else:
                    self.upload_file(bucket, key, local_path, metadata=metadata)
                return {"key": key, "local_path": local_path, "status": "ok"}
            except Exception as e:
                logger.error(f"Upload failed {local_path}: {e}")
                return {"key": key, "local_path": local_path, "status": "error", "error": str(e)}

        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {
                pool.submit(_upload_one, lp, k): k
                for lp, k in file_pairs
            }
            for future in as_completed(futures):
                results.append(future.result())

        ok = sum(1 for r in results if r["status"] == "ok")
        failed = sum(1 for r in results if r["status"] == "error")
        logger.info(f"Upload directory done: {ok} ok, {failed} failed (total {len(file_pairs)} files)")
        return results

    def download_directory(
        self,
        bucket: Optional[str],
        prefix: str,
        local_dir: str,
        strip_prefix: Optional[str] = None,
        max_workers: int = 4,
        skip_existing: bool = True,
    ) -> List[Dict[str, Any]]:
        """
        下载整个远端前缀目录到本地

        :param bucket: 桶名
        :param prefix: 远端 key 前缀
        :param local_dir: 本地目录路径
        :param strip_prefix: 从 key 中去掉的前缀（默认等于 prefix），用于控制本地目录结构
        :param max_workers: 并发线程数
        :param skip_existing: 是否跳过本地已存在且 MD5 匹配的文件
        :return: 每个文件的下载结果列表
        """
        bucket = self._resolve_bucket(bucket)
        if strip_prefix is None:
            strip_prefix = prefix

        # 列出所有对象
        objects = self.list_objects(bucket, prefix=prefix)
        file_map = {}
        for obj in objects:
            key = obj["Key"]
            if key.endswith("/"):
                continue  # 跳过目录标记
            rel_path = key
            if strip_prefix and key.startswith(strip_prefix):
                rel_path = key[len(strip_prefix):].lstrip("/")
            local_path = os.path.join(local_dir, rel_path)
            file_map[key] = local_path

        if not file_map:
            logger.info(f"No objects found under prefix: {prefix}")
            return []

        return self.batch_download(bucket, file_map, max_workers=max_workers, skip_existing=skip_existing)

    def generate_presigned_url(
        self,
        bucket: Optional[str],
        key: str,
        expires_in: int = 3600,
        http_method: str = "GET",
        content_type: Optional[str] = None,
        content_length_range: Optional[tuple] = None,
    ) -> str:
        """
        生成预签名 URL

        :param expires_in: 有效期（秒）
        :param http_method: GET (下载) 或 PUT (上传)
        :param content_type: PUT 时限定 Content-Type（如 "image/png"）
        :param content_length_range: PUT 时无实际强制作用（S3 presigned URL 不支持
               服务端 content-length 限制，需在业务层校验），仅作为文档参数预留
        :return: 预签名 URL 字符串
        """
        bucket = self._resolve_bucket(bucket)
        client_method = "get_object" if http_method.upper() == "GET" else "put_object"
        params: Dict[str, Any] = {"Bucket": bucket, "Key": key}
        if content_type and http_method.upper() == "PUT":
            params["ContentType"] = content_type
        return self._s3.generate_presigned_url(
            ClientMethod=client_method,
            Params=params,
            ExpiresIn=expires_in,
        )

    # ======================== 工具方法 ========================

    @staticmethod
    def _file_md5(filepath: str) -> str:
        """计算文件 MD5（返回十六进制字符串）"""
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return h.hexdigest()

    @staticmethod
    def _compute_content_md5(filepath: str) -> str:
        """
        计算文件的 Content-MD5（Base64 编码的二进制 MD5）。

        用于 put_object 时附带 Content-MD5 头，让 OSS/S3 服务端
        在接收完数据后校验完整性。如果数据在传输中损坏，服务端会
        返回 BadDigest 错误，从根源杜绝存储损坏文件。
        """
        h = hashlib.md5()
        with open(filepath, "rb") as f:
            while chunk := f.read(8192):
                h.update(chunk)
        return base64.b64encode(h.digest()).decode("utf-8")

    @staticmethod
    def _extract_remote_md5(head_response: Dict[str, Any]) -> str:
        """
        从 head_object 响应中提取 MD5（兼容新旧格式）

        优先级：Metadata["md5"] > Metadata["x-cos-meta-md5"]
        """
        meta = head_response.get("Metadata") or {}
        return meta.get("md5", "") or meta.get("x-cos-meta-md5", "")

    @classmethod
    def from_credentials(
        cls,
        credentials: TemporaryCredentials,
        provider: str = "cos",
        bucket: Optional[str] = None,
        **kwargs,
    ) -> "StorageClient":
        """
        从 TemporaryCredentials 构建客户端（用于 STS 临时凭证场景）
        """
        return cls(
            provider=provider,
            region=credentials.region,
            access_key=credentials.access_key,
            secret_key=credentials.secret_key,
            session_token=credentials.session_token,
            bucket=bucket or credentials.bucket,
            **kwargs,
        )

    @classmethod
    def from_env(
        cls,
        provider: Optional[str] = None,
        prefix: str = "NEXTGEN_OSS",
        **kwargs,
    ) -> "StorageClient":
        """
        从环境变量构建客户端

        支持两种环境变量格式：
        1. 标准格式（prefix=NEXTGEN_OSS）：
           NEXTGEN_OSS_PROVIDER, NEXTGEN_OSS_REGION, NEXTGEN_OSS_ACCESS_KEY,
           NEXTGEN_OSS_SECRET_KEY, NEXTGEN_OSS_SESSION_TOKEN, NEXTGEN_OSS_BUCKET
        2. 旧版 COS 格式（prefix=cos）：
           cos_secretId, cos_secretKey, cos_sessionToken, cos_region, cos_bucket

        :param provider: 厂商标识，None 时从环境变量读取
        :param prefix: 环境变量前缀
        :return: StorageClient 实例
        """
        if prefix.lower() == "cos":
            # 兼容旧版 CosNodes 环境变量
            return cls(
                provider=provider or os.environ.get("cos_provider", "cos-internal"),
                region=os.environ.get("cos_region", "ap-guangzhou"),
                access_key=os.environ.get("cos_secretId", ""),
                secret_key=os.environ.get("cos_secretKey", ""),
                session_token=os.environ.get("cos_sessionToken"),
                bucket=os.environ.get("cos_bucket", ""),
                **kwargs,
            )
        else:
            return cls(
                provider=provider or os.environ.get(f"{prefix}_PROVIDER", "cos"),
                region=os.environ.get(f"{prefix}_REGION", ""),
                access_key=os.environ.get(f"{prefix}_ACCESS_KEY", ""),
                secret_key=os.environ.get(f"{prefix}_SECRET_KEY", ""),
                session_token=os.environ.get(f"{prefix}_SESSION_TOKEN"),
                bucket=os.environ.get(f"{prefix}_BUCKET", ""),
                **kwargs,
            )


def create_client(
    provider: str = "cos",
    region: str = "",
    access_key: str = "",
    secret_key: str = "",
    session_token: Optional[str] = None,
    bucket: Optional[str] = None,
    **kwargs,
) -> StorageClient:
    """
    工厂函数：快速创建 StorageClient

    示例:
        # 腾讯云 COS
        client = create_client("cos", region="ap-guangzhou",
                               access_key="AK...", secret_key="SK...",
                               bucket="my-bucket-1258344700")

        # 阿里云 OSS
        client = create_client("oss", region="cn-hangzhou",
                               access_key="AK...", secret_key="SK...")

        # AWS S3
        client = create_client("s3", region="us-east-1",
                               access_key="AK...", secret_key="SK...")
    """
    return StorageClient(
        provider=provider,
        region=region,
        access_key=access_key,
        secret_key=secret_key,
        session_token=session_token,
        bucket=bucket,
        **kwargs,
    )
