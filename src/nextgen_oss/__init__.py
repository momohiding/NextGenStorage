"""
nextgen-oss: 统一对象存储抽象层

支持腾讯云 COS / 阿里云 OSS / AWS S3 等 S3 兼容存储，
通过配置切换后端，业务代码无需修改。

用法:
    from nextgen_oss import StorageClient, create_client

    client = create_client("cos", region="ap-guangzhou", ...)
    client.upload_file(bucket, "key/path", "/local/file.png")
"""

from nextgen_oss.client import StorageClient, create_client
from nextgen_oss.credentials import (
    TemporaryCredentials,
    STSProvider,
    CosSTSProvider,
    AwsSTSProvider,
    OssSTSProvider,
    CachedSTSProvider,
)
from nextgen_oss.providers import PROVIDER_REGISTRY, ProviderConfig, register_provider

__version__ = "0.2.0"

__all__ = [
    "StorageClient",
    "create_client",
    "TemporaryCredentials",
    "STSProvider",
    "CosSTSProvider",
    "AwsSTSProvider",
    "OssSTSProvider",
    "CachedSTSProvider",
    "PROVIDER_REGISTRY",
    "ProviderConfig",
    "register_provider",
]
