"""Source connector package for the ingestion app."""

from __future__ import annotations

from .api_connectors import (
    ArXivConnector,
    COREConnector,
    CrossrefConnector,
    DBLPConnector,
    DOAJConnector,
    EuropePMCConnector,
    ExaConnector,
    HALConnector,
    IACRConnector,
    OpenAlexConnector,
    PMCConnector,
    PubMedConnector,
    ZenodoConnector,
)
from .base import (
    AsyncApiConnector,
    BaseConnector,
    ConnectorFetchError,
    RawArticle,
    SourceProfile,
)
from .html_connectors import (
    AJOLConnector,
    CiNiiConnector,
    CyberLeninkaConnector,
    DergiParkConnector,
    HrcakConnector,
    MathNetConnector,
    MedknowConnector,
    OpenEditionConnector,
    PerseeConnector,
    SciELOConnector,
    SciEngineConnector,
)
from .registry import CONNECTORS

__all__ = [
    "CONNECTORS",
    "AJOLConnector",
    "ArXivConnector",
    "AsyncApiConnector",
    "BaseConnector",
    "COREConnector",
    "CiNiiConnector",
    "ConnectorFetchError",
    "CrossrefConnector",
    "CyberLeninkaConnector",
    "DBLPConnector",
    "DOAJConnector",
    "DergiParkConnector",
    "EuropePMCConnector",
    "ExaConnector",
    "HALConnector",
    "HrcakConnector",
    "IACRConnector",
    "MathNetConnector",
    "MedknowConnector",
    "OpenAlexConnector",
    "OpenEditionConnector",
    "PMCConnector",
    "PerseeConnector",
    "PubMedConnector",
    "RawArticle",
    "SciELOConnector",
    "SciEngineConnector",
    "SourceProfile",
    "ZenodoConnector",
]
