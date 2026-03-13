"""
NextGenStorage 客户端单元测试

使用 mock 模拟 boto3 S3 客户端，验证业务逻辑正确性。
不需要真实的云存储连接。
"""

import os
import hashlib
import tempfile
import threading
from unittest import mock

import pytest

from nextgen_oss.client import StorageClient
from nextgen_oss.credentials import (
    TemporaryCredentials,
    CachedSTSProvider,
    STSProvider,
)


# ======================== Fixtures ========================


@pytest.fixture
def mock_s3():
    """返回一个 mock 的 boto3 s3 client"""
    with mock.patch("nextgen_oss.client.boto3") as mock_boto3:
        mock_client = mock.MagicMock()
        mock_boto3.client.return_value = mock_client

        # 模拟 ClientError 异常类（需要有 response 属性，与 botocore 一致）
        class MockClientError(Exception):
            def __init__(self, error_response, operation_name):
                self.response = error_response
                self.operation_name = operation_name
                super().__init__(f"{operation_name}: {error_response}")

        mock_client.exceptions = mock.MagicMock()
        mock_client.exceptions.ClientError = MockClientError
        yield mock_client


@pytest.fixture
def client(mock_s3):
    """返回一个使用 mock s3 的 StorageClient"""
    c = StorageClient(
        provider="cos",
        region="ap-guangzhou",
        access_key="test-ak",
        secret_key="test-sk",
        bucket="test-bucket",
    )
    return c


@pytest.fixture
def temp_dir():
    """创建临时目录"""
    with tempfile.TemporaryDirectory() as d:
        yield d


def _make_file(path: str, content: bytes = b"hello") -> str:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path, "wb") as f:
        f.write(content)
    return path


def _md5(content: bytes) -> str:
    return hashlib.md5(content).hexdigest()


# ======================== copy_object ========================


class TestCopyObject:
    def test_copy_same_bucket(self, client, mock_s3):
        result = client.copy_object(None, "src/a.png", "dst/a.png")
        assert result == "dst/a.png"
        mock_s3.copy_object.assert_called_once_with(
            Bucket="test-bucket",
            Key="dst/a.png",
            CopySource={"Bucket": "test-bucket", "Key": "src/a.png"},
        )

    def test_copy_cross_bucket(self, client, mock_s3):
        client.copy_object(None, "src/a.png", "dst/a.png", src_bucket="other-bucket")
        call_kwargs = mock_s3.copy_object.call_args[1]
        assert call_kwargs["CopySource"]["Bucket"] == "other-bucket"

    def test_copy_with_metadata_replace(self, client, mock_s3):
        client.copy_object(None, "src/a.png", "dst/a.png", metadata={"tag": "v1"})
        call_kwargs = mock_s3.copy_object.call_args[1]
        assert call_kwargs["Metadata"] == {"tag": "v1"}
        assert call_kwargs["MetadataDirective"] == "REPLACE"


# ======================== batch_download ========================


class TestBatchDownload:
    def test_basic_download(self, client, mock_s3, temp_dir):
        file_map = {
            "remote/a.txt": os.path.join(temp_dir, "a.txt"),
            "remote/b.txt": os.path.join(temp_dir, "b.txt"),
        }
        results = client.batch_download(None, file_map, max_workers=2, skip_existing=False)
        assert len(results) == 2
        assert all(r["status"] == "ok" for r in results)
        assert mock_s3.download_file.call_count == 2

    def test_skip_existing_with_matching_md5(self, client, mock_s3, temp_dir):
        content = b"test content"
        local_path = _make_file(os.path.join(temp_dir, "exists.txt"), content)
        md5 = _md5(content)

        mock_s3.head_object.return_value = {"Metadata": {"md5": md5}}

        file_map = {"remote/exists.txt": local_path}
        results = client.batch_download(None, file_map, skip_existing=True)
        assert results[0]["status"] == "skipped"
        mock_s3.download_file.assert_not_called()

    def test_skip_existing_with_old_md5_format(self, client, mock_s3, temp_dir):
        """测试兼容旧版 x-cos-meta-md5 格式"""
        content = b"old format test"
        local_path = _make_file(os.path.join(temp_dir, "old.txt"), content)
        md5 = _md5(content)

        mock_s3.head_object.return_value = {"Metadata": {"x-cos-meta-md5": md5}}

        file_map = {"remote/old.txt": local_path}
        results = client.batch_download(None, file_map, skip_existing=True)
        assert results[0]["status"] == "skipped"

    def test_download_error_captured(self, client, mock_s3, temp_dir):
        mock_s3.download_file.side_effect = Exception("network error")
        file_map = {"remote/fail.txt": os.path.join(temp_dir, "fail.txt")}
        results = client.batch_download(None, file_map, skip_existing=False)
        assert results[0]["status"] == "error"
        assert "network error" in results[0]["error"]


