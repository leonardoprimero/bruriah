# Deterministic request classification (Slice 6B-1). Maps an InvestigationRequest to intent,
# domain, claim type, risk, and jurisdiction. Pure function: no I/O, no registry lookup, no
# retrieval, no network, and it never concludes anything about the user's actual question --
# see design.md "Routing/retrieval": "routing.py classifies intent, domain, claim type, risk,
# and jurisdiction; it never concludes." Source/capability lookup (6B-2) and the route-or-abstain
# decision (6B-3) are explicitly out of scope here.
#
# Value sets are the smallest closed enumerations that satisfy the acceptance scenarios in
# spec.md's "Domain-Sensitive Outcomes and Unsupported-Domain Abstention" and "Local Knowledge
# and Capability Discovery" requirements; each constant below cites the requirement it serves.
from __future__ import annotations

import re
import unicodedata
from dataclasses import dataclass
from typing import Literal

from .contracts import InvestigationRequest

Intent = Literal["investigate", "consequential_action", "unclear"]
Domain = Literal["law", "accounting", "cybersecurity", "programming", "ux_design", "general", "unsupported"]
ClaimType = Literal["factual", "capability_recommendation", "professional_conclusion"]
RiskLevel = Literal["low", "medium", "high", "regulated", "unknown"]

_WORD = re.compile(r"\w+", re.UNICODE)


class ClassificationError(ValueError):
    def __init__(self, code: str):
        self.code = code
        super().__init__(code)


@dataclass(frozen=True)
class RequestClassification:
    """Closed classification result. Every field is a fixed Literal value set; there is no
    open text field, so nothing from the request's free-text `task` can be echoed back as a
    conclusion (see `test_classify.py::test_classifier_never_concludes`)."""

    intent: Intent
    domain: Domain
    claim_type: ClaimType
    risk: RiskLevel
    jurisdiction: str  # request.jurisdiction verbatim, or "unknown" -- never guessed from text


# --- Domain keyword sets --------------------------------------------------------------------
# Requirement "Domain-Sensitive Outcomes and Unsupported-Domain Abstention" names exactly these
# five domain families as requiring distinct treatment; anything else stays "general", or, if it
# names a specific unsupported profession, "unsupported" (never a nearest-match guess).
_LAW_WORDS = frozenset({
    "law", "legal", "lawsuit", "contract", "regulation", "statute", "liability", "compliance",
    "ley", "demanda", "contrato", "normativa", "estatuto", "responsabilidad",
})
_ACCOUNTING_WORDS = frozenset({
    "tax", "taxes", "accounting", "audit", "invoice", "bookkeeping", "vat", "payroll", "filing",
    "impuesto", "impuestos", "contabilidad", "auditoria", "auditoría", "factura", "nomina", "nómina", "iva",
})
_CYBERSECURITY_WORDS = frozenset({
    "vulnerability", "exploit", "malware", "pentest", "penetration", "cve", "breach", "firewall",
    "vulnerabilidad", "explotar", "brecha", "cortafuegos", "seguridad",
})
_PROGRAMMING_WORDS = frozenset({
    "code", "python", "javascript", "api", "bug", "compile", "framework", "sdk", "repository",
    "codigo", "código", "programacion", "programación", "libreria", "librería", "repositorio",
})
_UX_WORDS = frozenset({
    "ux", "ui", "accessibility", "usability", "wireframe", "interaction", "usabilidad",
    "accesibilidad", "diseño", "interfaz", "wireframes",
})
# This list catches only EXPLICITLY-NAMED unsupported professions, so their risk reads as
# `unknown` rather than a bare `low`. It is NOT — and cannot be — a complete answer to the spec's
# "Arbitrary unsupported profession" scenario: an open-ended profession space cannot be enumerated
# in a keyword set, so unlisted professions (e.g. commercial pilot, funeral director) fall through
# to `general`. That is correct here: whether a domain is *supported* depends on approved-pack
# availability, which is a registry question owned by 6B-2/6B-3, not by this pure classifier. The
# real abstention backstop is domain-agnostic ("no local pack or evidence found") and MUST run in
# 6B-3 for `general` exactly as for `unsupported`.
_UNSUPPORTED_PROFESSION_WORDS = frozenset({
    "medical", "medicine", "doctor", "diagnosis", "prescription", "veterinary", "veterinarian",
    "notary", "immigration", "structural", "architect", "architecture",
    "medico", "médico", "medicina", "diagnostico", "diagnóstico", "receta", "veterinario",
    "veterinaria", "notario", "inmigracion", "inmigración", "estructural", "arquitecto",
})
_DOMAIN_KEYWORDS: tuple[tuple[Domain, frozenset[str]], ...] = (
    ("law", _LAW_WORDS), ("accounting", _ACCOUNTING_WORDS), ("cybersecurity", _CYBERSECURITY_WORDS),
    ("programming", _PROGRAMMING_WORDS), ("ux_design", _UX_WORDS),
)

