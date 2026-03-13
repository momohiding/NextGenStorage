"""
云厂商预设配置

每家对象存储都兼容 S3 协议，差异只在 endpoint 格式和 STS 地址。
通过 ProviderConfig 统一描述，StorageClient 根据 provider 名称自动选取。
"""

from dataclasses import dataclass, field
from typing import Dict, Optional, Callable


@dataclass
class ProviderConfig:
    """单个云厂商的 S3 兼容配置"""
    name: str
    # endpoint 模板，{region} 会被替换为实际 region
    endpoint_template: str
    # 是否使用 virtual-hosted style（大多数厂商都是）
    addressing_style: str = "virtual"
    # STS 签发的 credential key 映射（各家字段名不同）
    credential_mapping: Dict[str, str] = field(default_factory=dict)
    # 签名版本
    signature_version: str = "s3v4"

    def get_endpoint(self, region: str) -> str:
        return self.endpoint_template.format(region=region)


# ======================== 内置厂商预设 ========================

PROVIDER_REGISTRY: Dict[str, ProviderConfig] = {
    "cos": ProviderConfig(
        name="Tencent Cloud COS",
        endpoint_template="https://cos.{region}.myqcloud.com",
        addressing_style="virtual",
        credential_mapping={
            "access_key": "tmpSecretId",
            "secret_key": "tmpSecretKey",
            "session_token": "sessionToken",
        },
    ),
    "cos-internal": ProviderConfig(
        name="Tencent Cloud COS (Internal)",
        endpoint_template="https://cos-internal.{region}.tencentcos.cn",
        addressing_style="virtual",
        credential_mapping={
            "access_key": "tmpSecretId",
            "secret_key": "tmpSecretKey",
            "session_token": "sessionToken",
        },
    ),
    "oss": ProviderConfig(
        name="Alibaba Cloud OSS",
        endpoint_template="https://oss-{region}.aliyuncs.com",
        addressing_style="virtual",
        credential_mapping={
            "access_key": "AccessKeyId",
            "secret_key": "AccessKeySecret",
            "session_token": "SecurityToken",
        },
    ),
    "oss-internal": ProviderConfig(
        name="Alibaba Cloud OSS (Internal)",
        endpoint_template="https://oss-{region}-internal.aliyuncs.com",
        addressing_style="virtual",
        credential_mapping={
            "access_key": "AccessKeyId",
            "secret_key": "AccessKeySecret",
            "session_token": "SecurityToken",
        },
    ),
    "s3": ProviderConfig(
        name="AWS S3",
        # AWS S3 不需要自定义 endpoint，boto3 原生支持
        endpoint_template="",
        addressing_style="virtual",
        credential_mapping={
            "access_key": "AccessKeyId",
            "secret_key": "SecretAccessKey",
            "session_token": "SessionToken",
        },
    ),
    "minio": ProviderConfig(
        name="MinIO",
        endpoint_template="http://{region}",  # region 当作 host:port 使用
        addressing_style="path",
        credential_mapping={
            "access_key": "accessKey",
            "secret_key": "secretKey",
            "session_token": "sessionToken",
        },
    ),
}


def register_provider(key: str, config: ProviderConfig):
    """注册自定义厂商配置"""
    PROVIDER_REGISTRY[key] = config


def get_provider(key: str) -> ProviderConfig:
    """获取厂商配置，不存在则抛异常"""
    if key not in PROVIDER_REGISTRY:
        available = ", ".join(PROVIDER_REGISTRY.keys())
        raise ValueError(f"Unknown provider '{key}', available: {available}")
    return PROVIDER_REGISTRY[key]