# ======================== upload_directory ========================


class TestUploadDirectory:
    def test_basic_upload(self, client, mock_s3, temp_dir):
        # 创建测试文件
        _make_file(os.path.join(temp_dir, "a.txt"), b"aaa")
        _make_file(os.path.join(temp_dir, "sub", "b.txt"), b"bbb")

        # mock head_object 返回 404（文件不存在，需要上传）
        error_response = {"Error": {"Code": "404"}}
        mock_s3.head_object.side_effect = mock_s3.exceptions.ClientError(error_response, "HeadObject")

        results = client.upload_directory(None, temp_dir, "prefix/", dedup=True, max_workers=2)
        assert len(results) == 2
        assert all(r["status"] == "ok" for r in results)

    def test_with_filter(self, client, mock_s3, temp_dir):
        _make_file(os.path.join(temp_dir, "keep.txt"), b"keep")
        _make_file(os.path.join(temp_dir, "skip.log"), b"skip")

        error_response = {"Error": {"Code": "404"}}
        mock_s3.head_object.side_effect = mock_s3.exceptions.ClientError(error_response, "HeadObject")

        results = client.upload_directory(
            None, temp_dir, "prefix/",
            file_filter=lambda p: p.endswith(".txt"),
            dedup=True,
        )
        assert len(results) == 1
        assert results[0]["key"] == "prefix/keep.txt"


# ======================== download_directory ========================


class TestDownloadDirectory:
    def test_basic(self, client, mock_s3, temp_dir):
        mock_s3.list_objects_v2.return_value = {
            "Contents": [
                {"Key": "prefix/a.txt", "Size": 10},
                {"Key": "prefix/sub/b.txt", "Size": 20},
            ],
            "IsTruncated": False,
        }

        results = client.download_directory(None, "prefix/", temp_dir, skip_existing=False)
        assert len(results) == 2
        assert mock_s3.download_file.call_count == 2


# ======================== from_env ========================


class TestFromEnv:
    def test_standard_prefix(self, mock_s3):
        with mock.patch.dict(os.environ, {
            "NEXTGEN_OSS_PROVIDER": "cos",
            "NEXTGEN_OSS_REGION": "ap-shanghai",
            "NEXTGEN_OSS_ACCESS_KEY": "ak123",
            "NEXTGEN_OSS_SECRET_KEY": "sk456",
            "NEXTGEN_OSS_SESSION_TOKEN": "token789",
            "NEXTGEN_OSS_BUCKET": "my-bucket",
        }):
            c = StorageClient.from_env()
            assert c._region == "ap-shanghai"
            assert c._default_bucket == "my-bucket"

    def test_cos_legacy_prefix(self, mock_s3):
        with mock.patch.dict(os.environ, {
            "cos_secretId": "legacy-ak",
            "cos_secretKey": "legacy-sk",
            "cos_sessionToken": "legacy-token",
            "cos_region": "ap-guangzhou",
            "cos_bucket": "legacy-bucket",
        }):
            c = StorageClient.from_env(prefix="cos")
            assert c._region == "ap-guangzhou"
            assert c._default_bucket == "legacy-bucket"


# ======================== upload_file_with_dedup MD5 兼容 ========================


class TestDedupMd5Compat:
    def test_writes_both_md5_keys(self, client, mock_s3, temp_dir):
        """验证 upload_file_with_dedup 同时写入 md5 和 x-cos-meta-md5"""
        content = b"dual md5"
        filepath = _make_file(os.path.join(temp_dir, "dual.txt"), content)
        md5 = _md5(content)

        # 模拟文件不存在
        error_response = {"Error": {"Code": "404"}}
        mock_s3.head_object.side_effect = mock_s3.exceptions.ClientError(error_response, "HeadObject")

        client.upload_file_with_dedup(None, "key.txt", filepath)

        call_kwargs = mock_s3.upload_file.call_args[1]
        meta = call_kwargs["ExtraArgs"]["Metadata"]
        assert meta["md5"] == md5
        assert meta["x-cos-meta-md5"] == md5

    def test_skips_on_old_format_match(self, client, mock_s3, temp_dir):
        """验证 upload_file_with_dedup 能识别旧格式 x-cos-meta-md5"""
        content = b"old format"
        filepath = _make_file(os.path.join(temp_dir, "old.txt"), content)
        md5 = _md5(content)

        mock_s3.head_object.return_value = {"Metadata": {"x-cos-meta-md5": md5}}

        result = client.upload_file_with_dedup(None, "key.txt", filepath)
        assert result is True
        mock_s3.upload_file.assert_not_called()