# Requirement "Read-Only Informational Boundary and Host Actions", scenario "Consequential
# action requested": credentials, a write, execution, or licensed judgment must route/abstain
# rather than be answered. Flagging intent here is the signal 6B-3 will use for that decision.
_CONSEQUENTIAL_ACTION_WORDS = frozenset({
    "install", "uninstall", "delete", "purchase", "buy", "transfer", "execute", "deploy",
    "authenticate", "login",
    "instalar", "desinstalar", "eliminar", "borrar", "comprar", "transferir", "ejecutar", "autenticar",
})
# Requirement "Local Knowledge and Capability Discovery": discovering skills, MCP servers,
# tools, libraries, datasets, and methods is a distinct claim shape from an evidence lookup.
_CAPABILITY_WORDS = frozenset({
    "tool", "library", "skill", "package", "mcp", "server", "dataset", "method",
    "herramienta", "biblioteca", "paquete", "servidor", "metodo", "método",
})
# Requirement "Read-Only Informational Boundary and Host Actions": Cerebro "SHALL provide
# evidence-linked information, not legal, accounting, security, medical, or other professional
# conclusions." Detecting a request FOR such a conclusion is classification, not concluding.
_PROFESSIONAL_CONCLUSION_WORDS = frozenset({
    "confirm", "liable", "guarantee", "guaranteed", "definitively", "legally",
    "confirmá", "confirmar", "garantizado", "definitivamente", "legalmente",
})

# "law and accounting MUST require jurisdiction and applicable/effective date" -> regulated;
# cybersecurity distinguishes evidence from authorization/incident judgment -> high; programming
# and UX are version- or standard-bound but not licensed professions -> medium; an unrecognized
# domain carries no assessable risk -> unknown, never a silently inferred "low" default.
_DOMAIN_RISK: dict[Domain, RiskLevel] = {
    "law": "regulated", "accounting": "regulated", "cybersecurity": "high",
    "programming": "medium", "ux_design": "medium", "general": "low", "unsupported": "unknown",
}


def _words(text: str) -> frozenset[str]:
    # Normalize to NFC before matching. The keyword sets are NFC (Python source literals are),
    # but macOS text fields, APFS filenames, and some browser/IME pipelines emit NFD, where an
    # accented character is a base letter plus a combining mark. Without this, "auditoría" in NFD
    # never matches the NFC keyword and silently drops to `general`/`low` -- including the medical
    # and immigration unsupported-profession safety net, for input that differs only in byte form.
    normalized = unicodedata.normalize("NFC", text)
    return frozenset(match.casefold() for match in _WORD.findall(normalized))


def _domain(words: frozenset[str]) -> Domain:
    # When several domains' keywords co-occur, the first match in `_DOMAIN_KEYWORDS` order wins
    # (law, accounting, cybersecurity, programming, ux_design). This is a deterministic tie-break,
    # not a confidence judgment: 6B-1 does not attempt multi-label classification, and 6B-3 is
    # responsible for handling a task that legitimately spans domains.
    for domain, keywords in _DOMAIN_KEYWORDS:
        if words & keywords:
            return domain
    if words & _UNSUPPORTED_PROFESSION_WORDS:
        return "unsupported"
    return "general"


def _intent(words: frozenset[str]) -> Intent:
    if not words:
        return "unclear"
    if words & _CONSEQUENTIAL_ACTION_WORDS:
        return "consequential_action"
    return "investigate"


def _claim_type(words: frozenset[str]) -> ClaimType:
    if words & _CAPABILITY_WORDS:
        return "capability_recommendation"
    if words & _PROFESSIONAL_CONCLUSION_WORDS:
        return "professional_conclusion"
    return "factual"


def classify(request: InvestigationRequest) -> RequestClassification:
    """Classify `request` deterministically. Reads only `task`, `outcome`, and `jurisdiction`;
    performs no I/O, lookup, retrieval, network access, or conclusion about the user's question.
    """
    if not isinstance(request, InvestigationRequest):
        raise ClassificationError("invalid_request_type")

    words = _words(request.task) | _words(request.outcome or "")
    domain = _domain(words)
    # Requirement: "Missing metadata MUST remain unknown" -- jurisdiction is read only from the
    # request's dedicated structured field, never guessed from free text.
    jurisdiction = request.jurisdiction if request.jurisdiction else "unknown"
    return RequestClassification(
        intent=_intent(words), domain=domain, claim_type=_claim_type(words),
        risk=_DOMAIN_RISK[domain], jurisdiction=jurisdiction,
    )


__all__ = ["ClassificationError", "ClaimType", "Domain", "Intent", "RequestClassification", "RiskLevel", "classify"]
