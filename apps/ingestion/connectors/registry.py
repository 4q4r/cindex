"""Connector registry mapping source keys to connector classes."""

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
from .base import BaseConnector
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

CONNECTORS: dict[str, type[BaseConnector]] = {
    c.profile.source_key: c
    for c in [
        # API-mode connectors (aiohttp)
        EuropePMCConnector,
        OpenAlexConnector,
        CrossrefConnector,
        PubMedConnector,
        ArXivConnector,
        DOAJConnector,
        PMCConnector,
        COREConnector,
        DBLPConnector,
        HALConnector,
        ZenodoConnector,
        IACRConnector,
        ExaConnector,
        # HTML/WebSocket-mode connectors (cloudscraper)
        CiNiiConnector,
        SciEngineConnector,
        CyberLeninkaConnector,
        MathNetConnector,
        SciELOConnector,
        PerseeConnector,
        OpenEditionConnector,
        MedknowConnector,
        DergiParkConnector,
        HrcakConnector,
        AJOLConnector,
    ]
}