# ======================== CachedSTSProvider ========================


class MockSTSProvider(STSProvider):
    """用于测试的 mock STS provider"""
    def __init__(self):
        self.call_count = 0

    def get_credential(self, actions, resources, duration_seconds=1800, **kwargs):
        import time
        self.call_count += 1
        return TemporaryCredentials(
            access_key=f"ak-{self.call_count}",
            secret_key=f"sk-{self.call_count}",
            session_token=f"token-{self.call_count}",
            expiration=time.time() + duration_seconds,
            region="ap-guangzhou",
            bucket="test-bucket",
        )


class TestCachedSTSProvider:
    def test_caches_credential(self):
        provider = MockSTSProvider()
        cached = CachedSTSProvider(provider, refresh_before=60)

        cred1 = cached.get_credential(["action"], ["resource"], 3600)
        cred2 = cached.get_credential(["action"], ["resource"], 3600)

        assert cred1.access_key == cred2.access_key
        assert provider.call_count == 1  # 只调用了一次

    def test_different_params_no_cache(self):
        provider = MockSTSProvider()
        cached = CachedSTSProvider(provider, refresh_before=60)

        cached.get_credential(["action1"], ["resource1"], 3600)
        cached.get_credential(["action2"], ["resource2"], 3600)

        assert provider.call_count == 2

    def test_invalidate_clears_cache(self):
        provider = MockSTSProvider()
        cached = CachedSTSProvider(provider, refresh_before=60)

        cached.get_credential(["action"], ["resource"], 3600)
        cached.invalidate()
        cached.get_credential(["action"], ["resource"], 3600)

        assert provider.call_count == 2

    def test_inject_env_cos_format(self):
        cred = TemporaryCredentials(
            access_key="ak-test",
            secret_key="sk-test",
            session_token="token-test",
            expiration=0,
            region="ap-guangzhou",
            bucket="test-bucket",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            CachedSTSProvider.inject_env(cred, prefix="cos")
            assert os.environ["cos_secretId"] == "ak-test"
            assert os.environ["cos_secretKey"] == "sk-test"
            assert os.environ["cos_sessionToken"] == "token-test"
            assert os.environ["cos_region"] == "ap-guangzhou"
            assert os.environ["cos_bucket"] == "test-bucket"

    def test_inject_env_standard_format(self):
        cred = TemporaryCredentials(
            access_key="ak-std",
            secret_key="sk-std",
            session_token="token-std",
            expiration=0,
            region="us-east-1",
            bucket="std-bucket",
        )
        with mock.patch.dict(os.environ, {}, clear=True):
            CachedSTSProvider.inject_env(cred, prefix="NEXTGEN_OSS")
            assert os.environ["NEXTGEN_OSS_ACCESS_KEY"] == "ak-std"
            assert os.environ["NEXTGEN_OSS_SECRET_KEY"] == "sk-std"
            assert os.environ["NEXTGEN_OSS_SESSION_TOKEN"] == "token-std"

    def test_thread_safety(self):
        """验证多线程下缓存不会重复签发"""
        provider = MockSTSProvider()
        cached = CachedSTSProvider(provider, refresh_before=60)

        results = []
        def _get():
            c = cached.get_credential(["action"], ["resource"], 3600)
            results.append(c.access_key)

        threads = [threading.Thread(target=_get) for _ in range(10)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

        # 所有线程应该拿到同一个凭证
        assert len(set(results)) == 1
        assert provider.call_count == 1


# ======================== _extract_remote_md5 ========================


class TestExtractRemoteMd5:
    def test_new_format(self):
        head = {"Metadata": {"md5": "abc123"}}
        assert StorageClient._extract_remote_md5(head) == "abc123"

    def test_old_format(self):
        head = {"Metadata": {"x-cos-meta-md5": "def456"}}
        assert StorageClient._extract_remote_md5(head) == "def456"

    def test_new_format_priority(self):
        head = {"Metadata": {"md5": "new", "x-cos-meta-md5": "old"}}
        assert StorageClient._extract_remote_md5(head) == "new"

    def test_no_metadata(self):
        head = {"Metadata": {}}
        assert StorageClient._extract_remote_md5(head) == ""

    def test_none_metadata(self):
        head = {}
        assert StorageClient._extract_remote_md5(head) == ""
